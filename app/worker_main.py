"""Docker Compose 使用的 Worker 进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from app.agents.analyzer import AnthropicAnalyzer
from app.core.config import load_config
from app.core.db import init_db, make_engine, make_session_factory
from app.integrations.gitlab import GitLabClient
from app.integrations.gitops import GitOps
from app.integrations.notify import FeishuWebhookNotifier, LogNotifier
from app.workflow.worker import Worker


async def main() -> None:
    # 本地直接运行 `python -m app.worker_main` 时自动加载 .env；
    # Docker Compose 已经通过 env_file 注入，重复加载不会覆盖现有变量。
    load_dotenv(".env")
    logging.basicConfig(level=logging.INFO)
    config_path = os.environ.get("AUTOFIX_CONFIG", "config.yaml")
    if not Path(config_path).exists():
        config_path = "config.example.yaml"
    config = load_config(config_path)
    engine = make_engine(config.app.database_url)
    if os.environ.get("AUTOFIX_INIT_DB") == "1":
        await init_db(engine)
    session_factory = make_session_factory(engine)

    api_key = os.environ.get(config.project.model.api_key_env, "")
    if not api_key:
        raise SystemExit(f"missing env {config.project.model.api_key_env}")
    analyzer = AnthropicAnalyzer(
        api_key=api_key,
        model=config.project.model.model,
        base_url=config.project.model.base_url or os.environ.get("ANTHROPIC_BASE_URL"),
        max_tokens=config.project.model.max_tokens,
        policy=config.project.analysis,
    )

    repo_path = Path(config.project.repo_path).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise SystemExit(
            f"repository not found: {repo_path}. "
            "Clone it to project.repo_path before starting the worker."
        )

    token = os.environ.get("GITLAB_TOKEN", "")
    project = config.source.project or config.project.repo
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    notifier = FeishuWebhookNotifier(webhook) if webhook else LogNotifier()

    async with GitLabClient(
        base_url=config.project.gitlab_url, token=token, project=project
    ) as client:
        worker = Worker(
            config=config,
            session_factory=session_factory,
            analyzer=analyzer,
            git=GitOps(repo_path),
            gitlab=client,
            notifier=notifier,
            worktree_root=Path("work"),
            artifacts_dir=Path("data/artifacts"),
        )
        await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())