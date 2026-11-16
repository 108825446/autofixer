#!/usr/bin/env python3
"""Bug Auto-Fix MVP 的 GitLab 只读预检。

检查：
  - Token 身份
  - 项目 merge method / squash 策略
  - 基础分支、测试分支的保护
  - 基础分支/测试分支是否存在
  - Token 是否具备合并权限（Developer+）
  - “流水线必须成功”是否会阻断自动合并

用法：
    GITLAB_URL=https://gitlab.example.com \
    GITLAB_TOKEN=xxxx \
    GITLAB_PROJECT=group/order-service \
    python3 scripts/verify_gitlab_preflight.py

不会写入任何数据。退出码 0 = 就绪，1 = 存在阻断项。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class Result:
    level: str  # PASS / WARN / FAIL
    name: str
    detail: str


class GitLab:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def get(self, path: str) -> dict | list:
        url = f"{self.base_url}/api/v4{path}"
        req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.token})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GET {path} -> HTTP {exc.code}: {body[:300]}") from exc


def access_label(level: int | None) -> str:
    return {
        5: "Minimal",
        10: "Guest",
        20: "Reporter",
        30: "Developer",
        40: "Maintainer",
        50: "Owner",
    }.get(level, f"Unknown({level})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("GITLAB_URL", ""))
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""))
    parser.add_argument("--project", default=os.environ.get("GITLAB_PROJECT", ""))
    parser.add_argument("--base-branch", default=os.environ.get("GITLAB_BASE_BRANCH", "develop"))
    parser.add_argument("--test-branch", default=os.environ.get("GITLAB_TEST_BRANCH", "test"))
    args = parser.parse_args()

    if not args.url or not args.token or not args.project:
        print("Missing GITLAB_URL / GITLAB_TOKEN / GITLAB_PROJECT (or --url/--token/--project)")
        return 2

    gl = GitLab(args.url, args.token)
    project_id = urllib.parse.quote(args.project, safe="")
    results: list[Result] = []

    try:
        user = gl.get("/user")
        results.append(Result("PASS", "token", f"user={user.get('username')} id={user.get('id')}"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL token: {exc}")
        return 1

    try:
        project = gl.get(f"/projects/{project_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL project: {exc}")
        return 1

    results.append(Result("PASS", "project", f"{project.get('path_with_namespace')} id={project.get('id')}"))

    merge_method = project.get("merge_method")
    if merge_method == "merge":
        results.append(Result("PASS", "merge_method", "merge commit is allowed"))
    else:
        results.append(
            Result(
                "FAIL",
                "merge_method",
                f"merge_method={merge_method}; must be 'merge' to preserve fix_sha (or change the design)",
            )
        )

    squash_option = project.get("squash_option")
    if squash_option == "always":
        results.append(Result("FAIL", "squash_option", "squash is always; cannot preserve one fix commit"))
    else:
        results.append(Result("PASS", "squash_option", f"squash_option={squash_option}"))

    if project.get("only_allow_merge_if_pipeline_succeeds"):
        results.append(
            Result(
                "WARN",
                "pipeline gate",
                "only_allow_merge_if_pipeline_succeeds=true; without CI integration auto-merge may be blocked; "
                "use CREATE_MR_ONLY fallback",
            )
        )
    else:
        results.append(Result("PASS", "pipeline gate", "pipeline success is not required"))

    if project.get("only_allow_merge_if_all_discussions_are_resolved"):
        results.append(Result("WARN", "discussions", "all discussions must be resolved before merge"))
    else:
        results.append(Result("PASS", "discussions", "discussions do not block merge"))

    access = (project.get("permissions") or {}).get("project_access") or {}
    level = access.get("access_level")
    if level is None:
        # 继承成员关系：尝试查询该用户的 members/all。
        try:
            members = gl.get(f"/projects/{project_id}/members/all?query={urllib.parse.quote(user['username'])}")
            for m in members:
                if m.get("id") == user.get("id"):
                    level = m.get("access_level")
                    break
        except Exception:
            pass
    if level is None:
        results.append(Result("WARN", "access", "cannot determine project access; verify manually"))
    elif level >= 40:
        results.append(Result("PASS", "access", f"{access_label(level)} (can merge protected branches)"))
    elif level >= 30:
        results.append(Result("WARN", "access", f"{access_label(level)} (merge depends on branch protection)"))
    else:
        results.append(Result("FAIL", "access", f"{access_label(level)} (need Developer+ to merge)"))

    try:
        protected = gl.get(f"/projects/{project_id}/protected_branches")
        names = {b.get("name") for b in protected}
        for branch in (args.base_branch, args.test_branch):
            if branch in names:
                results.append(Result("PASS", f"protected:{branch}", "protected"))
            else:
                results.append(Result("FAIL", f"protected:{branch}", "not protected; configure branch protection"))
    except Exception as exc:  # noqa: BLE001
        results.append(Result("WARN", "protected branches", f"cannot read: {exc}"))

    for branch in (args.base_branch, args.test_branch):
        try:
            b = gl.get(f"/projects/{project_id}/repository/branches/{urllib.parse.quote(branch, safe='')}")
            commit = (b.get("commit") or {}).get("short_id")
            results.append(Result("PASS", f"branch:{branch}", f"exists at {commit}"))
        except Exception as exc:  # noqa: BLE001
            results.append(Result("FAIL", f"branch:{branch}", f"missing/unreadable: {exc}"))

    print("\n=== GitLab preflight ===")
    for r in results:
        print(f"{r.level:4}  {r.name}: {r.detail}")

    failed = any(r.level == "FAIL" for r in results)
    print("\nRESULT:", "FAIL" if failed else "PASS (warnings may still require a decision)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())