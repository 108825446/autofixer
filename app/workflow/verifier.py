"""L1-L3 门禁、Red-Green 证据和 Diff 范围检查。

只信工具退出码；验证器还强制 10 文件 / 200 行、允许/禁止路径，
以及禁止删除既有测试。"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Limits
from app.integrations.gitops import GitOps


@dataclass
class GateResult:
    """单个门禁命令的结果。"""
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class ScopeResult:
    """Diff 范围评估（文件、行数、路径策略）。"""
    ok: bool
    files: list[str]
    lines: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class RedGreenResult:
    """回归测试修复前失败、修复后通过的证据。"""
    ok: bool
    reproduced: bool
    base_returncode: int | None
    fix_returncode: int | None
    reason: str = ""


@dataclass
class VerifyResult:
    """门禁、范围和 Red-Green 的合并结果。"""
    ok: bool
    gates: list[GateResult]
    scope: ScopeResult
    red_green: RedGreenResult
    reasons: list[str] = field(default_factory=list)


def is_test_path(path: str) -> bool:
    """根据常见 Java/Python/JS 目录布局判断测试文件。"""

    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (
        "/test/" in p
        or "/tests/" in p
        or p.startswith("test/")
        or p.startswith("tests/")
        or p.startswith("src/test/")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("Test.java")
        or name.endswith("Tests.java")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
    )


def matches_any(path: str, patterns: list[str]) -> bool:
    """`path` 是否匹配 `patterns` 中任意 glob。"""

    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def check_scope(
    files: list[str],
    numstat: list[tuple[int, int, str]],
    deleted: list[str],
    limits: Limits,
) -> ScopeResult:
    """评估硬性 Diff 约束（设计中的 I1）。

    刻意不依赖 Agent 自述：数字来自 `git diff --numstat`，
    路径列表来自 `git diff --name-only`。
    """

    reasons: list[str] = []
    if len(files) > limits.max_files:
        reasons.append(f"too many files: {len(files)} > {limits.max_files}")

    lines = 0
    for add, delete, path in numstat:
        if add < 0 or delete < 0:
            reasons.append(f"binary file changed: {path}")
            continue
        lines += add + delete
    if lines > limits.max_lines:
        reasons.append(f"too many lines: {lines} > {limits.max_lines}")

    for path in files:
        if not matches_any(path, limits.allowed_paths):
            reasons.append(f"path not allowed: {path}")
        if matches_any(path, limits.forbidden_paths):
            reasons.append(f"forbidden path: {path}")
        if any(ch in path for ch in ("..", "\x00")):
            reasons.append(f"unsafe path: {path}")

    for path in deleted:
        if is_test_path(path):
            reasons.append(f"test file deleted: {path}")

    return ScopeResult(ok=not reasons, files=files, lines=lines, reasons=reasons)


def run_gate(command: str, cwd: str | Path, timeout: int) -> GateResult:
    """运行一条 shell 命令并采集退出码/输出。

    超时统一为退出码 124，调用方按门禁失败处理，而不是抛异常。
    """

    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return GateResult(command, proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return GateResult(
            command,
            124,
            stdout,
            stderr,
            timed_out=True,
        )


def _copy_test_files(source_worktree: Path, target_worktree: Path, test_files: list[str]) -> None:
    for rel in test_files:
        src = source_worktree / rel
        dst = target_worktree / rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


class RedGreenVerifier:
    """在 base_sha 和 fix_sha 上运行同一回归测试。"""
    def __init__(self, git: GitOps):
        self.git = git

    def verify(
        self,
        *,
        base_sha: str,
        fix_worktree: str | Path,
        test_command: str,
        test_files: list[str],
        base_worktree: str | Path,
        timeout: int,
        working_dir: str = ".",
    ) -> RedGreenResult:
        """在 base 和 fix 上运行同一回归命令。

        有效结果是 base != 0 且 fix == 0；如果 base 已通过，说明测试
        没有复现 Bug，任务不能被自动批准。
        """

        base_path = Path(base_worktree)
        fix_path = Path(fix_worktree)
        if not test_files:
            return RedGreenResult(False, False, None, None, "no test files provided")

        self.git.worktree_add(base_path, ref=base_sha, force=True)
        try:
            _copy_test_files(fix_path, base_path, test_files)
            base_result = run_gate(test_command, base_path / working_dir, timeout)
            fix_result = run_gate(test_command, fix_path / working_dir, timeout)
            reproduced = base_result.returncode != 0
            ok = reproduced and fix_result.returncode == 0
            reason = ""
            if not reproduced:
                reason = "regression test passes on base_sha (not reproduced)"
            elif fix_result.returncode != 0:
                reason = "regression test fails on fix_sha"
            return RedGreenResult(
                ok=ok,
                reproduced=reproduced,
                base_returncode=base_result.returncode,
                fix_returncode=fix_result.returncode,
                reason=reason,
            )
        finally:
            self.git.worktree_remove(base_path)


def verify_diff(
    git: GitOps,
    *,
    base_sha: str,
    head_sha: str,
    limits: Limits,
    worktree: str | Path | None = None,
    compile_command: str | None = None,
    unit_command: str | None = None,
    regression_command: str | None = None,
    test_files: list[str] | None = None,
    timeout: int = 600,
    working_dir: str = ".",
) -> VerifyResult:
    """运行编译/单测/回归门禁，并检查 Diff 与 Red-Green。"""

    files = git.diff_name_only(base_sha, head_sha, cwd=worktree)
    numstat = git.diff_numstat(base_sha, head_sha, cwd=worktree)
    deleted = git.diff_deleted(base_sha, head_sha, cwd=worktree)
    scope = check_scope(files, numstat, deleted, limits)

    command_root = Path(worktree or git.repo_path) / working_dir
    gates: list[GateResult] = []
    if compile_command:
        gates.append(run_gate(compile_command, command_root, timeout))
    if unit_command:
        gates.append(run_gate(unit_command, command_root, timeout))
    if regression_command:
        gates.append(run_gate(regression_command, command_root, timeout))

    rg = RedGreenResult(False, False, None, None, "not run")
    if test_files and regression_command:
        base_worktree = Path(worktree or git.repo_path).parent / "rg-base"
        fix_worktree = Path(worktree or git.repo_path)
        base_sha_for_test = base_sha
        rg = RedGreenVerifier(git).verify(
            base_sha=base_sha_for_test,
            fix_worktree=fix_worktree,
            test_command=regression_command,
            test_files=test_files,
            base_worktree=base_worktree,
            timeout=timeout,
            working_dir=working_dir,
        )
        gates.append(GateResult("red-green", 0 if rg.ok else 1, "", rg.reason))

    reasons = list(scope.reasons)
    for gate in gates:
        if not gate.ok:
            reasons.append(f"gate failed: {gate.command} (exit {gate.returncode})")
    if test_files and not rg.ok:
        reasons.append(f"red-green failed: {rg.reason}")

    return VerifyResult(
        ok=scope.ok and all(g.ok for g in gates) and (rg.ok if test_files else True),
        gates=gates,
        scope=scope,
        red_green=rg,
        reasons=reasons,
    )