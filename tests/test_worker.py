from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.acp import AcpRunResult
from app.agents.analyzer import AnalysisResult, StaticAnalyzer
from app.core.models import Artifact
from app.integrations.gitops import GitOps
from app.integrations.notify import NullNotifier
from app.workflow.approvals import ApprovalService
from app.workflow.worker import Worker


class FakeAcp:
    def __init__(self, worktree: Path):
        self.worktree = worktree

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def initialize(self):
        return {"protocolVersion": 1}

    async def new_session(self, cwd=None):
        return "ses_fake"

    async def prompt(self, text: str, timeout: float = 600):
        (self.worktree / "src" / "main" / "app.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (self.worktree / "src" / "test" / "test_regression.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src' / 'main'))\n"
            "from app import add\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        return AcpRunResult(
            stop_reason="end_turn",
            usage={"inputTokens": 1, "outputTokens": 1},
            messages=["done"],
        )


class FakeGitLab:
    async def create_mr(self, **kwargs):
        return {"iid": 42}

    async def wait_mergeable(self, iid, **kwargs):
        return {"iid": iid, "detailed_merge_status": "mergeable"}

    async def merge_mr(self, iid, **kwargs):
        return {"merge_commit_sha": "f" * 40}


@pytest.mark.asyncio
async def test_worker_end_to_end(session_factory, session, config, repo, task, analysis_payload, tmp_path):
    session.add(task)
    await session.commit()

    worker = Worker(
        config=config,
        session_factory=session_factory,
        analyzer=StaticAnalyzer(AnalysisResult.model_validate(analysis_payload)),
        git=GitOps(repo),
        gitlab=FakeGitLab(),
        notifier=NullNotifier(),
        worktree_root=tmp_path / "worktrees",
        artifacts_dir=tmp_path / "artifacts",
        acp_factory=lambda command, cwd, policy: FakeAcp(Path(cwd)),
    )

    await worker.handle(session, task)
    await session.commit()
    assert task.state == "WAITING_PLAN_APPROVAL"
    assert task.base_sha
    assert task.plan_version == 1

    approvals = ApprovalService(config)
    plan = await approvals.latest(session, task.id, "PLAN")
    assert plan is not None
    await approvals.decide(session, plan, actor="bug-owner", decision="APPROVED")
    await session.commit()
    assert task.state == "FIXING"

    await worker.handle(session, task)
    await session.commit()
    assert task.state == "VERIFYING", task.last_error
    assert task.fix_sha

    await worker.handle(session, task)
    await session.commit()
    assert task.state == "WAITING_APPROVAL", task.last_error
    result = await approvals.latest(session, task.id, "RESULT")
    merge = await approvals.latest(session, task.id, "MERGE")
    assert result is not None and merge is not None
    await approvals.decide(session, result, actor="bug-owner", decision="APPROVED")
    await approvals.decide(session, merge, actor="code-owner", decision="APPROVED")
    await session.commit()
    assert task.state == "DELIVERING"

    await worker.handle(session, task)
    await session.commit()
    assert task.state == "DONE", task.last_error
    assert task.test_merge_sha

    artifacts = (
        await session.execute(
            __import__("sqlalchemy").select(Artifact).where(Artifact.task_id == task.id)
        )
    ).scalars().all()
    types = {a.type for a in artifacts}
    assert {"ANALYSIS", "DIFF", "EVIDENCE"} <= types