"""Price alerts: wick/zone/bar-close conditions, trigger modes, windows, history, polling, push."""

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
    monkeypatch.setattr(pa, "HISTORY_PATH", tmp_path / "price_alert_history.json")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC"):
        monkeypatch.delenv(name, raising=False)


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

    def fetch(symbol, interval, n_bars):
        calls.append(symbol)
        label = (now - timedelta(minutes=10)).astimezone(IST).strftime("%Y-%m-%dT%H:%M")
        return [{"date": label, "open": 99, "high": 101, "low": 99, "close": 100.5, "volume": 0}]

    watcher = pa.PriceAlertWatcher(fetch=fetch, is_open=lambda s: s.startswith("NSE:"))
    status = asyncio.run(watcher.check())
    assert calls == ["NSE:INFY"]
    assert len(status["events"]) == 2
    assert len(pa.load_history()) == 2  # persisted, survives an API restart
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


# ---------------------------------------------------------------- v2 features


def test_trigger_is_stamped_at_the_bar_not_the_poll():
    alert = make(condition="crosses_above")
    assert pa.evaluate(alert, [bar("10:05", 100.4, 99, 99.2), bar("10:10", 99.6, 99, 99.1)], ist("10:40"))
    assert alert["last_triggered_at"] == pa._iso(ist("10:10"))  # end of the 10:05 bar
    assert alert["last_trigger_price"] == 99.2


def test_zone_enter_and_exit():
    enter = make(level=100, level2=102, reference=98, condition="enters_zone")
    assert enter["last_side"] == "below"
    assert pa.evaluate(enter, [bar("10:00", 99.5, 98, 99)], ist("10:06")) is False
    assert pa.evaluate(enter, [bar("10:05", 100.1, 99, 99.5)], ist("10:11")) is True  # wick into the zone

    leave = make(level=102, level2=100, reference=101, condition="exits_zone", trigger="every_check")
    assert leave["last_side"] == "inside"
    assert pa.evaluate(leave, [bar("10:00", 101.8, 100.2, 101)], ist("10:06")) is False
    assert pa.evaluate(leave, [bar("10:05", 102.4, 100.5, 101)], ist("10:11")) is True


def test_zone_needs_both_edges_and_line_drops_level2():
    with pytest.raises(ValueError):
        make(condition="enters_zone")
    line = make(level2=105)
    assert line["level2"] is None and line["timeframe"] == "5m"


def test_bar_close_uses_completed_timeframe_bars_only():
    alert = make(condition="closes_above", timeframe="15m")
    assert alert["timeframe"] == "15m"
    wick_only = {"15m": [bar("10:00", 101, 99, 99.8)]}  # wick above, close below
    assert pa.evaluate(alert, wick_only, ist("10:16")) is False
    forming = {"15m": [bar("10:00", 101, 99, 99.8), bar("10:15", 101, 99, 100.6)]}
    assert pa.evaluate(alert, forming, ist("10:25")) is False  # 10:15 bar closes at 10:30
    assert pa.evaluate(alert, forming, ist("10:31")) is True
    assert alert["last_triggered_at"] == pa._iso(ist("10:30"))


def test_daily_close_follows_market_cutoff():
    alert = make(condition="closes_above", timeframe="1D", window="nse_hours", created="09:00")
    day = [bar("09:15", 99.5, 99, 99.2), bar("15:25", 100.8, 99.9, 100.5)]
    assert pa.evaluate(alert, day, ist("16:00")) is False  # NSE day final only at 17:00 IST
    assert pa.evaluate(alert, day, ist("17:01")) is True   # window ignored for daily closes
    assert alert["last_triggered_at"] == pa._iso(ist("17:00"))


def test_window_blocks_firing_but_tracks_side():
    alert = pa.create({"symbol": "OANDA:XAUUSD", "level": 100, "reference_price": 99, "window": "silver_bullet",
                       "trigger": "every_check"}, now=datetime.fromisoformat("2026-09-30T09:00:00-04:00"))
    ny_bar = lambda hhmm, high, close: {  # noqa: E731
        "date": datetime.fromisoformat(f"2026-09-30T{hhmm}:00-04:00").astimezone(IST).strftime("%Y-%m-%dT%H:%M"),
        "open": 99, "high": high, "low": 98, "close": close, "volume": 0}
    assert pa.evaluate(alert, [ny_bar("09:30", 100.5, 99)], datetime.fromisoformat("2026-09-30T09:40:00-04:00")) is False
    assert alert["last_side"] == "below"
    assert pa.evaluate(alert, [ny_bar("10:10", 100.5, 99)], datetime.fromisoformat("2026-09-30T10:20:00-04:00")) is True


