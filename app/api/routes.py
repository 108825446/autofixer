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
from app.core.security import (
    merge_subject_hash,
    plan_subject_hash,
    result_subject_hash,
    sha256_hex,
    verify_clarification_token,
)
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

    def normalize_questions(raw: object) -> list[dict]:
        """兼容旧版字符串问题，并把新版问题规范化为页面可直接渲染的结构。"""

        result: list[dict] = []
        if not isinstance(raw, list):
            return result
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    result.append(
                        {"text": text, "type": "text", "options": [], "allow_other": True}
                    )
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            options = [str(o).strip() for o in item.get("options", []) if str(o).strip()]
            qtype = str(item.get("type", "single_choice"))
            if qtype not in ("single_choice", "text") or not options:
                qtype = "text"
            result.append(
                {
                    "text": text,
                    "type": qtype,
                    "options": options,
                    "allow_other": bool(item.get("allow_other", True)),
                }
            )
        return result

    async def _clarification_context(
        session: AsyncSession, token: str
    ) -> tuple[BugTask, list[dict], str | None]:
        """解析澄清 Token，返回任务、问题列表和错误提示。"""

        parsed = verify_clarification_token(config.approval_secret(), token)
        if parsed is None:
            raise HTTPException(404, "clarification link is invalid or expired")
        task_id, analysis_version = parsed
        task = await session.get(BugTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.state != "WAITING_INFO":
            return task, [], "该 Bug 已进入下一步，无需再补充信息。"
        if task.plan_version != analysis_version:
            return task, [], "问题已更新，请打开最新澄清链接。"

        stmt = (
            select(Artifact)
            .where(Artifact.task_id == task.id, Artifact.type == "ANALYSIS")
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        artifact = (await session.execute(stmt)).scalar_one_or_none()
        if artifact is None:
            return task, [], "分析结果尚未生成，请稍后重试。"
        try:
            payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return task, [], "分析结果暂时不可用，请联系管理员。"
        analysis = payload.get("analysis", payload)
        questions = normalize_questions(analysis.get("questions", []))
        return task, questions, None

    @router.get("/clarify/{token}", response_class=HTMLResponse)
    async def clarify_page(
        token: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
    ):
        """给 Bug 提出者展示简洁的澄清表单。"""

        task, questions, error = await _clarification_context(session, token)
        return templates.TemplateResponse(
            request,
            "clarify.html",
            {
                "task": task,
                "questions": questions,
                "token": token,
                "error": error,
                "submitted": False,
            },
            status_code=409 if error else 200,
        )

    @router.post("/clarify/{token}", response_class=HTMLResponse)
    async def submit_clarification(
        token: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
    ):
        """提交澄清回答，把任务重新送回分析阶段。"""

        task, questions, error = await _clarification_context(session, token)
        if error:
            return templates.TemplateResponse(
                request,
                "clarify.html",
                {
                    "task": task,
                    "questions": questions,
                    "token": token,
                    "error": error,
                    "submitted": False,
                },
                status_code=409,
            )

        form = await request.form()
        answers: list[str] = []
        for index, question in enumerate(questions):
            answer = str(form.get(f"answer_{index}", "")).strip()
            other = str(form.get(f"other_{index}", "")).strip()
            if answer == "__other__" or (not answer and other):
                answer = other
            if answer:
                answers.append(f"问题：{question['text']}\n回答：{answer}")
        extra = str(form.get("extra", "")).strip()
        if not answers and not extra:
            return templates.TemplateResponse(
                request,
                "clarify.html",
                {
                    "task": task,
                    "questions": questions,
                    "token": token,
                    "error": "请至少填写一项信息，或补充日志/链接。",
                    "submitted": False,
                },
                status_code=400,
            )

        parts = [f"澄清补充（{utcnow().strftime('%Y-%m-%d %H:%M UTC')}）："]
        parts.extend(answers)
        if extra:
            parts.append(f"补充材料/链接：\n{extra}")
        task.body = f"{task.body.rstrip()}\n\n" + "\n\n".join(parts) + "\n"
        transition(task, "answered", actor="reporter", session=session)
        task.next_run_at = utcnow()
        task.lease_until = None
        await session.commit()

        return templates.TemplateResponse(
            request,
            "clarify.html",
            {
                "task": task,
                "questions": questions,
                "token": token,
                "error": None,
                "submitted": True,
            },
        )

    async def load_plan_analysis(
        session: AsyncSession, task: BugTask
    ) -> tuple[dict, str | None]:
        """读取最新分析并补全页面渲染所需字段。"""

        defaults = {
            "root_cause": "",
            "confidence": 0.0,
            "evidence": [],
            "candidate_files": [],
            "plan": [],
            "test_plan": "",
            "test_command": "",
            "risk": "UNKNOWN",
            "rollback": "",
            "assumptions": [],
            "questions": [],
        }
        stmt = (
            select(Artifact)
            .where(Artifact.task_id == task.id, Artifact.type == "ANALYSIS")
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        artifact = (await session.execute(stmt)).scalar_one_or_none()
        if artifact is None:
            return defaults, task.base_sha
        try:
            payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults, task.base_sha
        raw = payload.get("analysis", payload)
        if not isinstance(raw, dict):
            return defaults, task.base_sha
        analysis = {**defaults, **raw}
        for key in ("evidence", "candidate_files", "plan", "assumptions", "questions"):
            if not isinstance(analysis.get(key), list):
                analysis[key] = []
        try:
            analysis["confidence"] = float(analysis.get("confidence") or 0)
        except (TypeError, ValueError):
            analysis["confidence"] = 0.0
        analysis["risk"] = str(analysis.get("risk") or "UNKNOWN")
        base_sha = payload.get("base_sha") or task.base_sha
        return analysis, base_sha

    async def load_result_context(
        session: AsyncSession, task: BugTask
    ) -> dict:
        """读取 Diff、验证证据和分析摘要，供修复结果确认页展示。"""

        analysis, plan_base_sha = await load_plan_analysis(session, task)
        diff_payload: dict = {}
        evidence_payload: dict = {}

        for artifact_type, target in (("DIFF", "diff"), ("EVIDENCE", "evidence")):
            stmt = (
                select(Artifact)
                .where(Artifact.task_id == task.id, Artifact.type == artifact_type)
                .order_by(Artifact.version.desc())
                .limit(1)
            )
            artifact = (await session.execute(stmt)).scalar_one_or_none()
            if artifact is None:
                continue
            try:
                payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if target == "diff":
                diff_payload = payload
            else:
                evidence_payload = payload

        return {
            "analysis": analysis,
            "base_sha": diff_payload.get("base_sha") or plan_base_sha or task.base_sha,
            "fix_sha": diff_payload.get("fix_sha") or task.fix_sha,
            "branch": task.branch,
            "files": diff_payload.get("files", []),
            "lines": diff_payload.get("lines", 0),
            "diff": diff_payload.get("diff", ""),
            "gates": evidence_payload.get("gates", []),
            "red_green": evidence_payload.get("red_green", {}),
            "scope": evidence_payload.get("scope", {}),
            "reasons": evidence_payload.get("reasons", []),
            "ok": bool(evidence_payload.get("ok", False)),
        }

    async def render_approval_page(
        request: Request,
        session: AsyncSession,
        token: str,
    ):
        """按审批门禁选择 PLAN/RESULT/MERGE 专用页面。"""

        service = ApprovalService(config)
        approval = await service.by_token(session, token)
        if approval is None:
            raise HTTPException(404, "approval link is invalid or expired")
        task = await session.get(BugTask, approval.task_id)
        if task is None:
            raise HTTPException(404, "task not found")

        default_actor = default_actor_for(task, approval.role)
        analysis: dict = {}
        result: dict = {}
        base_sha = task.base_sha
        if approval.gate == "PLAN":
            analysis, base_sha = await load_plan_analysis(session, task)
            template = "approve_plan.html"
        elif approval.gate == "RESULT":
            result = await load_result_context(session, task)
            analysis = result.get("analysis", {})
            base_sha = result.get("base_sha")
            template = "approve_result.html"
        elif approval.gate == "MERGE":
            result = await load_merge_context(session, task)
            analysis = result.get("analysis", {})
            base_sha = result.get("base_sha")
            template = "approve_merge.html"
        else:
            raise HTTPException(404, "unknown approval gate")
        return templates.TemplateResponse(
            request,
            template,
            {
                "approval": approval,
                "task": task,
                "default_actor": default_actor or "",
                "token": token,
                "analysis": analysis,
                "result": result,
                "base_sha": base_sha,
                "action_url": request.url.path,
                "decided": approval.decision is not None,
                "message": (
                    f"审批已记录：{approval.decision}"
                    if approval.decision is not None
                    else None
                ),
            },
        )

    async def load_merge_context(
        session: AsyncSession, task: BugTask
    ) -> dict:
        """聚合合并授权所需的 Diff、验证证据和结果审批信息。"""

        context = await load_result_context(session, task)
        stmt = (
            select(Approval)
            .where(
                Approval.task_id == task.id,
                Approval.gate == "RESULT",
                Approval.decision == "APPROVED",
            )
            .order_by(Approval.id.desc())
            .limit(1)
        )
        result_approval = (await session.execute(stmt)).scalar_one_or_none()
        context.update(
            {
                "base_branch": task.base_branch,
                "test_branch": task.test_branch,
                "merge_mode": config.project.delivery.merge_mode,
                "result_approval": (
                    {
                        "id": result_approval.id,
                        "decided_by": result_approval.decided_by,
                        "decided_at": (
                            result_approval.decided_at.strftime("%Y-%m-%d %H:%M")
                            if result_approval.decided_at
                            else ""
                        ),
                        "comment": result_approval.comment,
                    }
                    if result_approval
                    else None
                ),
            }
        )
        return context

    @router.get("/approve/{kind}/{token}", response_class=HTMLResponse)
    async def approval_page_by_kind(
        kind: str,
        token: str,
        request: Request,
        session: AsyncSession = Depends(get_session),
    ):
        """门禁专用页面：/approve/plan|result|merge/{token}。"""

        if kind not in ("plan", "result", "merge"):
            raise HTTPException(404, "unknown approval page")
        service = ApprovalService(config)
        approval = await service.by_token(session, token)
        if approval is None:
            raise HTTPException(404, "approval link is invalid or expired")
        if approval.gate.lower() != kind:
            raise HTTPException(404, "approval page does not match this link")
        return await render_approval_page(request, session, token)

    @router.post("/approve/{kind}/{token}", response_class=HTMLResponse)
    async def decide(
        kind: str,
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
        if kind not in ("plan", "result", "merge"):
            raise HTTPException(404, "unknown approval page")
        if approval.gate.lower() != kind:
            raise HTTPException(404, "approval page does not match this link")
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

        analysis: dict = {}
        result: dict = {}
        base_sha = task.base_sha
        if approval.gate == "PLAN":
            analysis, base_sha = await load_plan_analysis(session, task)
            template = "approve_plan.html"
        elif approval.gate == "RESULT":
            result = await load_result_context(session, task)
            analysis = result.get("analysis", {})
            base_sha = result.get("base_sha")
            template = "approve_result.html"
        elif approval.gate == "MERGE":
            result = await load_merge_context(session, task)
            analysis = result.get("analysis", {})
            base_sha = result.get("base_sha")
            template = "approve_merge.html"
        else:
            raise HTTPException(404, "unknown approval gate")
        return templates.TemplateResponse(
            request,
            template,
            {
                "approval": approval,
                "task": task,
                "default_actor": chosen,
                "token": token,
                "analysis": analysis,
                "result": result,
                "base_sha": base_sha,
                "decided": True,
                "action_url": request.url.path,
                "message": f"审批已记录：{decision}",
            },
        )

    return router