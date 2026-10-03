"""Intraday (LTF) CISD confirmation of armed daily setups."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src"), str(ROOT / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

import app_settings  # noqa: E402
import ltf_confirmation as ltf  # noqa: E402


def _setup(**overrides):
    base = dict(
        key="TEST|midweek_reversal_sweep|2026-02-12|1", symbol="TEST", strategy="midweek_reversal_sweep",
        direction=1, zone_low=99.9, zone_high=102.0, invalidation=97.8,
        signal_date="2026-02-12", valid_until="2026-02-13", market="NSE",
    )
    base.update(overrides)
    return ltf.LtfSetup(**base)


def _bars(rows):
    """rows: (naive IST 'YYYY-MM-DD HH:MM', O, H, L, C) as TradingView returns them."""
    return ltf.bars_from_rows(
        [{"date": ts, "open": o, "high": h, "low": l, "close": c} for ts, o, h, l, c in rows]
    )


BULLISH_DAY = [
    ("2026-02-12 14:15", 101.0, 102.0, 100.0, 101.5),   # signal day: ignored
    ("2026-02-13 09:15", 101.5, 101.8, 100.5, 100.8),   # red, trades into the zone (series open 101.5)
    ("2026-02-13 10:15", 100.8, 101.0, 99.9, 100.2),    # red, extends the series
    ("2026-02-13 11:15", 100.2, 101.9, 100.1, 101.7),   # green close 101.7 > 101.5 -> CISD
]


def _ist(text):
    return pd.Timestamp(text, tz="Asia/Kolkata").to_pydatetime()


def test_cisd_after_zone_touch_triggers_with_stop_beyond_intraday_low():
    result = ltf.evaluate_ltf(_setup(), _bars(BULLISH_DAY), _ist("2026-02-13 12:30"), "1h")
    assert result["state"] == ltf.STATE_TRIGGERED
    assert result["entry"] == 101.7
    assert result["sl"] == 99.9
    # Instants are emitted in UTC (time contract): 11:15 IST == 05:45 UTC.
    assert pd.Timestamp(result["at"]) == pd.Timestamp("2026-02-13 11:15", tz="Asia/Kolkata")


def test_utc_rows_give_same_result_as_legacy_ist_rows():
    """Parity: TradingView rows now carry UTC instants; signals must not change."""
    utc_rows = [
        (pd.Timestamp(ts, tz="Asia/Kolkata").tz_convert("UTC").isoformat(), o, h, l, c)
        for ts, o, h, l, c in BULLISH_DAY
    ]
    now = _ist("2026-02-13 12:30")
    assert ltf.evaluate_ltf(_setup(), _bars(utc_rows), now, "1h") == ltf.evaluate_ltf(_setup(), _bars(BULLISH_DAY), now, "1h")


def test_forming_bar_is_not_used():
    result = ltf.evaluate_ltf(_setup(), _bars(BULLISH_DAY), _ist("2026-02-13 12:00"), "1h")
    assert result["state"] == ltf.STATE_ARMED
    assert result["reached_at"] is not None


def test_close_through_invalidation_first_invalidates():
    rows = BULLISH_DAY[:2] + [("2026-02-13 10:15", 100.8, 101.0, 97.0, 97.5)] + BULLISH_DAY[3:]
    result = ltf.evaluate_ltf(_setup(), _bars(rows), _ist("2026-02-13 15:30"), "1h")
    assert result["state"] == ltf.STATE_INVALIDATED


def test_setup_expires_after_valid_window():
    rows = [("2026-02-13 09:15", 103.0, 104.0, 102.5, 103.5)]   # never trades into the zone
    result = ltf.evaluate_ltf(_setup(), _bars(rows), _ist("2026-02-16 10:00"), "1h")
    assert result["state"] == ltf.STATE_EXPIRED


def test_bearish_mirror():
    setup = _setup(direction=-1, zone_low=98.0, zone_high=100.1, invalidation=102.2,
                   key="TEST|x|2026-02-12|-1")
    rows = [
        ("2026-02-13 09:15", 98.5, 99.5, 98.2, 99.2),    # green, trades into the zone (series open 98.5)
        ("2026-02-13 10:15", 99.2, 100.1, 99.0, 99.8),   # green, extends the series
        ("2026-02-13 11:15", 99.8, 99.9, 98.0, 98.3),    # red close 98.3 < 98.5 -> CISD
    ]
    result = ltf.evaluate_ltf(setup, _bars(rows), _ist("2026-02-13 12:30"), "1h")
    assert result["state"] == ltf.STATE_TRIGGERED
    assert result["sl"] == 100.1


def test_forex_session_rolls_at_new_york_five_pm():
    ts = pd.Timestamp("2026-02-12 18:00", tz="America/New_York")
    assert str(ltf.session_date(ts, "FOREX")) == "2026-02-13"
    assert str(ltf.session_date(pd.Timestamp("2026-02-12 16:00", tz="America/New_York"), "FOREX")) == "2026-02-12"


def test_store_arms_once_and_records_transition(tmp_path):
    store = ltf.LtfSetupStore(str(tmp_path / "ltf.json"))
    now = datetime(2026, 2, 12, 18, 0)
    assert len(store.arm([_setup()], now)) == 1
    assert store.arm([_setup()], now) == []          # same key is not re-armed
    result = ltf.evaluate_ltf(_setup(), _bars(BULLISH_DAY), _ist("2026-02-13 12:30"), "1h")
    changed = store.apply(_setup().key, result, now)
    assert changed is not None and changed.state == ltf.STATE_TRIGGERED
    assert store.active() == []
    assert store.apply(_setup().key, result, now) is None   # terminal: no further changes


def test_bridge_turns_armed_daily_rows_into_setups(monkeypatch):
    from datetime import date

    import all_strategy
    import strategy_bridge
    from test_strategy_alignment import MIDWEEK_THU_FALLBACK, _frame, _truncate

    thursday = date(2026, 2, 12)
    frame = _truncate(_frame(MIDWEEK_THU_FALLBACK), thursday)
    module = strategy_bridge.load_module()
    monkeypatch.setattr(
        strategy_bridge, "list_strategies",
        lambda: ([{"name": "midweek_reversal_sweep", "enabled": True}], True),
    )
    monkeypatch.setattr(
        module, "run_strategies",
        lambda **kwargs: [all_strategy.run_weekly_profile(
            ["TEST"], thursday, daily_map={"TEST": frame}, profile_key="midweek_reversal_sweep")],
    )
    setups = strategy_bridge.collect_ltf_setups(["TEST"], thursday)
    assert len(setups) == 1
    setup = setups[0]
    assert setup.direction == 1 and setup.market == "NSE"
    assert setup.signal_date == "2026-02-12" and setup.valid_until == "2026-02-13"
    assert (setup.zone_low, setup.zone_high) == (99.9, 102.0)


def test_app_settings_strategy_choices_fall_back_to_default():
    merged = app_settings._clamp(app_settings._merge(
        app_settings.DEFAULTS, {"strategy": {"ltf_timeframe": "4h", "propulsion_mean_threshold": "body"}}
    ))
    assert merged["strategy"]["ltf_timeframe"] == "1h"
    assert merged["strategy"]["propulsion_mean_threshold"] == "body"
    assert merged["automation"]["ltf_confirmation"]["enabled"] is False
