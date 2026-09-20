"""Tests for timezone-safe AM Silver Bullet evaluation."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.silver_bullet import evaluate_am_silver_bullet


NEW_YORK = ZoneInfo("America/New_York")


def test_naive_ist_bars_are_evaluated_in_new_york_window():
    rows = [
        {"date": "2026-09-20T18:30:00", "open": 100, "high": 105, "low": 99, "close": 102},
        {"date": "2026-09-20T18:45:00", "open": 102, "high": 104, "low": 100, "close": 103},
        {"date": "2026-09-20T19:30:00", "open": 103, "high": 104, "low": 98, "close": 101},
    ]

    signal = evaluate_am_silver_bullet(
        "COMEX:GC1!",
        rows,
        trading_date=date(2026, 9, 20),
        now=datetime(2026, 9, 20, 11, 0, tzinfo=NEW_YORK),
    )

    assert signal is not None
    assert signal.direction == "bullish"
    assert signal.signal_time == "2026-09-20T10:00:00-04:00"
