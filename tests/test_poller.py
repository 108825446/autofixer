from __future__ import annotations

import pytest

from app.workflow.intake import BugEvent, StaticAdapter
from app.workflow.poller import Poller


@pytest.mark.asyncio
async def test_poller_ingests_and_updates_cursor(session_factory, config):
    adapter = StaticAdapter(
        [
            BugEvent(
                event_id="e1",
                source="static",
                issue_key="BUG-1",
                title="wrong add",
                body="add(2,3) == -1",
                assignee="bug-owner",
            )
        ]
    )
    poller = Poller(
        config=config, session_factory=session_factory, adapter=adapter, interval_seconds=1
    )
    assert await poller.run_once() == 1
    assert await poller.run_once() == 1  # idempotent, existing active task