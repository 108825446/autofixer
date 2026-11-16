"""通知适配器（日志/空实现/飞书 Webhook）。"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, text: str, link: str | None = None) -> None:
        ...


class NullNotifier:
    async def send(self, text: str, link: str | None = None) -> None:
        return None


class LogNotifier:
    async def send(self, text: str, link: str | None = None) -> None:
        logger.info("notify: %s%s", text, f" {link}" if link else "")


class FeishuWebhookNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send(self, text: str, link: str | None = None) -> None:
        content = text if not link else f"{text}\n{link}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self.webhook_url,
                json={"msg_type": "text", "content": {"text": content}},
            )
            response.raise_for_status()