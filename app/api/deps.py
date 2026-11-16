"""FastAPI 依赖工厂。

依赖从应用配置/Session 工厂构建，而不是模块级全局变量，
以便测试创建隔离的应用实例。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import AppConfig


def make_session_dependency(
    session_factory: async_sessionmaker[AsyncSession],
):
    """返回每个请求一个异步数据库 Session 的依赖。"""

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    return get_session


def make_admin_dependency(config: AppConfig):
    """返回校验管理员 Bearer Token 的依赖。

    未配置 Token 时，本地开发会跳过校验；生产部署必须设置 Token。
    """

    def require_admin(request: Request) -> None:
        expected = config.admin_token()
        if not expected:
            return
        header = request.headers.get("authorization", "")
        if header != f"Bearer {expected}":
            raise HTTPException(status_code=403, detail="admin token required")

    return require_admin