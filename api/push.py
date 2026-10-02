"""Push notifications (Telegram / ntfy) shared by the background automations.

Credentials come only from environment variables: TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID, and/or NTFY_TOPIC (optional NTFY_SERVER). Each automation
owns a ``Pusher`` whose ``enabled`` flag mirrors its ``push`` setting.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable


def channels() -> list[str]:
    configured = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        configured.append("telegram")
    if os.environ.get("NTFY_TOPIC"):
        configured.append("ntfy")
    return configured


def send(text: str, title: str) -> list[str]:
    """Send ``text`` to every configured channel; returns error strings."""
    errors: list[str] = []
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        body = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{text}"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=10).close()
        except Exception as exc:  # network / bad token: report, never raise
            errors.append(f"telegram: {exc}")
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        request = urllib.request.Request(f"{server}/{topic}", data=text.encode("utf-8"), headers={"Title": title})
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:
            errors.append(f"ntfy: {exc}")
    return errors


class Pusher:
    """One automation's push switch and last send error."""

    def __init__(self, title: str, send_text: Callable[[str], list[str]] | None = None) -> None:
        self.title = title
        self.enabled = False
        self.last_error: str | None = None
        self._send = send_text or (lambda text: send(text, title))

    async def push(self, texts: Iterable[str]) -> None:
        """Send each text off the event loop; no-op when off or no channel is set."""
        texts = list(texts)
        if not texts or not self.enabled or not channels():
            return
        errors: list[str] = []
        for text in texts:
            errors += await asyncio.to_thread(self._send, text)
        self.last_error = "; ".join(errors) or None

    def status(self) -> dict[str, Any]:
        return {"push_enabled": self.enabled, "push_channels": channels(), "last_push_error": self.last_error}
