"""合法工作流迁移与审计事件创建。

迁移表刻意保持显式：每次状态变化都可评审、可测试，
非法迁移直接失败。"""

from __future__ import annotations

from typing import Any

from app.core.models import TERMINAL_STATES, BugTask, EventLog, utcnow

LEGAL: dict[tuple[str, str], str] = {
    ("NEW", "analyze"): "ANALYZING",
    ("ANALYZING", "need_info"): "WAITING_INFO",
    ("ANALYZING", "analysis_ready"): "WAITING_PLAN_APPROVAL",
    ("ANALYZING", "analysis_failed"): "MANUAL",
    ("WAITING_INFO", "answered"): "ANALYZING",
    ("WAITING_INFO", "info_timeout"): "MANUAL",
    ("WAITING_PLAN_APPROVAL", "plan_approved"): "FIXING",
    ("WAITING_PLAN_APPROVAL", "plan_changes_requested"): "ANALYZING",
    ("WAITING_PLAN_APPROVAL", "reanalyze"): "ANALYZING",
    ("WAITING_PLAN_APPROVAL", "plan_rejected"): "CANCELLED",
    ("WAITING_PLAN_APPROVAL", "plan_timeout"): "MANUAL",
    ("FIXING", "fix_committed"): "VERIFYING",
    ("FIXING", "fix_failed"): "MANUAL",
    ("VERIFYING", "verification_passed"): "WAITING_APPROVAL",
    ("VERIFYING", "verification_failed"): "FIXING",
    ("VERIFYING", "verification_blocked"): "MANUAL",
    ("WAITING_APPROVAL", "both_approved"): "DELIVERING",
    ("WAITING_APPROVAL", "approval_rejected"): "MANUAL",
    ("WAITING_APPROVAL", "approval_timeout"): "MANUAL",
    ("DELIVERING", "delivery_succeeded"): "DONE",
    ("DELIVERING", "delivery_failed"): "MANUAL",
    ("DELIVERING", "delivery_unrecoverable"): "FAILED",
    ("MANUAL", "resume_analyzing"): "ANALYZING",
    ("MANUAL", "resume_fixing"): "FIXING",
    ("MANUAL", "resume_verifying"): "VERIFYING",
    ("MANUAL", "resume_delivering"): "DELIVERING",
    ("MANUAL", "cancel"): "CANCELLED",
}


class TransitionError(ValueError):
    """当前状态不允许该事件时抛出。"""
    pass


def can_transition(state: str, event: str) -> bool:
    if event == "cancel" and state not in TERMINAL_STATES:
        return True
    return (state, event) in LEGAL


def transition(
    task: BugTask,
    event: str,
    *,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
    now=None,
    session=None,
) -> str:
    """执行合法迁移，并可选写入事件日志。"""
    current = task.state
    target: str | None
    if event == "cancel" and current not in TERMINAL_STATES:
        target = "CANCELLED"
    else:
        target = LEGAL.get((current, event))
    if target is None:
        raise TransitionError(f"illegal transition: {current} --{event}--> ?")

    task.state = target
    task.version = (task.version or 0) + 1
    task.updated_at = now or utcnow()
    task.lease_until = None

    if session is not None:
        add_event(
            session,
            task,
            event,
            actor=actor,
            payload=payload,
            from_state=current,
            to_state=target,
        )
    return target


def add_event(
    session,
    task: BugTask,
    event: str,
    *,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
) -> None:
    session.add(
        EventLog(
            task_id=task.id,
            from_state=from_state if from_state is not None else task.state,
            to_state=to_state if to_state is not None else task.state,
            event=event,
            actor=actor,
            payload=payload or {},
        )
    )