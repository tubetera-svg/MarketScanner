"""Tests for timezone-safe AM Silver Bullet evaluation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.silver_bullet import evaluate_am_silver_bullet


NEW_YORK = ZoneInfo("America/New_York")
DAY = date(2026, 9, 21)
# 09:00 New York (EDT) == 18:30 IST; bars are stored as naive IST wall time.
IST_0900 = datetime(2026, 9, 21, 18, 30)
# 09:00-09:55 New York range: high 105, low 99.
RANGE = [(100, 105, 99, 102)] + [(102, 104, 100, 103)] * 11


def _rows(window_bars: list[tuple[float, float, float, float]]) -> list[dict]:
    bars = RANGE + window_bars
    return [
        {"date": (IST_0900 + timedelta(minutes=5 * i)).isoformat(), "open": o, "high": h, "low": low, "close": c}
        for i, (o, h, low, c) in enumerate(bars)
    ]


def _evaluate(window_bars, hour=11, minute=0):
    return evaluate_am_silver_bullet(
        "COMEX:GC1!",
        _rows(window_bars),
        trading_date=DAY,
        now=datetime(2026, 9, 21, hour, minute, tzinfo=NEW_YORK),
    )


def test_bullish_sweep_confirms_on_first_displacement_fvg():
    window = [
        (100, 100, 98, 99.5),    # 10:00 sweeps range low
        (99.5, 102, 99.2, 101.8),  # 10:05 displacement
        (101.8, 102.5, 100.5, 102),  # 10:10 low 100.5 > 10:00 high 100 -> FVG
    ]
    signal = _evaluate(window)

    assert signal is not None
    assert signal.direction == "bullish"
    assert signal.signal_time == "2026-09-21T10:10:00-04:00"
    assert (signal.entry, signal.stop_loss, signal.target) == (100.5, 98, 105)


def test_signal_waits_for_the_fvg_third_candle_to_close():
    window = [(100, 100, 98, 99.5), (99.5, 102, 99.2, 101.8), (101.8, 102.5, 100.5, 102)]

    assert _evaluate(window, hour=10, minute=14) is None
    assert _evaluate(window, hour=10, minute=15) is not None


def test_sweep_that_closes_outside_is_confirmed_by_later_bars():
    window = [
        (99, 98.5, 97, 97.5),     # 10:00 sweeps and closes below range low
        (97.5, 99.6, 97.8, 99.4),  # 10:05 displacement
        (99.4, 100.5, 99.8, 100.2),  # 10:10 FVG over 98.5, closes back inside
    ]
    signal = _evaluate(window)

    assert signal is not None
    assert signal.direction == "bullish"
    assert (signal.entry, signal.stop_loss) == (99.8, 97)


def test_bearish_sweep_mirrors_bullish():
    window = [
        (104, 106, 104, 104.5),  # 10:00 sweeps range high
        (104.5, 104.8, 102, 102.2),  # 10:05 displacement
        (102.2, 103.5, 101.5, 102),  # 10:10 high 103.5 < 10:00 low 104 -> FVG
    ]
    signal = _evaluate(window)

    assert signal is not None
    assert signal.direction == "bearish"
    assert (signal.entry, signal.stop_loss, signal.target) == (103.5, 106, 99)


def test_bar_sweeping_both_sides_voids_the_session():
    window = [
        (102, 106, 98, 102),  # 10:00 takes both range high and low
        (102, 102.5, 99.5, 100), (100, 101, 99.2, 100.5),
        (100.5, 101, 98.5, 99.5), (99.5, 102, 99.2, 101.8), (101.8, 102.5, 100.5, 102),
    ]

    assert _evaluate(window) is None


def test_target_side_swept_before_confirmation_voids_setup():
    window = [
        (100, 100, 98, 99.5),  # 10:00 sweeps low
        (99.5, 99.8, 99.1, 99.6),  # no FVG yet
        (99.6, 105.5, 99.5, 105.2),  # 10:10 runs through range high first
        (105.2, 106, 104.5, 105),
    ]

    assert _evaluate(window) is None


def test_utc_rows_give_identical_signal_to_legacy_ist_rows():
    """Parity: TradingView rows now carry UTC instants (time contract)."""
    window = [
        (100, 100, 98, 99.5),
        (99.5, 102, 99.2, 101.8),
        (101.8, 102.5, 100.5, 102),
    ]
    legacy = _rows(window)
    ist = ZoneInfo("Asia/Kolkata")
    utc_rows = [
        {**row, "date": datetime.fromisoformat(row["date"]).replace(tzinfo=ist).astimezone(ZoneInfo("UTC")).isoformat()}
        for row in legacy
    ]
    now = datetime(2026, 9, 21, 11, 0, tzinfo=NEW_YORK)
    expected = evaluate_am_silver_bullet("COMEX:GC1!", legacy, trading_date=DAY, now=now)
    assert expected is not None
    assert evaluate_am_silver_bullet("COMEX:GC1!", utc_rows, trading_date=DAY, now=now) == expected
