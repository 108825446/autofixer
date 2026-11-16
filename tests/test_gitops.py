from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.integrations.gitops import GitOps
from app.workflow.delivery import DeliveryService


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class FakeGitLab:
    async def create_mr(self, **kwargs):
        return {"iid": 1}

    async def wait_mergeable(self, iid, **kwargs):
        return {"iid": iid, "detailed_merge_status": "mergeable"}

    async def merge_mr(self, iid, **kwargs):
        return {"merge_commit_sha": "f" * 40}


@pytest.mark.asyncio
async def test_promote_cherry_picks_only_the_fix(repo: Path, config, tmp_path: Path):
    git(repo, "checkout", "-b", "bugfix/BUG-1")
    (repo / "src" / "main" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "src" / "test" / "test_regression.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src' / 'main'))\n"
        "from app import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix")
    fix_sha = git(repo, "rev-parse", "HEAD")

    # 在修复基线之后向 develop 加一个无关提交，用来验证隔离性。
    git(repo, "checkout", "develop")
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unrelated")
    git(repo, "merge", "--no-ff", "-q", "bugfix/BUG-1", "-m", "merge fix")
    git(repo, "push", "origin", "develop")

    service = DeliveryService(config, GitOps(repo), FakeGitLab(), tmp_path / "worktrees")
    result = service.promote(
        fix_sha=fix_sha,
        test_branch="test",
        promote_branch="promote/BUG-1",
        worktree_path=tmp_path / "promote",
    )
    assert result.patch_id_before == result.patch_id_after
    files = GitOps(repo).diff_name_only("origin/test", result.promote_sha, cwd=result.worktree)
    assert files == ["src/main/app.py", "src/test/test_regression.py"]
    assert "unrelated.txt" not in files
    service.git.worktree_remove(result.worktree)


def test_direct_merge_brings_unrelated_commit(repo: Path):
    # 和真实情况一样，bugfix 基于包含无关提交的 develop。
    git(repo, "checkout", "develop")
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unrelated")
    git(repo, "checkout", "-b", "bugfix/BUG-1")
    (repo / "src" / "main" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix")
    git(repo, "checkout", "test")
    git(repo, "merge", "--no-ff", "-q", "bugfix/BUG-1", "-m", "direct merge")
    changed = GitOps(repo).diff_name_only("origin/test", "HEAD")
    assert "unrelated.txt" in changed
    assert "src/main/app.py" in changed