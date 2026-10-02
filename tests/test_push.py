"""Shared push helper: gating on the per-automation flag and configured channels."""
import asyncio

from api import push


def test_pusher_sends_only_when_enabled_and_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    sent: list[str] = []
    pusher = push.Pusher("Test", lambda text: sent.append(text) or [])

    pusher.enabled = True
    asyncio.run(pusher.push(["no channel"]))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    pusher.enabled = False
    asyncio.run(pusher.push(["disabled"]))
    assert sent == []

    pusher.enabled = True
    asyncio.run(pusher.push(f"msg {i}" for i in range(2)))
    assert sent == ["msg 0", "msg 1"]
    assert pusher.status() == {"push_enabled": True, "push_channels": ["telegram"], "last_push_error": None}


def test_pusher_records_send_errors(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    pusher = push.Pusher("Test", lambda text: ["ntfy: down"])
    pusher.enabled = True
    asyncio.run(pusher.push(["a"]))
    assert pusher.last_error == "ntfy: down"


def _capture(owner, monkeypatch) -> list[str]:
    """Swap ``owner.pusher`` for an enabled one that records texts instead of sending."""
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    sent: list[str] = []
    owner.pusher = type(owner.pusher)("Test", lambda text: sent.append(text) or [])
    owner.pusher.enabled = True
    return sent


def test_silver_bullet_live_check_pushes_new_signals(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from api.main import SilverBulletLiveScanner

    scanner = SilverBulletLiveScanner()
    sent = _capture(scanner, monkeypatch)
    inside = datetime(2026, 10, 1, 10, 30, tzinfo=ZoneInfo("America/New_York"))  # Thursday, in window

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return inside.astimezone(tz) if tz else inside.replace(tzinfo=None)

    signal = {"id": "COMEX:GC1!|bullish|10:25", "symbol": "COMEX:GC1!", "direction": "bullish", "signal_time": "10:25",
              "entry": 2650.5, "stop_loss": 2645.0, "target": 2661.0}

    async def fake_scan(scan_date, now):
        return [signal]

    monkeypatch.setattr("api.main.datetime", _Clock)
    monkeypatch.setattr(scanner, "_scan", fake_scan)
    asyncio.run(scanner._check())
    scanner.stop()  # a re-armed scan sees the same signal as new again
    asyncio.run(scanner._check())
    assert sent == ["COMEX:GC1! bullish @ 10:25; entry 2650.5, SL 2645, TP 2661"]


def test_ltf_check_pushes_only_triggered(monkeypatch):
    from types import SimpleNamespace

    from api.main import LtfConfirmationWatcher

    watcher = LtfConfirmationWatcher()
    sent = _capture(watcher, monkeypatch)
    watcher._alert(SimpleNamespace(symbol="OLD", strategy="s", direction="long", entry=1, sl=0, target=2, note=""), "triggered")

    def setup(symbol):
        return SimpleNamespace(symbol=symbol, strategy="propulsion", direction="long", entry=100, sl=95, target=110, note="")

    async def arm(force):
        watcher._alert(setup("NSE:A"), "armed")
        return []

    async def confirm():
        watcher._alert(setup("NSE:B"), "triggered")
        watcher._alert(setup("NSE:C"), "invalidated")
        return []

    monkeypatch.setattr(watcher, "_arm_due", arm)
    monkeypatch.setattr(watcher, "_confirm", confirm)
    monkeypatch.setattr(watcher, "status", lambda: {})
    asyncio.run(watcher.check())
    assert sent == ["NSE:B propulsion long triggered; entry 100, SL 95, TP 110"]
