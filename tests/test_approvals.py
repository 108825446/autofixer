from __future__ import annotations

import pytest

from app.core.security import merge_subject_hash, plan_subject_hash, result_subject_hash
from app.workflow.approvals import ApprovalError, ApprovalService


@pytest.mark.asyncio
async def test_plan_approval_token_and_decision(session, config, task):
    session.add(task)
    await session.commit()
    service = ApprovalService(config)
    task.base_sha = "a" * 40
    task.plan_version = 1
    approval, token = await service.create_plan(
        session, task, analysis_hash="analysis-hash", base_sha=task.base_sha
    )
    task.state = "WAITING_PLAN_APPROVAL"
    await session.commit()

    loaded = await service.by_token(session, token)
    assert loaded is not None
    assert loaded.id == approval.id
    assert loaded.subject_hash == plan_subject_hash(
        "analysis-hash", 1, task.base_sha, config.config_hash()
    )

    decided = await service.decide(
        session, loaded, actor="bug-owner", decision="APPROVED", current_subject_hash=loaded.subject_hash
    )
    assert decided.state == "FIXING"
    assert loaded.decision == "APPROVED"
    await session.commit()

    with pytest.raises(ApprovalError, match="already used"):
        await service.decide(session, loaded, actor="bug-owner", decision="APPROVED")


@pytest.mark.asyncio
async def test_subject_mismatch_rejected(session, config, task):
    session.add(task)
    await session.commit()
    service = ApprovalService(config)
    task.base_sha = "b" * 40
    task.plan_version = 1
    approval, _ = await service.create_plan(
        session, task, analysis_hash="h", base_sha=task.base_sha
    )
    await session.commit()
    with pytest.raises(ApprovalError, match="subject changed"):
        await service.decide(
            session,
            approval,
            actor="bug-owner",
            decision="APPROVED",
            current_subject_hash="different",
        )


@pytest.mark.asyncio
async def test_unauthorized_actor_rejected(session, config, task):
    session.add(task)
    await session.commit()
    service = ApprovalService(config)
    task.base_sha = "c" * 40
    task.plan_version = 1
    approval, _ = await service.create_plan(session, task, analysis_hash="h", base_sha=task.base_sha)
    await session.commit()
    with pytest.raises(ApprovalError, match="not authorized"):
        await service.decide(session, approval, actor="stranger", decision="APPROVED")


@pytest.mark.asyncio
async def test_result_and_merge_both_required(session, config, task):
    task.state = "WAITING_APPROVAL"
    task.fix_sha = "d" * 40
    task.plan_version = 1
    session.add(task)
    await session.commit()
    service = ApprovalService(config)
    result, _ = await service.create_result(session, task, fix_sha=task.fix_sha, diff_hash="diff")
    merge, _ = await service.create_merge(session, task, fix_sha=task.fix_sha, diff_hash="diff")
    await session.commit()

    await service.decide(session, result, actor="bug-owner", decision="APPROVED")
    assert task.state == "WAITING_APPROVAL"
    await service.decide(session, merge, actor="code-owner", decision="APPROVED")
    assert task.state == "DELIVERING"
    assert result.subject_hash == result_subject_hash(
        task.fix_sha, "diff", 1, config.config_hash()
    )
    assert merge.subject_hash == merge_subject_hash(
        task.fix_sha, "diff", [task.base_branch, task.test_branch], config.config_hash()
    )