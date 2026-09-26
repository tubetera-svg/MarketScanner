# Backtesting Engine — Technical Specification & Implementation Plan

> Scope: add a backtesting engine to the MarketScanner platform that replays every
> currently-implemented strategy over historical daily OHLC, computes performance
> metrics, and renders an equity-curve report in the existing Next.js UI.
>
> This document is a spec + plan only. No engine code is committed. Where entry/SL/
> target logic must be *chosen*, the decision is flagged as OPEN and must be confirmed
> before implementation (see AGENTS.md rule 3). Repainting / look-ahead / consistency
> risks are called out in §7 with file:line references.

---

## 1. Goal & Non-goals

**Goal** — Let a user pick one or more of the 11 implemented strategies, a symbol
universe, and a date range, then:

1. Ingest/ensure the required historical daily bars are present in SQLite.
2. Replay each strategy bar-by-bar (point-in-time) across the range.
3. Turn each historical signal into a simulated trade using a explicit execution model.
4. Compute equity curve + metrics (Sharpe, max drawdown, win rate, …).
5. Visualize the equity curve (and drawdown) in the frontend.

**Non-goals (this phase)** — intraday/5m backtesting (ICT intraday FVG logic is
live-only; all 11 strategies are daily-close based), portfolio optimization, live
paper-trading, parameter optimization / walk-forward.

---

## 2. Current Architecture Map (what we reuse)

| Concern | Existing code | Reuse |
|---|---|---|
| Strategy registry | `src/all_strategy.py:1663` `strategy_registry()` | Drives which strategies run |
| Per-strategy runners | `run_weekly_vs_daily` (`:517`), `run_inside_bar_daily_sweep` (`:575`), `run_daily_fvg_sweep` (`:626`), `run_ema5_sweep` (`:677`), `run_weekly_profile` (`:1494`) | Called once per historical date with a `daily_map` truncated to that date |
| Runner signature | `(symbols, as_of_date, verbose, print_values, daily_map=None)` → `StrategyExecution` | The `daily_map` arg is the backtest integration seam |
| Point-in-time guard | `_trim_in_progress_daily` (`:1468`) only trims when `date.today()` is the last bar → no-op during historical replay (as_of_date ≠ today) | Safe to ignore in backtest |
| Result shape | `StrategyExecution(name, results, bullish, bearish)` (`:64`); weekly profiles also populate `entry/sl/target/rr/direction` (`:1517-1584`) | Source of signal + trade levels |
| OHLC storage | `market_data/database.py:31` `ohlc_daily(source, symbol, exchange, date, O/H/L/C, volume)` | Backtest reads from here (no live fetch) |
| OHLC retrieval | `market_data.database.query_ohlc` / `query_ohlc_multi` (`:181`,`:141`) | Load full series once, slice per date |
| Data ingestion | `market_data.service.ensure_backdate_data` (`:404`) + `get_ohlc` (`:139`) | Ensure range present before replay |
| Trading calendar | `market_data.service.expected_trading_dates` (`service.py:105`) + `NSE_HOLIDAYS` | Build the replay date axis (no weekends/holidays) |
| API bridge | `api/strategy_bridge.run_scan` (`:153`), `api/main.py` | New `POST /api/backtest` follows the same pattern |
| Tracker close logic | `src/weekly_profile_tracker.py:50` `_evaluate_close` | Reuse for SL/target hit detection |
| Frontend | Next.js + React 19 (`frontend/package.json`) | New `/backtest` page; chart lib to be added |

---

## 3. Strategies In Scope (all 10)

From `strategy_registry()` (`src/all_strategy.py:1663`) + `WEEKLY_PROFILE_FLAGS`:

- Core (no built-in SL/target): `weekly_vs_daily_sweep` (420d), `inside_bar_pattern_daily_sweep` (160d), `daily_fvg_sweep` (80d), `ema5_sweep` (40d).
- Weekly profiles (carry `entry/sl/target/rr`): `classic_expansion_sweep`, `midweek_reversal_sweep`, `consolidation_reversal_sweep`, `intraweek_reversal_sweep`, `thursday_counter_sweep`, `tgif_setup_sweep` (all 60d).

The per-strategy required lookback is already centralized in `run_strategies`'s
`lookback_by_strategy` dict (`src/all_strategy.py:1744`) — the engine reuses it to
size the data window.

---

## 4. Architectural Approach

```
┌──────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│  Frontend    │   │   api/main.py       │   │   src/backtest/      │
│ /backtest    │──▶│ POST /api/backtest  │──▶│  engine.py           │
│ (Next.js)    │   │  (FastAPI)         │   │  metrics.py          │
└──────┬───────┘   └─────────┬──────────┘   │  report.py           │
       │                     │              └───────────┬──────────┘
       │  equity + metrics   │                          │ uses
       ◀─────────────────────┘                          ▼
                                          ┌─────────────────────────────┐
                                          │ market_data (SQLite OHLC)   │
                                          │ + ensure_backdate_data()    │
                                          │ + strategy_registry()/run_* │
                                          └─────────────────────────────┘
```

