from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.acp import AcpRunResult
from app.agents.analyzer import AnalysisResult, StaticAnalyzer
from app.core.models import SysConfig
from app.integrations.gitops import GitOps
from app.integrations.notify import NullNotifier
from app.workflow.worker import Worker


class NoopAcp:
    def __init__(self, worktree: Path):
        self.worktree = worktree

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def initialize(self):
        return {}

    async def new_session(self, cwd=None):
        return "ses"

    async def prompt(self, text, timeout=600):
        return AcpRunResult(stop_reason="end_turn")


class FakeGitLab:
    async def create_mr(self, **kwargs):
        return {"iid": 1}

    async def wait_mergeable(self, iid, **kwargs):
        return {"iid": iid}

    async def merge_mr(self, iid, **kwargs):
        return {"merge_commit_sha": "f" * 40}


def make_worker(config, session_factory, repo, tmp_path, analysis_payload):
    return Worker(
        config=config,
        session_factory=session_factory,
        analyzer=StaticAnalyzer(AnalysisResult.model_validate(analysis_payload)),
        git=GitOps(repo),
        gitlab=FakeGitLab(),
        notifier=NullNotifier(),
        worktree_root=tmp_path / "worktrees",
        artifacts_dir=tmp_path / "artifacts",
        acp_factory=lambda command, cwd, policy: NoopAcp(Path(cwd)),
    )


@pytest.mark.asyncio
async def test_kill_switch_blocks_claim(
    session_factory, session, config, repo, task, analysis_payload, tmp_path
):
    session.add(task)
    session.add(SysConfig(key="kill_switch", value=True))
    await session.commit()
    worker = make_worker(config, session_factory, repo, tmp_path, analysis_payload)
    assert await worker.run_once() is False
    await session.refresh(task)
    assert task.state == "NEW"

    row = await session.get(SysConfig, "kill_switch")
    row.value = False
    await session.commit()
    assert await worker.run_once() is True
    await session.refresh(task)
    assert task.state == "WAITING_PLAN_APPROVAL"


@pytest.mark.asyncio
async def test_config_hash_changes_with_limits(config):
    before = config.config_hash()
    config.project.limits.max_files += 1
    after = config.config_hash()
    assert before != after