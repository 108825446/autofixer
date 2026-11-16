"""交付链实现。

MR #1 把唯一的修复提交合入基础分支；测试分支只接收
该提交在提升分支上的 cherry-pick，因此 develop 上无关提交
不会进入 test。"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.models import BugTask, Delivery
from app.integrations.gitlab import GitLabClient, GitLabError
from app.integrations.gitops import GitError, GitOps


class DeliveryError(RuntimeError):
    """交付失败，并标记为需人工或不可恢复。"""
    def __init__(self, message: str, *, manual_required: bool = False, unrecoverable: bool = False):
        super().__init__(message)
        self.manual_required = manual_required
        self.unrecoverable = unrecoverable


@dataclass
class PromoteResult:
    """把修复 cherry-pick 到提升分支的结果。"""
    worktree: Path
    promote_branch: str
    promote_sha: str
    patch_id_before: str
    patch_id_after: str


class DeliveryService:
    """执行 MR1 → merge → promote → MR2 → merge → 可选部署。"""
    def __init__(self, config: AppConfig, git: GitOps, gitlab: GitLabClient, worktree_root: str | Path):
        self.config = config
        self.git = git
        self.gitlab = gitlab
        self.worktree_root = Path(worktree_root)

    async def _step(self, session: AsyncSession, task_id: str, step: str) -> Delivery:
        stmt = select(Delivery).where(Delivery.task_id == task_id, Delivery.step == step)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = Delivery(task_id=task_id, step=step, status="PENDING")
            session.add(row)
            await session.flush()
        return row

    async def _mark(
        self,
        session: AsyncSession,
        row: Delivery,
        status: str,
        *,
        ref: str | None = None,
        detail: dict | None = None,
    ) -> None:
        row.status = status
        row.attempts += 1
        if ref is not None:
            row.ref = ref
        if detail is not None:
            row.detail = detail
        await session.flush()

    async def _push_fix_branch(self, task: BugTask, fix_branch: str) -> None:
        self.git.push("origin", fix_branch, force=True)

    async def _create_or_get_mr1(self, session: AsyncSession, task: BugTask, fix_branch: str) -> int:
        row = await self._step(session, task.id, "MR1")
        if row.status == "OK" and row.ref:
            return int(row.ref)
        if not row.ref:
            mr = await self.gitlab.create_mr(
                source_branch=fix_branch,
                target_branch=task.base_branch,
                title=f"fix: {task.issue_key} {task.title}",
                description=f"Automated fix for {task.issue_key}.\n\nTask: {task.id}",
            )
            row.ref = str(mr["iid"])
            await session.flush()
        await self._mark(session, row, "OK", ref=row.ref)
        return int(row.ref)

    async def _merge_mr(
        self,
        session: AsyncSession,
        task: BugTask,
        *,
        step: str,
        iid: int,
        message: str,
    ) -> str:
        row = await self._step(session, task.id, step)
        if row.status == "OK" and row.ref:
            return row.ref
        if self.config.project.delivery.merge_mode == "CREATE_MR_ONLY":
            await self._mark(session, row, "SKIPPED", ref=str(iid))
            raise DeliveryError(
                f"MR !{iid} created; manual merge required (CREATE_MR_ONLY)",
                manual_required=True,
            )
        try:
            await self.gitlab.wait_mergeable(iid)
            merged = await self.gitlab.merge_mr(iid, message=message)
        except GitLabError as exc:
            if exc.status_code in (405, 406, 409, 422):
                await self._mark(session, row, "FAILED", ref=str(iid), detail={"error": str(exc)})
                raise DeliveryError(
                    f"MR !{iid} cannot be merged automatically: {exc}",
                    manual_required=True,
                ) from exc
            raise
        merge_sha = merged.get("merge_commit_sha") or merged.get("sha") or ""
        await self._mark(session, row, "OK", ref=merge_sha, detail={"mr_iid": iid})
        return merge_sha

    def promote(
        self,
        *,
        fix_sha: str,
        test_branch: str,
        promote_branch: str,
        worktree_path: str | Path,
    ) -> PromoteResult:
        """从 test 创建提升分支，并只 cherry-pick 一个修复提交。

        分支从 origin/test 而不是 develop 开始，这是阻止无关 develop
        提交进入测试分支的关键；patch-id 比较证明 cherry-pick 应用的
        是同一个变更。
        """
        self.git.fetch()
        path = Path(worktree_path)
        self.git.run(["branch", "-D", promote_branch], check=False)
        self.git.worktree_add(path, ref=f"origin/{test_branch}", branch=promote_branch, force=True)
        try:
            patch_before = self.git.patch_id(fix_sha)
            promote_sha = self.git.cherry_pick(fix_sha, cwd=path)
            patch_after = self.git.patch_id(promote_sha)
            if not patch_before or patch_before != patch_after:
                raise DeliveryError(
                    f"patch-id mismatch: {patch_before} != {patch_after}",
                    manual_required=True,
                )
            return PromoteResult(path, promote_branch, promote_sha, patch_before, patch_after)
        except GitError as exc:
            raise DeliveryError(f"cherry-pick failed: {exc}", manual_required=True) from exc
        except Exception:
            self.git.worktree_remove(path)
            raise

    async def _deploy_local(self, task: BugTask, sha: str) -> None:
        deploy = self.config.project.delivery.deploy
        if deploy.mode != "LOCAL_DEPLOY":
            return
        if not deploy.command:
            raise DeliveryError("LOCAL_DEPLOY requires deploy.command", manual_required=True)
        env = {**os.environ, "SHA": sha, "TASK_ID": task.id}
        try:
            proc = subprocess.run(
                deploy.command,
                shell=True,
                cwd=str(self.git.repo_path),
                env=env,
                text=True,
                capture_output=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryError("deploy command timed out", manual_required=True) from exc
        if proc.returncode != 0:
            raise DeliveryError(
                f"deploy command failed: {proc.returncode} {proc.stderr[-500:]}",
                manual_required=True,
            )
        if deploy.health_url:
            await self._health_check(deploy.health_url)

    async def _health_check(self, url: str) -> None:
        last_error = ""
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(3):
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                    last_error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                await asyncio.sleep(2)
        raise DeliveryError(f"health check failed: {last_error}", manual_required=True)

    async def deliver(self, session: AsyncSession, task: BugTask, *, fix_branch: str) -> None:
        """执行 MR1 → merge → promote → MR2 → merge → 可选部署。

        每一步先写入 `delivery` 表再继续，重试会跳过已完成步骤。
        测试分支永远不从 develop 合并，只接收包含一个 cherry-pick 的提升分支。
        """
        await self._push_fix_branch(task, fix_branch)
        mr1 = await self._create_or_get_mr1(session, task, fix_branch)
        if not task.fix_merge_sha:
            task.fix_merge_sha = await self._merge_mr(
                session,
                task,
                step="MERGE1",
                iid=mr1,
                message=f"Merge {task.issue_key} fix",
            )
            await session.flush()

        if not task.fix_merge_sha:
            raise DeliveryError("no fix_merge_sha after MR1 merge", manual_required=True)

        promote_row = await self._step(session, task.id, "PROMOTE")
        promote_branch = f"promote/{task.issue_key}"
        promote_path = self.worktree_root / f"promote-{task.id}"
        if promote_row.status != "OK":
            result = self.promote(
                fix_sha=task.fix_sha or task.fix_merge_sha,
                test_branch=task.test_branch,
                promote_branch=promote_branch,
                worktree_path=promote_path,
            )
            self.git.push("origin", promote_branch, cwd=promote_path, force=True)
            await self._mark(
                session,
                promote_row,
                "OK",
                ref=result.promote_sha,
                detail={
                    "promote_branch": promote_branch,
                    "patch_id_before": result.patch_id_before,
                    "patch_id_after": result.patch_id_after,
                },
            )
            self.git.worktree_remove(promote_path)
            mr2 = await self.gitlab.create_mr(
                source_branch=promote_branch,
                target_branch=task.test_branch,
                title=f"promote: {task.issue_key} only this fix",
                description=f"Cherry-pick of {task.fix_sha} for {task.issue_key}.",
            )
            row2 = await self._step(session, task.id, "MR2")
            row2.ref = str(mr2["iid"])
            await self._mark(session, row2, "OK", ref=row2.ref)

        row2 = await self._step(session, task.id, "MR2")
        if not row2.ref:
            mr2 = await self.gitlab.create_mr(
                source_branch=promote_branch,
                target_branch=task.test_branch,
                title=f"promote: {task.issue_key} only this fix",
                description=f"Cherry-pick of {task.fix_sha} for {task.issue_key}.",
            )
            row2.ref = str(mr2["iid"])
            await self._mark(session, row2, "OK", ref=row2.ref)

        if not task.test_merge_sha:
            task.test_merge_sha = await self._merge_mr(
                session,
                task,
                step="MERGE2",
                iid=int(row2.ref or "0"),
                message=f"Promote {task.issue_key} to test",
            )
            await session.flush()

        if self.config.project.delivery.deploy.mode == "LOCAL_DEPLOY":
            deploy_row = await self._step(session, task.id, "DEPLOY_LOCAL")
            if deploy_row.status != "OK":
                try:
                    await self._deploy_local(task, task.test_merge_sha or "")
                except DeliveryError:
                    await self._mark(session, deploy_row, "FAILED")
                    if self.config.project.delivery.deploy.rollback_command:
                        subprocess.run(
                            self.config.project.delivery.deploy.rollback_command,
                            shell=True,
                            cwd=str(self.git.repo_path),
                            text=True,
                            capture_output=True,
                        )
                    raise
                await self._mark(session, deploy_row, "OK")
                task.deployed = True
                await session.flush()
                health_row = await self._step(session, task.id, "HEALTH")
                await self._mark(session, health_row, "OK")