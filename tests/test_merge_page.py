from __future__ import annotations

import json

import httpx
import pytest

from app.core.models import Artifact, BugTask
from app.core.security import sha256_hex
from app.main import create_app
from app.workflow.approvals import ApprovalService


@pytest.mark.asyncio
async def test_merge_approval_page_and_decision(config, session_factory, task, tmp_path):
    async with session_factory() as session:
        task.state = "WAITING_APPROVAL"
        task.plan_version = 1
        task.fix_sha = "f" * 40
        task.base_sha = "a" * 40
        task.branch = "bugfix/BUG-1"
        session.add(task)

        diff_text = "diff --git a/app.js b/app.js\n+fixed\n"
        diff_path = tmp_path / "DIFF-v1.json"
        diff_path.write_text(
            json.dumps(
                {
                    "base_sha": task.base_sha,
                    "fix_sha": task.fix_sha,
                    "files": ["app.js", "app.test.js"],
                    "lines": 42,
                    "diff": diff_text,
                }
            ),
            encoding="utf-8",
        )
        session.add(
            Artifact(
                task_id=task.id,
                type="DIFF",
                version=1,
                path=str(diff_path),
                sha256=sha256_hex(diff_text),
            )
        )

        evidence_text = json.dumps(
            {
                "ok": True,
                "scope": {"files": ["app.js", "app.test.js"], "lines": 42, "ok": True},
                "red_green": {
                    "ok": True,
                    "reproduced": True,
                    "base_returncode": 1,
                    "fix_returncode": 0,
                    "reason": "",
                },
                "gates": [{"command": "npm test", "returncode": 0, "ok": True}],
                "reasons": [],
            }
        )
        evidence_path = tmp_path / "EVIDENCE-v1.json"
        evidence_path.write_text(evidence_text, encoding="utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                type="EVIDENCE",
                version=1,
                path=str(evidence_path),
                sha256=sha256_hex(evidence_text),
            )
        )

        service = ApprovalService(config)
        result_approval, _ = await service.create_result(
            session, task, fix_sha=task.fix_sha, diff_hash=sha256_hex(diff_text)
        )
        await service.decide(session, result_approval, actor="bug-owner", decision="APPROVED")
        merge_approval, token = await service.create_merge(
            session, task, fix_sha=task.fix_sha, diff_hash=sha256_hex(diff_text)
        )
        await session.commit()

    app = create_app(config, session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/approve/merge/{token}")
        assert page.status_code == 200, page.text
        assert "合并授权" in page.text
        assert task.base_branch in page.text
        assert task.test_branch in page.text
        assert "只 cherry-pick" in page.text
        assert f'action="/approve/merge/{token}"' in page.text

        decision = await client.post(
            f"/approve/merge/{token}",
            data={"decision": "APPROVED", "actor": "code-owner", "comment": "同意合并"},
        )
        assert decision.status_code == 200, decision.text
        assert "已授权合并" in decision.text

    async with session_factory() as session:
        refreshed = await session.get(BugTask, task.id)
        assert refreshed.state == "DELIVERING"
