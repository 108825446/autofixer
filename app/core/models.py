"""持久化领域模型（7 张表）。

`bug_task` 同时是工作流状态行和 Worker 队列；其余表记录
Agent 运行、不可变 Artifact、审批、交付步骤、审计事件和运行时开关。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

ID_TYPE = BigInteger().with_variant(Integer, "sqlite")

TERMINAL_STATES = ("DONE", "CANCELLED", "FAILED")
ACTIVE_STATES = (
    "NEW",
    "ANALYZING",
    "WAITING_INFO",
    "WAITING_PLAN_APPROVAL",
    "FIXING",
    "VERIFYING",
    "WAITING_APPROVAL",
    "DELIVERING",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class BugTask(Base):
    """一个 Bug 工作流实例，同时是 Worker 队列行。"""
    __tablename__ = "bug_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(64), nullable=False, default="develop")
    test_branch: Mapped[str] = mapped_column(String(64), nullable=False, default="test")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    branch: Mapped[str | None] = mapped_column(String(255))
    base_sha: Mapped[str | None] = mapped_column(String(40))
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_reject_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fix_sha: Mapped[str | None] = mapped_column(String(40))
    patch_id: Mapped[str | None] = mapped_column(String(128))
    fix_merge_sha: Mapped[str | None] = mapped_column(String(40))
    test_merge_sha: Mapped[str | None] = mapped_column(String(40))
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MERGE_ONLY")
    deployed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 历史字段：当前 MVP 不回写来源系统，保留列以兼容已有数据库。
    writeback_status: Mapped[str | None] = mapped_column(String(64))
    assignee: Mapped[str | None] = mapped_column(String(128))
    code_owner: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("bug_task_pick_idx", "state", "next_run_at"),)


Index(
    "bug_task_active_idx",
    BugTask.source,
    BugTask.issue_key,
    unique=True,
    sqlite_where=BugTask.state.notin_(TERMINAL_STATES),
    postgresql_where=BugTask.state.notin_(TERMINAL_STATES),
)


class AgentRun(Base):
    """一次分析或编码 Agent 调用（Token、成本、结果）。"""
    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_task.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)  # ANALYZE / FIX
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_sha: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict | None] = mapped_column(JSON)
    log_path: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    """保存在磁盘上的不可变分析/Diff/证据产物。"""
    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_task.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("task_id", "type", "version", name="artifact_version_uq"),)


class Approval(Base):
    """绑定 subject hash 的一次 PLAN/RESULT/MERGE 决策。"""
    __tablename__ = "approval"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_task.id"), nullable=False)
    gate: Mapped[str] = mapped_column(String(16), nullable=False)  # PLAN / RESULT / MERGE
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # BUG_OWNER / CODE_OWNER
    subject_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_version: Mapped[int | None] = mapped_column(Integer)
    fix_sha: Mapped[str | None] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="link")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(16))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    ua: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        UniqueConstraint("task_id", "gate", "subject_hash", name="approval_subject_uq"),
    )


class Delivery(Base):
    """幂等交付步骤（MR1、promote、MR2、deploy 等）。"""
    __tablename__ = "delivery"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_task.id"), nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    ref: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("task_id", "step", name="delivery_step_uq"),)


class EventLog(Base):
    """只追加的工作流/审计事件流。"""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("event_log_task_idx", "task_id", "id"),)


class SysConfig(Base):
    """运行时键值开关，如接入游标和 Kill Switch。"""
    __tablename__ = "sys_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )