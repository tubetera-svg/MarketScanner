"""Strategy behaviour aligned with the TTrades source material.

Covers the weekly-profile once-per-week / Friday-label rules, the Midweek
Reversal Thursday fallback, daily-bias invalidation, the stale-data guard
and run_strategies timeframe / extra-info plumbing.
Synthetic frames only (no network, no SQLite).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import all_strategy  # noqa: E402


def _frame(rows_by_date):
    """rows_by_date: list of (date, (O, H, L, C))."""
    return pd.DataFrame(
        [ohlc for _, ohlc in rows_by_date],
        columns=["Open", "High", "Low", "Close"],
        index=pd.to_datetime([d for d, _ in rows_by_date]),
    )


def _flat_history():
    # Two quiet weeks before the week of 2026-02-09 (no NSE holidays inside).
    days = [date(2026, 1, 27), date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
            date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4), date(2026, 2, 5), date(2026, 2, 6)]
    return [(d, (100.0, 101.0, 99.0, 100.2)) for d in days]


def _truncate(frame, day):
    return frame[frame.index <= pd.Timestamp(day)]


# ---------------------------------------------------------------------------
# Weekly profiles
# ---------------------------------------------------------------------------
MIDWEEK_THU_FALLBACK = _flat_history() + [
    (date(2026, 2, 9), (100.0, 101.0, 98.5, 99.5)),    # Mon
    (date(2026, 2, 10), (99.5, 100.0, 97.0, 97.5)),    # Tue: lower close, takes Monday's low
    (date(2026, 2, 11), (97.5, 98.5, 96.5, 98.0)),     # Wed: no clean closure above Tuesday's high
    (date(2026, 2, 12), (98.0, 102.0, 97.8, 101.5)),   # Thu: closes above Wednesday's high
    (date(2026, 2, 13), (101.5, 103.0, 101.0, 102.5)), # Fri: holds above the pivot close
]


def _midweek(as_of):
    frame = _truncate(_frame(MIDWEEK_THU_FALLBACK), as_of)
    return all_strategy.run_weekly_profile(
        ["TEST"], as_of, daily_map={"TEST": frame}, profile_key="midweek_reversal_sweep"
    ).results.iloc[0]


def test_midweek_reversal_waits_for_thursday_when_wednesday_unclear():
    wed = _midweek(date(2026, 2, 11))
    assert bool(wed["bullish_match"]) is False
    assert wed["note"] == "wednesday_unclear_await_thursday"

    thu = _midweek(date(2026, 2, 12))
    assert bool(thu["bullish_match"]) is True
    assert bool(thu["final_signal"]) is True
    assert thu["note"].startswith("pivot_thu_fallback")
    # Armed for intraday confirmation through the end of the week.
    assert thu["ltf_valid_until"] == "2026-02-13"
    assert thu["ltf_zone_low"] == 99.9 and thu["ltf_zone_high"] == 102.0


def test_weekly_profile_fires_once_per_week():
    fri = _midweek(date(2026, 2, 13))
    assert bool(fri["bullish_match"]) is True       # still listed for tracking
    assert bool(fri["final_signal"]) is False       # but no second trade
    assert "first signalled 2026-02-12" in fri["note"]


CLASSIC_FRIDAY_ONLY = _flat_history() + [
    (date(2026, 2, 9), (100.0, 101.0, 99.0, 100.0)),   # Mon
    (date(2026, 2, 10), (100.0, 100.5, 97.0, 97.5)),   # Tue: weekly low
    (date(2026, 2, 11), (97.5, 99.0, 97.6, 98.8)),     # Wed: candle two pending
    (date(2026, 2, 12), (98.8, 99.5, 98.2, 99.2)),     # Thu: weak body, still pending
    (date(2026, 2, 13), (99.2, 100.0, 99.0, 99.8)),    # Fri: slowing up-close
]


def test_first_match_at_friday_close_is_label_only():
    day = date(2026, 2, 13)
    row = all_strategy.run_weekly_profile(
        ["TEST"], day, daily_map={"TEST": _frame(CLASSIC_FRIDAY_ONLY)},
        profile_key="classic_expansion_sweep",
    ).results.iloc[0]
    assert bool(row["bullish_match"]) is True
    assert bool(row["final_signal"]) is False
    assert row["state"] == "expired"
    assert "friday_close_label_only" in row["note"]


def test_thursday_counter_targets_weekly_open():
    rows = _flat_history() + [
        (date(2026, 2, 9), (100.0, 101.0, 99.8, 100.8)),   # Mon open = weekly open 100
        (date(2026, 2, 10), (100.8, 102.0, 100.5, 101.8)),
        (date(2026, 2, 11), (101.8, 103.5, 101.5, 103.0)), # Wed: new high, +3% week
        (date(2026, 2, 12), (103.0, 104.0, 101.8, 102.0)), # Thu: takes Wed high, closes below Wed close
    ]
    row = all_strategy.run_weekly_profile(
        ["TEST"], date(2026, 2, 12), daily_map={"TEST": _frame(rows)},
        profile_key="thursday_counter_sweep",
    ).results.iloc[0]
    assert bool(row["bearish_match"]) is True and bool(row["final_signal"]) is True
    assert row["target"] == 100.0


# ---------------------------------------------------------------------------
# Daily bias invalidation
# ---------------------------------------------------------------------------
def _dbi(continuation, invalidation=(104.5, 106.0, 103.0, 104.0)):
    rows = [
        (date(2026, 2, 3), (100.0, 101.0, 99.0, 100.0)),
        (date(2026, 2, 4), (100.0, 102.0, 99.5, 101.0)),     # prior
        (date(2026, 2, 5), (101.0, 105.0, 100.5, 104.5)),    # reference: close > prior high -> Bullish, EQ 102.75
        (date(2026, 2, 6), invalidation),
        (date(2026, 2, 9), continuation),
    ]
    return all_strategy.run_daily_bias_invalidation(
        ["TEST"], date(2026, 2, 9), daily_map={"TEST": _frame(rows)}
    ).results.iloc[0]


def test_opposing_setup_requires_sweep_and_close_back_inside():
    row = _dbi((104.0, 104.2, 101.5, 102.0))
    assert bool(row["bearish_match"]) is True
    assert row["invalidation_type"] == "opposing_setup"
    assert row["entry"] == 102.0 and row["sl"] == 106.0 and row["target"] == 100.5
    assert row["signal_date"] == "2026-02-09"


def test_daily_bias_invalidation_signal_date_reaches_signal_frame():
    rows = [
        (date(2026, 2, 3), (100.0, 101.0, 99.0, 100.0)),
        (date(2026, 2, 4), (100.0, 102.0, 99.5, 101.0)),
        (date(2026, 2, 5), (101.0, 105.0, 100.5, 104.5)),
        (date(2026, 2, 6), (104.5, 106.0, 103.0, 104.0)),
        (date(2026, 2, 9), (104.0, 104.2, 101.5, 102.0)),
    ]
    execution = all_strategy.run_daily_bias_invalidation(
        ["TEST"], date(2026, 2, 9), daily_map={"TEST": _frame(rows)}
    )
    assert execution.bearish.iloc[0]["signal_date"] == "2026-02-09"


def test_sweep_that_closes_beyond_is_not_invalidation():
    row = _dbi((105.5, 105.6, 101.5, 102.0), invalidation=(104.5, 106.0, 103.0, 105.5))
    assert bool(row["final_signal"]) is False


def test_eq_reclaimed_continuation_is_not_a_signal():
    row = _dbi((104.0, 104.2, 102.9, 103.0))   # lower close but back above EQ 102.75
    assert bool(row["final_signal"]) is False


def test_target_behind_entry_is_dropped():
    row = _dbi((104.0, 104.2, 99.0, 100.0))    # already below the reference low 100.5
    assert bool(row["final_signal"]) is True
    assert row["target"] is None and row["rr"] is None


def test_invalidation_bar_sweeping_both_sides_and_closing_inside_is_ignored():
    # Invalidation takes the reference high 105 and low 100.5, closes inside above EQ.
    row = _dbi((104.0, 104.2, 101.5, 102.0), invalidation=(104.5, 106.0, 100.0, 104.0))
    assert bool(row["final_signal"]) is False


# ---------------------------------------------------------------------------
# Two-sided sweeps
# ---------------------------------------------------------------------------
def _consolidation(thursday):
    rows = _flat_history() + [
        (date(2026, 2, 9), (100.0, 101.0, 99.0, 100.2)),
        (date(2026, 2, 10), (100.2, 100.8, 99.2, 100.0)),
        (date(2026, 2, 11), (100.0, 100.9, 99.1, 100.1)),
        (date(2026, 2, 12), thursday),
    ]
    return all_strategy.run_weekly_profile(
        ["TEST"], date(2026, 2, 12), daily_map={"TEST": _frame(rows)},
        profile_key="consolidation_reversal_sweep",
    ).results.iloc[0]


def test_consolidation_reversal_one_sided_fake_break_signals():
    row = _consolidation((100.1, 101.5, 99.5, 100.2))
    assert bool(row["bearish_match"]) is True


def test_consolidation_reversal_both_sides_swept_is_ignored():
    row = _consolidation((100.1, 101.5, 98.5, 100.2))
    assert bool(row["bullish_match"]) is False and bool(row["bearish_match"]) is False
    assert "thursday_swept_both_sides" in row["note"]


def test_ict_sweep_of_both_sides_closing_inside_is_ignored():
    import ict_scanner

    levels = dict(pdh=105.0, pdl=100.0, pwh=110.0, pwl=95.0)
    assert ict_scanner.detect_liquidity_sweep([(102.0, 106.0, 99.0, 102.0)], **levels) is None
    assert ict_scanner.detect_liquidity_sweep([(102.0, 104.0, 99.0, 102.0)], **levels) == "PDL"
    # Took PDL but closed beyond PDH: close-beyond keeps the sell-side read.
    assert ict_scanner.detect_liquidity_sweep([(102.0, 106.0, 99.0, 105.5)], **levels) == "PDL"


def test_daily_bias_invalidation_fetches_when_no_daily_map(monkeypatch):
    calls = []
    monkeypatch.setattr(
        all_strategy, "_fetch_daily_from_bhavcopy",
        lambda *args, **kwargs: calls.append(args) or pd.DataFrame(columns=["Open", "High", "Low", "Close"]),
    )
    all_strategy.run_daily_bias_invalidation(["TEST"], date(2026, 2, 9))
    assert calls


# ---------------------------------------------------------------------------
# Stale data + run_strategies plumbing
# ---------------------------------------------------------------------------
def test_stale_symbol_is_not_evaluated():
    rows = [(d, (100.0, 101.0, 99.0, 100.0)) for d in pd.bdate_range("2026-01-05", "2026-01-23").date]
    execution = all_strategy.run_ema5_sweep(["TEST"], date(2026, 2, 6), daily_map={"TEST": _frame(rows)})
    assert execution.results.iloc[0]["status"] == "stale"


def test_run_strategies_passes_timeframe_to_single_structure_strategy(monkeypatch):
    seen = {}

    def fake_runner(**kwargs):
        seen.update(kwargs)
        return all_strategy.StrategyExecution("points_of_interest", pd.DataFrame(), pd.DataFrame(), pd.DataFrame())

    monkeypatch.setattr(all_strategy, "run_points_of_interest", fake_runner)
    monkeypatch.setattr(all_strategy, "_build_daily_map_for_symbols", lambda **kwargs: {})
    all_strategy.run_strategies(["points_of_interest"], ["TEST"], date(2026, 2, 6), timeframe="1h")
    assert seen["timeframe"] == "1h"


def _patch_extra_info_run(monkeypatch):
    calls, lookbacks = [], []
    rows = [(d, (100.0, 101.0, 99.0, 100.0)) for d in pd.bdate_range("2026-01-05", "2026-02-06").date]

    def fake_runner(name):
        def run(**kwargs):
            frame = pd.DataFrame({"symbol": ["ABC", "ABC"], "direction": [1, -1]})
            return all_strategy.StrategyExecution(name, frame, frame.iloc[:1], frame.iloc[1:])
        return run

    monkeypatch.setattr(all_strategy, "run_ema5_sweep", fake_runner("ema5_sweep"))
    monkeypatch.setattr(all_strategy, "run_inside_bar_daily_sweep", fake_runner("inside_bar_pattern_daily_sweep"))
    monkeypatch.setattr(
        all_strategy, "_build_daily_map_for_symbols",
        lambda **kwargs: lookbacks.append(kwargs["max_lookback_days"]) or {"ABC": _frame(rows)},
    )
    monkeypatch.setattr(all_strategy, "_track_mode_for", lambda symbol: "live")
    monkeypatch.setattr(all_strategy, "_daily_frame_stale", lambda *args: False)
    real = all_strategy.compute_daily_context
    monkeypatch.setattr(all_strategy, "compute_daily_context", lambda daily, bias=True: calls.append(1) or real(daily, bias))
    return calls, lookbacks


def test_extra_info_computed_once_per_symbol(monkeypatch):
    calls, lookbacks = _patch_extra_info_run(monkeypatch)
    names = ["ema5_sweep", "inside_bar_pattern_daily_sweep"]
    executions = all_strategy.run_strategies(names, ["ABC", "abc"], date(2026, 2, 6), parallel=False)
    assert len(calls) == 1
    assert lookbacks == [160]  # inside-bar lookback already exceeds CONTEXT_LOOKBACK_DAYS
    for execution in executions:
        assert execution.results["ctx_bias_d"].notna().all()


def test_extra_info_off_skips_context(monkeypatch):
    calls, lookbacks = _patch_extra_info_run(monkeypatch)
    on = all_strategy.run_strategies(["ema5_sweep"], ["ABC"], date(2026, 2, 6), parallel=False)
    assert lookbacks == [all_strategy.CONTEXT_LOOKBACK_DAYS] and len(calls) == 1
    assert "ctx_bias_d" in on[0].results.columns
    calls.clear(), lookbacks.clear()
    no_bias = all_strategy.run_strategies(["ema5_sweep"], ["ABC"], date(2026, 2, 6), parallel=False, include_bias=False)
    assert lookbacks == [40] and len(calls) == 1
    assert no_bias[0].results["ctx_adr"].notna().all() and no_bias[0].results["ctx_bias_d"].isna().all()
    calls.clear(), lookbacks.clear()
    executions = all_strategy.run_strategies(["ema5_sweep"], ["ABC"], date(2026, 2, 6), include_context=False)
    assert calls == []
    assert lookbacks == [40]
    assert "ctx_bias_d" not in executions[0].results.columns
