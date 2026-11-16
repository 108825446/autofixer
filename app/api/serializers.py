"""HTTP API 响应序列化。

序列化与路由处理分离，便于在不改业务逻辑的情况下演进接口格式。
"""

from __future__ import annotations

from app.core.models import Approval, BugTask


def task_to_dict(task: BugTask) -> dict:
    """只暴露控制台/API 消费方需要的字段。"""

    return {
        "id": task.id,
        "source": task.source,
        "issue_key": task.issue_key,
        "title": task.title,
        "state": task.state,
        "repo": task.repo,
        "branch": task.branch,
        "base_sha": task.base_sha,
        "fix_sha": task.fix_sha,
        "fix_merge_sha": task.fix_merge_sha,
        "test_merge_sha": task.test_merge_sha,
        "delivery_mode": task.delivery_mode,
        "deployed": task.deployed,
        "attempt": task.attempt,
        "last_error": task.last_error,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def approval_to_dict(approval: Approval) -> dict:
    """序列化审批，且不暴露一次性 Token。"""

    return {
        "id": approval.id,
        "gate": approval.gate,
        "role": approval.role,
        "decision": approval.decision,
        "subject_ref": approval.subject_ref,
        "subject_hash": approval.subject_hash,
        "decided_by": approval.decided_by,
        "expires_at": approval.expires_at.isoformat(),
    }

def event_to_dict(event) -> dict:
    """把状态迁移/审计事件序列化为时间线节点。"""

    return {
        "id": event.id,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "event": event.event,
        "actor": event.actor,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def agent_run_to_dict(run) -> dict:
    """把一次 Agent 调用序列化为时间线节点。"""

    return {
        "id": run.id,
        "phase": run.phase,
        "attempt": run.attempt,
        "provider": run.provider,
        "model": run.model,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": run.cost_usd,
        "exit_code": run.exit_code,
        "result": run.result,
        "log_path": run.log_path,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


def artifact_to_dict(artifact, content=None) -> dict:
    """把 Artifact 元数据（可选内容）序列化为时间线节点。"""

    data = {
        "id": artifact.id,
        "type": artifact.type,
        "version": artifact.version,
        "path": artifact.path,
        "sha256": artifact.sha256,
        "size": artifact.size,
        "created_at": artifact.created_at.isoformat(),
    }
    if content is not None:
        data["content"] = content
    return data


def delivery_to_dict(row) -> dict:
    """把交付步骤序列化为时间线节点。"""

    return {
        "id": row.id,
        "step": row.step,
        "status": row.status,
        "ref": row.ref,
        "detail": row.detail,
        "attempts": row.attempts,
        "updated_at": row.updated_at.isoformat(),
    }