"""周期性接入轮询循环。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import AppConfig, load_config
from app.core.db import init_db, make_engine, make_session_factory
from app.integrations.gitlab import GitLabClient
from app.workflow.intake import BugSourceAdapter, GitLabIssueAdapter, IntakeService

logger = logging.getLogger(__name__)


class Poller:
    """周期性拉取来源事件并创建任务。"""
    def __init__(
        self,
        *,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        adapter: BugSourceAdapter,
        interval_seconds: int = 300,
    ):
        self.config = config
        self.session_factory = session_factory
        self.adapter = adapter
        self.interval_seconds = interval_seconds

    async def run_once(self) -> int:
        """轮询一次，返回创建/更新的任务数。"""

        async with self.session_factory() as session:
            tasks = await IntakeService(self.config).poll(session, self.adapter)
            await session.commit()
            if tasks:
                logger.info("intake poll created/updated %d tasks", len(tasks))
            return len(tasks)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("intake poll failed")
            await asyncio.sleep(self.interval_seconds)


async def main() -> None:
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
    token = os.environ.get(config.source.token_env, "") or os.environ.get("GITLAB_TOKEN", "")
    project = config.source.project or config.project.repo
    async with GitLabClient(
        base_url=config.project.gitlab_url, token=token, project=project
    ) as client:
        adapter = GitLabIssueAdapter(client, config)
        await Poller(
            config=config,
            session_factory=session_factory,
            adapter=adapter,
            interval_seconds=config.source.poll_interval_seconds,
        ).run_forever()


if __name__ == "__main__":
    asyncio.run(main())