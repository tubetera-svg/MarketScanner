"""Price alerts: crossing on completed 5m bars, trigger modes, expiry, closed-market skip."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from api import main as api_main

pa = api_main.price_alerts
IST = pa.IST


def ist(hhmm: str, day: str = "2026-09-30") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=IST)


def bar(hhmm: str, high: float, low: float, close: float, day: str = "2026-09-30") -> dict:
    return {"date": f"{day}T{hhmm}", "open": close, "high": high, "low": low, "close": close, "volume": 0}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "STORE_PATH", tmp_path / "price_alerts.json")


def make(level=100.0, reference=99.0, created="10:00", **fields):
    return pa.create({"symbol": "infy", "level": level, "reference_price": reference, **fields}, now=ist(created))


def test_create_normalizes_and_seeds_side():
    alert = make()
    assert alert["symbol"] == "NSE:INFY"
    assert alert["last_side"] == "below"
    assert pa.load()[0]["id"] == alert["id"]
    with pytest.raises(ValueError):
        pa.create({"symbol": "INFY", "level": 100, "condition": "touches"})


def test_wick_cross_between_polls_fires_once():
    alert = make(condition="crosses_above")
    bars = [bar("10:00", 99.5, 98, 99), bar("10:05", 100.4, 99, 99.2), bar("10:10", 99.6, 99, 99.1)]
    assert pa.evaluate(alert, bars, ist("10:15")) is True
    assert alert["status"] == "triggered"
    assert alert["last_checked_at"] == pa._iso(ist("10:15"))
    assert pa.evaluate(alert, bars + [bar("10:15", 101, 99, 101)], ist("10:30")) is False


def test_forming_bar_and_pre_creation_bar_are_ignored():
    alert = make(created="10:02")
    # 10:00 opened before the alert; 10:10 is still forming at 10:12.
    bars = [bar("10:00", 101, 98, 99), bar("10:05", 99.5, 98, 99), bar("10:10", 102, 99, 101)]
    assert pa.evaluate(alert, bars, ist("10:12")) is False
    assert alert["last_checked_at"] == pa._iso(ist("10:10"))
    assert pa.evaluate(alert, bars, ist("10:16")) is True  # 10:10 bar now closed


def test_already_beyond_level_waits_for_fresh_cross():
    alert = make(reference=101.0, condition="crosses_above")
    assert pa.evaluate(alert, [bar("10:00", 102, 100.5, 101)], ist("10:06")) is False
    assert pa.evaluate(alert, [bar("10:05", 101, 99, 99.5)], ist("10:11")) is False
    assert pa.evaluate(alert, [bar("10:10", 100.2, 99, 100.1)], ist("10:16")) is True


def test_direction_filter():
    alert = make(reference=101.0, condition="crosses_above")
    assert pa.evaluate(alert, [bar("10:00", 101, 99, 99.5)], ist("10:06")) is False
    down = make(reference=101.0, condition="crosses_below")
    assert pa.evaluate(down, [bar("10:00", 101, 99, 99.5)], ist("10:06")) is True


def test_no_reference_seeds_without_firing():
    alert = make(reference=None)
    assert alert["last_side"] is None
    assert pa.evaluate(alert, [bar("10:00", 101, 99, 101)], ist("10:06")) is False
    assert alert["last_side"] == "above"


def test_every_check_cooldown_and_once_per_day():
    alert = make(trigger="every_check", cooldown_min=30)
    assert pa.evaluate(alert, [bar("10:00", 100.5, 99, 99)], ist("10:06")) is True
    assert pa.evaluate(alert, [bar("10:05", 100.5, 99, 99)], ist("10:11")) is False  # cooldown
    assert pa.evaluate(alert, [bar("10:40", 100.5, 99, 99)], ist("10:46")) is True
    assert alert["status"] == "active" and alert["trigger_count"] == 2

    daily = make(trigger="once_per_day")
    assert pa.evaluate(daily, [bar("10:00", 100.5, 99, 99)], ist("10:06")) is True
    assert pa.evaluate(daily, [bar("11:00", 100.5, 99, 99)], ist("11:06")) is False
    next_day = [bar("10:00", 100.5, 99, 99, day="2026-10-01")]
    assert pa.evaluate(daily, next_day, ist("10:06", day="2026-10-01")) is True


def test_expiry():
    alert = make(expires_at=pa._iso(ist("10:30")))
    assert pa.evaluate(alert, [], ist("10:31")) is False
    assert alert["status"] == "expired"


def test_update_level_reseeds_side():
    alert = make(reference=99.0)
    updated = pa.update(alert["id"], {"level": 95})
    assert updated["last_side"] == "above"
    with pytest.raises(ValueError):
        pa.update(alert["id"], {"status": "triggered"})
    assert pa.delete(alert["id"]) and pa.load() == []


def test_watcher_batches_per_symbol_and_skips_closed(monkeypatch):
    calls: list[str] = []
    now = datetime.now(timezone.utc)
    created = now - timedelta(minutes=30)
    for symbol in ("INFY", "INFY", "OANDA:XAUUSD"):
        pa.create({"symbol": symbol, "level": 100, "reference_price": 99}, now=created)

    def fetch(symbol, n_bars):
        calls.append(symbol)
        label = (now - timedelta(minutes=10)).astimezone(IST).strftime("%Y-%m-%dT%H:%M")
        return [{"date": label, "open": 99, "high": 101, "low": 99, "close": 100.5, "volume": 0}]

    watcher = pa.PriceAlertWatcher(fetch=fetch, is_open=lambda s: s.startswith("NSE:"))
    status = asyncio.run(watcher.check())
    assert calls == ["NSE:INFY"]
    assert len(status["events"]) == 2
    states = {a["symbol"]: a["status"] for a in status["alerts"]}
    assert states["OANDA:XAUUSD"] == "active"
    assert [a["status"] for a in status["alerts"] if a["symbol"] == "NSE:INFY"] == ["triggered", "triggered"]


def test_settings_interval_floor():
    settings = api_main.app_settings._clamp(api_main.app_settings._merge(
        api_main.app_settings.DEFAULTS, {"automation": {"price_alerts": {"interval_minutes": 1}}}))
    assert settings["automation"]["price_alerts"]["interval_minutes"] == 5
    assert api_main.app_settings.DEFAULTS["automation"]["price_alerts"]["interval_minutes"] == 15


@pytest.mark.parametrize(
    ("symbol", "before", "after"),
    [
        # Same-day markets: the bar rolls at the cut-off on its own date.
        ("NSE:INFY", "2026-09-30T16:59:00+05:30", "2026-09-30T17:01:00+05:30"),
        ("OANDA:XAUUSD", "2026-09-30T16:59:00-04:00", "2026-09-30T17:01:00-04:00"),
        # Next-day markets: the bar dated D closes at the cut-off on D+1.
        ("CRYPTO:BTCUSD", "2026-09-30T23:59:00+00:00", "2026-10-01T00:01:00+00:00"),
        ("NSEIX:NIFTY1!", "2026-10-01T02:59:00+05:30", "2026-10-01T03:01:00+05:30"),
    ],
)
def test_market_day_rolls_at_daily_bar_cutoff(symbol, before, after):
    first, second = datetime.fromisoformat(before), datetime.fromisoformat(after)
    assert pa.market_day(first, symbol) != pa.market_day(second, symbol)
    assert pa.market_day(first - timedelta(hours=2), symbol) == pa.market_day(first, symbol)


def test_market_day_follows_configured_cutoff(tmp_path):
    import json
    import os

    import ict_scanner

    path = os.environ["MARKET_SCANNER_SETTINGS_PATH"]
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"data_cutoffs": {"nse": "15:45"}}, file)
    ict_scanner._CUTOFF_CACHE["key"] = None
    assert pa.market_day(ist("15:50"), "NSE:INFY") == "2026-10-01"
    assert pa.market_day(ist("15:40"), "NSE:INFY") == "2026-09-30"


def test_once_per_day_resets_at_cutoff_and_session_count():
    alert = pa.create({"symbol": "OANDA:XAUUSD", "level": 100, "reference_price": 99, "trigger": "once_per_day"},
                      now=datetime.fromisoformat("2026-09-30T16:00:00-04:00"))
    ny_bar = lambda hhmm, day="2026-09-30": {  # noqa: E731  5m bar labelled in IST
        "date": datetime.fromisoformat(f"{day}T{hhmm}:00-04:00").astimezone(IST).strftime("%Y-%m-%dT%H:%M"),
        "open": 99, "high": 100.5, "low": 99, "close": 99, "volume": 0}
    assert pa.evaluate(alert, [ny_bar("16:30")], datetime.fromisoformat("2026-09-30T16:36:00-04:00")) is True
    assert pa.triggered_this_session(alert, datetime.fromisoformat("2026-09-30T16:50:00-04:00"))
    # 17:10 NY is already the next commodities day: allowed again.
    assert pa.evaluate(alert, [ny_bar("17:05")], datetime.fromisoformat("2026-09-30T17:11:00-04:00")) is True
    assert not pa.triggered_this_session(alert, datetime.fromisoformat("2026-10-01T17:30:00-04:00"))
