from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.core.config import Limits
from app.integrations.gitops import GitOps
from app.workflow.verifier import RedGreenVerifier, check_scope, is_test_path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_scope_limits_and_forbidden_paths():
    limits = Limits(
        max_files=2,
        max_lines=10,
        allowed_paths=["src/main/**", "src/test/**"],
        forbidden_paths=["pom.xml", "ci/**"],
    )
    result = check_scope(
        ["src/main/a.py", "src/main/b.py", "src/main/c.py", "pom.xml"],
        [(4, 2, "src/main/a.py"), (10, 0, "src/main/b.py"), (1, 0, "pom.xml")],
        ["src/test/old_test.py"],
        limits,
    )
    assert not result.ok
    assert any("too many files" in r for r in result.reasons)
    assert any("too many lines" in r for r in result.reasons)
    assert any("forbidden path" in r for r in result.reasons)
    assert any("test file deleted" in r for r in result.reasons)


def test_is_test_path():
    assert is_test_path("src/test/java/FooTest.java")
    assert is_test_path("tests/test_api.py")
    assert is_test_path("web/src/foo.spec.ts")
    assert not is_test_path("src/main/java/Foo.java")


def test_red_green(repo: Path, tmp_path: Path):
    git_ops = GitOps(repo)
    base_sha = git(repo, "rev-parse", "HEAD")

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

    fix_worktree = tmp_path / "fix"
    git_ops.worktree_add(fix_worktree, ref=fix_sha, force=True)
    result = RedGreenVerifier(git_ops).verify(
        base_sha=base_sha,
        fix_worktree=fix_worktree,
        test_command=f"{sys.executable} -m pytest -q src/test/test_regression.py",
        test_files=["src/test/test_regression.py"],
        base_worktree=tmp_path / "rg-base",
        timeout=60,
    )
    assert result.ok, result
    assert result.reproduced is True
    assert result.base_returncode != 0
    assert result.fix_returncode == 0
    git_ops.worktree_remove(fix_worktree)