from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import AppConfig
from app.integrations.gitlab import GitLabClient
from app.workflow.intake import GitLabIssueAdapter

BASE = "http://gitlab.test/api/v4"
PROJECT = "group/order-service"
PID = "group%2Forder-service"


@pytest.mark.asyncio
@respx.mock
async def test_mr_create_merge_and_issue_adapter(config: AppConfig):
    respx.post(f"{BASE}/projects/{PID}/merge_requests").mock(
        return_value=httpx.Response(
            201,
            json={"iid": 7, "web_url": "http://gitlab.test/mr/7", "sha": "a" * 40},
        )
    )
    respx.get(f"{BASE}/projects/{PID}/merge_requests/7").mock(
        return_value=httpx.Response(
            200, json={"iid": 7, "detailed_merge_status": "mergeable", "sha": "a" * 40}
        )
    )
    respx.put(f"{BASE}/projects/{PID}/merge_requests/7/merge").mock(
        return_value=httpx.Response(
            200, json={"iid": 7, "state": "merged", "merge_commit_sha": "b" * 40}
        )
    )
    respx.get(f"{BASE}/projects/{PID}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "iid": 11,
                    "title": "wrong add",
                    "description": "add(2,3) == -1",
                    "updated_at": "2026-11-15T00:00:00Z",
                    "assignee": {"username": "bug-owner"},
                    "web_url": "http://gitlab.test/issues/11",
                }
            ],
        )
    )

    async with GitLabClient(base_url="http://gitlab.test", token="t", project=PROJECT) as client:
        mr = await client.create_mr(
            source_branch="bugfix/11",
            target_branch="develop",
            title="fix",
            description="desc",
        )
        assert mr["iid"] == 7
        mergeable = await client.wait_mergeable(7, timeout=1, interval=0.01)
        assert mergeable["detailed_merge_status"] == "mergeable"
        merged = await client.merge_mr(7)
        assert merged["merge_commit_sha"] == "b" * 40

        adapter = GitLabIssueAdapter(client, config)
        fetched = await adapter.fetch_updated(None)
        assert fetched.items[0].issue_key == "11"
        assert fetched.items[0].assignee == "bug-owner"
        assert fetched.next_cursor == "2026-11-15T00:00:00Z"