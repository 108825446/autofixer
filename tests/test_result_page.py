from __future__ import annotations

import json

import httpx
import pytest

from app.core.models import Artifact, BugTask
from app.core.security import sha256_hex
from app.main import create_app
from app.workflow.approvals import ApprovalService


@pytest.mark.asyncio
async def test_result_approval_page(config, session_factory, task, tmp_path):
    async with session_factory() as session:
        task.state = "WAITING_APPROVAL"
        task.plan_version = 1
        task.fix_sha = "f" * 40
        task.base_sha = "a" * 40
        task.branch = "bugfix/BUG-1"
        session.add(task)

        diff_text = "diff --git a/frontend/src/app.js b/frontend/src/app.js\n+fixed\n"
        diff_payload = {
            "base_sha": task.base_sha,
            "fix_sha": task.fix_sha,
            "files": ["frontend/src/app.js", "frontend/tests/app.test.js"],
            "lines": 42,
            "diff": diff_text,
        }
        diff_path = tmp_path / "DIFF-v1.json"
        diff_path.write_text(json.dumps(diff_payload), encoding="utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                type="DIFF",
                version=1,
                path=str(diff_path),
                sha256=sha256_hex(diff_text),
            )
        )

        evidence_payload = {
            "ok": True,
            "reasons": [],
            "scope": {
                "files": diff_payload["files"],
                "lines": 42,
                "ok": True,
            },
            "red_green": {
                "ok": True,
                "reproduced": True,
                "base_returncode": 1,
                "fix_returncode": 0,
                "reason": "",
            },
            "gates": [{"command": "npm test", "returncode": 0, "ok": True}],
        }
        evidence_path = tmp_path / "EVIDENCE-v1.json"
        evidence_text = json.dumps(evidence_payload)
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
        approval, token = await service.create_result(
            session, task, fix_sha=task.fix_sha, diff_hash=sha256_hex(diff_text)
        )
        await session.commit()

    app = create_app(config, session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/approve/result/{token}")
        assert page.status_code == 200, page.text
        assert "修复结果确认" in page.text
        assert "frontend/src/app.js" in page.text
        assert "npm test" in page.text
        assert "42" in page.text
        assert f'action="/approve/result/{token}"' in page.text

        decision = await client.post(
            f"/approve/result/{token}",
            data={"decision": "APPROVED", "actor": "bug-owner", "comment": "确认"},
        )
        assert decision.status_code == 200, decision.text
        assert "等待 Code Owner 合并授权" in decision.text

    async with session_factory() as session:
        refreshed = await session.get(BugTask, task.id)
        assert refreshed.state == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_result_page_rejects_plan_url(config, session_factory, task):
    async with session_factory() as session:
        task.state = "WAITING_APPROVAL"
        task.plan_version = 1
        task.fix_sha = "f" * 40
        session.add(task)
        service = ApprovalService(config)
        _, token = await service.create_result(
            session, task, fix_sha=task.fix_sha, diff_hash="diff"
        )
        await session.commit()

    app = create_app(config, session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/approve/plan/{token}")
        assert response.status_code == 404
