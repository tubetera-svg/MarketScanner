from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ICT_PATH = ROOT / "src" / "ict_scanner.py"
for _extra_path in (str(ROOT), str(ROOT / "api"), str(ROOT / "src")):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

import strategy_bridge  # noqa: E402  (strategy profiles panel: lives in the api folder)
from market_data.routes import router as market_data_router  # noqa: E402
from market_data.service import ensure_backdate_data  # noqa: E402


class ScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)


class HistoricalTestRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)
    anchor_date: date


class StrategyScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)
    strategies: list[str] | None = Field(default=None, max_length=50)
    anchor_date: date | None = None


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=500)
    strategies: list[str] = Field(min_length=1, max_length=50)
    start_date: date
    end_date: date
    initial_capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    position_pct: float = 10.0
    commission_per_trade_pct: float = 0.0
    hold_days: int = Field(default=5, ge=1, le=250)
    benchmark_symbol: str | None = None
    sync: bool = True


class StrategyFlagUpdate(BaseModel):
    enabled: bool


class ScheduleStartRequest(BaseModel):
    interval_minutes: int = Field(ge=1, le=1440)
    symbols: list[str] | None = Field(default=None, max_length=500)


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)


class WatchlistRemoveRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)


class WatchlistRenameRequest(BaseModel):
    old_symbol: str = Field(min_length=1, max_length=80)
    new_symbol: str = Field(min_length=1, max_length=80)


