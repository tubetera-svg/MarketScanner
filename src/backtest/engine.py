"""Backtesting engine.

Replays the platform's existing daily strategies point-in-time over historical
daily OHLC stored in SQLite, turns each historical signal into a simulated trade
via an explicit execution model, and produces an equity curve + trade blotter.

Design contract (see BACKTEST_ENGINE_SPEC.md §5, §7):
- Every strategy runner takes ``(symbols, as_of_date, ..., daily_map)`` and only
  ever reads bars at or before ``as_of_date``. We honor that by building a
  per-date ``daily_map`` sliced to ``index <= as_of_date`` — no future bars reach
  the evaluator, so there is no look-ahead / repainting.
- We never call the live provider during replay: the full series is loaded once
  from ``market_data.database`` and sliced in memory.
- Entries fill on the NEXT trading day's open (never the signal bar's close), the
  key guard against using the evaluator's own close.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

SRC = Path(__file__).resolve().parent.parent  # .../src
ROOT = SRC.parent                              # project root
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_data import database as md_database  # noqa: E402
from market_data import service as md_service  # noqa: E402


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    symbols: List[str]
    strategies: List[str]
    start_date: date
    end_date: date
    initial_capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0       # sizing for strategies that emit an SL
    position_pct: float = 10.0             # notional fraction for no-SL strategies
    commission_per_trade_pct: float = 0.0
    hold_days: int = 5                     # exit model for core (no-SL) strategies
    benchmark_symbol: Optional[str] = None


@dataclass
class Signal:
    symbol: str
    strategy: str
    date: date
    direction: int                        # +1 long, -1 short, 0 none
    entry: Optional[float]
    sl: Optional[float]
    target: Optional[float]
    rr: Optional[float]
    note: str = ""


@dataclass
class Trade:
    symbol: str
    strategy: str
    side: int
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_reason: str
    qty: float
    pnl: float
    pnl_pct: float
    mfe: float
    mae: float


@dataclass
class EquityPoint:
    date: date
    equity: float
    drawdown_pct: float
    exposure: float


@dataclass
class BacktestReport:
    strategy: str
    config: Dict[str, Any]
    equity_curve: List[Dict[str, Any]]
    trades: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    benchmark_curve: Optional[List[Dict[str, Any]]]
    warnings: List[str]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "config": self.config,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "metrics": self.metrics,
            "benchmark_curve": self.benchmark_curve,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# all_strategy module loader (mirrors api/strategy_bridge.py)
# ---------------------------------------------------------------------------
def _load_all_strategy():
    spec = importlib.util.spec_from_file_location("all_strategy_bt", SRC / "all_strategy.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load src/all_strategy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Data ingestion / loading
# ---------------------------------------------------------------------------
def load_series(
    symbols: Sequence[str], start: date, end: date
) -> Dict[str, pd.DataFrame]:
    """Load full daily OHLC per symbol from SQLite once (no live fetch).

    Returns {UPPER_SYMBOL: DataFrame[Open,High,Low,Close] indexed by Date}. Only
    symbols with >=1 stored row are returned.
    """
    out: Dict[str, pd.DataFrame] = {}
    for raw in symbols:
        sym = str(raw).strip().upper()
        if not sym:
            continue
        source = _source_for_symbol(sym)
        rows = md_database.query_ohlc(source, sym, start, end)
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["Date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["Date"]).sort_values("Date").set_index("Date")
        out[sym] = pd.DataFrame({
            "Open": frame["open"].astype(float),
            "High": frame["high"].astype(float),
            "Low": frame["low"].astype(float),
            "Close": frame["close"].astype(float),
        })
    return out


def _source_for_symbol(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if ":" in value:
        prefix = value.split(":", 1)[0]
        return "NSE" if prefix == "NSE" else "TRADINGVIEW"
    return "NSE"


def build_axis(symbols: Sequence[str], start: date, end: date) -> List[date]:
    """Union of per-source expected trading dates in [start, end] (no future)."""
    sources = {_source_for_symbol(str(s).strip().upper()) for s in symbols if str(s).strip()}
    dates: set[date] = set()
    for src in sources:
        try:
            dates.update(md_service.expected_trading_dates(src, start, end))
        except Exception:
            continue
    return sorted(dates)


# ---------------------------------------------------------------------------
# Position (internal)
# ---------------------------------------------------------------------------
@dataclass
class _Position:
    symbol: str
    strategy: str
    side: int
    entry_date: date
    entry_price: float
    qty: float
    sl: Optional[float]
    target: Optional[float]
    exit_model: str            # "sl_target" | "hold"
    entry_axis_idx: int
    pending: bool = True       # filled on the next trading day's open
    mfe: float = 0.0
    mae: float = 0.0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class BacktestEngine:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self._module = _load_all_strategy()
        self.series = load_series(config.symbols, config.start_date, config.end_date)
        self.axis = build_axis(config.symbols, config.start_date, config.end_date)
        self.warnings: List[str] = []
        missing = [s for s in config.symbols if s.strip().upper() not in self.series]
        if missing:
            self.warnings.append(
                f"No stored OHLC for {len(missing)} symbol(s): {', '.join(missing[:10])}. "
                f"Run a data sync (ensure_backdate_data) before backtesting."
            )
        if not self.axis:
            self.warnings.append("No trading dates in range after applying source calendars.")

    # -- strategy resolution -------------------------------------------------
    def _runners(self, strategy_names: Sequence[str]):
        registry = self._module.strategy_registry()
        unknown = [n for n in strategy_names if n not in registry]
        if unknown:
            raise ValueError(f"Unknown strategy: {unknown[0]}")
        return [(n, registry[n].runner) for n in strategy_names]

    # -- daily_map slicing ----------------------------------------------------
    def _daily_map(self, as_of: date) -> Dict[str, pd.DataFrame]:
        cutoff = pd.Timestamp(as_of)
        out: Dict[str, pd.DataFrame] = {}
        for sym, df in self.series.items():
            sliced = df[df.index <= cutoff]
            out[sym] = sliced if not sliced.empty else df.iloc[0:0]
        return out

    # -- signal extraction ----------------------------------------------------
    def _signals_from(self, execution, as_of: date) -> List[Signal]:
        sigs: List[Signal] = []
        res = execution.results
        for _, row in res.iterrows():
            final = bool(row.get("final_signal", False))
            if not final:
                continue
            direction = int(row.get("direction", 0) or 0)
            # Core strategies may not carry a `direction` column.
            if direction == 0:
                bull = bool(row.get("bullish_match", False))
                bear = bool(row.get("bearish_match", False))
                if bull and not bear:
                    direction = 1
                elif bear and not bull:
                    direction = -1
                else:
                    continue  # ambiguous (both) -> skip
            entry = row.get("entry")
            sl = row.get("sl")
            target = row.get("target")
            rr = row.get("rr")
            note = str(row.get("note", "") or "")
            sigs.append(Signal(
                symbol=str(row.get("symbol", "")).upper(),
                strategy=execution.name,
                date=as_of,
                direction=direction,
                entry=(float(entry) if entry is not None else None),
                sl=(float(sl) if sl is not None else None),
                target=(float(target) if target is not None else None),
                rr=(float(rr) if rr is not None else None),
                note=note,
            ))
        return sigs

    # -- replay ---------------------------------------------------------------
    def run(self, strategy_names: Sequence[str]) -> BacktestReport:
        runners = self._runners(strategy_names)
        warnings = list(self.warnings)

        cash = self.config.initial_capital
        positions: List[_Position] = []
        trades: List[Trade] = []
        equity_points: List[EquityPoint] = []
        benchmark_curve: Optional[List[EquityPoint]] = self._benchmark_init()

        open_days = 0

        for i, d in enumerate(self.axis):
            daily_map = self._daily_map(d)

            # 1) Fill pending entries at d's open.
            still_open = []
            for p in positions:
                if p.pending:
                    df = daily_map.get(p.symbol)
                    if df is None or df.empty:
                        still_open.append(p)
                        continue
                    open_price = float(df.iloc[-1]["Open"])
                    if p.sl is not None and p.exit_model == "sl_target":
                        risk_dist = abs(open_price - p.sl)
                        if risk_dist <= 0:
                            still_open.append(p)
                            continue
                        qty = (cash * (self.config.risk_per_trade_pct / 100.0)) / risk_dist
                    else:
                        qty = (cash * (self.config.position_pct / 100.0)) / open_price if open_price > 0 else 0
                    if qty <= 0:
                        still_open.append(p)
                        continue
                    p.entry_price = open_price
                    p.qty = qty
                    p.pending = False
                    p.entry_axis_idx = i
                    notional = qty * open_price
                    comm = notional * (self.config.commission_per_trade_pct / 100.0)
                    cash -= comm
                    if p.side > 0:
                        cash -= notional
                    else:
                        cash += notional
                    still_open.append(p)
                else:
                    still_open.append(p)
            positions = still_open

            # 2) Exits on d's range.
            still_open = []
            for p in positions:
                if p.pending:
                    still_open.append(p)
                    continue
                df = daily_map.get(p.symbol)
                if df is None or df.empty:
                    still_open.append(p)
                    continue
                bar = df.iloc[-1]
                hi = float(bar["High"])
                lo = float(bar["Low"])
                close = float(bar["Close"])
                exit_price = close
                exit_reason = ""
                if p.side > 0:
                    p.mfe = max(p.mfe, (hi - p.entry_price) * p.qty)
                    p.mae = min(p.mae, (lo - p.entry_price) * p.qty)
                else:
                    p.mfe = max(p.mfe, (p.entry_price - lo) * p.qty)
                    p.mae = min(p.mae, (p.entry_price - hi) * p.qty)

                if p.exit_model == "sl_target":
                    if p.side > 0:
                        if p.sl is not None and lo <= p.sl:
                            exit_price, exit_reason = p.sl, "stop"
                        elif p.target is not None and hi >= p.target:
                            exit_price, exit_reason = p.target, "target"
                    else:
                        if p.sl is not None and hi >= p.sl:
                            exit_price, exit_reason = p.sl, "stop"
                        elif p.target is not None and lo <= p.target:
                            exit_price, exit_reason = p.target, "target"
                else:  # hold model
                    if (i - p.entry_axis_idx) >= self.config.hold_days:
                        exit_price, exit_reason = close, "timeout"
                if exit_reason:
                    notional = p.qty * exit_price
                    comm = notional * (self.config.commission_per_trade_pct / 100.0)
                    cash -= comm
                    if p.side > 0:
                        cash += notional
                        pnl = (exit_price - p.entry_price) * p.qty - 2 * comm
                    else:
                        cash -= notional
                        pnl = (p.entry_price - exit_price) * p.qty - 2 * comm
                    pnl_pct = (pnl / (p.qty * p.entry_price)) if p.qty * p.entry_price else 0.0
                    trades.append(Trade(
                        symbol=p.symbol, strategy=p.strategy, side=p.side,
                        entry_date=p.entry_date, entry_price=p.entry_price,
                        exit_date=d, exit_price=exit_price, exit_reason=exit_reason,
                        qty=p.qty, pnl=pnl, pnl_pct=pnl_pct, mfe=p.mfe, mae=p.mae,
                    ))
                else:
                    still_open.append(p)
            positions = still_open

            # 3) Generate signals at d and schedule entries for d+1.
            for name, runner in runners:
                try:
                    execution = runner(
                        symbols=self.config.symbols,
                        as_of_date=d,
                        verbose=False,
                        print_values=False,
                        daily_map=daily_map,
                    )
                except Exception as exc:  # defensive: one bad date must not kill the run
                    warnings.append(f"{name} failed at {d.isoformat()}: {exc}")
                    continue
                for sig in self._signals_from(execution, d):
                    key = (sig.symbol, name)
                    if any((p.symbol, p.strategy) == key for p in positions):
                        continue  # already have a position for this symbol+strategy
                    if sig.direction == 0:
                        continue
                    exit_model = "sl_target" if (sig.sl is not None and sig.target is not None) else "hold"
                    positions.append(_Position(
                        symbol=sig.symbol, strategy=name, side=sig.direction,
                        entry_date=d, entry_price=sig.entry or 0.0, qty=0.0,
                        sl=sig.sl, target=sig.target, exit_model=exit_model,
                        entry_axis_idx=i, pending=True,
                    ))

            # 4) Mark-to-market equity.
            equity = cash
            has_open = False
            for p in positions:
                if p.pending:
                    continue
                df = daily_map.get(p.symbol)
                if df is None or df.empty:
                    continue
                close = float(df.iloc[-1]["Close"])
                has_open = True
                if p.side > 0:
                    equity += p.qty * close
                else:
                    equity -= p.qty * close
            if has_open:
                open_days += 1
            equity_points.append(EquityPoint(date=d, equity=equity, drawdown_pct=0.0, exposure=1.0 if has_open else 0.0))

            if benchmark_curve is not None:
                self._benchmark_update(benchmark_curve, d, daily_map)

        # Finalize drawdown from equity peaks.
        peak = None
        for pt in equity_points:
            if peak is None or pt.equity > peak:
                peak = pt.equity
            pt.drawdown_pct = ((peak - pt.equity) / peak) if peak > 0 else 0.0

        exposure = (open_days / len(self.axis)) if self.axis else 0.0
        from .metrics import compute_metrics
        metrics = compute_metrics(
            equity_curve=[p.equity for p in equity_points],
            trades=trades,
            exposure_fraction=exposure,
            n_days=len(self.axis),
            initial_capital=self.config.initial_capital,
        )

        label = ",".join(strategy_names) if len(strategy_names) > 1 else (strategy_names[0] if strategy_names else "none")
        return BacktestReport(
            strategy=label,
            config={
                "symbols": self.config.symbols,
                "strategies": list(strategy_names),
                "start_date": self.config.start_date.isoformat(),
                "end_date": self.config.end_date.isoformat(),
                "initial_capital": self.config.initial_capital,
                "risk_per_trade_pct": self.config.risk_per_trade_pct,
                "position_pct": self.config.position_pct,
                "commission_per_trade_pct": self.config.commission_per_trade_pct,
                "hold_days": self.config.hold_days,
                "benchmark_symbol": self.config.benchmark_symbol,
            },
            equity_curve=[{
                "date": p.date.isoformat(),
                "equity": round(p.equity, 2),
                "drawdown_pct": round(p.drawdown_pct, 4),
                "exposure": p.exposure,
            } for p in equity_points],
            trades=[{
                "symbol": t.symbol, "strategy": t.strategy, "side": t.side,
                "entry_date": t.entry_date.isoformat(), "entry_price": round(t.entry_price, 4),
                "exit_date": t.exit_date.isoformat(), "exit_price": round(t.exit_price, 4),
                "exit_reason": t.exit_reason, "qty": round(t.qty, 4),
                "pnl": round(t.pnl, 2), "pnl_pct": round(t.pnl_pct, 4),
            } for t in trades],
            metrics=metrics.to_dict(),
            benchmark_curve=([{
                "date": p.date.isoformat(), "equity": round(p.equity, 2),
            } for p in benchmark_curve] if benchmark_curve else None),
            warnings=warnings,
        )

    # -- benchmark helpers ----------------------------------------------------
    def _benchmark_init(self) -> Optional[List[EquityPoint]]:
        sym = self.config.benchmark_symbol
        if not sym:
            return None
        sym = sym.strip().upper()
        if sym not in self.series or self.series[sym].empty:
            self.warnings.append(f"Benchmark symbol {sym} has no stored data; benchmark omitted.")
            return None
        return []

    def _benchmark_update(self, curve: List[EquityPoint], d: date, daily_map: Dict[str, pd.DataFrame]) -> None:
        sym = self.config.benchmark_symbol.strip().upper()
        df = daily_map.get(sym)
        if df is None or df.empty:
            return
        close = float(df.iloc[-1]["Close"])
        if not curve:
            self._bench_start = close
            curve.append(EquityPoint(date=d, equity=self.config.initial_capital, drawdown_pct=0.0, exposure=0.0))
            return
        eq = self.config.initial_capital * (close / self._bench_start)
        curve.append(EquityPoint(date=d, equity=eq, drawdown_pct=0.0, exposure=0.0))


# ---------------------------------------------------------------------------
# Public entry point: per-strategy + combined
# ---------------------------------------------------------------------------
def run_backtest(config: BacktestConfig) -> Dict[str, BacktestReport]:
    """Return {strategy_name: report, ..., 'combined': report}.

    'combined' runs every selected strategy in one shared-capital replay so
    positions from different strategies coexist in a single equity curve.
    """
    engine = BacktestEngine(config)
    reports: Dict[str, BacktestReport] = {}
    for name in config.strategies:
        reports[name] = engine.run([name])
    if len(config.strategies) > 1:
        reports["combined"] = engine.run(config.strategies)
    elif config.strategies:
        reports["combined"] = reports[config.strategies[0]]
    return reports
