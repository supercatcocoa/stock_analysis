"""Discord Webhook への通知。"""
from __future__ import annotations

import io

import requests

from .config import settings

DISCORD_LIMIT = 2000


def send(
    message: str,
    file: io.BytesIO | None = None,
    filename: str = "chart.png",
    mime: str = "image/png",
    webhook_url: str | None = None,
):
    """メッセージを送る。file を渡すと画像や PDF を添付する（Webhook の上限は通常 10MB）。"""
    url = webhook_url or settings.discord_webhook_url
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    if len(message) > DISCORD_LIMIT:
        message = message[: DISCORD_LIMIT - 1] + "…"
    if file is not None:
        res = requests.post(url, data={"content": message}, files={"file": (filename, file, mime)}, timeout=60)
    else:
        res = requests.post(url, json={"content": message}, timeout=30)
    if res.status_code >= 300:
        raise RuntimeError(f"Discord 送信失敗 {res.status_code}: {res.text[:200]}")
