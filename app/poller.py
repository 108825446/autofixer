"""接入轮询器 CLI 入口。

保留在包根目录，确保 `python -m app.poller` 稳定；
具体实现位于 `app.workflow.poller`。
"""

from __future__ import annotations

import asyncio

from app.workflow.poller import Poller, main

__all__ = ["Poller", "main"]


if __name__ == "__main__":
    asyncio.run(main())
