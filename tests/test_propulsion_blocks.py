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


def test_propulsion_blocks_registered():
    registry = all_strategy.strategy_registry()
    assert "propulsion_blocks" in registry
    assert registry["propulsion_blocks"].runner is all_strategy.run_propulsion_blocks