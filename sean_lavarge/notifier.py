"""Alerts to Discord and/or Telegram. Silent if no webhook is configured; never crashes the bot."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("sean_lavarge")


def send(text: str) -> None:
    text = f"[Sean Lavarge{' DEMO' if os.getenv('TESTNET', 'true').lower() != 'false' else ''}] {text}"
    log.info("ALERT %s", text)
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": text[:1900]}, timeout=10)
        except requests.RequestException as e:
            log.warning("discord alert failed: %s", e)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000]}, timeout=10)
        except requests.RequestException as e:
            log.warning("telegram alert failed: %s", e)
