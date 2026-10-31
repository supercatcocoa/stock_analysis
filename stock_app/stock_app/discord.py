"""Discord Webhook への通知。"""
from __future__ import annotations

import io

import requests

from .config import settings

DISCORD_LIMIT = 2000


def send(message: str, image: io.BytesIO | None = None, filename: str = "chart.png", webhook_url: str | None = None):
    url = webhook_url or settings.discord_webhook_url
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    if len(message) > DISCORD_LIMIT:
        message = message[: DISCORD_LIMIT - 1] + "…"
    if image is not None:
        res = requests.post(url, data={"content": message}, files={"file": (filename, image, "image/png")}, timeout=30)
    else:
        res = requests.post(url, json={"content": message}, timeout=30)
    if res.status_code >= 300:
        raise RuntimeError(f"Discord 送信失敗 {res.status_code}: {res.text[:200]}")
