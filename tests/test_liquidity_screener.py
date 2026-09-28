"""Tests for the IPO liquidity screener and the official-listing-date review rule."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from market_data import equity_master
from market_data import ipo as ipo_mod
from market_data import liquidity_screener as ls


def _bar(close: float, volume: float, high: float | None = None, low: float | None = None) -> dict:
    return {
        "close": close,
        "volume": volume,
        "high": close * 1.02 if high is None else high,
        "low": close * 0.98 if low is None else low,
    }


@pytest.fixture()
def bars(monkeypatch):
    """Serve `holder["rows"]` from get_ohlc and stub fundamentals."""
    holder: dict = {"rows": []}
    monkeypatch.setattr(ls, "get_ohlc", lambda *a, **k: SimpleNamespace(rows=holder["rows"]))
    monkeypatch.setattr(ls, "fetch_fundamentals", lambda symbol: {"market_cap_cr": None, "free_float_pct": None})
    return holder


def test_wide_daily_range_does_not_make_liquid_stock_illiquid(bars):
    # Rs.5 cr/day with a 10% daily range: volatile, not illiquid.
    bars["rows"] = [_bar(100.0, 500_000, high=105.0, low=95.0) for _ in range(40)]
    result = ls.screen_symbol("NSE:VOLATILE")
    assert result["liquidity_tier"] == "LIQUID"
    assert result["decision"] == "KEEP"
    assert ls.compute_liquidity_metrics("NSE:VOLATILE")["avg_daily_range_pct"] == 10.0


def test_median_value_ignores_single_spike(bars):
    # 39 days at Rs.0.5 cr plus one Rs.400 cr day: mean ~Rs.10.5 cr, median Rs.0.5 cr.
    bars["rows"] = [_bar(100.0, 50_000) for _ in range(39)] + [_bar(100.0, 40_000_000)]
    metrics = ls.compute_liquidity_metrics("NSE:SPIKE")
    assert metrics["avg_daily_value_cr"] > 10
    assert metrics["median_daily_value_cr"] == pytest.approx(0.5)
    assert ls.screen_symbol("NSE:SPIKE")["liquidity_tier"] == "BORDERLINE"


def test_circuit_locked_days_count_as_illiquid(bars):
    # Rs.5 cr/day, but 10 days locked at the band (high == low).
    rows = [_bar(100.0, 500_000) for _ in range(30)]
    rows += [_bar(100.0, 500_000, high=100.0, low=100.0) for _ in range(10)]
    bars["rows"] = rows
    metrics = ls.compute_liquidity_metrics("NSE:LOCKED")
    assert metrics["circuit_locked_days"] == 10
    assert metrics["zero_trade_days"] == 0
    result = ls.screen_symbol("NSE:LOCKED")
    assert result["liquidity_tier"] == "BORDERLINE"
    assert "circuit-locked days 10" in result["reason"]


def test_zero_volume_bar_is_not_counted_as_locked(bars):
    rows = [_bar(100.0, 500_000) for _ in range(39)] + [_bar(100.0, 0, high=100.0, low=100.0)]
    bars["rows"] = rows
    metrics = ls.compute_liquidity_metrics("NSE:QUIET")
    assert metrics["zero_trade_days"] == 1
    assert metrics["circuit_locked_days"] == 0


def test_parse_listing_dates():
    content = (
        "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE\n"
        "CAPILLARY,Capillary Technologies,EQ,21-NOV-2025,2\n"
        "BADDATE,Bad,EQ,,2\n"
    )
    parsed = equity_master._parse_listing_dates(content)
    assert str(parsed["CAPILLARY"]) == "2025-11-21"
    assert "BADDATE" not in parsed


REF = date(2026, 9, 27)


def _perf(symbol: str, listing_date: str) -> dict:
    age_days, age_label = ipo_mod.ipo_age(listing_date, REF)
    return {
        "symbol": symbol, "listing_date": listing_date, "latest_date": "2026-09-25",
        "age_days": age_days, "age_label": age_label,
        "liquidity": "LIQUID", "avg_value_cr_60d": 5.0, "zero_days_20d": 0,
        "signal": "LEADER", "strength_score": 1.0,
    }


@pytest.fixture()
def review(monkeypatch):
    monkeypatch.setattr(ipo_mod.equity_master, "load_equity_master", lambda **k: {})
    monkeypatch.setattr(ipo_mod.etf_list, "load_etf_symbols", lambda **k: set())
    monkeypatch.setattr(ipo_mod, "ipo_ineligibility_reason", lambda *a, **k: None)

    def run(perf: list[dict], official: dict) -> dict:
        monkeypatch.setattr(ipo_mod, "ipo_performance", lambda **k: perf)
        monkeypatch.setattr(ipo_mod.equity_master, "load_listing_dates", lambda **k: official)
        result = ipo_mod.ipo_review(reference_date=REF)
        return {r["symbol"]: r for r in result["keep"] + result["discard"]}

    return run


def test_review_flags_old_official_listing_date(review):
    rows = review(
        [_perf("NSE:OLDCO", "2023-09-01"), _perf("NSE:RENAMED", "2024-06-07"), _perf("NSE:REALIPO", "2025-11-21")],
        {"OLDCO": date(2010, 12, 10), "RENAMED": date(2001, 9, 27), "REALIPO": date(2025, 11, 21)},
    )
    assert rows["NSE:OLDCO"]["verdict"] == "DISCARD"
    assert rows["NSE:RENAMED"]["verdict"] == "DISCARD"
    assert "predates first bhavcopy appearance" in rows["NSE:RENAMED"]["reasons"][0]
    assert rows["NSE:REALIPO"]["verdict"] == "KEEP"


def test_review_official_date_overrides_window_start_heuristic(review):
    # A real IPO that happens to list on the window-start day is kept.
    rows = review(
        [_perf("NSE:FIRSTDAY", "2024-01-02"), _perf("NSE:LATER", "2024-03-10")],
        {"FIRSTDAY": date(2023, 12, 29)},
    )
    assert rows["NSE:FIRSTDAY"]["verdict"] == "KEEP"


def test_review_falls_back_to_window_start_without_official_date(review):
    rows = review([_perf("NSE:UNKNOWN", "2024-01-02"), _perf("NSE:LATER", "2024-03-10")], {})
    assert rows["NSE:UNKNOWN"]["verdict"] == "DISCARD"
    assert "discovery-window start" in rows["NSE:UNKNOWN"]["reasons"][0]
    assert rows["NSE:LATER"]["verdict"] == "KEEP"


@pytest.mark.parametrize("listed, days, label", [
    ("2026-09-20", 7, "7d"),
    ("2026-06-27", 92, "3m"),
    ("2025-09-27", 365, "1y"),
    ("2024-05-28", 852, "2y 3m"),
    ("2023-09-07", 1116, "3y"),
])
def test_ipo_age(listed, days, label):
    assert ipo_mod.ipo_age(listed, REF) == (days, label)


def test_review_flags_aged_out_ipo(review):
    rows = review(
        [_perf("NSE:OLDIPO", "2023-09-07"), _perf("NSE:EDGE", "2023-09-28"), _perf("NSE:YOUNG", "2025-01-15")],
        {"OLDIPO": date(2023, 9, 7), "EDGE": date(2023, 9, 28), "YOUNG": date(2025, 1, 15)},
    )
    assert rows["NSE:OLDIPO"]["verdict"] == "DISCARD"
    assert rows["NSE:OLDIPO"]["reasons"][0].startswith("aged out: listed 2023-09-07, 3y ago")
    assert rows["NSE:EDGE"]["verdict"] == "KEEP"  # exactly 1095 days old: not past the limit
    assert rows["NSE:YOUNG"]["verdict"] == "KEEP"
