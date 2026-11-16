"""轻量异步 GitLab REST 客户端。

只实现 MVP 需要的接口：Issue、MR、评论、合并和分支查询。
把 HTTP 细节留在这一层，避免 GitLab 语义渗透到工作流代码。"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import httpx


class GitLabError(RuntimeError):
    """GitLab API 异常，尽可能携带 HTTP 状态码。"""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitLabClient:
    """绑定单个项目的异步 GitLab REST 客户端。"""
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        project: str,
        timeout: float = 30,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project = project
        self.project_id = urllib.parse.quote(project, safe="")
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout

    async def __aenter__(self) -> GitLabClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v4",
                headers={"PRIVATE-TOKEN": self.token},
                timeout=self.timeout,
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise GitLabError("GitLabClient is not started")
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GitLabError(
                f"GitLab {method} {path} -> {response.status_code}: {response.text[:500]}",
                response.status_code,
            )
        if response.status_code == 204:
            return None
        return response.json()

    async def get_project(self) -> dict:
        return await self._request("GET", f"/projects/{self.project_id}")

    async def get_branch(self, branch: str) -> dict:
        encoded = urllib.parse.quote(branch, safe="")
        return await self._request("GET", f"/projects/{self.project_id}/repository/branches/{encoded}")

    async def create_mr(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict:
        return await self._request(
            "POST",
            f"/projects/{self.project_id}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": False,
                "squash": False,
            },
        )

    async def get_mr(self, iid: int) -> dict:
        return await self._request("GET", f"/projects/{self.project_id}/merge_requests/{iid}")

    async def wait_mergeable(self, iid: int, *, timeout: float = 300, interval: float = 5) -> dict:
        """轮询 MR，直到 GitLab 报告可合并。

        冲突/关闭状态或超时抛出 `GitLabError`；调用方会转为 MANUAL 任务。
        """

        deadline = asyncio.get_running_loop().time() + timeout
        last: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            last = await self.get_mr(iid)
            status = last.get("detailed_merge_status") or last.get("merge_status")
            if status in ("mergeable", "can_be_merged"):
                return last
            if status in ("cannot_be_merged", "broken", "conflict"):
                raise GitLabError(f"MR !{iid} is not mergeable: {status}")
            await asyncio.sleep(interval)
        raise GitLabError(f"MR !{iid} not mergeable within {timeout}s: {last.get('detailed_merge_status')}")

    async def merge_mr(self, iid: int, *, message: str = "") -> dict:
        return await self._request(
            "PUT",
            f"/projects/{self.project_id}/merge_requests/{iid}/merge",
            json={
                "squash": False,
                "should_remove_source_branch": False,
                "merge_commit_message": message,
            },
        )

    async def list_issues(
        self,
        *,
        updated_after: str | None = None,
        labels: list[str] | None = None,
        per_page: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "state": "opened",
            "order_by": "updated_at",
            "sort": "asc",
            "per_page": per_page,
        }
        if updated_after:
            params["updated_after"] = updated_after
        if labels:
            params["labels"] = ",".join(labels)
        return await self._request("GET", f"/projects/{self.project_id}/issues", params=params)

    async def comment_issue(self, issue_iid: int, body: str) -> dict:
        return await self._request(
            "POST",
            f"/projects/{self.project_id}/issues/{issue_iid}/notes",
            json={"body": body},
        )

    async def update_issue(
        self,
        issue_iid: int,
        *,
        state_event: str | None = None,
        labels: list[str] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if state_event:
            payload["state_event"] = state_event
        if labels is not None:
            payload["labels"] = ",".join(labels)
        return await self._request(
            "PUT",
            f"/projects/{self.project_id}/issues/{issue_iid}",
            json=payload,
        )

    async def comment_mr(self, mr_iid: int, body: str) -> dict:
        return await self._request(
            "POST",
            f"/projects/{self.project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )