"""测试使用的确定性 ACP Server。

只实现 AcpClient 需要的 JSON-RPC：
initialize、session/new、session/prompt、session/request_permission。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def main() -> None:
    cwd = Path.cwd()
    session_id = "ses_test"
    while True:
        msg = read()
        if msg is None:
            return
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "sessionCapabilities": {"resume": {}},
                        },
                        "agentInfo": {"name": "FakeACP", "version": "0.1"},
                    },
                }
            )
        elif method == "session/new":
            cwd = Path(msg["params"]["cwd"])
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            text = msg["params"]["prompt"][0]["text"]
            target = "allowed.txt" if "allow" in text else "denied.txt"
            tool_call_id = "call_1"
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tool_call_id,
                            "title": target,
                            "kind": "edit",
                            "status": "pending",
                            "locations": [{"path": str(cwd / target)}],
                            "rawInput": {"filePath": str(cwd / target), "content": "ok"},
                        },
                    },
                }
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "toolCall": {
                            "toolCallId": tool_call_id,
                            "title": target,
                            "kind": "edit",
                            "status": "pending",
                            "locations": [{"path": str(cwd / target)}],
                            "rawInput": {"filePath": str(cwd / target), "content": "ok"},
                        },
                        "options": [
                            {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                            {"optionId": "always", "kind": "allow_always", "name": "Always"},
                            {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
                        ],
                    },
                }
            )
            response = read()
            allowed = False
            if response and "result" in response:
                outcome = response["result"].get("outcome", {})
                allowed = outcome.get("optionId") == "once"
            if allowed:
                (cwd / target).write_text("ok", encoding="utf-8")
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tool_call_id,
                            "status": "completed" if allowed else "failed",
                            "kind": "edit",
                            "title": target,
                        },
                    },
                }
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": "msg_1",
                            "content": {"type": "text", "text": "done"},
                        },
                    },
                }
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "stopReason": "end_turn",
                        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                    },
                }
            )
        else:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()