"""Display-only daily context columns (src/daily_context.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import daily_context as dc  # noqa: E402


def _frame(rows, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def test_candle_types():
    prev = pd.Series({"Open": 100, "High": 110, "Low": 90, "Close": 105})
    bar = lambda o, h, l, c: pd.Series({"Open": o, "High": h, "Low": l, "Close": c})  # noqa: E731
    assert dc.candle_type(prev, bar(105, 108, 95, 100)) == "inside"
    assert dc.candle_type(prev, bar(105, 115, 85, 100)) == "outside"
    assert dc.candle_type(prev, bar(105, 115, 100, 112)) == "close_above_pdh"
    assert dc.candle_type(prev, bar(105, 115, 100, 104)) == "swept_pdh_rejected"
    assert dc.candle_type(prev, bar(95, 100, 85, 88)) == "close_below_pdl"
    assert dc.candle_type(prev, bar(95, 100, 85, 96)) == "swept_pdl_rejected"


def test_adr_uses_only_prior_bars():
    rows = [[100, 110, 100, 105]] * 20 + [[105, 145, 105, 140]]  # prior ranges 10, latest 40
    ctx = dc.compute_daily_context(_frame(rows))
    assert ctx["ctx_adr"] == 10.0
    assert ctx["ctx_adr_used_pct"] == 400.0


def test_continuation_streak_and_phase_change():
    rows = [[100, 110, 90, 100], [100, 115, 99, 114], [114, 120, 110, 119], [119, 126, 117, 125]]
    ctx = dc.compute_daily_context(_frame(rows))
    assert ctx["ctx_candle_type"] == "close_above_pdh"
    assert ctx["ctx_next_day_bias"] == "bullish"
    assert ctx["ctx_cont_streak"] == 3
    assert ctx["ctx_phase_change"] is True


def test_prev_wick_midpoint_status():
    # bar 2 sweeps bar 1's high and closes back inside: upper wick 104..112, mid 108
    rows = [[100, 110, 90, 100], [100, 112, 95, 104]]
    respected = dc.compute_daily_context(_frame(rows + [[104, 107, 96, 97]]))
    assert respected["ctx_prev_wick_mid"] == 108.0
    assert respected["ctx_prev_wick_status"] == "respected"
    broken = dc.compute_daily_context(_frame(rows + [[104, 111, 103, 109]]))
    assert broken["ctx_prev_wick_status"] == "closed_through"


def test_prev_eq_status():
    rows = [[100, 110, 90, 108], [108, 115, 101, 114]]  # prev eq 100, latest low 101
    ctx = dc.compute_daily_context(_frame(rows))
    assert ctx["ctx_prev_eq"] == 100.0
    assert ctx["ctx_prev_eq_status"] == "held_upper"


def test_unswept_draws_skip_swept_swings():
    rows = [
        [100, 101, 99, 100], [100, 120, 99, 110], [110, 111, 105, 106],  # swing high 120
        [106, 107, 80, 85], [85, 95, 84, 94],                            # swing low 80
        [94, 125, 93, 96], [96, 100, 90, 98],                            # 125 sweeps 120
    ]
    above, below = dc.unswept_draws(_frame(rows))
    assert above == 125.0
    assert below == 80.0


def test_prior_week_month_extremes_need_full_period():
    idx = pd.bdate_range("2026-01-01", "2026-03-10")
    daily = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0}, index=idx)
    daily.loc["2026-02-10", "High"] = 150.0
    daily.loc["2026-03-03", "Low"] = 50.0  # previous week (latest bar is Tue 2026-03-10)
    ctx = dc.compute_daily_context(daily)
    assert ctx["ctx_pmh"] == 150.0
    assert ctx["ctx_pwl"] == 50.0
    short = dc.compute_daily_context(daily.loc["2026-02-15":])
    assert short["ctx_pmh"] is None  # history starts inside February


def test_annotate_frame_side_specific_wick():
    rows = [[100, 110, 90, 100], [100, 120, 98, 118]]  # lower wick 2/22, upper wick 2/22
    rows[-1] = [100, 120, 80, 118]  # lower wick 20/40 = 50%, upper 2/40 = 5%
    ctx = {"ABC": dc.compute_daily_context(_frame(rows))}
    frame = pd.DataFrame({"symbol": ["ABC", "XYZ"], "direction": [1, -1]})
    bull = dc.annotate_frame(frame, ctx, 1)
    assert bull.loc[0, "ctx_opposing_wick_pct"] == 50.0
    assert bull.loc[0, "ctx_wick_class"] == "large"
    assert bull.loc[1, "ctx_adr"] is None  # no context for XYZ
    bear = dc.annotate_frame(frame, ctx, -1)
    assert bear.loc[0, "ctx_wick_class"] == "small"
    assert "_upper_wick_pct" not in bull.columns


def _weeks(start, *bars):
    rows = []
    for i, ohlc in enumerate(bars):
        rows += [ohlc] * 5
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"],
                        index=pd.bdate_range(start, periods=len(rows)))


def test_sweep_bias_rule():
    ref = pd.Series({"Open": 100, "High": 102, "Low": 98, "Close": 100})
    bar = lambda o, h, l, c: pd.Series({"Open": o, "High": h, "Low": l, "Close": c})  # noqa: E731
    assert dc.sweep_bias(bar(99, 101, 97, 99), ref) == "Bullish"    # swept low, closed back inside
    assert dc.sweep_bias(bar(100, 104, 99, 103), ref) == "Bullish"  # close above high
    assert dc.sweep_bias(bar(100, 103, 97, 100.5), ref) == "Neutral"  # outside bar closing inside
    assert dc.sweep_bias(bar(100, 101, 99, 100), ref) == "Neutral"  # inside bar


def test_mtf_unfinished_week_reads_w1_vs_w2():
    # W-2 range 98-102; W-1 sweeps 97 and closes 99 back inside -> Bullish.
    # The partial current week (Mon 2026-02-09) breaks lower but is ignored.
    daily = _weeks("2026-01-19", (100, 102, 98, 100), (100, 102, 98, 100), (99, 101, 97, 99))
    daily.loc[pd.Timestamp("2026-02-09")] = [95, 96, 90, 91]
    bias = dc.multi_timeframe_bias(daily)
    assert bias["weekly"] == "Bullish"
    assert bias["daily"] == "Bearish"


def test_mtf_friday_closes_current_week():
    daily = _weeks("2026-01-19", (100, 102, 98, 100), (100, 102, 98, 100), (95, 96, 90, 91))
    assert daily.index[-1].weekday() == 4
    assert dc.multi_timeframe_bias(daily)["weekly"] == "Bearish"


def test_mtf_reference_week_must_be_complete():
    # Only two weeks: the reference week is the first bucket (may be partial).
    daily = _weeks("2026-01-26", (100, 102, 98, 100), (95, 96, 90, 91))
    assert dc.multi_timeframe_bias(daily)["weekly"] == "Neutral"


def test_mtf_monthly_on_closed_months_and_in_context():
    def month(start, end, ohlc):
        idx = pd.bdate_range(start, end)
        return pd.DataFrame([ohlc] * len(idx), columns=["Open", "High", "Low", "Close"], index=idx)

    daily = pd.concat([
        month("2025-11-01", "2025-11-30", (100, 102, 98, 100)),
        month("2025-12-01", "2025-12-31", (100, 102, 98, 100)),  # M-2 range 98-102
        month("2026-01-01", "2026-01-30", (99, 101, 97, 99)),    # M-1 sweeps 97, closes inside
        month("2026-02-02", "2026-02-02", (95, 96, 90, 91)),     # partial month, ignored
    ])
    ctx = dc.compute_daily_context(daily)
    assert ctx["ctx_bias_m"] == "Bullish"
    assert ctx["ctx_bias_d"] == "Bearish"
    assert ctx["ctx_bias_direction"] == 0  # daily disagrees with monthly
