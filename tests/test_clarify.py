from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from app.core.models import Artifact, BugTask, utcnow
from app.core.security import make_clarification_token, sha256_hex
from app.main import create_app


def _app(config, session_factory):
    return create_app(config, session_factory)


@pytest.mark.asyncio
async def test_clarification_page_and_submit(config, session_factory, task, tmp_path):
    async with session_factory() as session:
        task.state = "WAITING_INFO"
        task.plan_version = 1
        session.add(task)
        payload = {
            "analysis": {
                "need_info": True,
                "questions": ["标题容器的选择器是什么？", "是否只在溢出时滚动？"],
            }
        }
        path = tmp_path / "analysis.json"
        text = json.dumps(payload, ensure_ascii=False)
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

    token = make_clarification_token(
        config.approval_secret(), task.id, 1, utcnow() + timedelta(hours=1)
    )
    transport = httpx.ASGITransport(app=_app(config, session_factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/clarify/{token}")
        assert page.status_code == 200, page.text
        assert "标题容器的选择器是什么？" in page.text

        response = await client.post(
            f"/clarify/{token}",
            data={
                "answer_0": ".hero__title",
                "answer_1": "只在溢出时滚动",
                "extra": "日志：https://example.test/log",
            },
        )
        assert response.status_code == 200, response.text
        assert "提交成功" in response.text

    async with session_factory() as session:
        refreshed = await session.get(BugTask, task.id)
        assert refreshed.state == "ANALYZING"
        assert ".hero__title" in refreshed.body
        assert "日志：https://example.test/log" in refreshed.body


@pytest.mark.asyncio
async def test_clarification_invalid_token(config, session_factory):
    transport = httpx.ASGITransport(app=_app(config, session_factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/clarify/not-a-valid-token")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_clarification_single_choice_with_other(
    config, session_factory, task, tmp_path
):
    async with session_factory() as session:
        task.state = "WAITING_INFO"
        task.plan_version = 1
        session.add(task)
        payload = {
            "analysis": {
                "need_info": True,
                "questions": [
                    {
                        "text": "标题滚动方向？",
                        "type": "single_choice",
                        "options": ["从右向左", "从左向右"],
                        "allow_other": True,
                    },
                    {"text": "还有其他说明吗？", "type": "text"},
                ],
            }
        }
        path = tmp_path / "analysis-choice.json"
        text = json.dumps(payload, ensure_ascii=False)
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

    token = make_clarification_token(
        config.approval_secret(), task.id, 1, utcnow() + timedelta(hours=1)
    )
    transport = httpx.ASGITransport(app=_app(config, session_factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(f"/clarify/{token}")
        assert page.status_code == 200, page.text
        assert "从右向左" in page.text
        assert 'type="radio"' in page.text

        response = await client.post(
            f"/clarify/{token}",
            data={
                "answer_0": "__other__",
                "other_0": "从上向下",
                "answer_1": "无其他说明",
            },
        )
        assert response.status_code == 200, response.text
        assert "提交成功" in response.text

    async with session_factory() as session:
        refreshed = await session.get(BugTask, task.id)
        assert refreshed.state == "ANALYZING"
        assert "从上向下" in refreshed.body
        assert "无其他说明" in refreshed.body
