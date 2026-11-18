from __future__ import annotations

import json

import httpx
import pytest

from app.core.models import Artifact, BugTask
from app.core.security import sha256_hex
from app.main import create_app
from app.workflow.approvals import ApprovalService


@pytest.fixture
def app(config, session_factory):
    return create_app(config, session_factory)


@pytest.mark.asyncio
async def test_health_and_intake(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        response = await client.post(
            "/api/intake",
            headers={"Authorization": "Bearer test-admin"},
            json={
                "event_id": "e1",
                "source": "static",
                "issue_key": "BUG-1",
                "title": "wrong add",
                "body": "add(2,3) == -1",
                "assignee": "bug-owner",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["created"] is True
        assert data["state"] == "NEW"

        tasks = (await client.get("/api/tasks")).json()
        assert len(tasks) == 1
        assert tasks[0]["issue_key"] == "BUG-1"


@pytest.mark.asyncio
async def test_intake_requires_admin(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/intake",
            json={
                "event_id": "e1",
                "source": "static",
                "issue_key": "BUG-1",
                "title": "wrong add",
                "body": "body",
            },
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_approval_page_and_decision(app, config, session_factory, task, tmp_path):
    async with session_factory() as session:
        task.base_sha = "a" * 40
        task.plan_version = 1
        task.state = "WAITING_PLAN_APPROVAL"
        session.add(task)
        payload = {"analysis": {"root_cause": "x"}, "base_sha": task.base_sha}
        path = tmp_path / "analysis.json"
        text = json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                type="ANALYSIS",
                version=1,
                path=str(path),
                sha256=sha256_hex(text),
            )
        )
        service = ApprovalService(config)
        approval, token = await service.create_plan(
            session, task, analysis_hash=sha256_hex(text), base_sha=task.base_sha
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/approve/plan/{token}")
        assert page.status_code == 200, page.text
        assert "方案审批" in page.text
        assert "x" in page.text

        decision = await client.post(
            f"/approve/plan/{token}",
            data={"decision": "APPROVED", "actor": "bug-owner", "comment": "ok"},
        )
        assert decision.status_code == 200, decision.text
        assert "审批已记录" in decision.text

        refreshed_page = await client.get(f"/approve/plan/{token}")
        assert refreshed_page.status_code == 200, refreshed_page.text
        assert "已记录：APPROVED" in refreshed_page.text
        assert "批准方案" not in refreshed_page.text

    async with session_factory() as session:
        refreshed = await session.get(BugTask, task.id)
        assert refreshed.state == "FIXING"

@pytest.mark.asyncio
async def test_timeline_artifact_and_info_resume(app, session_factory, task, tmp_path):
    async with session_factory() as session:
        task.state = "WAITING_INFO"
        session.add(task)
        payload = {
            "analysis": {
                "need_info": True,
                "questions": ["请提供标题容器的 HTML/CSS"],
                "root_cause": "",
                "confidence": 0.2,
                "evidence": [],
                "candidate_files": [],
                "plan": [],
                "test_plan": "",
                "test_command": "",
                "risk": "LOW",
                "rollback": "",
            },
            "base_sha": "a" * 40,
        }
        path = tmp_path / "analysis-info.json"
        text = json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        session.add(
            Artifact(
                task_id=task.id,
                type="ANALYSIS",
                version=1,
                path=str(path),
                sha256=sha256_hex(text),
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        timeline = await client.get(f"/api/tasks/{task.id}/timeline?include_content=true")
        assert timeline.status_code == 200, timeline.text
        data = timeline.json()
        assert data["task"]["state"] == "WAITING_INFO"
        assert data["artifacts"][0]["type"] == "ANALYSIS"
        assert data["artifacts"][0]["content"]["analysis"]["questions"]

        artifact = await client.get(f"/api/tasks/{task.id}/artifacts/ANALYSIS")
        assert artifact.status_code == 200, artifact.text
        assert artifact.json()["content"]["analysis"]["need_info"] is True

        resumed = await client.post(
            f"/api/tasks/{task.id}/info",
            headers={"Authorization": "Bearer test-admin"},
            json={"answer": "标题容器是 .hero__title，CSS 在 styles.css", "actor": "bug-owner"},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["state"] == "ANALYZING"


@pytest.mark.asyncio
async def test_reanalyze_from_plan_approval(app, session_factory, task):
    async with session_factory() as session:
        task.state = "WAITING_PLAN_APPROVAL"
        session.add(task)
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/tasks/{task.id}/reanalyze",
            headers={"Authorization": "Bearer test-admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "ANALYZING"