New backend package `src/backtest/` (kept out of `src/all_strategy.py` to avoid
 bloating the strategy module; it imports the registry + runners):

- **`ingest.py`** — `ensure_backtest_data(symbols, start, end, strategies)`: for each
  symbol resolves its source (`_source_for_symbol`, `all_strategy.py:105`), computes
  `max_lookback = max(lookback_by_strategy[s] for s in strategies)`, then calls
  `market_data.service.ensure_backdate_data` (or a window-extended variant) to fill
  `[start - max_lookback, end]`. Reports per-symbol fetched/synced/failed.
- **`engine.py`** — `BacktestEngine.run()` orchestrates replay → trades → equity.
- **`execution.py`** — per-strategy `ExecutionModel` (§6) turning a signal into a trade.
- **`metrics.py`** — pure functions for Sharpe, drawdown, win rate, etc.
- **`report.py`** — serializes a `BacktestReport` to the JSON the API returns.

API: add `POST /api/backtest` in `api/main.py` mirroring `run_strategy_scan`
(`api/main.py:449`); it delegates to `src/backtest/engine.py` inside
`asyncio.to_thread`. Frontend: new route `frontend/app/backtest/page.tsx` + chart.

---

## 5. Required Data Structures

```python
# src/backtest/engine.py (proposed)
@dataclass
class BacktestConfig:
    symbols: list[str]
    strategies: list[str]
    start_date: date
    end_date: date
    initial_capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0      # position sizing from SL distance
    commission_per_trade_pct: float = 0.0
    hold_days_default: int = 5           # exit model for core strategies (OPEN §6)
    benchmark_symbol: str | None = None  # e.g. "NSE:NIFTY" for buy&hold curve

@dataclass
class Signal:
    symbol: str
    strategy: str
    date: date
    direction: int          # +1 long, -1 short, 0 none
    entry: float | None
    sl: float | None
    target: float | None
    rr: float | None
    note: str

@dataclass
class Trade:
    symbol: str
    strategy: str
    side: int               # +1/-1
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_reason: str        # "target" | "stop" | "signal_reverse" | "timeout"
    qty: float
    pnl: float
    pnl_pct: float
    mfe: float              # max favorable excursion (for MAE/MFE analysis)
    mae: float

@dataclass
class EquityPoint:
    date: date
    equity: float
    drawdown_pct: float
    exposure: float         # fraction of capital in the market

@dataclass
class BacktestReport:
    config: BacktestConfig
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    metrics: dict[str, float]      # sharpe, sortino, max_dd, win_rate, ...
    benchmark_curve: list[EquityPoint] | None
    warnings: list[str]            # data gaps, no-data symbols, repaint notes
    generated_at: str
```

Replay loop (per strategy, then optionally combined):

1. Load full daily series per symbol once via `market_data.database.query_ohlc`
   (source resolved per symbol). Cache in memory.
2. Build the replay axis = `expected_trading_dates(source, start, end)` (use the
   strict source calendar so NSE vs TradingView holidays differ correctly).
3. For each date `D` in the axis:
   - Slice each symbol's series to `index <= D`, build the `daily_map` dict the
     runners expect (key = upper symbol → truncated `DataFrame[O,H,L,C]`).
   - Call `registry[name].runner(symbols=symbols, as_of_date=D, daily_map=daily_map)`.
   - For each row where `final_signal` is True, construct a `Signal` with the
     strategy's `direction`/`entry`/`sl`/`target`/`rr` (weekly profiles) or the
     core-strategy execution model (§6).
4. Feed signals to `ExecutionModel` which maintains open positions and emits `Trade`s.
5. At each date, mark-to-market open positions → append `EquityPoint`.

> Why slice `daily_map` per date instead of calling `_build_daily_map_for_symbols`
> repeatedly: that helper re-reads SQLite each call (`all_strategy.py:234`); slicing
> an in-memory frame is O(n) per step and avoids 420-day re-queries. We still honor
> the *point-in-time* contract because the runner only ever sees bars ≤ D.

---

## 6. Execution / P&L Model (OPEN DECISIONS)

The strategies are signal generators, not execution engines. We must define how a
signal becomes a trade. Two tiers:

