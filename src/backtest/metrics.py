"""Performance metrics for the backtesting engine.

Pure functions over an equity series and a list of closed trades. No I/O, no
strategy knowledge — everything is computed from the realized + marked-to-market
equity curve and the trade blotter produced by ``engine.py``.

Annualization assumes ~252 trading days per year (daily backtest resolution).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Metrics:
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown_pct: float = 0.0
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy: Optional[float] = None
    cagr: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    num_trades: int = 0
    num_wins: int = 0
    num_losses: int = 0
    pct_exposure: float = 0.0
    recovery_factor: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "cagr": self.cagr,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "num_trades": self.num_trades,
            "num_wins": self.num_wins,
            "num_losses": self.num_losses,
            "pct_exposure": self.pct_exposure,
            "recovery_factor": self.recovery_factor,
        }


def _daily_returns(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev not in (None, 0) and cur is not None:
            out.append(cur / prev - 1.0)
    return out


def _mean(xs: Sequence[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs: Sequence[float], ddof: int = 1) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return var ** 0.5


def compute_metrics(
    equity_curve: Sequence[float],
    trades: Sequence[object],
    exposure_fraction: float = 0.0,
    n_days: int = 0,
    initial_capital: float = 1.0,
) -> Metrics:
    """Build a :class:`Metrics` from an equity series and a trade blotter.

    ``trades`` is a sequence of objects exposing ``pnl`` (float). ``exposure_fraction``
    is the fraction of dates with an open position.
    """
    m = Metrics()

    rets = _daily_returns(equity_curve)
    mu = _mean(rets)
    sd = _std(rets)
    if mu is not None and sd not in (None, 0):
        m.sharpe = (mu / sd) * (TRADING_DAYS_PER_YEAR ** 0.5)

    downside = [r for r in rets if r < 0]
    dd = _std(downside, ddof=0)
    if mu is not None and dd not in (None, 0):
        m.sortino = (mu / dd) * (TRADING_DAYS_PER_YEAR ** 0.5)

    # Drawdown from the equity series itself.
    peak = None
    max_dd = 0.0
    for v in equity_curve:
        if v is None:
            continue
        if peak is None or v > peak:
            peak = v
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    m.max_drawdown_pct = max_dd

    pnls = [float(getattr(t, "pnl", 0.0) or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    m.num_trades = len(pnls)
    m.num_wins = len(wins)
    m.num_losses = len(losses)
    m.win_rate = (len(wins) / len(pnls)) if pnls else None
    m.avg_win = _mean(wins)
    m.avg_loss = _mean(losses)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        m.profit_factor = gross_win / gross_loss
    m.expectancy = _mean(pnls)

    if initial_capital > 0 and n_days > 0 and len(equity_curve) > 1:
        final = equity_curve[-1]
        if final > 0:
            m.cagr = (final / initial_capital) ** (TRADING_DAYS_PER_YEAR / n_days) - 1

    if max_dd > 0 and initial_capital > 0:
        net = (equity_curve[-1] - initial_capital) if equity_curve else 0.0
        m.recovery_factor = net / (max_dd * initial_capital)

    m.pct_exposure = exposure_fraction
    return m
