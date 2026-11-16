"""Bug 事件规范化、去重和 GitLab Issues 适配器。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.models import TERMINAL_STATES, BugTask, EventLog, SysConfig
from app.integrations.gitlab import GitLabClient


class BugEvent(BaseModel):
    """来自轮询或推送 Webhook 的规范化 Bug 事件。"""
    event_id: str
    source: str
    issue_key: str
    title: str
    body: str = ""
    repo: str | None = None
    assignee: str | None = None
    code_owner: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any] = Field(default_factory=dict)


@dataclass
class FetchResult:
    """一页接入事件及下一个游标。"""
    items: list[BugEvent]
    next_cursor: str | None


class BugSourceAdapter(Protocol):
    async def fetch_updated(self, cursor: str | None, limit: int = 50) -> FetchResult:
        ...



class StaticAdapter:
    """测试使用的内存 BugSourceAdapter。"""
    def __init__(self, items: list[BugEvent] | None = None):
        self.items = items or []

    async def fetch_updated(self, cursor: str | None, limit: int = 50) -> FetchResult:
        return FetchResult(self.items[:limit], None)


class GitLabIssueAdapter:
    """基于 GitLab Issues 的 BugSourceAdapter。"""
    def __init__(self, client: GitLabClient, config: AppConfig):
        self.client = client
        self.config = config

    async def fetch_updated(self, cursor: str | None, limit: int = 50) -> FetchResult:
        issues = await self.client.list_issues(
            updated_after=cursor,
            labels=self.config.source.labels or None,
            per_page=limit,
        )
        items = [
            BugEvent(
                event_id=f"gitlab:{issue['iid']}:{issue['updated_at']}",
                source="gitlab_issues",
                issue_key=str(issue["iid"]),
                title=issue.get("title", ""),
                body=issue.get("description") or "",
                repo=self.config.project.repo,
                assignee=(issue.get("assignee") or {}).get("username"),
                updated_at=issue["updated_at"],
                raw=issue,
            )
            for issue in issues
        ]
        next_cursor = issues[-1]["updated_at"] if issues else cursor
        return FetchResult(items, next_cursor)


def fingerprint(title: str, body: str) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return hashlib.sha256(f"{title}\n{first_line}".encode()).hexdigest()


class IntakeService:
    """幂等任务创建与轮询游标管理。"""
    def __init__(self, config: AppConfig):
        self.config = config

    async def ingest(self, session: AsyncSession, event: BugEvent) -> tuple[BugTask, bool]:
        """为 Issue 创建或更新活动任务。

        返回 `(task, created)`。同一活动 Issue 的重复轮询/Webhook
        只更新元数据，不会创建新工作流。
        """

        stmt = select(BugTask).where(
            BugTask.source == event.source,
            BugTask.issue_key == event.issue_key,
            BugTask.state.notin_(TERMINAL_STATES),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.title = event.title
            existing.body = event.body
            existing.assignee = event.assignee or existing.assignee
            existing.code_owner = event.code_owner or existing.code_owner
            await session.flush()
            return existing, False

        task = BugTask(
            source=event.source,
            issue_key=event.issue_key,
            fingerprint=fingerprint(event.title, event.body),
            title=event.title,
            body=event.body,
            repo=event.repo or self.config.project.repo,
            base_branch=self.config.project.branches.base,
            test_branch=self.config.project.branches.test,
            delivery_mode=self.config.project.delivery.deploy.mode,
            assignee=event.assignee,
            code_owner=event.code_owner,
        )
        try:
            async with session.begin_nested():
                session.add(task)
                await session.flush()
        except IntegrityError:
            existing = (await session.execute(stmt)).scalar_one()
            return existing, False

        session.add(
            EventLog(
                task_id=task.id,
                from_state=None,
                to_state="NEW",
                event="intake",
                actor="intake",
                payload={"event_id": event.event_id, "issue_key": event.issue_key},
            )
        )
        await session.flush()
        return task, True

    async def poll(self, session: AsyncSession, adapter: BugSourceAdapter) -> list[BugTask]:
        """拉取一页更新并推进已保存的游标。"""

        key = f"intake:cursor:{self.config.source.type}"
        row = await session.get(SysConfig, key)
        cursor = str(row.value) if row and row.value else None
        fetched = await adapter.fetch_updated(cursor)
        tasks: list[BugTask] = []
        for event in fetched.items:
            task, _ = await self.ingest(session, event)
            tasks.append(task)
        if fetched.next_cursor:
            if row is None:
                row = SysConfig(key=key, value=fetched.next_cursor)
                session.add(row)
            else:
                row.value = fetched.next_cursor
        await session.flush()
        return tasks