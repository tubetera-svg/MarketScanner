"""Backtesting engine package.

Public entry point: :func:`run_backtest` and :class:`BacktestConfig`.
"""

from __future__ import annotations

from .engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestReport,
    EquityPoint,
    Signal,
    Trade,
    run_backtest,
)
from .metrics import Metrics, compute_metrics

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestReport",
    "EquityPoint",
    "Signal",
    "Trade",
    "run_backtest",
    "Metrics",
    "compute_metrics",
]