def test_snooze_mutes_and_session_end_expiry():
    alert = make(trigger="every_check")
    pa.update(alert["id"], {"snooze_minutes": 30})
    stored = pa.load()[0]
    stored["snoozed_until"] = pa._iso(ist("10:30"))
    assert pa.evaluate(stored, [bar("10:00", 100.5, 99, 99)], ist("10:06")) is False
    assert pa.evaluate(stored, [bar("10:30", 100.5, 99, 99)], ist("10:36")) is True

    ends = pa.create({"symbol": "INFY", "level": 100, "expires_at": "session_end"}, now=ist("11:00"))
    assert ends["expires_at"] == pa._iso(ist("17:00"))


def test_note_edit_does_not_reseed_but_shape_change_does():
    alert = make()
    checked = alert["last_checked_at"]
    assert pa.update(alert["id"], {"note": "hi"})["last_checked_at"] == checked
    zone = pa.update(alert["id"], {"condition": "enters_zone", "level2": 101})
    assert zone["last_side"] == "below" and zone["level2"] == 101


def test_bulk_and_batch_create():
    a, b = make(), make(level=90)
    assert pa.bulk([a["id"], b["id"]], "pause") == 2
    assert {x["status"] for x in pa.load()} == {"paused"}
    assert pa.bulk([a["id"]], "resume") == 1
    assert pa.bulk([b["id"]], "delete") == 1 and len(pa.load()) == 1
    with pytest.raises(ValueError):  # all or nothing
        pa.create_many([{"symbol": "INFY", "level": 1}, {"symbol": "INFY", "level": -1}])
    assert len(pa.load()) == 1


def test_outcomes_fill_from_later_bars():
    event = {"ts": pa._iso(ist("10:10")), "price": 100.0, "eod_at": pa._iso(ist("17:00")), "outcomes": {}}
    bars = [bar(f"10:{m:02d}", 101, 99, 100 + m / 100) for m in range(5, 60, 5)] + [bar("11:10", 102, 101, 102)]
    assert pa.resolve_outcomes(event, bars, ist("11:20"))
    assert event["outcomes"]["15m"]["price"] == 100.2   # last close at/before 10:25
    assert event["outcomes"]["1h"]["price"] == 100.55   # 10:55 bar (closes 11:00); 11:10 bar ends after 11:10
    assert event["outcomes"]["1h"]["pct"] == 0.55
    assert "eod" not in event["outcomes"]


def test_adaptive_polling_near_level():
    watcher = pa.PriceAlertWatcher(fetch=lambda *a: [], is_open=lambda s: True)
    watcher.interval_minutes, watcher.near_pct = 15, 0.5
    now = datetime.now(timezone.utc)
    watcher._polled["NSE:INFY"] = now - timedelta(minutes=5)
    far = {"condition": "crosses", "level": 100, "last_price": 90}
    near = {"condition": "crosses", "level": 100, "last_price": 99.7}
    assert not watcher._due("NSE:INFY", [far], now, force=False)
    assert watcher._due("NSE:INFY", [far, near], now, force=False)
    assert watcher._due("NSE:INFY", [far], now, force=True)
    watcher.near_pct = 0
    assert not watcher._due("NSE:INFY", [near], now, force=False)


def test_push_sends_fired_events_when_enabled(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    sent: list[str] = []
    now = datetime.now(timezone.utc)
    pa.create({"symbol": "INFY", "level": 100, "reference_price": 99, "note": "pdh"}, now=now - timedelta(minutes=30))
    label = (now - timedelta(minutes=10)).astimezone(IST).strftime("%Y-%m-%dT%H:%M")
    fetch = lambda symbol, interval, n: [{"date": label, "open": 99, "high": 101, "low": 99, "close": 100.5, "volume": 0}]  # noqa: E731
    watcher = pa.PriceAlertWatcher(fetch=fetch, is_open=lambda s: True, push=lambda text: sent.append(text) or [])
    watcher.pusher.enabled = True
    status = asyncio.run(watcher.check())
    assert status["push_channels"] == ["ntfy"]
    assert sent and sent[0].startswith("NSE:INFY crossed 100") and "pdh" in sent[0]


def test_full_edit_from_form_payload():
    alert = make(note="old")
    payload = {"level": 101, "level2": None, "condition": "closes_above", "timeframe": "1h", "trigger": "every_check",
               "cooldown_min": 15, "window": "nse_hours", "note": "edited"}
    edited = pa.update(alert["id"], payload)
    assert (edited["condition"], edited["timeframe"], edited["window"], edited["cooldown_min"], edited["note"]) == (
        "closes_above", "1h", "nse_hours", 15, "edited")
    assert edited["last_side"] == "below" and edited["expires_at"] is None  # expires_at omitted = kept
    back = pa.update(alert["id"], {**payload, "condition": "crosses", "timeframe": "5m"})
    assert back["timeframe"] == "5m" and back["level2"] is None
