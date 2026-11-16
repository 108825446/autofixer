from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import (
    Agent,
    AppConfig,
    Approval,
    ApprovalGate,
    ApprovalGates,
    AppSection,
    Branches,
    Budget,
    Delivery,
    Deploy,
    Gates,
    Limits,
    Model,
    Project,
    SourceSection,
)
from app.core.db import init_db
from app.core.models import BugTask


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    (repo / "src" / "main").mkdir(parents=True)
    (repo / "src" / "test").mkdir(parents=True)
    (repo / "src" / "main" / "app.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "src" / "test" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "-M", "develop")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "develop")
    git(repo, "checkout", "-b", "test")
    git(repo, "push", "-u", "origin", "test")
    git(repo, "checkout", "develop")
    return repo


@pytest.fixture
def config(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("AUTOFIX_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("AUTOFIX_APPROVAL_SECRET", "test-secret")
    return AppConfig(
        app=AppSection(
            base_url="http://testserver",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        ),
        source=SourceSection(type="static", project="group/order-service"),
        project=Project(
            key="order-service",
            repo="group/order-service",
            gitlab_url="http://gitlab.test",
            repo_path=str(repo),
            branches=Branches(base="develop", test="test", allow_squash=False),
            limits=Limits(
                max_files=10,
                max_lines=200,
                allowed_paths=["src/main/**", "src/test/**"],
                forbidden_paths=["pom.xml", ".gitlab-ci.yml", "ci/**", "db/migration/**"],
            ),
            gates=Gates(
                compile=f"{sys.executable} -c \"print('compile ok')\"",
                unit=f"{sys.executable} -c \"print('unit ok')\"",
                regression=f"{sys.executable} -m pytest -q {{test_class}}",
                timeout_seconds=60,
            ),
            delivery=Delivery(
                merge_mode="AUTO_MERGE_IF_ALLOWED",
                deploy=Deploy(mode="MERGE_ONLY"),
            ),
            approval=Approval(
                user_map={},
                code_owners=["code-owner"],
                gates=ApprovalGates(
                    plan=ApprovalGate(approver="assignee", fallback=["bug-owner"], ttl_hours=24),
                    result=ApprovalGate(approver="assignee", fallback=["bug-owner"], ttl_hours=48),
                    merge=ApprovalGate(
                        approver="code_owner", fallback=["code-owner"], ttl_hours=8
                    ),
                ),
            ),
            agent=Agent(timeout_minutes=5, max_attempts=3),
            model=Model(provider="test", model="test-model"),
            budget=Budget(max_usd_per_task=8, max_usd_per_day=200),
        ),
    )


@pytest_asyncio.fixture
async def engine(tmp_path: Path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite3")
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


@pytest.fixture
def task() -> BugTask:
    return BugTask(
        id="00000000-0000-0000-0000-000000000001",
        source="static",
        issue_key="BUG-1",
        fingerprint="fp",
        title="add returns wrong value",
        body="add(2,3) returns -1",
        repo="group/order-service",
        base_branch="develop",
        test_branch="test",
        state="NEW",
        assignee="bug-owner",
        code_owner="code-owner",
    )


@pytest.fixture
def analysis_payload():
    return {
        "need_info": False,
        "questions": [],
        "root_cause": "add subtracts instead of adding",
        "confidence": 0.9,
        "evidence": ["src/main/app.py:2"],
        "candidate_files": ["src/main/app.py"],
        "plan": ["return a + b"],
        "test_plan": "src/test/test_regression.py",
        "test_command": f"{sys.executable} -m pytest -q src/test/test_regression.py",
        "risk": "LOW",
        "rollback": "revert the fix commit",
    }