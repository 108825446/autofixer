"""应用组装根。

职责：
- 构建 FastAPI 应用与 lifespan
- 未注入时创建数据库 Engine/Session 工厂
- 挂载 API Router 与 HTML 模板
- 暴露 `uvicorn app.main:app` 使用的模块级 `app`
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes import build_router
from app.core.config import AppConfig, load_config
from app.core.db import init_db, make_engine, make_session_factory
from app.core.state_machine import TransitionError


def create_app(
    config: AppConfig,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    init_database: bool = False,
) -> FastAPI:
    """创建应用实例。

    测试会注入 `session_factory`；生产从 `config.app.database_url` 创建。
    `init_database` 仅用于本地/MVP 启动和测试夹具；生产应执行 Alembic。
    """

    engine = None
    if session_factory is None:
        engine = make_engine(config.app.database_url)
        session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_database and engine is not None:
            await init_db(engine)
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Bug Auto-Fix MVP", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.session_factory = session_factory

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "api" / "templates"))
    app.state.templates = templates
    app.include_router(build_router(config, session_factory, templates))

    @app.exception_handler(TransitionError)
    async def transition_error_handler(request, exc: TransitionError):
        """非法状态迁移返回 409，而不是 500。"""

        return HTMLResponse(f"<h1>409</h1><p>{exc}</p>", status_code=409)

    return app


def _app_from_env() -> FastAPI:
    """为 `uvicorn app.main:app` 构建模块级应用。"""

    load_dotenv(".env")
    config_path = os.environ.get("AUTOFIX_CONFIG", "config.yaml")
    if not Path(config_path).exists():
        config_path = "config.example.yaml"
    config = load_config(config_path)
    # 默认不建表：生产/验证环境统一用 alembic upgrade head。
    # 仅本地临时开发可设置 AUTOFIX_INIT_DB=1 允许 create_all。
    return create_app(config, init_database=os.environ.get("AUTOFIX_INIT_DB") == "1")


app = _app_from_env()