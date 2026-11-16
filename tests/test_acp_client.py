from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents.acp import AcpClient, PermissionPolicy


@pytest.mark.asyncio
async def test_acp_initialize_session_prompt_and_permissions(tmp_path: Path):
    server = Path(__file__).parent / "fake_acp_server.py"
    policy = PermissionPolicy(tmp_path, ["allowed.txt"])
    client = AcpClient([sys.executable, str(server)], cwd=tmp_path, permission_policy=policy)
    async with client:
        init = await client.initialize()
        assert init["protocolVersion"] == 1
        session_id = await client.new_session(str(tmp_path))
        assert session_id == "ses_test"

        run = await client.prompt("allow this write")
        assert run.stop_reason == "end_turn"
        assert "done" in run.messages
        assert (tmp_path / "allowed.txt").read_text() == "ok"
        assert run.permissions
        assert run.permissions[0]["params"]["toolCall"]["kind"] == "edit"

        denied = await client.prompt("deny this write")
        assert denied.stop_reason == "end_turn"
        assert not (tmp_path / "denied.txt").exists()
        assert denied.permissions