"""持久化任务 Worker 与按状态分发的处理器。

单个 Worker 进程通过租约领取 `bug_task` 行，按状态分发，
并提交下一个稳定状态。由于租约可能过期、handler 可能重跑，
所有外部副作用都必须是幂等的。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.acp import AcpClient, PermissionPolicy
from app.agents.analyzer import Analyzer
from app.core.config import AppConfig
from app.core.models import AgentRun, Artifact, BugTask, SysConfig, utcnow
from app.core.security import make_clarification_token, sha256_hex
from app.core.state_machine import transition
from app.integrations.gitlab import GitLabClient
from app.integrations.gitops import GitOps
from app.integrations.notify import Notifier
from app.workflow.approvals import ApprovalService
from app.workflow.delivery import DeliveryError, DeliveryService
from app.workflow.verifier import RedGreenResult, VerifyResult, is_test_path, verify_diff

logger = logging.getLogger(__name__)

CLAIMABLE_STATES = ("NEW", "ANALYZING", "FIXING", "VERIFYING", "DELIVERING")


class WorkerError(RuntimeError):
    """任务缺少必要状态、无法处理时抛出。"""
    pass


class Worker:
    """领取任务并按状态分发到对应处理器。"""
    def __init__(
        self,
        *,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        analyzer: Analyzer,
        git: GitOps,
        gitlab: GitLabClient,
        notifier: Notifier,
        worktree_root: str | Path,
        artifacts_dir: str | Path,
        acp_factory: Callable[..., AcpClient] | None = None,
    ):
        self.config = config
        self.session_factory = session_factory
        self.analyzer = analyzer
        self.git = git
        self.gitlab = gitlab
        self.notifier = notifier
        # 必须使用绝对路径：git worktree add 以 repo_path 为 cwd，
        # 相对路径会被解析到仓库目录下，而不是项目根目录。
        self.worktree_root = Path(worktree_root).expanduser().resolve()
        self.artifacts_dir = Path(artifacts_dir).expanduser().resolve()
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.acp_factory = acp_factory or self._default_acp
        self.approvals = ApprovalService(config)

    def _default_acp(self, command: list[str], cwd: Path, policy: PermissionPolicy) -> AcpClient:
        return AcpClient(command, cwd=cwd, permission_policy=policy)

    async def claim(self, session: AsyncSession) -> BugTask | None:
        """使用租约领取一个可运行任务。

        PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`；测试中的 SQLite
        不支持行锁，回退为普通查询。
        """
        kill = await session.get(SysConfig, "kill_switch")
        if kill is not None and bool(kill.value):
            return None
        now = utcnow()
        stmt = (
            select(BugTask)
            .where(
                BugTask.state.in_(CLAIMABLE_STATES),
                BugTask.next_run_at <= now,
                (BugTask.lease_until.is_(None)) | (BugTask.lease_until < now),
            )
            .order_by(BugTask.next_run_at)
            .limit(1)
        )
        dialect = session.get_bind().dialect.name
        if dialect != "sqlite":
            stmt = stmt.with_for_update(skip_locked=True)
        task = (await session.execute(stmt)).scalar_one_or_none()
        if task is None:
            return None
        task.lease_until = now + timedelta(minutes=50)
        task.version += 1
        await session.flush()
        return task

    async def run_once(self) -> bool:
        """最多领取并处理一个任务；空闲时返回 False。"""

        async with self.session_factory() as session:
            task = await self.claim(session)
            if task is None:
                return False
            await session.commit()
            task_id = task.id
            try:
                await self.handle(session, task)
                await session.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.exception("task %s failed", task_id)
                fresh = await session.get(BugTask, task_id)
                if fresh is not None:
                    fresh.last_error = str(exc)[:4000]
                    fresh.lease_until = None
                    fresh.next_run_at = utcnow() + timedelta(minutes=2 ** min(fresh.attempt, 3))
                    await session.commit()
                return True

    async def run_forever(self, poll_seconds: float = 2.0) -> None:
        while True:
            try:
                worked = await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("worker loop error")
                worked = False
            if not worked:
                await asyncio.sleep(poll_seconds)

    async def handle(self, session: AsyncSession, task: BugTask) -> None:
        """把已领取任务分发到当前状态对应的处理器。"""

        if task.state in ("NEW", "ANALYZING"):
            await self._analyze(session, task)
        elif task.state == "FIXING":
            await self._fix(session, task)
        elif task.state == "VERIFYING":
            await self._verify(session, task)
        elif task.state == "DELIVERING":
            await self._deliver(session, task)
        else:
            logger.info("task %s in non-claimable state %s", task.id, task.state)

    def _base_sha(self, task: BugTask) -> str:
        if task.base_sha:
            return task.base_sha
        for ref in (f"origin/{task.base_branch}", task.base_branch, "HEAD"):
            try:
                return self.git.rev_parse(ref)
            except Exception:  # noqa: BLE001
                continue
        raise WorkerError("cannot resolve base sha")

    async def _next_artifact_version(
        self, session: AsyncSession, task_id: str, type_: str
    ) -> int:
        """返回下一个 Artifact 版本；重试/attempt 重置时也不会撞唯一键。"""

        stmt = (
            select(Artifact.version)
            .where(Artifact.task_id == task_id, Artifact.type == type_)
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        latest = (await session.execute(stmt)).scalar_one_or_none()
        return int(latest or 0) + 1

    async def _save_artifact(
        self,
        session: AsyncSession,
        task: BugTask,
        type_: str,
        version: int,
        payload: dict,
    ) -> Artifact:
        path = self.artifacts_dir / task.id / f"{type_}-v{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(text, encoding="utf-8")
        artifact = Artifact(
            task_id=task.id,
            type=type_,
            version=version,
            path=str(path),
            sha256=sha256_hex(text),
            size=len(text.encode()),
        )
        session.add(artifact)
        await session.flush()
        return artifact

    async def _latest_artifact(self, session: AsyncSession, task_id: str, type_: str) -> Artifact | None:
        stmt = (
            select(Artifact)
            .where(Artifact.task_id == task_id, Artifact.type == type_)
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    def _load_artifact(self, artifact: Artifact) -> dict:
        return json.loads(Path(artifact.path).read_text(encoding="utf-8"))

    async def _analyze(self, session: AsyncSession, task: BugTask) -> None:
        """执行分析、保存 Artifact，并请求 PLAN 审批。"""

        if task.state == "NEW":
            transition(task, "analyze", actor="worker", session=session)
            await session.flush()
        if task.base_sha is None:
            try:
                task.base_sha = self._base_sha(task)
            except Exception as exc:  # noqa: BLE001
                task.last_error = f"analysis failed: cannot resolve base sha: {exc}"
                transition(task, "analysis_failed", actor="worker", session=session)
                await session.flush()
                return
        try:
            analysis = await self.analyzer.analyze(
                issue_key=task.issue_key,
                title=task.title,
                body=task.body,
                repo_path=str(self.git.repo_path),
            )
        except Exception as exc:  # noqa: BLE001
            task.last_error = f"analysis failed: {exc}"
            transition(task, "analysis_failed", actor="worker", session=session)
            await session.flush()
            return

        session.add(
            AgentRun(
                task_id=task.id,
                phase="ANALYZE",
                attempt=task.attempt + 1,
                provider=self.config.project.model.provider,
                model=self.config.project.model.model,
                result=analysis.model_dump(),
                exit_code=0,
                started_at=utcnow(),
                ended_at=utcnow(),
            )
        )
        task.plan_version += 1
        artifact = await self._save_artifact(
            session,
            task,
            "ANALYSIS",
            task.plan_version,
            {"analysis": analysis.model_dump(), "base_sha": task.base_sha},
        )

        if analysis.need_info:
            transition(task, "need_info", actor="worker", session=session)
            expires_at = utcnow() + timedelta(hours=72)
            token = make_clarification_token(
                self.config.approval_secret(),
                task.id,
                task.plan_version,
                expires_at,
            )
            await self.notifier.send(
                f"{task.issue_key} 需要补充 Bug 信息",
                f"{self.config.app.base_url.rstrip('/')}/clarify/{token}",
            )
            await session.flush()
            return

        if (
            analysis.confidence < 0.5
            or not analysis.evidence
            or not analysis.test_command
            or not analysis.candidate_files
        ):
            task.last_error = "analysis low confidence or missing evidence/test command"
            transition(task, "analysis_failed", actor="worker", session=session)
            await session.flush()
            return

        approval, token = await self.approvals.create_plan(
            session, task, analysis_hash=artifact.sha256, base_sha=task.base_sha
        )
        task.last_error = None
        transition(task, "analysis_ready", actor="worker", session=session)
        await session.flush()
        await self.notifier.send(
            f"{task.issue_key} analysis ready; approve plan",
            f"{self.config.app.base_url}/approve/plan/{token}",
        )

    def _fix_prompt(self, task: BugTask, analysis: dict, failure_summary: str = "") -> str:
        limits = self.config.project.limits
        return f"""Fix the bug in the current repository.

Bug: {task.issue_key} {task.title}
Root cause: {analysis.get('root_cause', '')}
Plan:
{chr(10).join('- ' + x for x in analysis.get('plan', []))}

Hard constraints:
- Only modify files under: {', '.join(limits.allowed_paths)}
- At most {limits.max_files} files and {limits.max_lines} changed lines.
- Do not modify CI, dependency locks, deployment scripts, or database migrations.
- Do not delete or weaken existing tests. Add the regression test described below.
- Regression test: {analysis.get('test_plan', '')}
- Test command: {analysis.get('test_command', '')}
- Commit your changes with git. Do not push.

Previous verification failure (if any):
{failure_summary or "None"}
"""

    async def _fix(self, session: AsyncSession, task: BugTask) -> None:
        """执行一次 ACP 编码，并强制一提交/范围约束。"""

        analysis_artifact = await self._latest_artifact(session, task.id, "ANALYSIS")
        if analysis_artifact is None:
            raise WorkerError("analysis artifact missing")
        analysis_payload = self._load_artifact(analysis_artifact)
        analysis = analysis_payload.get("analysis", analysis_payload)
        failure_summary = ""
        evidence_artifact = await self._latest_artifact(session, task.id, "EVIDENCE")
        if evidence_artifact is not None:
            evidence = self._load_artifact(evidence_artifact)
            reasons = evidence.get("reasons") or []
            if reasons:
                failure_summary = "; ".join(str(r) for r in reasons)

        # 每次编码都从冻结的 base SHA 创建干净 Worktree。
        # 分支名固定，重试时覆盖上一次尝试。
        try:
            base_sha = task.base_sha or self._base_sha(task)
        except Exception as exc:  # noqa: BLE001
            task.last_error = f"code fix failed: cannot resolve base sha: {exc}"
            transition(task, "fix_failed", actor="worker", session=session)
            await session.flush()
            return
        branch = f"bugfix/{task.issue_key}"
        worktree = self.worktree_root / f"fix-{task.id}"
        try:
            self.git.fetch()
        except Exception:  # noqa: BLE001
            pass
        self.git.worktree_remove(worktree)
        self.git.run(["branch", "-D", branch], check=False)
        self.git.worktree_add(worktree, ref=base_sha, branch=branch, force=True)
        task.branch = branch
        task.base_sha = base_sha

        policy = PermissionPolicy(worktree, self.config.project.limits.allowed_paths)
        client = self.acp_factory(self.config.project.agent.command, worktree, policy)
        run = None
        try:
            async with client:
                await client.initialize()
                await client.new_session(str(worktree))
                run = await client.prompt(
                    self._fix_prompt(task, analysis, failure_summary),
                    timeout=self.config.project.agent.timeout_minutes * 60,
                )
        except Exception as exc:  # noqa: BLE001
            task.last_error = f"ACP fix failed: {exc}"
            task.attempt += 1
            if task.attempt >= self.config.project.agent.max_attempts:
                transition(task, "fix_failed", actor="worker", session=session)
            else:
                task.next_run_at = utcnow() + timedelta(minutes=2 ** task.attempt)
                task.lease_until = None
            await session.flush()
            return

        session.add(
            AgentRun(
                task_id=task.id,
                phase="FIX",
                attempt=task.attempt + 1,
                provider=self.config.project.agent.type,
                model="agent-managed",
                result={"stop_reason": run.stop_reason, "usage": run.usage},
                exit_code=0,
                started_at=utcnow(),
                ended_at=utcnow(),
            )
        )

        # 交付契约要求恰好一个修复提交。先提交 Agent 未提交的改动，
        # 再用 soft reset 把多个提交压成一个。
        status = self.git.status_porcelain(cwd=worktree)
        if status:
            self.git.commit_all(f"fix: {task.issue_key} {task.title}", cwd=worktree)
        commits = self.git.rev_list(base_sha, "HEAD", no_merges=True, cwd=worktree)
        if not commits:
            task.last_error = "agent produced no changes"
            task.attempt += 1
            if task.attempt >= self.config.project.agent.max_attempts:
                transition(task, "fix_failed", actor="worker", session=session)
            else:
                task.next_run_at = utcnow() + timedelta(minutes=2 ** task.attempt)
                task.lease_until = None
            await session.flush()
            return
        if len(commits) > 1:
            # 保留工作区，把所有 Agent 提交压成一个修复提交。
            self.git.run(["reset", "--soft", base_sha], cwd=worktree)
            self.git.commit_all(f"fix: {task.issue_key} {task.title}", cwd=worktree)

        fix_sha = self.git.head_sha(cwd=worktree)
        files = self.git.diff_name_only(base_sha, fix_sha, cwd=worktree)
        numstat = self.git.diff_numstat(base_sha, fix_sha, cwd=worktree)
        deleted = self.git.diff_deleted(base_sha, fix_sha, cwd=worktree)
        from app.workflow.verifier import check_scope

        # 推送前检查变更范围：越界改动不会到达远端，
        # 即使 Agent 声称已经完成。
        scope = check_scope(files, numstat, deleted, self.config.project.limits)
        if not scope.ok:
            task.last_error = "scope violation: " + "; ".join(scope.reasons)
            transition(task, "fix_failed", actor="worker", session=session)
            await session.flush()
            return

        self.git.push("origin", branch, cwd=worktree, force=True)
        task.fix_sha = fix_sha
        task.patch_id = self.git.patch_id(fix_sha)
        await self._save_artifact(
            session,
            task,
            "DIFF",
            await self._next_artifact_version(session, task.id, "DIFF"),
            {
                "base_sha": base_sha,
                "fix_sha": fix_sha,
                "files": files,
                "lines": scope.lines,
                "diff": self.git.show_diff(base_sha, fix_sha, cwd=worktree),
            },
        )
        task.last_error = None
        transition(task, "fix_committed", actor="worker", session=session)
        await session.flush()

    async def _verify(self, session: AsyncSession, task: BugTask) -> None:
        """执行 L1-L3/Red-Green，成功后创建 RESULT + MERGE 审批。"""

        analysis_artifact = await self._latest_artifact(session, task.id, "ANALYSIS")
        if analysis_artifact is None or not task.fix_sha:
            raise WorkerError("analysis or fix_sha missing")
        payload = self._load_artifact(analysis_artifact)
        analysis = payload.get("analysis", payload)
        worktree = self.worktree_root / f"fix-{task.id}"
        changed_files = self.git.diff_name_only(task.base_sha or "", task.fix_sha, cwd=worktree)
        test_files = [f for f in changed_files if is_test_path(f)]
        # 没有回归测试就没有 Red-Green 证据；应失败关闭，
        # 而不是批准一个只能编译的改动。
        if not test_files:
            task.last_error = "verification requires at least one regression test file"
            transition(task, "verification_blocked", actor="worker", session=session)
            await session.flush()
            return

        suites = self.config.project.gates.resolve(changed_files)
        suite_results: list[tuple[str, VerifyResult, bool]] = []
        for suite_name, suite in suites:
            suite_test_files = [
                f
                for f in test_files
                if any(fnmatch(f, pattern) for pattern in suite.paths)
            ]
            regression = suite.regression.replace(
                "{test_class}", str(analysis.get("test_plan", ""))
            )
            result = verify_diff(
                self.git,
                base_sha=task.base_sha or "",
                head_sha=task.fix_sha,
                limits=self.config.project.limits,
                worktree=worktree,
                compile_command=suite.compile,
                unit_command=suite.unit,
                regression_command=regression or None,
                test_files=suite_test_files or None,
                timeout=suite.timeout_seconds,
                working_dir=suite.working_dir,
            )
            suite_results.append((suite_name, result, bool(suite_test_files)))

        if len(suite_results) == 1:
            result = suite_results[0][1]
        else:
            gates = [gate for _, item, _ in suite_results for gate in item.gates]
            reasons = list(dict.fromkeys(r for _, item, _ in suite_results for r in item.reasons))
            red_green_items = [item.red_green for _, item, had_tests in suite_results if had_tests]
            red_green = RedGreenResult(
                ok=bool(red_green_items) and all(r.ok for r in red_green_items),
                reproduced=bool(red_green_items) and all(r.reproduced for r in red_green_items),
                base_returncode=None,
                fix_returncode=None,
                reason="; ".join(r.reason for r in red_green_items if r.reason),
            )
            result = VerifyResult(
                ok=all(item.ok for _, item, _ in suite_results)
                and bool(red_green_items)
                and all(r.ok for r in red_green_items),
                gates=gates,
                scope=suite_results[0][1].scope,
                red_green=red_green,
                reasons=reasons,
            )
        await self._save_artifact(
            session,
            task,
            "EVIDENCE",
            await self._next_artifact_version(session, task.id, "EVIDENCE"),
            {
                "ok": result.ok,
                "reasons": result.reasons,
                "scope": {
                    "files": result.scope.files,
                    "lines": result.scope.lines,
                    "ok": result.scope.ok,
                },
                "red_green": {
                    "ok": result.red_green.ok,
                    "reproduced": result.red_green.reproduced,
                    "base_returncode": result.red_green.base_returncode,
                    "fix_returncode": result.red_green.fix_returncode,
                    "reason": result.red_green.reason,
                },
                "gates": [
                    {"command": g.command, "returncode": g.returncode, "ok": g.ok}
                    for g in result.gates
                ],
            },
        )
        if not result.ok:
            task.last_error = "; ".join(result.reasons)[:4000]

        if result.ok:
            # 需要两个独立决策：Bug Owner 确认修复，
            # Code Owner 授权合并。
            diff_artifact = await self._latest_artifact(session, task.id, "DIFF")
            diff_payload = self._load_artifact(diff_artifact) if diff_artifact else {}
            diff_hash = sha256_hex(diff_payload.get("diff", ""))
            result_approval, result_token = await self.approvals.create_result(
                session, task, fix_sha=task.fix_sha, diff_hash=diff_hash
            )
            merge_approval, merge_token = await self.approvals.create_merge(
                session, task, fix_sha=task.fix_sha, diff_hash=diff_hash
            )
            transition(task, "verification_passed", actor="worker", session=session)
            await session.flush()
            await self.notifier.send(
                f"{task.issue_key} verification passed; result approval",
                f"{self.config.app.base_url}/approve/result/{result_token}",
            )
            await self.notifier.send(
                f"{task.issue_key} merge authorization",
                f"{self.config.app.base_url}/approve/merge/{merge_token}",
            )
            return

        task.attempt += 1
        if task.attempt >= self.config.project.agent.max_attempts:
            transition(task, "verification_blocked", actor="worker", session=session)
        else:
            transition(task, "verification_failed", actor="worker", session=session)
        await session.flush()

    async def _deliver(self, session: AsyncSession, task: BugTask) -> None:
        """交付到 test、回写来源系统并结束。"""

        delivery = DeliveryService(
            self.config,
            self.git,
            self.gitlab,
            self.worktree_root,
        )
        try:
            await delivery.deliver(session, task, fix_branch=task.branch or f"bugfix/{task.issue_key}")
        except DeliveryError as exc:
            task.last_error = str(exc)
            if exc.unrecoverable:
                transition(task, "delivery_unrecoverable", actor="worker", session=session)
            else:
                transition(task, "delivery_failed", actor="worker", session=session)
            await session.flush()
            await self.notifier.send(f"{task.issue_key} delivery needs attention: {exc}")
            return

        # 当前 MVP 不执行来源系统回写；DONE 只表示已合入 test。
        transition(task, "delivery_succeeded", actor="worker", session=session)
        await session.flush()
        await self.notifier.send(f"{task.issue_key} delivered to test")