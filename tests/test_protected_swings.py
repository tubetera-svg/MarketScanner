"""Tests for the self-contained Protected Swings detection module.

These exercise the discrete detection primitives directly on small synthetic
OHLC DataFrames (no network, no SQLite), plus the ``run_protected_swings``
runner contract and one end-to-end backtest-engine run seeded from SQLite.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import all_strategy  # noqa: E402
import protected_swings as ps  # noqa: E402

NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
    date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14),
    date(2026, 5, 1), date(2026, 5, 27), date(2026, 6, 26),
}


def _df(rows):
    """rows: list of (O, H, L, C); index = consecutive business days from 2026-01-05."""
    out = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])
    days = []
    d = date(2026, 1, 5)
    while len(days) < len(rows):
        if d.weekday() < 5 and d not in NSE_HOLIDAYS:
            days.append(d)
        d += timedelta(days=1)
    out.index = pd.to_datetime(days)
    return out


# ---------------------------------------------------------------------------
# Swing-point detection
# ---------------------------------------------------------------------------
def test_detect_swing_points_finds_fractal_low_and_high():
    # Rising up to a local high, then a down move into a clear V low.
    rows = [
        (100, 102, 99, 101),    # 0
        (101, 104, 100, 103),   # 1
        (103, 106, 102, 105),   # 2
        (105, 108, 104, 107),   # 3
        (107, 109, 92, 95),     # 4  <- swing high (109; idx3 108 is lower)
        (95, 97, 88, 90),       # 5  down
        (90, 92, 84, 86),       # 6  <- swing low (84)
        (86, 91, 85, 89),       # 7  up
        (89, 95, 88, 93),       # 8  up
    ]
    swings = ps.detect_swing_points(_df(rows))
    lows = [s for s in swings if not s.is_high]
    highs = [s for s in swings if s.is_high]
    assert any(abs(s.low - 84.0) < 1e-9 for s in lows)
    assert any(abs(s.high - 109.0) < 1e-9 for s in highs)


def test_detect_swing_points_needs_confirmed_window():
    # Only 3 bars -> with left=right=2 no swing can be confirmed.
    swings = ps.detect_swing_points(_df([(100, 102, 99, 101), (101, 104, 100, 103), (103, 106, 102, 105)]))
    assert swings == []


# ---------------------------------------------------------------------------
# FVG detection (3-candle, same definition as ict_scanner)
# ---------------------------------------------------------------------------
def test_find_fvgs_bullish_gap():
    rows = [
        (100, 105, 99, 101),    # 0 c1 high 105
        (95, 100, 96, 99),      # 1 c2 bullish (99>95)
        (101, 104, 110, 112),   # 2 c3 low 110 > c1 high 105 -> bullish FVG
    ]
    gaps = ps.find_fvgs(_df(rows))
    bullish = [g for g in gaps if g.fvg_type == "bullish"]
    assert len(bullish) == 1
    assert abs(bullish[0].gap_low - 105.0) < 1e-9
    assert abs(bullish[0].gap_high - 110.0) < 1e-9


def test_find_fvgs_bearish_gap():
    rows = [
        (110, 115, 110, 111),   # 0 c1 low 110
        (111, 114, 109, 109),   # 1 c2 bearish (109<111)
        (101, 106, 100, 101),   # 2 c3 high 106 < c1 low 110 -> bearish FVG
    ]
    gaps = ps.find_fvgs(_df(rows))
    bearish = [g for g in gaps if g.fvg_type == "bearish"]
    assert len(bearish) == 1
    assert abs(bearish[0].gap_low - 106.0) < 1e-9
    assert abs(bearish[0].gap_high - 110.0) < 1e-9


# ---------------------------------------------------------------------------
# Sweep / confirmation primitives
# ---------------------------------------------------------------------------
def test_detect_liquidity_sweep_bullish():
    # A clear local high (idx2=106) then a bar whose high pierces it.
    rows = [
        (100, 102, 99, 101),    # 0
        (101, 103, 100, 102),   # 1
        (102, 106, 101, 105),   # 2  swing high 106
        (105, 105.5, 103, 104), # 3
        (104, 105, 102, 104),   # 4
        (104, 110, 103, 108),   # 5  high 110 > 106 -> sweep
    ]
    frame = _df(rows)
    swings = ps.detect_swing_points(frame)
    sh = [s for s in swings if s.is_high and abs(s.high - 106.0) < 1e-9][0]
    assert ps.detect_liquidity_sweep(frame, sh.idx, is_high=True) == 5


def test_confirm_close_and_invalidate():
    # closes: 9,10,12,8 ; protected_level 11
    rows = [
        (0, 10, 5, 9),    # 0
        (9, 12, 8, 10),   # 1 close 10 < 11
        (10, 11, 7, 12),  # 2 close 12 > 11 -> confirm
        (12, 13, 6, 8),   # 3 close 8 < 11 -> invalidate
    ]
    frame = _df(rows)
    assert ps.confirm_close(frame, 11.0, after_idx=1, above=True) == 2
    assert ps.invalidate_close(frame, 11.0, after_idx=2, above=False) == 3
    # if every later close stays above the level -> no invalidation
    rows2 = [
        (0, 10, 5, 9), (9, 12, 8, 10), (10, 11, 7, 12), (12, 13, 12, 13),
    ]
    assert ps.invalidate_close(_df(rows2), 11.0, after_idx=2, above=False) is None


# ---------------------------------------------------------------------------
# evaluate_protected_swings: lifecycle state machine
# ---------------------------------------------------------------------------
# Up move to a swing high (108), gap-free down move (overlapping ranges, no FVG)
# into a swing low (84), sweep below it, then recover above the down-move series
# high (108) -> exactly one confirmed bullish protected low.
BULLISH_LOW = [
    (100, 102, 99, 101),   # 0 up
    (101, 104, 100, 103),  # 1 up
    (103, 106, 102, 105),  # 2 up
    (105, 108, 104, 107),  # 3 high 108
    (107, 108, 98, 100),   # 4 down-close (overlaps idx3 -> no gap)
    (100, 104, 92, 94),    # 5 down-close (overlaps -> no bearish FVG)
    (94, 98, 84, 86),      # 6 swing low 84, down-close (series high 108)
    (86, 94, 85, 92),      # 7 up
    (92, 100, 88, 98),     # 8 up
    (98, 104, 83, 85),     # 9 sweep low 83 < 84
    (85, 110, 84, 111),    # 10 confirm close 111 > 108
    (111, 112, 100, 110),  # 11 stays above 108
]

# Mirror down move: gap-free up-close series into a swing high (108), dump sweeps
# above, then a close below the series low (99) -> one confirmed bearish high.
BEARISH_HIGH = [
    (100, 102, 99, 101),   # 0 up
    (100, 103, 99, 102),   # 1 up
    (102, 105, 101, 104),  # 2 up
    (104, 108, 103, 107),  # 3 swing high 108 (up-close series low 99)
    (107, 107.5, 104, 106),# 4
    (106, 106.5, 102, 105),# 5  -> idx3 swing high
    (105, 120, 104, 118),  # 6 sweep high 120 > 108
    (118, 119, 95, 96),    # 7 confirm close 96 < 99
]


# Confirmed bullish FVG: gap at idx2 (c1 high 105 < c3 low 110, c2 bullish),
# idx3 is the red candle immediately before the idx4 pierce. The protected
# level is its body open (101), not the FVG formation wicks. idx5 confirms.
# idx4 high 112 avoids a spurious bearish FVG at i=4. The
# >=6-bar runner minimum is also satisfied.
FVG_BULLISH = [
    (100, 105, 99, 101),   # 0 c1  (o=100)
    (95, 100, 96, 99),     # 1 c2 bullish (o=95)
    (101, 104, 110, 112),  # 2 c3 -> bullish FVG (gap_low 105, gap_high 110)
    (115, 118, 111, 112),   # 3 red candle immediately before pierce
    (101, 112, 90, 101),    # 4 low 90 <= 110 -> trades into gap (entry)
    (95, 108, 91, 116),     # 5 close 116 > body 115, below wick 118
]


def _bullish_events(an):
    return [e for e in an.events if e.direction == 1 and e.mode == ps.MODE_SWEEP]


def test_confirmed_bullish_protected_low():
    an = ps.evaluate_protected_swings(_df(BULLISH_LOW))
    assert an.bias == 1
    assert an.active is not None
    assert an.active.direction == 1
    assert an.active.state == ps.STATE_CONFIRMED
    assert abs(an.active.swing_level - 84.0) < 1e-9
    assert abs(an.active.protected_level - 98.0) < 1e-9  # body: open of immediate red sweep series (idx9)
    assert abs(an.active.confirmation_price - 111.0) < 1e-9
    assert an.active.mode == ps.MODE_SWEEP
    assert an.active.tag == ps.TAG_SWEEP_BASED
    assert an.active.tag != ps.TAG_FVG_BASED


def test_tag_fvg_based_event():
    an = ps.evaluate_protected_swings(_df(FVG_BULLISH))
    fvg_ev = [e for e in an.events if e.mode == ps.MODE_FVG]
    assert fvg_ev, "expected at least one FVG-based event"
    assert all(e.tag == ps.TAG_FVG_BASED for e in fvg_ev)
    assert an.active is not None
    assert an.active.tag == ps.TAG_FVG_BASED
    assert an.active.mode == ps.MODE_FVG
    assert abs(an.active.protected_level - 115.0) < 1e-9
    assert abs(an.active.confirmation_price - 116.0) < 1e-9
    assert "fvg=111.00" in an.note


def test_invalidated_after_confirmation():
    # Close below the swept swing low (84) to invalidate the bullish event.
    # The same bar also sweeps a newer high, but its close above that swept
    # swing must prevent a later bearish confirmation.
    rows = BULLISH_LOW + [
        (111, 115, 80, 80),  # close 80 < 84 -> invalidates the bullish swing at idx=6
                    # and confirms the newer bearish swing
    ]
    an = ps.evaluate_protected_swings(_df(rows))
    bull = _bullish_events(an)
    assert any(e.state == ps.STATE_INVALIDATED for e in bull), "old swing should be invalidated"
    # The old swing is invalidated and no newer bearish swing is confirmed.
    assert an.active is None
    assert an.bias == 0


def _hammer_rows(hammer):
    """BULLISH_LOW up to the swing low, a red candle, then ``hammer`` sweeping 84."""
    return BULLISH_LOW[:8] + [
        (95, 96, 89, 90),     # 8 red: the down-close series into the sweep (open 95)
        hammer,               # 9 green sweep candle (low 83 < 84)
    ]


def test_green_sweep_candle_uses_prior_red_series():
    # A hammer that sweeps the low and closes up no longer disqualifies the
    # swing: the red series before it defines the CISD level (95).
    rows = _hammer_rows((90, 93, 83, 92)) + [(92, 100, 91, 97)]  # 10 closes 97 > 95
    an = ps.evaluate_protected_swings(_df(rows))
    assert an.active is not None and an.active.direction == 1
    assert an.active.protected_level == 95.0
    assert an.active.confirm_idx == 10
    assert an.active.sweep_extreme == 83.0


def test_green_sweep_candle_can_confirm_on_its_own_close():
    an = ps.evaluate_protected_swings(_df(_hammer_rows((90, 97, 83, 96))))
    assert an.active is not None and an.active.confirm_idx == 9


def test_runner_confirms_on_frames_longer_than_scan_window():
    # 100 gap-free rising bars (no swings, no FVGs) ahead of the setup push the
    # frame past the 80-bar scan window. The confirmation day must still fire.
    pad = [(50 + k * 0.3, 50 + k * 0.3 + 0.7, 50 + k * 0.3 - 0.5, 50 + k * 0.3 + 0.2) for k in range(100)]
    frame = _df(pad + BULLISH_LOW[:11])
    assert len(frame) > ps.PROTECTED_SWINGS_LOOKBACK_DAYS
    an = ps.evaluate_protected_swings(frame)
    assert an.active is not None and an.active.confirm_idx == len(frame) - 1
    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=frame.index[-1].date(), daily_map={"TEST": frame},
    )
    assert bool(ex.results.iloc[0]["final_signal"]) is True


def test_anticipated_when_only_swept():
    an = ps.evaluate_protected_swings(_df(BULLISH_LOW[:10]))  # sweep bar idx9, no confirm
    assert an.active is None
    assert an.bias == 0


def test_no_active_swing_returns_none():
    rows = [
        (100, 102, 99, 101), (101, 104, 100, 103), (103, 106, 102, 105),
        (105, 107, 104, 106), (106, 108, 105, 107),
    ]
    an = ps.evaluate_protected_swings(_df(rows))
    assert an.active is None
    assert an.bias == 0


def test_bearish_protected_high():
    an = ps.evaluate_protected_swings(_df(BEARISH_HIGH))
    assert an.bias == -1
    assert an.active is not None
    assert an.active.direction == -1
    assert an.active.state == ps.STATE_CONFIRMED
    assert abs(an.active.swing_level - 108.0) < 1e-9
    assert abs(an.active.protected_level - 105.0) < 1e-9  # body: open of immediate green sweep series (idx6)


def test_sweep_series_threshold_can_keep_older_bias_active():
    # The later high is already swept, but its immediate sweep series threshold
    # is lower than the closing price, so the earlier bullish bias remains.
    rows = BULLISH_LOW + [
        (110, 112, 95, 98),  # close 98 does not break the immediate sweep threshold
    ]
    an = ps.evaluate_protected_swings(_df(rows))
    assert an.active is not None
    assert an.active.direction == 1
    assert an.active.state == ps.STATE_CONFIRMED
    assert an.bias == 1


def test_sweep_confirmation_uses_immediate_sweep_series():
    rows = [
        (100, 102, 99, 101),   # 0
        (101, 105, 100, 104),  # 1
        (104, 110, 103, 109),  # 2 swing high; old green series low open=100
        (109, 108, 105, 106),  # 3
        (106, 107, 104, 105),  # 4
        (120, 125, 115, 124),  # 5 sweep high; immediate green open=120
        (124, 126, 100, 110),  # 6 confirms below 120, not below old level 100
    ]
    an = ps.evaluate_protected_swings(_df(rows))
    event = next(e for e in an.events if e.direction == -1 and e.mode == ps.MODE_SWEEP)
    assert event.protected_level == 120.0
    assert event.confirm_date == _df(rows).index[6]
    assert event.confirmation_price == 110.0


def test_sweep_is_rejected_after_close_beyond_swept_extreme():
    # A later close below the bearish body threshold must not confirm after a
    # prior close above the swept high. The bullish mirror is checked as well.
    bearish_rows = [
        (100, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 110, 103, 109),  # swing high
        (109, 108, 105, 106),
        (106, 107, 102, 105),
        (120, 125, 115, 124),  # sweep high
        (124, 127, 121, 122),  # close above swept high: invalidates setup
        (122, 123, 95, 96),    # would otherwise confirm below protection
    ]
    bearish = ps.evaluate_protected_swings(_df(bearish_rows))
    assert not any(e.state == ps.STATE_CONFIRMED for e in bearish.events if e.mode == ps.MODE_SWEEP)

    bullish_rows = [
        (100, 102, 99, 101),
        (101, 104, 100, 103),
        (103, 106, 102, 105),
        (105, 108, 104, 107),
        (107, 108, 98, 100),
        (100, 104, 92, 94),
        (94, 98, 84, 86),   # swing low
        (86, 94, 85, 92),
        (92, 100, 83, 85),  # sweep low
        (85, 82, 80, 82),   # close below swept low: invalidates setup
        (82, 110, 81, 111), # would otherwise confirm above protection
    ]
    bullish = ps.evaluate_protected_swings(_df(bullish_rows))
    assert not any(e.state == ps.STATE_CONFIRMED for e in bullish.events if e.mode == ps.MODE_SWEEP)


def test_sweep_requires_directional_sweep_candle():
    rows = [
        (100, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 110, 103, 109),  # swing high
        (109, 108, 105, 106),
        (106, 107, 102, 105),
        (120, 125, 115, 118),  # non-directional for bearish sweep
        (118, 119, 95, 96),    # must not confirm from the wrong series
    ]
    analysis = ps.evaluate_protected_swings(_df(rows))
    assert not any(event.state == ps.STATE_CONFIRMED for event in analysis.events)


def test_fvg_invalidated_before_confirmation_cannot_confirm_later():
    rows = FVG_BULLISH[:5] + [
        (101, 104, 95, 100),   # closes below the CISD level (115) before any confirmation
        (100, 120, 99, 121),   # later close above protection must not confirm
    ]
    analysis = ps.evaluate_protected_swings(_df(rows))
    assert not any(
        event.state == ps.STATE_CONFIRMED
        for event in analysis.events
        if event.mode == ps.MODE_FVG
    )


def test_fvg_invalidation_keys_off_protected_low_not_cisd_level():
    # After confirmation a close back under the CISD open (115) is not
    # invalidation; only a close through the protected low itself (90) is.
    held = ps.evaluate_protected_swings(_df(FVG_BULLISH + [(116, 117, 100, 100)]))
    fvg = [e for e in held.events if e.mode == ps.MODE_FVG]
    assert fvg and fvg[0].state == ps.STATE_CONFIRMED
    assert fvg[0].sweep_extreme == 90.0
    broken = ps.evaluate_protected_swings(_df(FVG_BULLISH + [(116, 117, 88, 89)]))
    fvg = [e for e in broken.events if e.mode == ps.MODE_FVG]
    assert fvg and fvg[0].state == ps.STATE_INVALIDATED


def test_signal_persists_while_no_newer_confirm():
    # A consolidation that neither closes below the protected level nor creates a
    # newer confirmed swing must leave the original bullish swing active.
    rows = BULLISH_LOW + [
        (110, 112, 106, 108), (110, 112, 106, 108), (110, 112, 106, 108),
    ]
    an = ps.evaluate_protected_swings(_df(rows))
    assert an.bias == 1
    assert an.active is not None
    assert an.active.state == ps.STATE_CONFIRMED


# ---------------------------------------------------------------------------
# Runner contract (all_strategy integration)
# ---------------------------------------------------------------------------
def test_run_protected_swings_output_contract():
    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=date(2026, 1, 19), verbose=False,
        daily_map={"TEST": _df(BULLISH_LOW[:11])},
    )
    assert ex.name == "protected_swings"
    row = ex.results.iloc[0]
    assert row["symbol"] == "TEST"
    assert row["profile"] == "Protected Swings"
    assert row["status"] == "complete"
    assert bool(row["final_signal"]) is True
    assert row["direction"] == 1
    assert row["state"] == ps.STATE_CONFIRMED
    assert bool(row["bullish_match"]) is True
    assert row["tag"] == ps.TAG_SWEEP_BASED
    # bullish/bearish frames carry the weekly-profile columns + tradingview_link
    assert "tradingview_link" in ex.bullish.columns
    assert "tag" in ex.bullish.columns
    assert ex.bullish.iloc[0]["tag"] == ps.TAG_SWEEP_BASED
    assert len(ex.bullish) == 1
    assert len(ex.bullish) == int(row["bullish_match"])


def test_run_protected_swings_no_signal_when_anticipated():
    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=date(2026, 1, 16), verbose=False,
        daily_map={"TEST": _df(BULLISH_LOW[:10])},
    )
    row = ex.results.iloc[0]
    assert row["final_signal"] is False or row["final_signal"] == False  # noqa: E712
    assert row["state"] == ps.STATE_ANTICIPATED
    assert row["tag"] == ps.TAG_SWEEP_BASED
    assert len(ex.bullish) == 0
    assert len(ex.bearish) == 0

    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=date(2026, 1, 21), verbose=False,
        daily_map={"TEST": _df(BULLISH_LOW + [(111, 112, 106, 110)])},
    )
    row = ex.results.iloc[0]
    assert bool(row["final_signal"]) is False
    assert bool(row["bullish_match"]) is False
    assert len(ex.bullish) == 0
    assert len(ex.bearish) == 0


def test_run_protected_swings_surfaces_fvg_tag():
    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=date(2026, 1, 12), verbose=False,
        daily_map={"TEST": _df(FVG_BULLISH)},
    )
    row = ex.results.iloc[0]
    assert row["symbol"] == "TEST"
    assert row["status"] == "complete"
    assert bool(row["final_signal"]) is True
    assert row["tag"] == ps.TAG_FVG_BASED
    assert row["state"] == ps.STATE_CONFIRMED
    assert abs(row["protected_level"] - 115.0) < 1e-9
    assert abs(row["confirmation_price"] - 116.0) < 1e-9
    assert len(ex.bullish) == 1
    assert "tag" in ex.bullish.columns
    assert ex.bullish.iloc[0]["tag"] == ps.TAG_FVG_BASED
    assert abs(ex.bullish.iloc[0]["swing_level"] - 111.0) < 1e-9


def test_run_protected_swings_emits_on_confirmation_day_only():
    # Swing confirmed on 2026-01-19 (idx10), but it must not be emitted again
    # on the following persistence session.
    ex = all_strategy.run_protected_swings(
        ["TEST"], as_of_date=date(2026, 1, 20), verbose=False,
        daily_map={"TEST": _df(BULLISH_LOW)},
    )
    row = ex.results.iloc[0]
    assert row["status"] == "complete"
    assert bool(row["final_signal"]) is False
    assert bool(row["bullish_match"]) is False
    assert bool(row["bearish_match"]) is False
    assert row["state"] == ps.STATE_CONFIRMED
    assert row["tag"] == ps.TAG_SWEEP_BASED
    assert len(ex.bullish) == 0


def test_points_of_interest_prioritizes_fvg_from_protected_swing():
    frame = _df([
        (100, 102, 99, 101),
        (101, 104, 100, 103),
        (103, 106, 102, 105),
        (105, 108, 104, 107),  # protected swing confirmation anchor
        (107, 109, 105, 108),
        (108, 110, 111, 112),  # bullish FVG starts here
        (112, 118, 110, 116),
        (116, 122, 114, 120),
    ])
    active = SimpleNamespace(confirm_idx=3, protected_level=100.0, direction=1)
    # First gap from the swing is 108-111; price reaches it at its top edge.
    assert all_strategy._select_point_of_interest(frame, active) == (111.0, "fvg")


def test_points_of_interest_skips_fvg_below_atr_threshold():
    # 20 bars of ~4-point ranges (ATR ~4), then a 0.2-wide bullish gap.
    rows = [(100 + i, 102 + i, 98 + i, 101 + i) for i in range(20)]
    rows += [(120, 121.0, 118, 120.5), (120.5, 123, 120, 122.8), (122.8, 126, 121.2, 125)]
    frame = _df(rows)
    active = SimpleNamespace(confirm_idx=19, protected_level=110.0, direction=1)
    poi = all_strategy._select_point_of_interest(frame, active)
    assert poi is None or poi[1] != "fvg"


def test_points_of_interest_falls_back_to_cisd():
    frame = _df([
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 103, 100.5, 101),  # down series starts: CISD = 102
        (101, 102, 99.5, 100),
        (100, 103, 99.8, 102.5),  # closes over 102 -> CISD confirmed
        (102.5, 104, 99.4, 103.5),
        (103.5, 105, 102, 104.5),
    ])
    active = SimpleNamespace(confirm_idx=0, protected_level=90.0, direction=1)
    assert all_strategy._select_point_of_interest(frame, active) == (102.0, "CISD")


def test_points_of_interest_skips_fvg_closed_through():
    frame = _df([
        (100, 102, 99, 101),
        (101, 104, 100, 103),
        (103, 106, 102, 105),
        (105, 108, 104, 107),  # protected swing confirmation anchor
        (107, 109, 105, 108),
        (108, 112, 111, 112),  # bullish FVG 108-111
        (112, 118, 110, 116),  # bullish FVG 109-110
        (116, 117, 106, 107),  # closes below both gaps -> spent
        (107, 115, 106, 114),
    ])
    active = SimpleNamespace(confirm_idx=3, protected_level=100.0, direction=1)
    poi = all_strategy._select_point_of_interest(frame, active)
    assert poi is None or poi[1] != "fvg"


def test_points_of_interest_is_not_a_backtest_signal():
    execution = all_strategy.run_points_of_interest(
        ["TEST"], as_of_date=date(2026, 1, 19), daily_map={"TEST": _df(BULLISH_LOW[:11])}
    )
    row = execution.results.iloc[0]
    assert row["type"] in {"fvg", "sweep", "CISD", ""}
    assert bool(row["final_signal"]) is False
    assert "entry" not in execution.results.columns
    assert "sl" not in execution.results.columns
    assert "rr" not in execution.results.columns


def _patch_active(monkeypatch, direction=1, protected_level=96.5):
    active = SimpleNamespace(direction=direction, protected_level=protected_level, mode="sweep")
    monkeypatch.setattr(
        all_strategy,
        "evaluate_protected_swings",
        lambda _frame: SimpleNamespace(active=active),
    )


def test_candle_3_closure_reports_equilibrium_without_trade_plan(monkeypatch):
    frame = _df([
        (98, 100, 96, 99),    # history
        (99, 103, 97, 101),   # history
        (100, 105, 95, 102),  # Candle 1
        (101, 104, 96, 99),   # Candle 2 reaches POI 96.5, holds candle 1's low (no sweep), closes down
        (99, 110, 98, 106),   # Candle 3 closes over Candle 2's body
    ])
    _patch_active(monkeypatch)
    closure = all_strategy._candle_closure(frame)
    assert closure == {"closure_type": "candle_3", "direction": 1, "level": 96.5, "poi_type": "protected"}
    execution = all_strategy.run_candle_3_closure(
        ["TEST"], as_of_date=date(2026, 1, 7), daily_map={"TEST": frame}
    )
    row = execution.results.iloc[0]
    assert row["equilibrium"] == 104.0
    assert row["closure_type"] == "candle_3"
    assert bool(row["bullish_match"]) is True
    assert bool(row["final_signal"]) is False
    assert "entry" not in execution.results.columns
    assert "sl" not in execution.results.columns
    assert "rr" not in execution.results.columns


def test_candle_3_rejected_when_candle_2_swept_candle_1_low(monkeypatch):
    # Candle 2 took candle 1's low (95 -> 90): that is candle-2 territory, not a
    # candle 3 closure, even though candle 3 closes over candle 2's body.
    frame = _df([
        (98, 100, 96, 99), (99, 103, 97, 101),
        (100, 105, 95, 102),
        (101, 104, 90, 99),
        (99, 110, 98, 106),
    ])
    _patch_active(monkeypatch)
    assert all_strategy._candle_closure(frame) is None


def test_candle_2_closure_sweeps_and_closes_back_inside(monkeypatch):
    frame = _df([
        (98, 100, 96, 99), (99, 103, 97, 101), (101, 104, 99, 102),
        (100, 105, 97, 98),   # Candle 1
        (98, 101, 94, 100),   # Candle 2: reaches POI, sweeps 97, closes back inside
    ])
    _patch_active(monkeypatch)
    closure = all_strategy._candle_closure(frame)
    assert closure is not None
    assert closure["closure_type"] == "candle_2"
    assert closure["direction"] == 1


def test_protected_swings_registered_in_registry_and_lookback():
    reg = all_strategy.strategy_registry()
    assert "protected_swings" in reg
    assert reg["protected_swings"].runner is all_strategy.run_protected_swings


def test_protected_swing_weekly_frame_resamples_historical_daily_data():
    daily = _df([
        (100 + i, 102 + i, 99 + i, 101 + i)
        for i in range(15)
    ])
    frame = all_strategy._protected_swing_frame("TEST", "weekly", daily)
    assert len(frame) == 3
    assert frame.iloc[0]["Open"] == 100
    assert frame.iloc[0]["Close"] == 105


def test_protected_swing_intraday_frame_uses_live_timeframe_fetch(monkeypatch):
    calls = []

    def fake_fetch_timeframe(**kwargs):
        calls.append(kwargs)
        return [{
            "date": "2026-09-18T10:00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
        }]

    from market_data.sources import tradingview_source

    monkeypatch.setattr(tradingview_source, "fetch_timeframe", fake_fetch_timeframe)
    frame = all_strategy._protected_swing_frame("NSE:TEST", "15m", pd.DataFrame())
    assert len(frame) == 1
    assert calls[0]["timeframe"] == "15m"
    # Live run fetches through today; a historical run must end at the tested
    # date so later intraday bars cannot leak in (look-ahead).
    assert calls[0]["end_date"] == date.today()
    all_strategy._protected_swing_frame("NSE:TEST", "15m", pd.DataFrame(), date(2026, 9, 18))
    assert calls[1]["end_date"] == date(2026, 9, 18)
    assert calls[1]["start_date"] == date(2026, 8, 19)
    # lookback dict in run_strategies must know the strategy (else default 60 used)
    import inspect

    src = inspect.getsource(all_strategy.run_strategies)
    assert "protected_swings" in src


# ---------------------------------------------------------------------------
# End-to-end backtest (SQLite seeded, no network)
# ---------------------------------------------------------------------------
def _clean_bull_db_rows(start: date, n: int = 40):
    """Seed ``n`` weekdays of a single bullish protected-low setup then a rally.

    Days are *every* weekday (holidays the NSE calendar skips are simply
    ignored by the backtest axis; the frame still contains the bars).
    """
    days = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    pattern = list(BULLISH_LOW)  # 12 bars: up -> sweep -> confirm
    assert len(pattern) >= 12
    # Rally well above any plausible target so the long exits via target.
    for i in range(len(pattern), n):
        base = 150.0 + (i - 12) * 3.0
        pattern.append((base, base + 6, base - 2, base + 4))
    rows = []
    for i, (o, h, l, c) in enumerate(pattern[:n]):
        rows.append({
            "source": "NSE", "exchange": "NSE", "symbol": "NSE:PSW1",
            "date": days[i].isoformat(), "open": o, "high": h, "low": l,
            "close": c, "volume": 1000.0,
        })
    return rows


def test_backtest_emits_long_trade_on_confirmed_protected_low(tmp_db):
    from market_data import database as db
    from backtest import BacktestConfig, run_backtest

    rows = _clean_bull_db_rows(date(2026, 1, 5))
    db.upsert_ohlc(rows)
    start = date.fromisoformat(rows[0]["date"])
    end = date.fromisoformat(rows[-1]["date"])
    cfg = BacktestConfig(
        symbols=["NSE:PSW1"], strategies=["protected_swings"],
        start_date=start, end_date=end, initial_capital=100_000.0,
    )
    rep = run_backtest(cfg)["protected_swings"]
    assert len(rep.trades) >= 1
    trade = rep.trades[0]
    assert trade["side"] == 1
    # entry fills next-day open after the confirm signal (engine convention)
    assert trade["exit_date"] >= trade["entry_date"]
    assert trade["pnl"] == trade["pnl"]  # not NaN
    assert rep.warnings == []


def test_backtest_no_lookahead_daily_map(tmp_db):
    from market_data import database as db
    from backtest.engine import BacktestEngine, BacktestConfig

    rows = _clean_bull_db_rows(date(2026, 1, 5))
    db.upsert_ohlc(rows)
    start = date.fromisoformat(rows[0]["date"])
    end = date.fromisoformat(rows[-1]["date"])
    cfg = BacktestConfig(
        symbols=["NSE:PSW1"], strategies=["protected_swings"],
        start_date=start, end_date=end,
    )
    engine = BacktestEngine(cfg)
    mid = engine.axis[len(engine.axis) // 2]
    dm = engine._daily_map(mid)
    for sym, frame in dm.items():
        if not frame.empty:
            assert frame.index.max() <= pd.Timestamp(mid)
