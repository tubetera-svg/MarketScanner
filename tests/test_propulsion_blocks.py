from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import all_strategy  # noqa: E402
from propulsion_blocks import (  # noqa: E402
    STATE_ANTICIPATED,
    STATE_CONFIRMED,
    STATE_INVALIDATED,
    evaluate_propulsion_blocks,
)


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["Open", "High", "Low", "Close"],
        index=pd.date_range("2026-01-01", periods=len(rows), freq="D"),
    )


ROWS = [
    (100, 101, 98, 99),
    (99, 100, 96, 97),
    (98, 103, 97, 102),
    (100, 101, 97, 99),
    (101, 108, 100, 107),
]


def test_bullish_propulsion_block_confirms_after_retrace():
    analysis = evaluate_propulsion_blocks(_frame(ROWS))
    assert analysis.active is not None
    assert analysis.active.direction == 1
    assert analysis.active.state == STATE_CONFIRMED
    assert analysis.active.propulsion_open == 100.0
    assert analysis.active.mean_threshold == 99.0


def test_propulsion_block_is_anticipated_before_displacement():
    analysis = evaluate_propulsion_blocks(_frame(ROWS[:4] + [(99, 100, 98, 99)]))
    assert analysis.active is None
    assert analysis.anticipated is not None
    assert analysis.anticipated.state == STATE_ANTICIPATED


def test_propulsion_block_invalidates_through_mean_threshold():
    analysis = evaluate_propulsion_blocks(_frame(ROWS + [(107, 108, 90, 95)]))
    assert analysis.active is None
    assert any(event.state == STATE_INVALIDATED for event in analysis.events)


def test_propulsion_runner_emits_only_on_confirmation_day():
    execution = all_strategy.run_propulsion_blocks(
        ["TEST"], date(2026, 1, 5), daily_map={"TEST": _frame(ROWS)}
    )
    row = execution.results.iloc[0]
    assert execution.name == "propulsion_blocks"
    assert bool(row["final_signal"]) is True
    assert bool(row["bullish_match"]) is True
    assert row["state"] == STATE_CONFIRMED
    assert row["triggered_level"] == row["propulsion_open"] == 100.0
    assert row["order_block_midpoint"] == 98.5
    assert execution.bullish.iloc[0]["order_block_midpoint"] == 98.5


def test_mean_threshold_body_mode_uses_candle_body():
    analysis = evaluate_propulsion_blocks(_frame(ROWS), mean_mode="body")
    assert analysis.active is not None
    assert analysis.active.mean_threshold == 99.5  # (open 100 + close 99) / 2


def test_close_through_mean_before_displacement_prevents_confirmation():
    rows = ROWS[:4] + [(99, 100, 97, 98), (101, 108, 100, 107)]  # 98 < mean 99, then displacement
    analysis = evaluate_propulsion_blocks(_frame(rows))
    event = next(e for e in analysis.events if e.order_block_start == 0 and e.direction == 1)
    assert event.state == STATE_INVALIDATED
    assert event.confirm_idx is None


def test_block_closed_through_before_retrace_is_discarded():
    rows = ROWS[:3] + [(102, 102, 94, 95), (100, 101, 97, 99), (101, 108, 100, 107)]
    analysis = evaluate_propulsion_blocks(_frame(rows))
    assert not any(e.order_block_start == 0 and e.direction == 1 for e in analysis.events)


def test_runner_confirms_on_frames_longer_than_lookback_and_enters_at_open():
    pad = [(50 + k * 0.3, 50 + k * 0.3 + 0.7, 50 + k * 0.3 - 0.5, 50 + k * 0.3 + 0.2) for k in range(100)]
    frame = pd.DataFrame(
        pad + ROWS, columns=["Open", "High", "Low", "Close"],
        index=pd.date_range("2025-06-01", periods=len(pad) + len(ROWS), freq="D"),
    )
    execution = all_strategy.run_propulsion_blocks(
        ["TEST"], frame.index[-1].date(), daily_map={"TEST": frame}
    )
    row = execution.results.iloc[0]
    assert bool(row["final_signal"]) is True
    assert row["entry"] == 100.0  # propulsion candle open, not the confirmation close


def test_propulsion_blocks_registered():
    registry = all_strategy.strategy_registry()
    assert "propulsion_blocks" in registry
    assert registry["propulsion_blocks"].runner is all_strategy.run_propulsion_blocks