**Tier A — Weekly profiles (`src/all_strategy.py:1517-1584`)** already expose
`entry`, `sl`, `target`. Model (proposed, low-risk):
- Enter at `entry` on the **next** trading day's open (D+1) — never same-bar close
  (avoids using the signal bar's close that the evaluator also used → no look-ahead).
- Each subsequent day: if long and `low <= sl` → exit at `sl` (reason `stop`);
  if `high >= target` → exit at `target` (reason `target`); mirror for short.
  Reuse `weekly_profile_tracker._evaluate_close` (`src/weekly_profile_tracker.py:50`).
- Position size = `risk_per_trade_pct% * capital / |entry - sl|`.

**Tier B — Core strategies (no SL/target). OPEN — needs your call before coding.**
Proposed default (flag for confirmation): enter at D+1 open in signal direction;
exit when (a) the **opposite** signal fires for that symbol, or (b) after
`hold_days_default` trading days (timeout). No hard SL. Alternatives to choose from:
  - (B1) fixed N-day hold (simplest, deterministic);
  - (B2) opposite-signal reversal exit (trend-following, no stop);
  - (B3) add a volatility stop (e.g. 2×ATR via `_daily_atr`, `all_strategy.py:1309`).
  - (B4) approximate a target using the profile-style liquidity pool.
  Recommend **B1 or B2** for v1 (no invented SL), since AGENTS.md forbids silently
  adding stop/target logic. This is the single biggest modeling decision.

**Per-trade accounting:** commission from `commission_per_trade_pct`; pnl in currency
and %; track MFE/MAE for later quality analysis.

---

## 7. Repainting / Look-Ahead / Consistency Flags (per AGENTS.md)

These must be honored; the engine design already mitigates most, but surface them:

1. **Per-bar signal uses only bars ≤ D** — satisfied by `daily_map` slicing (§5). The
   runners read `daily.iloc[-1]`/`-2` etc., which are the *latest completed* bars as
   of D. No future bars reach the evaluator. (`src/all_strategy.py` `run_*` bodies.)
2. **In-progress bar guard** `_trim_in_progress_daily` (`all_strategy.py:1468`) only
   acts when `date.today()` is the last row; during replay `as_of_date != today`, so
   it is a no-op — the engine must NOT call it and must NOT pass `date.today()` as the
   anchor. Confirmed safe.
3. **Weekly resample mid-week** `run_weekly_vs_daily` resamples `W-FRI`
   (`all_strategy.py:288`). At a Wed anchor the current weekly bucket contains only
   Mon–Wed data (resample is inclusive up to the slice end), so no future-week days
   leak in. Correct point-in-time.
4. **Forex/commodity "live" track mode** `_track_mode_for` (`all_strategy.py:1324`)
   returns `"live"` for non-NSE, meaning the live scanner keeps an in-progress bar.
   In backtest we always truncate to D, so we simulate *closed* daily bars only —
   consistent with how NSE EOD behaves. This is a deliberate simplification: the
   live intraday FVG path (`ict_scanner.py` `TvDatafeedFetcher`) is NOT backtestable
   here (daily only). **Flag:** intraday ICT setups cannot be backtested at daily
   resolution; only the daily-close sweeps are covered.
5. **Timezone / session mismatch** — NSE uses IST `NSE_HOLIDAYS`
   (`all_strategy.py:43`, `ict_scanner.py:210`); TradingView/forex uses NY 17:00
   rollover (`ict_scanner.py:250`). The replay axis must use the *per-source*
   `expected_trading_dates` (NSE vs TradingView differ). Do NOT use a single calendar
   for mixed universes, or forex entries will be mis-dated. (Reuse
   `market_data.service.expected_trading_dates`.)
6. **Cached vs live data** — backtest reads SQLite only (`query_ohlc`), never the
   provider. This is consistent with the existing historical-read policy in
   `_fetch_strategy_daily` (`all_strategy.py:191`, "Historical → SQLite only"). Good.
7. **`ema5_sweep` uses `Close` of prior bars only** — no
    look-ahead. Fine.
8. **Entry timing** — entering at D+1 open (not signal-bar close) is the key guard
   against using the evaluator's own close. Enforce in `execution.py`.

---

## 8. Performance Metrics (formulas, `src/backtest/metrics.py`)

Daily returns `r_t = equity_t / equity_{t-1} - 1` over the replay axis.

- **Sharpe** = `mean(r) / std(r) * sqrt(252)` (annualization; risk-free = 0 by
  default, make configurable). Use `std` with `ddof=1`.
- **Sortino** = `mean(r) / downside_std(r) * sqrt(252)` (downside deviation only).
- **Max Drawdown** = `max over t of (peak_equity_up_to_t - equity_t) / peak_equity_up_to_t`.
- **Win rate** = `#trades with pnl>0 / #closed_trades`.
- **Profit factor** = `sum(wins) / sum(losses)`.
- **Expectancy** = `mean(pnl_per_trade)`.
- **CAGR** = `(final/initial)^(252/n_days) - 1`.
- **Avg win / avg loss**, **# trades**, **% exposure**, **recovery factor**.
- Optional: **MAE/MFE scatter** for trade-quality (data already in `Trade`).

All metrics must be computed on the *realized + marked-to-market* equity series, not
on a synthetic buy&hold, and reported alongside an optional benchmark curve.

---

## 9. Visual Reporting

- **Frontend stack**: Next.js 15 + React 19, currently **no charting dependency**
  (`frontend/package.json`). Proposal: add **Recharts** (`recharts`, React-19
  compatible, declarative) for the equity curve + drawdown area chart. Alternative:
  `lightweight-charts` (TradingView) if candlestick overlays are wanted later.
- **New page** `frontend/app/backtest/page.tsx`:
  - Form: strategy multi-select (from `GET /api/strategies`), symbol picker
    (from `GET /api/watchlist`), start/end date, capital, risk%.
  - POST `/api/backtest` → render:
    - Equity curve line (strategy) vs benchmark (dashed).
    - Drawdown area chart beneath (from `drawdown_pct`).
    - Metrics cards (Sharpe, Max DD, Win rate, Profit factor, # trades, CAGR).
    - Trades table (symbol, side, entry/exit, pnl%, reason).
- **API payload**: `BacktestReport` JSON (§5). Keep the equity curve downsampled
  (e.g. weekly points) if the range is long, to keep payload small.
- Optional server-side PNG via `matplotlib` is **not** required; render client-side.

---

## 10. Step-by-Step Development Roadmap

**Phase 0 — Spike / decisions (½ day)**
- Confirm OPEN decision §6 (core-strategy exit model B1–B4) and chart lib (Recharts).
- Add `recharts` to `frontend/package.json`.

**Phase 1 — Data ingestion (`src/backtest/ingest.py`)**
- Wrap `market_data.service.ensure_backdate_data` with a `max_lookback` derived from
  `lookback_by_strategy` (`all_strategy.py:1744`); return synced/failed per symbol.
- Add unit test with a fake fetcher (`register_fetcher`, `service.py:94`).

**Phase 2 — Replay engine (`src/backtest/engine.py`, `execution.py`)**
- `BacktestConfig`/`Signal`/`Trade`/`EquityPoint`/`BacktestReport` dataclasses.
- In-memory load via `query_ohlc_multi`; per-date `daily_map` slicing.
- Call `registry[name].runner(..., daily_map=daily_map)` per date (reuse signature at
  `all_strategy.py:1663`/runners). Assert no live fetch occurs.
- `ExecutionModel` with D+1-open entry + SL/target hit (reuse
  `weekly_profile_tracker._evaluate_close`).

**Phase 3 — Metrics (`src/backtest/metrics.py`)**
- Pure functions for Sharpe/Sortino/MaxDD/win rate/profit factor/CAGR; table test
  against a hand-computed fixture.

**Phase 4 — Report + API**
- `report.py` serializes `BacktestReport`.
- `POST /api/backtest` in `api/main.py` (mirror `run_strategy_scan`, `api/main.py:449`);
  add `BacktestRequest` pydantic model.
- Guard: 409 if `service.lock` locked; run inside `asyncio.to_thread`.

**Phase 5 — Frontend (`frontend/app/backtest/page.tsx`)**
- Form + fetch + Recharts equity/drawdown + metrics cards + trades table.

**Phase 6 — Tests & validation**
- `tests/test_backtest.py`: deterministic replay on a tiny in-memory SQLite fixture;
  assert no look-ahead (signal at D never references >D), equity monotonic sanity,
  metrics known-values.
- Run `pytest` and the frontend `next lint`/`next build`.

**Phase 7 — Docs & RUNBOOK**
- Add a short section to `RUNBOOK.md` for running a backtest; note the daily-only
  limitation (§7.4).

---

## 11. Testing Approach

- Reuse `tests/conftest.py` + `market_data` test patterns (`tests/test_market_data.py`).
- Inject a deterministic OHLC fixture into SQLite; register a stub fetcher so no
  network is touched (`service.register_fetcher`, `service.py:94`).
- Property test: for every emitted `Trade`, `entry_date > signal_date` (no same-bar
  entry) and `exit_date >= entry_date`; for every `Signal`, the runner received a
  `daily_map` whose last index == `signal_date` (no future leak).
- Golden-value test for metrics on a 3-trade fixture.

---

## 12. Open Questions for Sign-off

1. **Core-strategy exit model** (§6 Tier B): B1 fixed-hold / B2 reversal / B3 ATR-stop
   / B4 profile-target? (Default recommendation: B1 for v1.)
2. **Chart library**: Recharts (recommended) vs lightweight-charts?
3. **Benchmark**: which symbol for buy&hold (e.g. `NSE:NIFTY`)? Optional.
4. **Position sizing**: fixed fraction per trade (recommended) vs fixed qty?
5. Should backtest also produce a **per-strategy** comparison when multiple strategies
   are selected, or a combined portfolio only?
