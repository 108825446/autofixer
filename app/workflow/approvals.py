"""PLAN / RESULT / MERGE 审批生命周期。

本模块负责角色授权、一次性 Token、subject-hash 绑定，
以及审批通过后进入 FIXING/DELIVERING 的迁移。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.models import Approval, BugTask, utcnow
from app.core.security import (
    hash_token,
    is_expired,
    make_approval_token,
    merge_subject_hash,
    plan_subject_hash,
    result_subject_hash,
    verify_approval_token,
)
from app.core.state_machine import TransitionError, transition


class ApprovalError(ValueError):
    """审批过期、已使用、对象变化或无权操作时抛出。"""
    pass


class ApprovalService:
    """创建、解析并决策 PLAN/RESULT/MERGE 审批。"""
    def __init__(self, config: AppConfig):
        self.config = config

    def _gate_config(self, gate: str):
        gate_key = gate.lower()
        return getattr(self.config.project.approval.gates, gate_key)

    def _actor_for_task(self, task: BugTask) -> str | None:
        if not task.assignee:
            return None
        return self.config.project.approval.user_map.get(task.assignee, task.assignee)

    def authorize(self, task: BugTask, gate: str, actor: str) -> bool:
        """检查 `actor` 是否具备该门禁要求的角色。"""

        gate_cfg = self._gate_config(gate)
        allowed: set[str] = set(gate_cfg.fallback)
        if gate in ("PLAN", "RESULT"):
            owner = self._actor_for_task(task)
            if owner:
                allowed.add(owner)
        else:
            allowed.update(self.config.project.approval.code_owners)
            if task.code_owner:
                allowed.add(task.code_owner)
        return actor in allowed

    async def create(
        self,
        session: AsyncSession,
        task: BugTask,
        *,
        gate: str,
        role: str,
        subject_ref: str,
        subject_hash: str,
        analysis_version: int | None = None,
        fix_sha: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Approval, str]:
        gate_cfg = self._gate_config(gate)
        current = now or utcnow()
        expires_at = current + timedelta(hours=gate_cfg.ttl_hours)
        approval = Approval(
            task_id=task.id,
            gate=gate,
            role=role,
            subject_ref=subject_ref,
            subject_hash=subject_hash,
            analysis_version=analysis_version,
            fix_sha=fix_sha,
            channel=self.config.project.approval.channel,
            token_hash="pending",
            expires_at=expires_at,
        )
        session.add(approval)
        await session.flush()
        token = make_approval_token(self.config.approval_secret(), approval.id, expires_at)
        approval.token_hash = hash_token(token)
        await session.flush()
        return approval, token

    async def create_plan(
        self,
        session: AsyncSession,
        task: BugTask,
        *,
        analysis_hash: str,
        base_sha: str,
    ) -> tuple[Approval, str]:
        subject = plan_subject_hash(
            analysis_hash, task.plan_version, base_sha, self.config.config_hash()
        )
        return await self.create(
            session,
            task,
            gate="PLAN",
            role="BUG_OWNER",
            subject_ref=f"analysis_v{task.plan_version}",
            subject_hash=subject,
            analysis_version=task.plan_version,
        )

    async def create_result(
        self,
        session: AsyncSession,
        task: BugTask,
        *,
        fix_sha: str,
        diff_hash: str,
    ) -> tuple[Approval, str]:
        subject = result_subject_hash(
            fix_sha, diff_hash, task.plan_version, self.config.config_hash()
        )
        return await self.create(
            session,
            task,
            gate="RESULT",
            role="BUG_OWNER",
            subject_ref=fix_sha,
            subject_hash=subject,
            analysis_version=task.plan_version,
            fix_sha=fix_sha,
        )

    async def create_merge(
        self,
        session: AsyncSession,
        task: BugTask,
        *,
        fix_sha: str,
        diff_hash: str,
    ) -> tuple[Approval, str]:
        subject = merge_subject_hash(
            fix_sha,
            diff_hash,
            [task.base_branch, task.test_branch],
            self.config.config_hash(),
        )
        return await self.create(
            session,
            task,
            gate="MERGE",
            role="CODE_OWNER",
            subject_ref=f"{fix_sha}:{task.base_branch}->{task.test_branch}",
            subject_hash=subject,
            analysis_version=task.plan_version,
            fix_sha=fix_sha,
        )

    async def by_token(self, session: AsyncSession, token: str) -> Approval | None:
        approval_id = verify_approval_token(self.config.approval_secret(), token)
        if approval_id is None:
            return None
        approval = await session.get(Approval, approval_id)
        if approval is None:
            return None
        if approval.token_hash != hash_token(token):
            return None
        return approval

    async def decide(
        self,
        session: AsyncSession,
        approval: Approval,
        *,
        actor: str,
        decision: str,
        comment: str | None = None,
        current_subject_hash: str | None = None,
        now: datetime | None = None,
    ) -> BugTask:
        """记录决策并执行对应状态迁移。

        审批一次性、校验角色、校验过期，并可选校验调用方
        重新计算出的 subject hash。
        """

        current = now or utcnow()
        if approval.decision is not None or approval.used_at is not None:
            raise ApprovalError("approval already used")
        if is_expired(approval.expires_at, current):
            raise ApprovalError("approval expired")
        if decision not in ("APPROVED", "CHANGES_REQUESTED", "REJECTED"):
            raise ApprovalError("invalid decision")

        task = await session.get(BugTask, approval.task_id)
        if task is None:
            raise ApprovalError("task not found")
        if not self.authorize(task, approval.gate, actor):
            raise ApprovalError("actor is not authorized for this gate")
        if current_subject_hash is not None and current_subject_hash != approval.subject_hash:
            raise ApprovalError("approval subject changed")

        approval.decision = decision
        approval.decided_by = actor
        approval.decided_at = current
        approval.used_at = current
        approval.comment = comment

        if approval.gate == "PLAN":
            if decision == "APPROVED":
                transition(task, "plan_approved", actor=actor, session=session)
            elif decision == "CHANGES_REQUESTED":
                task.plan_reject_count += 1
                max_reject = self._gate_config("PLAN").max_reject
                if task.plan_reject_count >= max_reject:
                    transition(
                        task,
                        "plan_timeout",
                        actor=actor,
                        payload={"reason": "max plan rejections"},
                        session=session,
                    )
                else:
                    transition(task, "plan_changes_requested", actor=actor, session=session)
            else:
                transition(task, "plan_rejected", actor=actor, session=session)
        else:
            if decision != "APPROVED":
                transition(task, "approval_rejected", actor=actor, session=session)
            elif await self.both_approved(session, task.id):
                transition(task, "both_approved", actor=actor, session=session)
        await session.flush()
        return task

    async def both_approved(self, session: AsyncSession, task_id: str) -> bool:
        """仅当最新 RESULT 和 MERGE 都已批准时返回 True。"""

        for gate in ("RESULT", "MERGE"):
            stmt = (
                select(Approval)
                .where(Approval.task_id == task_id, Approval.gate == gate)
                .order_by(desc(Approval.id))
                .limit(1)
            )
            approval = (await session.execute(stmt)).scalar_one_or_none()
            if approval is None or approval.decision != "APPROVED":
                return False
        return True

    async def latest(self, session: AsyncSession, task_id: str, gate: str) -> Approval | None:
        stmt = (
            select(Approval)
            .where(Approval.task_id == task_id, Approval.gate == gate)
            .order_by(desc(Approval.id))
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


def decision_to_transition(gate: str, decision: str) -> str:
    if gate == "PLAN":
        return {
            "APPROVED": "plan_approved",
            "CHANGES_REQUESTED": "plan_changes_requested",
            "REJECTED": "plan_rejected",
        }[decision]
    if decision == "APPROVED":
        return "both_approved"
    return "approval_rejected"


__all__ = ["ApprovalService", "ApprovalError", "decision_to_transition", "TransitionError"]