class ScannerService:
    def __init__(self) -> None:
        spec = importlib.util.spec_from_file_location("ict_scanner_api", ICT_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {ICT_PATH.name}")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        # Alert sounds are played in the browser UI instead of on this machine.
        self.module.play_alert = lambda: None
        self.lock = asyncio.Lock()
        self.last_results: list[dict[str, Any]] = []
        self.last_scan_at: str | None = None
        self.last_date_note: dict[str, str | None] = {"requested_date": None, "resolved_date": None, "resolution_reason": None}

    def watchlist(self) -> list[dict[str, str]]:
        entries = self.module.load_watchlist(str(ROOT / "config" / "watchlist.txt"))
        result: list[dict[str, str]] = []
        for symbol, session in entries:
            try:
                category = self.module.categorize_symbol(symbol)
                result.append({
                    "symbol": symbol,
                    "session": session.value,
                    "exchange": category["exchange"],
                    "asset_class": category["asset_class"],
                    "scope": category["scope"],
                })
            except Exception:
                result.append({
                    "symbol": symbol,
                    "session": session.value,
                    "exchange": "",
                    "asset_class": "unknown",
                    "scope": "",
                })
        return result

    def add_to_watchlist(self, value: str) -> list[dict[str, str]]:
        try:
            category = self.module.categorize_symbol(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        symbol = category["symbol"]

        path = ROOT / "config" / "watchlist.txt"
        entries = self.module.load_watchlist(str(path))
        if any(existing_symbol.upper() == symbol for existing_symbol, _ in entries):
            raise ValueError(f"{symbol} is already in the watchlist")

        with path.open("a", encoding="utf-8") as file:
            file.write(f"{symbol}\n")
        return self.watchlist()

    def remove_from_watchlist(self, value: str) -> list[dict[str, str]]:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("A symbol is required")
        path = ROOT / "config" / "watchlist.txt"
        entries = self.module.load_watchlist(str(path))
        kept = [(existing_symbol, session) for existing_symbol, session in entries if existing_symbol.upper() != symbol]
        if len(kept) == len(entries):
            raise ValueError(f"{symbol} is not in the watchlist")
        with path.open("w", encoding="utf-8") as file:
            for existing_symbol, _ in kept:
                file.write(f"{existing_symbol}\n")
        # Drop any alias mapping for the removed symbol.
        try:
            from market_data.config import load_symbol_aliases, save_symbol_aliases

            alias_map = load_symbol_aliases()
            if alias_map.pop(symbol, None) is not None:
                save_symbol_aliases(alias_map)
        except Exception:  # pragma: no cover - aliases are best-effort
            pass
        return self.watchlist()

    def rename_in_watchlist(self, old_value: str, new_value: str) -> list[dict[str, str]]:
        old_symbol = old_value.strip().upper()
        try:
            new_category = self.module.categorize_symbol(new_value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        new_symbol = new_category["symbol"]
        path = ROOT / "config" / "watchlist.txt"
        entries = self.module.load_watchlist(str(path))
        if not any(existing_symbol.upper() == old_symbol for existing_symbol, _ in entries):
            raise ValueError(f"{old_symbol} is not in the watchlist")
        if any(existing_symbol.upper() == new_symbol for existing_symbol, _ in entries):
            raise ValueError(f"{new_symbol} is already in the watchlist")
        with path.open("w", encoding="utf-8") as file:
            for existing_symbol, _ in entries:
                file.write(f"{(new_symbol if existing_symbol.upper() == old_symbol else existing_symbol)}\n")
        # Carry the alias mapping over to the new symbol name.
        try:
            from market_data.config import load_symbol_aliases, save_symbol_aliases

            alias_map = load_symbol_aliases()
            if old_symbol in alias_map:
                alias_map[new_symbol] = alias_map.pop(old_symbol)
                save_symbol_aliases(alias_map)
        except Exception:  # pragma: no cover - aliases are best-effort
            pass
        return self.watchlist()

    def create_fetcher(self, config: Any, cache: Any) -> Any:
        try:
            return self.module.TvDatafeedFetcher(config=config, cache=cache)
        except ImportError:
            if os.environ.get("OANDA_API_KEY"):
                return self.module.OandaDataFetcher(environment="practice", config=config, cache=cache)
            return self.module.DataFetcher()

    def scan(self, requested_symbols: list[str] | None) -> list[dict[str, Any]]:
        entries = self.module.load_watchlist(str(ROOT / "config" / "watchlist.txt"))
        selected = {value.strip().upper() for value in requested_symbols or [] if value.strip()}
        if selected:
            entries = [(symbol, session) for symbol, session in entries if symbol.upper() in selected]
        if not entries:
            raise ValueError("No watchlist symbols selected")

        config = self.module.ICTConfig(
            approaching_poi_pct=0.005,
            tier_a_poi_pct=0.01,
            displacement_lookback=10,
            displacement_body_multiplier=1.5,
            liquidity_tolerance=0.0005,
            fvg_lookback=30,
            minimum_rr=2.0,
            swing_len=5,
            intraday_timeframe="5m",
            intraday_bars=100,
            daily_bars=20,
            weekly_bars=5,
        )
        cache = self.module.OHLCCache(path=str(ROOT / "config" / "ohlc_cache.json"))
        scanner = self.module.AdaptiveScanner(
            watchlist=entries,
            data_fetcher=self.create_fetcher(config, cache),
            results_file_prefix=str(ROOT / "logs" / "scan_results"),
            operating_start=self.module.dtime(0, 0),
            operating_end=self.module.dtime(23, 59),
            operating_tz=self.module.IST,
            output_tiers=set(self.module.Tier),
            state_cache=self.module.TrackerStateCache(path=str(ROOT / "config" / "tracker_state_cache.json")),
        )
        scanner.run_once()
        results = []
        for tracker in scanner.trackers.values():
            if tracker.snapshot is None:
                continue
            row = asdict(tracker.snapshot)
            row.update({"symbol": tracker.symbol, "session": tracker.session.value, "tier": tracker.tier.value, "state": tracker.state.value})
            results.append(row)
        self.last_results = results
        self.last_scan_at = datetime.now().astimezone().isoformat()
        return results

    def historical_test(self, requested_symbols: list[str] | None, anchor_date: date) -> dict[str, Any]:
        requested_date, resolved_date, reason = self.module.resolve_previous_working_date(anchor_date)
        entries = self.module.load_watchlist(str(ROOT / "config" / "watchlist.txt"))
        selected = {value.strip().upper() for value in requested_symbols or [] if value.strip()}
        entries = [(symbol, session) for symbol, session in entries if not selected or symbol.upper() in selected]
        if not entries:
            raise ValueError("No watchlist symbols selected for historical testing")

        # Backdate test: fetch+store OHLC into SQLite for any dates missing
        # around the tested date (respects FETCH_* / AUTO_FETCH flags; never
        # blocks the scan on failure).
        try:
            backdate_sync = ensure_backdate_data(entries, resolved_date)
        except Exception as sync_exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning("Backdate data sync failed: %s", sync_exc)
            backdate_sync = {"anchor_date": resolved_date.isoformat(), "synced": [], "failed": [{"error": str(sync_exc)}]}

        config = self.module.ICTConfig(
            anchor_date=resolved_date,
            intraday_bars=100,
            daily_bars=20,
            weekly_bars=5,
        )
        cache = self.module.OHLCCache(path=str(ROOT / "config" / "ohlc_cache.json"))
        scanner = self.module.AdaptiveScanner(
            watchlist=entries,
            data_fetcher=self.create_fetcher(config, cache),
            results_file_prefix=str(ROOT / "logs" / "scan_results"),
            operating_start=self.module.dtime(0, 0),
            operating_end=self.module.dtime(23, 59),
            operating_tz=self.module.IST,
            output_tiers=set(self.module.Tier),
            state_cache=self.module.TrackerStateCache(path=str(ROOT / "config" / "tracker_state_cache.json")),
        )
        scanner.run_once(anchor_date=resolved_date)
        results = []
        for tracker in scanner.trackers.values():
            if tracker.snapshot is None:
                continue
            row = asdict(tracker.snapshot)
            row.update({"symbol": tracker.symbol, "session": tracker.session.value, "tier": tracker.tier.value, "state": tracker.state.value})
            results.append(row)

        self.last_date_note = {
            "requested_date": requested_date.isoformat(),
            "resolved_date": resolved_date.isoformat(),
            "resolution_reason": reason,
        }
        return {"results": results, **self.last_date_note, "backdate_data_sync": backdate_sync}

    def latest_file_results(self) -> list[dict[str, Any]]:
        files = sorted(ROOT.glob("logs/scan_results_*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            return []
        columns = [
            "symbol", "daily_bias", "weekly_bias", "tier", "state", "price", "poi_price",
            "entry", "stop_loss", "tp1", "tp2", "risk_reward", "liquidity_swept", "trade_confirmed",
        ]
        rows = []
        seen_symbols: set[str] = set()
        for line in files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            values = re.split(r"\s+", line.strip())
            if len(values) < len(columns):
                continue
            row = dict(zip(columns, values[: len(columns)]))
            if row["symbol"] in seen_symbols:
                continue
            seen_symbols.add(row["symbol"])
            row["trade_confirmed"] = row["trade_confirmed"] == "YES"
            for key in ("price", "poi_price", "entry", "stop_loss", "tp1", "tp2", "risk_reward"):
                if row[key] == "N/A":
                    row[key] = None
                else:
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        row[key] = None
            row["session"] = "forex_24_5" if ":" in row["symbol"] and not row["symbol"].startswith("NSE:") else "nse"
            rows.append(row)
        return rows


class ScanScheduler:
    """Continuously re-runs the scan on a fixed interval until stopped."""

    def __init__(self, service: ScannerService) -> None:
        self.service = service
        self.task: asyncio.Task[None] | None = None
        self.scanning: bool = False
        self.interval_minutes: int | None = None
        self.symbols: list[str] | None = None
        self.next_run_at: str | None = None
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.run_count: int = 0

    def status(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "scanning": self.scanning,
            "interval_minutes": self.interval_minutes,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }

    def start(self, interval_minutes: int, symbols: list[str] | None) -> dict[str, Any]:
        self.stop()
        self.interval_minutes = interval_minutes
        self.symbols = symbols
        self.task = asyncio.create_task(self._loop())
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        self.interval_minutes = None
        self.symbols = None
        self.next_run_at = None
        return self.status()

    async def _loop(self) -> None:
        while True:
            await self._run_scheduled_scan()
            seconds = max(60, (self.interval_minutes or 1) * 60)
            self.next_run_at = (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()
            await asyncio.sleep(seconds)

    async def _run_scheduled_scan(self) -> None:
        if self.service.lock.locked():
            self.last_error = f"Skipped at {datetime.now().astimezone():%H:%M:%S}: a scan was already running"
            return
        module = self.service.module
        if not (
            module.is_market_open(module.Session.NSE)
            or module.is_market_open(module.Session.FOREX_24_5)
            or module.is_daily_bar_ready(module.Session.NSE)
        ):
            self.last_error = f"All markets closed at {datetime.now().astimezone():%H:%M:%S}: auto-scan idle"
            return
        async with self.service.lock:
            self.scanning = True
            try:
                await asyncio.to_thread(self.service.scan, self.symbols)
                self.last_run_at = self.service.last_scan_at
                self.last_error = None
                self.run_count += 1
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                self.scanning = False


service = ScannerService()
scheduler = ScanScheduler(service)
app = FastAPI(title="ICT Scanner API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Local SQLite market-data layer: GET /ohlc, GET /api/ohlc, POST /api/market-data/sync
app.include_router(market_data_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/watchlist")
def get_watchlist() -> dict[str, Any]:
    return {"symbols": service.watchlist()}


@app.post("/api/watchlist")
def add_watchlist_item(request: WatchlistAddRequest) -> dict[str, Any]:
    try:
        return {"symbols": service.add_to_watchlist(request.symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/watchlist")
def remove_watchlist_item(request: WatchlistRemoveRequest) -> dict[str, Any]:
    try:
        return {"symbols": service.remove_from_watchlist(request.symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/watchlist")
def rename_watchlist_item(request: WatchlistRenameRequest) -> dict[str, Any]:
    try:
        return {"symbols": service.rename_in_watchlist(request.old_symbol, request.new_symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/results")
def get_results() -> dict[str, Any]:
    results = service.last_results or service.latest_file_results()
    return {"results": results, "scanned_at": service.last_scan_at, **service.last_date_note}


@app.post("/api/scan")
async def run_scan(request: ScanRequest) -> dict[str, Any]:
    if service.lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    async with service.lock:
        try:
            results = await asyncio.to_thread(service.scan, request.symbols)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"results": results, "scanned_at": service.last_scan_at, **service.last_date_note}


@app.post("/api/historical-test")
async def run_historical_test(request: HistoricalTestRequest) -> dict[str, Any]:
    if service.lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    async with service.lock:
        try:
            return await asyncio.to_thread(service.historical_test, request.symbols, request.anchor_date)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/strategies")
def get_strategies() -> dict[str, Any]:
    strategies, master = strategy_bridge.list_strategies()
    return {"strategies": strategies, "weekly_profiles_master_enabled": master}


@app.put("/api/strategies/{name}")
def update_strategy_flag(name: str, request: StrategyFlagUpdate) -> dict[str, Any]:
    try:
        strategy_bridge.set_strategy_flag(name, request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    strategies, master = strategy_bridge.list_strategies()
    return {"strategies": strategies, "weekly_profiles_master_enabled": master}


@app.post("/api/strategy-scan")
async def run_strategy_scan(request: StrategyScanRequest) -> dict[str, Any]:
    if service.lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    async with service.lock:
        try:
            return await asyncio.to_thread(strategy_bridge.run_scan, request.symbols, request.strategies, request.anchor_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/backtest")
async def run_backtest_endpoint(request: BacktestRequest) -> dict[str, Any]:
    """Run a backtest over the selected strategies + symbol universe.

    Ensures the required historical window is present in SQLite (best-effort,
    never blocks on failure), then replays each strategy point-in-time via
    ``src/backtest`` and returns per-strategy + combined reports.
    """
    if service.lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    async with service.lock:
        try:
            return await asyncio.to_thread(_run_backtest, request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


def _run_backtest(request: BacktestRequest) -> dict[str, Any]:
    from datetime import timedelta

    from backtest import BacktestConfig, run_backtest as _run

    if request.end_date < request.start_date:
        raise ValueError("end_date must be on or after start_date")

    cleaned_symbols = []
    for value in request.symbols:
        token = str(value).strip().upper()
        if token and token not in cleaned_symbols:
            cleaned_symbols.append(token)
    cleaned_strategies = []
    for value in request.strategies:
        token = str(value).strip()
        if token and token not in cleaned_strategies:
            cleaned_strategies.append(token)
    if not cleaned_symbols:
        raise ValueError("No symbols selected.")
    if not cleaned_strategies:
        raise ValueError("No strategies selected.")

    if request.sync:
        try:
            lookback = (request.end_date - request.start_date).days + 420
            entries = [(s, None) for s in cleaned_symbols]
            ensure_backdate_data(entries, request.end_date, lookback_days=max(1, lookback))
        except Exception as sync_exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning("Backtest data sync failed: %s", sync_exc)

    config = BacktestConfig(
        symbols=cleaned_symbols,
        strategies=cleaned_strategies,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        risk_per_trade_pct=request.risk_per_trade_pct,
        position_pct=request.position_pct,
        commission_per_trade_pct=request.commission_per_trade_pct,
        hold_days=request.hold_days,
        benchmark_symbol=request.benchmark_symbol,
    )
    reports = _run(config)
    return {
        "reports": {key: report.to_dict() for key, report in reports.items()},
        "generated_at": datetime.now().astimezone().isoformat(),
    }


@app.get("/api/weekly-profile-tracker")
def weekly_profile_tracker(symbols: Optional[str] = None) -> dict[str, Any]:
    """Return tracked weekly-profile setups (cross-scan).

    Pass ``symbols`` (comma-separated) to restrict to a watchlist subset —
    used by the frontend to show only the symbols a strategy scan ran over.
    """
    try:
        from weekly_profile_tracker import ACTIVE_STATES, ProfileTrackerStore

        store = ProfileTrackerStore()
        setups = store.all_setups()
        if symbols:
            wanted = {token.strip().upper() for token in symbols.split(",") if token.strip()}
            setups = [s for s in setups if str(s.get("symbol", "")).upper() in wanted]
        active = [s for s in setups if s.get("state") in ACTIVE_STATES]
        return {
            "active": active,
            "setups": setups,
            "active_count": len(active),
        }
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/weekly-profile-tracker/repair")
def repair_weekly_profile_tracker() -> dict[str, Any]:
    """Recompute targets/R:R and revert false closed_target flags written by a
    previous buggy build. Safe to call repeatedly (idempotent)."""
    try:
        from weekly_profile_tracker import ProfileTrackerStore

        store = ProfileTrackerStore()
        return {"fixed": store.repair_store()}
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/schedule")
def get_schedule() -> dict[str, Any]:
    return scheduler.status()


@app.post("/api/schedule/start")
async def start_schedule(request: ScheduleStartRequest) -> dict[str, Any]:
    return scheduler.start(request.interval_minutes, request.symbols)


@app.post("/api/schedule/stop")
async def stop_schedule() -> dict[str, Any]:
    return scheduler.stop()


@app.get("/api/markets")
def get_markets() -> dict[str, Any]:
    return {
        "nse": bool(service.module.is_nse_market_open()),
        "forex_commodities": bool(service.module.is_forex_24_5_open()),
    }


