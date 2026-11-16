"""HTTP API 与 HTML 审批路由。

Router 以工厂方式构建，依赖注入配置和 Session 工厂，
便于测试并避免全局状态。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import make_admin_dependency, make_session_dependency
from app.api.serializers import (
    agent_run_to_dict,
    approval_to_dict,
    artifact_to_dict,
    delivery_to_dict,
    event_to_dict,
    task_to_dict,
)
from app.core.config import AppConfig
from app.core.metrics import APPROVALS, TASKS_COMPLETED, TASKS_FAILED, TASKS_STARTED, render
from app.core.models import AgentRun, Approval, Artifact, BugTask, Delivery, EventLog, SysConfig, utcnow
from app.core.security import merge_subject_hash, plan_subject_hash, result_subject_hash, sha256_hex
from app.core.state_machine import TransitionError, transition
from app.workflow.approvals import ApprovalError, ApprovalService
from app.workflow.intake import BugEvent, IntakeService


class InfoSubmission(BaseModel):
    """用户补充的 Bug 信息；会追加到任务描述并重新进入分析。"""

    answer: str = Field(min_length=1, max_length=20000)
    actor: str = "admin"


def build_router(
    config: AppConfig,
    session_factory: async_sessionmaker[AsyncSession],
    templates: Jinja2Templates,
) -> APIRouter:
    """创建 API Router，并把依赖绑定到当前应用实例。"""

    router = APIRouter()
    get_session = make_session_dependency(session_factory)
    require_admin = make_admin_dependency(config)

    def default_actor_for(task: BugTask, role: str) -> str:
        """为审批页预填默认审批人；MERGE 优先使用 code_owner 列表。"""

        if role == "BUG_OWNER":
            return task.assignee or ""
        if task.code_owner:
            return task.code_owner
        return config.project.approval.code_owners[0] if config.project.approval.code_owners else ""

    async def current_subject(
        session: AsyncSession, approval: Approval, task: BugTask
    ) -> str | None:
        """重新计算审批必须绑定的对象指纹。

        缺少 Artifact 或 SHA 时返回 None；调用方应拒绝审批，
        而不是接受一个无法验证对象的审批。
        """

        if approval.gate == "PLAN":
            stmt = (
                select(Artifact)
                .where(Artifact.task_id == task.id, Artifact.type == "ANALYSIS")
                .order_by(Artifact.version.desc())
                .limit(1)
            )
            artifact = (await session.execute(stmt)).scalar_one_or_none()
            if artifact is None or not task.base_sha:
                return None
            return plan_subject_hash(
                artifact.sha256, task.plan_version, task.base_sha, config.config_hash()
            )

        stmt = (
            select(Artifact)
            .where(Artifact.task_id == task.id, Artifact.type == "DIFF")
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        artifact = (await session.execute(stmt)).scalar_one_or_none()
        if artifact is None or not task.fix_sha:
            return None

        payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        diff_hash = sha256_hex(payload.get("diff", ""))
        if approval.gate == "RESULT":
            return result_subject_hash(
                task.fix_sha, diff_hash, task.plan_version, config.config_hash()
            )
        return merge_subject_hash(
            task.fix_sha, diff_hash, [task.base_branch, task.test_branch], config.config_hash()
        )

    @router.get("/healthz")
    async def healthz():
        """容器/平台存活探针。"""

        return {"status": "ok"}

    @router.get("/metrics")
    async def metrics():
        """Prometheus 指标端点。"""

        return Response(render(), media_type="text/plain; version=0.0.4")

    @router.post("/api/intake")
    async def intake(
        event: BugEvent,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """接收推送的 Bug 事件；轮询复用同一个接入服务。"""

        service = IntakeService(config)
        task, created = await service.ingest(session, event)
        if created:
            TASKS_STARTED.inc()
        await session.commit()
        return {"task_id": task.id, "created": created, "state": task.state}

    @router.get("/api/tasks")
    async def list_tasks(
        state: str | None = None,
        limit: int = 50,
        session: AsyncSession = Depends(get_session),
    ):
        """列出最近任务，可按状态过滤。"""

        stmt = select(BugTask).order_by(BugTask.created_at.desc()).limit(min(limit, 200))
        if state:
            stmt = stmt.where(BugTask.state == state)
        tasks = (await session.execute(stmt)).scalars().all()
        return [task_to_dict(t) for t in tasks]

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
        """返回单个任务及其审批历史。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        approvals = (
            await session.execute(
                select(Approval).where(Approval.task_id == task_id).order_by(Approval.id.desc())
            )
        ).scalars().all()
        data = task_to_dict(task)
        data["approvals"] = [approval_to_dict(a) for a in approvals]
        return data

    @router.get("/api/tasks/{task_id}/timeline")
    async def get_timeline(
        task_id: str,
        include_content: bool = False,
        session: AsyncSession = Depends(get_session),
    ):
        """返回完整时间线：状态迁移、Agent 调用、Artifact、审批、交付步骤。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        events = (
            await session.execute(
                select(EventLog).where(EventLog.task_id == task_id).order_by(EventLog.id)
            )
        ).scalars().all()
        runs = (
            await session.execute(
                select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.id)
            )
        ).scalars().all()
        artifacts = (
            await session.execute(
                select(Artifact)
                .where(Artifact.task_id == task_id)
                .order_by(Artifact.version, Artifact.id)
            )
        ).scalars().all()
        approvals = (
            await session.execute(
                select(Approval).where(Approval.task_id == task_id).order_by(Approval.id)
            )
        ).scalars().all()
        deliveries = (
            await session.execute(
                select(Delivery).where(Delivery.task_id == task_id).order_by(Delivery.id)
            )
        ).scalars().all()

        artifact_items = []
        for artifact in artifacts:
            content = None
            if include_content:
                try:
                    content = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    content = None
            artifact_items.append(artifact_to_dict(artifact, content))

        return {
            "task": task_to_dict(task),
            "events": [event_to_dict(e) for e in events],
            "agent_runs": [agent_run_to_dict(r) for r in runs],
            "artifacts": artifact_items,
            "approvals": [approval_to_dict(a) for a in approvals],
            "delivery": [delivery_to_dict(d) for d in deliveries],
        }

    @router.get("/api/tasks/{task_id}/artifacts/{artifact_type}")
    async def get_artifact(
        task_id: str,
        artifact_type: str,
        session: AsyncSession = Depends(get_session),
    ):
        """返回某类 Artifact 的最新版本内容，例如 ANALYSIS、DIFF、EVIDENCE。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        stmt = (
            select(Artifact)
            .where(Artifact.task_id == task_id, Artifact.type == artifact_type.upper())
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        artifact = (await session.execute(stmt)).scalar_one_or_none()
        if artifact is None:
            raise HTTPException(404, "artifact not found")
        try:
            content = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(409, f"artifact content is unavailable: {exc}") from exc
        return {"artifact": artifact_to_dict(artifact), "content": content}

    @router.post("/api/tasks/{task_id}/info")
    async def submit_info(
        task_id: str,
        payload: InfoSubmission,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """提交 WAITING_INFO 任务的补充信息，并重新进入分析。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.state != "WAITING_INFO":
            raise HTTPException(409, f"task is {task.state}, not WAITING_INFO")

        task.body = f"{task.body.rstrip()}\n\n补充信息（{payload.actor}）：\n{payload.answer.strip()}\n"
        transition(task, "answered", actor=payload.actor, session=session)
        task.next_run_at = utcnow()
        task.lease_until = None
        await session.commit()
        return {"task_id": task.id, "state": task.state}

    @router.post("/api/tasks/{task_id}/reanalyze")
    async def reanalyze_task(
        task_id: str,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """配置或上下文变化后，放弃旧方案并重新分析。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.state != "WAITING_PLAN_APPROVAL":
            raise HTTPException(409, f"task is {task.state}, not WAITING_PLAN_APPROVAL")

        pending = (
            await session.execute(
                select(Approval).where(
                    Approval.task_id == task_id,
                    Approval.gate == "PLAN",
                    Approval.decision.is_(None),
                )
            )
        ).scalars().all()
        for approval in pending:
            approval.decision = "EXPIRED"
            approval.used_at = utcnow()
            approval.comment = "superseded by reanalysis"

        transition(task, "reanalyze", actor="admin", session=session)
        task.next_run_at = utcnow()
        task.lease_until = None
        await session.commit()
        return {"task_id": task.id, "state": task.state}

    @router.post("/api/tasks/{task_id}/retry")
    async def retry_task(
        task_id: str,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """把 MANUAL 任务放回分析阶段再试一次。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.state != "MANUAL":
            raise HTTPException(409, f"task is {task.state}, not MANUAL")
        transition(task, "resume_analyzing", actor="admin", session=session)
        task.attempt = 0
        task.next_run_at = utcnow()
        await session.commit()
        return {"task_id": task.id, "state": task.state}

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """取消未进入终态的任务。"""

        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        try:
            transition(task, "cancel", actor="admin", session=session)
        except TransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        await session.commit()
        return {"task_id": task.id, "state": task.state}

    @router.post("/api/admin/pause")
    async def pause(
        enabled: bool = True,
        session: AsyncSession = Depends(get_session),
        _: None = Depends(require_admin),
    ):
        """全局 Kill Switch：Worker 停止领取新任务。"""

        row = await session.get(SysConfig, "kill_switch")
        if row is None:
            row = SysConfig(key="kill_switch", value=enabled)
            session.add(row)
        else:
            row.value = enabled
        await session.commit()
        return {"kill_switch": enabled}

    @router.get("/a/{token}", response_class=HTMLResponse)
    async def approval_page(
        token: str, request: Request, session: AsyncSession = Depends(get_session)
    ):
        """使用一次性 Token 渲染 PLAN/RESULT/MERGE 审批页。"""

        service = ApprovalService(config)
        approval = await service.by_token(session, token)
        if approval is None:
            raise HTTPException(404, "approval link is invalid or expired")
        task = await session.get(BugTask, approval.task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        default_actor = default_actor_for(task, approval.role)
        return templates.TemplateResponse(
            request,
            "approve.html",
            {
                "approval": approval,
                "task": task,
                "default_actor": default_actor or "",
                "token": token,
            },
        )

    @router.post("/a/{token}", response_class=HTMLResponse)
    async def decide(
        token: str,
        request: Request,
        decision: str = Form(...),
        actor: str = Form(""),
        comment: str = Form(""),
        session: AsyncSession = Depends(get_session),
    ):
        """重新校验 subject hash 后记录审批决策。"""

        service = ApprovalService(config)
        approval = await service.by_token(session, token)
        if approval is None:
            raise HTTPException(404, "approval link is invalid or expired")
        task = await session.get(BugTask, approval.task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        chosen = actor or default_actor_for(task, approval.role)
        expected = await current_subject(session, approval, task)
        if expected is None:
            raise HTTPException(409, "approval subject is no longer available")

        try:
            await service.decide(
                session,
                approval,
                actor=chosen,
                decision=decision,
                comment=comment,
                current_subject_hash=expected,
            )
        except ApprovalError as exc:
            await session.rollback()
            raise HTTPException(409, str(exc)) from exc

        APPROVALS.labels(gate=approval.gate, decision=decision).inc()
        if task.state == "DONE":
            TASKS_COMPLETED.inc()
        elif task.state in ("MANUAL", "FAILED"):
            TASKS_FAILED.inc()
        await session.commit()

        return templates.TemplateResponse(
            request,
            "approve.html",
            {
                "approval": approval,
                "task": task,
                "default_actor": chosen,
                "token": token,
                "decided": True,
                "message": f"Decision recorded: {decision}",
            },
        )

    return router