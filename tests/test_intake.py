from __future__ import annotations

import pytest

from app.core.models import BugTask, SysConfig
from app.workflow.intake import BugEvent, IntakeService, StaticAdapter


@pytest.mark.asyncio
async def test_ingest_is_idempotent(session, config):
    service = IntakeService(config)
    event = BugEvent(
        event_id="e1",
        source="static",
        issue_key="BUG-1",
        title="wrong add",
        body="add(2,3) == -1",
        assignee="bug-owner",
    )
    task1, created1 = await service.ingest(session, event)
    task2, created2 = await service.ingest(session, event)
    await session.commit()
    assert created1 is True
    assert created2 is False
    assert task1.id == task2.id
    assert (await session.get(BugTask, task1.id)).state == "NEW"


@pytest.mark.asyncio
async def test_poll_updates_cursor(session, config):
    service = IntakeService(config)
    adapter = StaticAdapter(
        [
            BugEvent(
                event_id="e1",
                source="static",
                issue_key="BUG-1",
                title="wrong add",
                body="body",
            )
        ]
    )
    tasks = await service.poll(session, adapter)
    await session.commit()
    assert len(tasks) == 1
    cursor = await session.get(SysConfig, "intake:cursor:static")
    # StaticAdapter 不返回 next_cursor，因此不需要游标行。
    assert cursor is None