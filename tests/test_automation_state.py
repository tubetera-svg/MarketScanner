"""Restart-safe automation markers: a fresh instance skips work an earlier one already did."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from market_data.automation_state import AutomationState


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_STATE_STORE", raising=False)
    return tmp_path / "automation_state.json"


def test_sections_are_independent_and_bad_files_read_as_empty(state_path):
    AutomationState("a", state_path).save({"x": 1})
    AutomationState("b", state_path).save({"y": 2})
    assert AutomationState("a", state_path).load() == {"x": 1}
    assert AutomationState("b", state_path).load() == {"y": 2}
    assert AutomationState("missing", state_path).load() == {}
    state_path.write_text("{not json", encoding="utf-8")
    assert AutomationState("a", state_path).load() == {}


def test_auto_sync_skips_session_synced_before_restart(state_path, monkeypatch):
    from market_data import auto_sync

    final = date(2026, 10, 1)
    monkeypatch.setattr(auto_sync, "latest_final_session", lambda source, now=None, symbol=None: final)
    monkeypatch.setattr(auto_sync, "source_enabled", lambda source: True)
    monkeypatch.setattr(auto_sync, "load_symbol_aliases", lambda: {})
    calls = []
    monkeypatch.setattr(
        auto_sync, "sync_symbol_range",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(notes=[], rows=[{"date": final.isoformat()}]),
    )
    entries = lambda: [("OANDA:XAUUSD", "forex_24_5")]  # noqa: E731

    auto_sync.DataAutoSync(entries, AutomationState("data_auto_sync", state_path)).run_once()
    assert len(calls) == 1
    restarted = auto_sync.DataAutoSync(entries, AutomationState("data_auto_sync", state_path))
    assert restarted.run_once()["TRADINGVIEW"]["skipped"] is True
    assert len(calls) == 1


def test_ipo_scanner_waits_out_interval_after_restart(state_path, monkeypatch):
    from api.main import IPOScanner

    state = AutomationState("ipo_scanner", state_path)
    assert IPOScanner(state=state)._first_wait_seconds() == 0  # never ran: scan at once

    state.save({"last_ran_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).astimezone().isoformat()})
    scanner = IPOScanner(state=state)
    assert 39 * 60 < scanner._first_wait_seconds() <= 40 * 60
    scanner.interval_minutes = 15  # already overdue under a shorter interval
    assert scanner._first_wait_seconds() == 0

    scanner.interval_minutes = 60
    ran, sleeps = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(scanner, "run_once", lambda: ran.append(1))
    monkeypatch.setattr("api.main.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scanner._loop())
    assert ran == [] and 39 * 60 < sleeps[0] <= 40 * 60


def test_ltf_watcher_does_not_rearm_session_after_restart(state_path, monkeypatch):
    import ltf_confirmation
    from market_data import service as md_service

    import api.main as main

    session = date(2026, 10, 1)
    collected = []
    monkeypatch.setattr(main, "service", SimpleNamespace(watchlist=lambda: [{"symbol": "NSE:ABB"}]))
    monkeypatch.setattr(md_service, "latest_final_session", lambda source, now=None, symbol=None: session)
    monkeypatch.setattr(main.strategy_bridge, "collect_ltf_setups", lambda symbols, day: collected.append(day) or [])
    monkeypatch.setattr(ltf_confirmation, "LtfSetupStore", lambda: SimpleNamespace(arm=lambda setups: []))

    state = AutomationState("ltf_confirmation", state_path)
    assert asyncio.run(main.LtfConfirmationWatcher(state)._arm_due(False)) == []
    assert collected == [session]
    restarted = main.LtfConfirmationWatcher(state)
    assert restarted.last_arm == {"NSE": session.isoformat()}
    asyncio.run(restarted._arm_due(False))
    assert collected == [session]  # skipped
    asyncio.run(restarted._arm_due(True))  # manual check still forces a re-arm
    assert collected == [session, session]


def test_silver_bullet_keeps_pushed_ids_and_manual_stop_across_restart(state_path, monkeypatch):
    from api.main import SilverBulletLiveScanner

    monkeypatch.setenv("NTFY_TOPIC", "topic")
    inside = datetime(2026, 10, 1, 10, 30, tzinfo=ZoneInfo("America/New_York"))  # Thursday, in window

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return inside.astimezone(tz) if tz else inside.replace(tzinfo=None)

    monkeypatch.setattr("api.main.datetime", _Clock)
    signal = {"id": "COMEX:GC1!|bullish|10:25", "symbol": "COMEX:GC1!", "direction": "bullish", "signal_time": "10:25",
              "entry": 2650.5, "stop_loss": 2645.0, "target": 2661.0}
    sent: list[str] = []

    def scanner_with_capture():
        scanner = SilverBulletLiveScanner(AutomationState("silver_bullet", state_path))
        scanner.pusher = type(scanner.pusher)("Test", lambda text: sent.append(text) or [])
        scanner.pusher.enabled = True

        async def fake_scan(scan_date, now):
            return [signal]

        monkeypatch.setattr(scanner, "_scan", fake_scan)
        return scanner

    asyncio.run(scanner_with_capture()._check())
    asyncio.run(scanner_with_capture()._check())  # after a restart: same signal, no second push
    assert len(sent) == 1

    scanner_with_capture().stop(manual=True)
    restarted = scanner_with_capture()
    assert restarted.manual_stop_date == "2026-10-01"
    restarted.stop()  # an internal stop (start/test restart) clears it
    assert scanner_with_capture().manual_stop_date is None
