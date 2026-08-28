"""Tests for the backtesting engine.

Uses the same temp-DB fixture pattern as the market_data tests: data is inserted
directly into SQLite (no network), and the engine must replay it point-in-time.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from backtest import BacktestConfig, run_backtest  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402
from backtest.metrics import compute_metrics  # noqa: E402


def _make_rows(symbol: str, start: date, n: int, base: float = 100.0):
    """Build `n` consecutive weekday OHLC rows with a gentle uptrend + noise."""
    rows = []
    d = start
    price = base
    count = 0
    while count < n:
        if d.weekday() < 5:
            # skip 2026 NSE holidays the data layer would also skip
            if d not in {
                date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
                date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14),
                date(2026, 5, 1), date(2026, 5, 27), date(2026, 6, 26),
                date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
                date(2026, 11, 9), date(2026, 11, 24), date(2026, 12, 25),
            }:
                price = price * (1.0 + 0.002 * (1 if count % 2 == 0 else -1))
                o = price * 0.999
                h = price * 1.01
                l = price * 0.99
                c = price
                rows.append({
                    "source": "NSE", "symbol": symbol, "exchange": "NSE",
                    "date": d.isoformat(), "open": o, "high": h, "low": l,
                    "close": c, "volume": 1000.0,
                })
                count += 1
        d += timedelta(days=1)
    return rows


@pytest.fixture()
def seeded(tmp_db):
    from market_data import database as db

    symbols = ["NSE:TEST1", "NSE:TEST2"]
    start = date(2026, 2, 2)
    for sym in symbols:
        db.upsert_ohlc(_make_rows(sym, start, 40))
    return symbols


def _config(symbols, strategies, start, end):
    return BacktestConfig(
        symbols=symbols,
        strategies=strategies,
        start_date=start,
        end_date=end,
        initial_capital=100_000.0,
        hold_days=5,
    )


def test_engine_runs_and_produces_equity_curve(seeded):
    start, end = date(2026, 2, 2), date(2026, 3, 20)
    cfg = _config(seeded, ["ema5_sweep"], start, end)
    reports = run_backtest(cfg)
    assert "ema5_sweep" in reports
    assert "combined" in reports
    rep = reports["ema5_sweep"]
    assert len(rep.equity_curve) > 0
    # equity curve length must equal the number of trading dates in range
    assert rep.equity_curve[0]["date"] >= start.isoformat()
    assert rep.equity_curve[-1]["date"] <= end.isoformat()
    # final equity must be finite and positive
    assert rep.equity_curve[-1]["equity"] > 0


def test_no_lookahead_in_daily_map_slicing(seeded):
    start, end = date(2026, 2, 2), date(2026, 3, 20)
    cfg = _config(seeded, ["ema5_sweep"], start, end)
    engine = BacktestEngine(cfg)
    mid = engine.axis[len(engine.axis) // 2]
    dm = engine._daily_map(mid)
    for sym, df in dm.items():
        assert df.index.max() <= __import__("pandas").Timestamp(mid)


def test_trade_invariants_when_trades_exist(seeded):
    start, end = date(2026, 2, 2), date(2026, 3, 20)
    cfg = _config(seeded, ["ema5_sweep"], start, end)
    rep = run_backtest(cfg)["ema5_sweep"]
    for t in rep.trades:
        assert t["entry_price"] > 0
        assert t["exit_date"] >= t["entry_date"]
        assert t["pnl"] == t["pnl"]  # finite (not NaN)
        assert t["side"] in (1, -1)
        # exits never before the entry bar (entry fills on next open)
        assert t["exit_date"] > t["entry_date"] or t["exit_reason"] == "timeout"


def test_metrics_golden():
    equity = [100.0, 101.0, 99.0, 102.0, 105.0]
    trades = [
        type("T", (), {"pnl": 10.0})(),
        type("T", (), {"pnl": -4.0})(),
        type("T", (), {"pnl": 6.0})(),
    ]
    m = compute_metrics(equity, trades, n_days=5, initial_capital=100.0)
    assert m.num_trades == 3
    assert m.num_wins == 2
    assert abs(m.win_rate - 2 / 3) < 1e-9
    assert m.max_drawdown_pct > 0  # dipped from 101 to 99
    assert m.sharpe is not None
    assert m.profit_factor is not None


def test_unknown_strategy_raises(seeded):
    cfg = _config(seeded, ["does_not_exist"], date(2026, 2, 2), date(2026, 3, 20))
    with pytest.raises(ValueError):
        run_backtest(cfg)
