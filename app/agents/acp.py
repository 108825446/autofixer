"""Agent Client Protocol（ACP）客户端与权限策略。

运行时作为 ACP Client：启动编码 Agent、创建会话、发送提示、接收流式事件、
响应权限请求，并在超时时取消。权限策略只是第二道防线；Worker 始终会
重新检查最终 Diff。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AcpError(RuntimeError):
    """ACP 协议、超时或进程异常。"""
    pass


@dataclass
class AcpRunResult:
    """一次 ACP 提示的汇总结果（消息、工具调用、用量）。"""
    stop_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)


class PermissionPolicy:
    """把 ACP 权限请求转换为沙箱允许/拒绝决策。

    这是第二道防线：Worker 仍会检查最终 Diff。
    """

    ALLOWED_COMMANDS = (
        "mvn",
        "./mvnw",
        "gradle",
        "./gradlew",
        "npm",
        "pnpm",
        "yarn",
        "pytest",
        "python",
        "python3",
        "git status",
        "git diff",
        "git log",
        "git add",
        "git commit",
        "ls",
        "cat",
        "rg",
        "find",
        "head",
        "tail",
    )
    DENIED_COMMANDS = (
        "rm -rf /",
        "curl ",
        "wget ",
        "ssh ",
        "scp ",
        "docker ",
        "kubectl ",
        "sudo ",
        "chmod 777",
        "git push",
    )

    def __init__(self, worktree: str | Path, allowed_paths: list[str], *, allow_network: bool = False):
        self.worktree = Path(worktree).resolve()
        self.allowed_paths = allowed_paths
        self.allow_network = allow_network

    def _inside_worktree(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self.worktree)
            return True
        except (ValueError, OSError):
            return False

    def _path_allowed(self, path: str) -> bool:
        if not self._inside_worktree(path):
            return False
        try:
            rel = str(Path(path).resolve().relative_to(self.worktree))
        except ValueError:
            return False
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.allowed_paths)

    def decide(self, params: dict[str, Any]) -> str:
        tool = params.get("toolCall", {})
        kind = tool.get("kind", "")
        raw = tool.get("rawInput", {}) or {}
        locations = tool.get("locations", []) or []

        if kind in ("read", "search", "list"):
            paths = [x.get("path") for x in locations if x.get("path")]
            if paths and all(self._inside_worktree(p) for p in paths):
                return "allow_once"
            return "reject_once"

        if kind in ("edit", "write", "create"):
            paths = [raw.get("filePath") or raw.get("filepath")]
            paths += [x.get("path") for x in locations if x.get("path")]
            paths = [p for p in paths if p]
            if paths and all(self._path_allowed(p) for p in paths):
                return "allow_once"
            return "reject_once"

        if kind in ("terminal", "bash", "execute"):
            command = str(raw.get("command", "")).strip()
            if not command:
                return "reject_once"
            if any(command.startswith(prefix) for prefix in self.DENIED_COMMANDS):
                return "reject_once"
            if any(command.startswith(prefix) for prefix in self.ALLOWED_COMMANDS):
                return "allow_once"
            return "reject_once"

        if kind == "fetch" or kind == "network":
            return "allow_once" if self.allow_network else "reject_once"

        return "reject_once"


class AcpClient:
    """面向 ACP 编码 Agent 子进程的 JSON-RPC 客户端。"""
    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        permission_policy: PermissionPolicy | None = None,
    ):
        self.command = command
        self.cwd = str(cwd)
        self.env = {**os.environ, **(env or {})}
        self.policy = permission_policy
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._next_id = 1
        self._stderr_task: asyncio.Task | None = None
        self._stderr = ""

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            self._stderr += line.decode(errors="replace")

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise AcpError("ACP process is not running")
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def _readline(self) -> str:
        if not self.proc or not self.proc.stdout:
            raise AcpError("ACP process is not running")
        line = await self.proc.stdout.readline()
        if not line:
            raise AcpError(f"ACP process closed; stderr={self._stderr[-1000:]}")
        return line.decode(errors="replace").strip()

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
        result: AcpRunResult | None = None,
    ) -> dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AcpError(f"ACP {method} timed out")
            try:
                line = await asyncio.wait_for(self._readline(), timeout=remaining)
            except TimeoutError as exc:
                raise AcpError(f"ACP {method} timed out") from exc
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise AcpError(f"ACP {method} error: {msg['error']}")
                return msg["result"]
            await self._handle_notification(msg, result)

    async def _handle_notification(self, msg: dict[str, Any], result: AcpRunResult | None) -> None:
        method = msg.get("method")
        if method == "session/request_permission":
            if result is not None:
                result.permissions.append(msg)
            decision = self.policy.decide(msg.get("params", {})) if self.policy else "reject_once"
            options = msg.get("params", {}).get("options", [])
            selected = next((o for o in options if o.get("kind") == decision), None)
            if selected is None:
                selected = next((o for o in options if o.get("kind") == "reject_once"), None)
            response = (
                {"outcome": {"outcome": "selected", "optionId": selected["optionId"]}}
                if selected
                else {"outcome": {"outcome": "cancelled"}}
            )
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": response})
            return

        if method == "session/update" and result is not None:
            update = msg.get("params", {}).get("update", {})
            result.updates.append(update)
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                text = update.get("content", {}).get("text", "")
                if text:
                    result.messages.append(text)
            elif kind in ("tool_call", "tool_call_update"):
                result.tool_calls.append(update)

    async def initialize(self, client_capabilities: dict | None = None) -> dict[str, Any]:
        return await self.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": client_capabilities or {}},
            timeout=15,
        )

    async def new_session(self, cwd: str | None = None) -> str:
        result = await self.request(
            "session/new",
            {"cwd": cwd or self.cwd, "mcpServers": []},
            timeout=20,
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise AcpError("ACP session/new returned no sessionId")
        self.session_id = session_id
        return session_id

    async def prompt(self, text: str, *, timeout: float = 600) -> AcpRunResult:
        """发送提示并收集流式更新，直到本轮结束。"""

        if not self.session_id:
            raise AcpError("ACP session is not initialized")
        run = AcpRunResult(stop_reason="unknown")
        result = await self.request(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
            timeout=timeout,
            result=run,
        )
        run.stop_reason = result.get("stopReason", "unknown")
        run.usage = result.get("usage", {})
        return run

    async def cancel(self) -> None:
        if self.session_id:
            await self._send(
                {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}}
            )

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self._stderr_task:
            self._stderr_task.cancel()

    async def __aenter__(self) -> AcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()