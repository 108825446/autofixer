#!/usr/bin/env python3
"""验证 OpenCode ACP：初始化、会话、提示、权限允许/拒绝。

用法：
    python3 scripts/verify_acp.py
    python3 scripts/verify_acp.py --agent-cmd "opencode acp --pure"

脚本会创建临时 git 仓库、启动 ACP Server，并检查：
  1. initialize 返回 protocolVersion 1
  2. session/new 返回 sessionId
  3. session/prompt 能读取文件
  4. 配置 permission=ask 的写入会触发 session/request_permission
  5. allow_once 会写入文件
  6. reject_once 不会写入文件

会产生少量模型调用，不要放进循环运行。
"""

from __future__ import annotations

import argparse
import json
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class AcpProbe:
    def __init__(self, command: list[str], cwd: Path):
        self.proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1
        self.pending_permission: list[dict] = []

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(
        self, method: str, params: dict, timeout: float = 30.0, permission_mode: str = "none"
    ) -> dict:
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._readline(deadline)
            if line is None:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg["result"]
            self._handle_notification(msg, permission_mode)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def _readline(self, deadline: float) -> str | None:
        assert self.proc.stdout is not None
        remaining = max(0.0, deadline - time.time())
        ready, _, _ = select.select([self.proc.stdout], [], [], min(0.3, remaining))
        if not ready:
            return None
        line = self.proc.stdout.readline()
        return line.rstrip() if line else None

    def _handle_notification(self, msg: dict, permission_mode: str = "none") -> None:
        if msg.get("method") == "session/request_permission":
            self.pending_permission.append(msg)
            if permission_mode != "none":
                self.respond_permission(msg, permission_mode)
            return
        if msg.get("method") == "session/update":
            update = msg.get("params", {}).get("update", {})
            if update.get("sessionUpdate") == "agent_message_chunk":
                text = update.get("content", {}).get("text", "")
                if text:
                    print(f"  agent: {text}", flush=True)

    def respond_permission(self, msg: dict, kind: str) -> str | None:
        options = msg.get("params", {}).get("options", [])
        selected = next((o for o in options if o.get("kind") == kind), None)
        option_id = selected.get("optionId") if selected else None
        if option_id is None:
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
            return None
        self.send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )
        return option_id

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        err = self.proc.stderr.read() if self.proc.stderr else ""
        if err.strip():
            print(f"  stderr: {err[:800]}", flush=True)


def make_repo(tmp: Path) -> None:
    (tmp / "hello.txt").write_text("hello-acp\n", encoding="utf-8")
    (tmp / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"edit": "ask", "bash": "ask"},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(
        ["git", "-c", "user.email=acp@test", "-c", "user.name=acp", "commit", "-qm", "init"],
        cwd=tmp,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-cmd", default="opencode acp --pure")
    args = parser.parse_args()
    command = args.agent_cmd.split()

    if shutil.which(command[0]) is None:
        print(f"FAIL: {command[0]} not found in PATH")
        return 1

    results: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="acp-verify-"))
    make_repo(tmp)
    probe = AcpProbe(command, tmp)

    try:
        print("[1] initialize")
        init = probe.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        version = init.get("protocolVersion")
        results.append(("initialize", version == 1, f"protocolVersion={version}"))

        print("[2] session/new")
        session = probe.request("session/new", {"cwd": str(tmp), "mcpServers": []})
        sid = session.get("sessionId")
        results.append(("session/new", bool(sid), f"sessionId={sid}"))
        if not sid:
            raise RuntimeError("no sessionId")

        print("[3] read prompt")
        probe.request(
            "session/prompt",
            {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "Read hello.txt and reply with its contents."}],
            },
            timeout=45,
        )
        results.append(("read prompt", True, "completed"))

        print("[4] write with permission allow")
        probe.pending_permission.clear()
        probe.request(
            "session/prompt",
            {
                "sessionId": sid,
                "prompt": [
                    {
                        "type": "text",
                        "text": "Create a file named allowed.txt with exactly: allowed-ok. Do not modify anything else.",
                    }
                ],
            },
            timeout=60,
            permission_mode="allow_once",
        )
        allow_msg = probe.pending_permission[-1] if probe.pending_permission else None
        if allow_msg is None:
            results.append(("permission allow", False, "no session/request_permission"))
        else:
            content = (tmp / "allowed.txt").read_text(encoding="utf-8") if (tmp / "allowed.txt").exists() else ""
            results.append(("permission allow", bool(content), f"content={content!r}"))

        print("[5] write with permission reject")
        probe.pending_permission.clear()
        probe.request(
            "session/prompt",
            {
                "sessionId": sid,
                "prompt": [
                    {
                        "type": "text",
                        "text": "Create a file named denied.txt with exactly: denied-ok. Do not modify anything else.",
                    }
                ],
            },
            timeout=90,
            permission_mode="reject_once",
        )
        reject_msg = probe.pending_permission[-1] if probe.pending_permission else None
        if reject_msg is None:
            results.append(("permission reject", False, "no session/request_permission"))
        else:
            exists = (tmp / "denied.txt").exists()
            results.append(("permission reject", not exists, f"exists={exists}"))

    except Exception as exc:  # noqa: BLE001
        results.append(("probe", False, str(exc)))
    finally:
        probe.close()

    print("\n=== Results ===")
    failed = False
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        failed = failed or not ok
    print(f"\nTemp repo: {tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())