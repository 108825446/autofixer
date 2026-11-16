"""Git 子进程封装。

所有仓库操作（Worktree、merge commit、cherry-pick、patch-id）
都经过这个类，测试可以使用真实临时仓库。"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """git 子进程非零退出时抛出。"""
    pass


@dataclass
class GitResult:
    """git 命令的 stdout/stderr/退出码。"""
    stdout: str
    stderr: str
    returncode: int


class GitOps:
    """编码、验证和交付使用的仓库操作。"""
    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path).resolve()

    def run(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        check: bool = True,
        timeout: int = 120,
    ) -> GitResult:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_path),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return GitResult(proc.stdout.strip(), proc.stderr.strip(), proc.returncode)

    def git(self, *args: str, cwd: str | Path | None = None, check: bool = True) -> str:
        return self.run(list(args), cwd=cwd, check=check).stdout

    def fetch(self, remote: str = "origin") -> None:
        self.run(["fetch", remote, "--prune"])

    def rev_parse(self, ref: str, cwd: str | Path | None = None) -> str:
        return self.git("rev-parse", ref, cwd=cwd)

    def head_sha(self, cwd: str | Path | None = None) -> str:
        return self.rev_parse("HEAD", cwd=cwd)

    def merge_base(self, a: str, b: str) -> str:
        return self.git("merge-base", a, b)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run(["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0

    def diff_name_only(self, base: str, head: str, cwd: str | Path | None = None) -> list[str]:
        out = self.git("diff", "--name-only", f"{base}..{head}", cwd=cwd)
        return [line for line in out.splitlines() if line]

    def diff_numstat(self, base: str, head: str, cwd: str | Path | None = None) -> list[tuple[int, int, str]]:
        out = self.git("diff", "--numstat", f"{base}..{head}", cwd=cwd)
        rows: list[tuple[int, int, str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, delete, path = parts
            rows.append((-1 if add == "-" else int(add), -1 if delete == "-" else int(delete), path))
        return rows

    def diff_deleted(self, base: str, head: str, cwd: str | Path | None = None) -> list[str]:
        out = self.git("diff", "--name-only", "--diff-filter=D", f"{base}..{head}", cwd=cwd)
        return [line for line in out.splitlines() if line]

    def show_diff(self, base: str, head: str, cwd: str | Path | None = None) -> str:
        return self.git("diff", "--binary", f"{base}..{head}", cwd=cwd)

    def rev_list(self, base: str, head: str, *, no_merges: bool = False, cwd=None) -> list[str]:
        args = ["rev-list"]
        if no_merges:
            args.append("--no-merges")
        args.append(f"{base}..{head}")
        out = self.git(*args, cwd=cwd)
        return [line for line in out.splitlines() if line]

    def patch_id(self, rev: str) -> str:
        """返回某个提交 Diff 的稳定 patch-id。

        patch-id 在 rebase/cherry-pick 后保持不变，因此可以证明
        提升分支包含的变更与已审批修复一致。
        """

        show = subprocess.run(
            ["git", "show", rev],
            cwd=str(self.repo_path),
            text=True,
            capture_output=True,
            check=True,
        )
        proc = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=str(self.repo_path),
            input=show.stdout,
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout.split()[0] if proc.stdout.strip() else ""

    def worktree_add(self, path: str | Path, *, ref: str, branch: str | None = None, force: bool = True) -> None:
        """创建 Worktree；如存在上次尝试的旧目录则替换。"""

        target = Path(path)
        if target.exists():
            if force:
                self.run(["worktree", "remove", "--force", str(target)], check=False)
                if target.exists():
                    shutil.rmtree(target)
            else:
                raise GitError(f"worktree path exists: {target}")
        self.run(["worktree", "prune"], check=False)
        args = ["worktree", "add", str(target)]
        if branch:
            args.extend(["-b", branch, ref])
        else:
            args.append(ref)
        self.run(args)

    def worktree_remove(self, path: str | Path) -> None:
        self.run(["worktree", "remove", "--force", str(path)], check=False)

    def prune_worktrees(self) -> None:
        self.run(["worktree", "prune"], check=False)

    def reset_hard(self, ref: str, cwd: str | Path | None = None) -> None:
        self.run(["reset", "--hard", ref], cwd=cwd)

    def status_porcelain(self, cwd: str | Path | None = None) -> str:
        return self.git("status", "--porcelain", cwd=cwd)

    def add_all(self, cwd: str | Path | None = None) -> None:
        self.run(["add", "-A"], cwd=cwd)

    def commit_all(self, message: str, cwd: str | Path | None = None) -> str:
        self.add_all(cwd=cwd)
        # 仅在明确需要时允许空提交；通常应失败。
        self.run(
            ["-c", "user.name=autofix-bot", "-c", "user.email=autofix@example.invalid", "commit", "-m", message],
            cwd=cwd,
        )
        return self.head_sha(cwd=cwd)

    def commit_exists(self, rev: str) -> bool:
        return self.run(["cat-file", "-e", f"{rev}^{{commit}}"], check=False).returncode == 0

    def push(
        self, remote: str, branch: str, cwd: str | Path | None = None, *, force: bool = False
    ) -> None:
        args = ["push", "-u"]
        if force:
            args.append("--force-with-lease")
        args.extend([remote, branch])
        self.run(args, cwd=cwd, timeout=180)

    def cherry_pick(self, rev: str, cwd: str | Path | None = None) -> str:
        """cherry-pick 单个提交，并用 `-x` 保留来源。"""

        self.run(["cherry-pick", "-x", rev], cwd=cwd)
        return self.head_sha(cwd=cwd)

    def merge_no_ff(self, rev: str, message: str, cwd: str | Path | None = None) -> str:
        """创建显式 merge commit，保留修复提交 SHA。"""

        self.run(["merge", "--no-ff", "-m", message, rev], cwd=cwd)
        return self.head_sha(cwd=cwd)

    def branch_exists(self, branch: str, remote: str = "origin") -> bool:
        return self.run(["rev-parse", "--verify", f"{remote}/{branch}"], check=False).returncode == 0

    def checkout_new_branch(self, branch: str, start: str, cwd: str | Path | None = None) -> None:
        self.run(["checkout", "-b", branch, start], cwd=cwd)

    def checkout(self, ref: str, cwd: str | Path | None = None) -> None:
        self.run(["checkout", ref], cwd=cwd)