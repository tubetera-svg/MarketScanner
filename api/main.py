from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ICT_PATH = ROOT / "src" / "ict_scanner.py"
for _extra_path in (str(ROOT), str(ROOT / "api"), str(ROOT / "src")):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

import app_settings  # noqa: E402  (persisted automation / show-hide settings)
import fno_membership  # noqa: E402  (NSE F&O list cache + watchlist F&O re-check)
import strategy_bridge  # noqa: E402  (strategy profiles panel: lives in the api folder)
from market_data.routes import router as market_data_router, _auto_sync  # noqa: E402
from market_data.service import ensure_backdate_data  # noqa: E402
from market_data.liquidity_screener import screen_all_ipos  # noqa: E402


class ScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)


class HistoricalTestRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)
    anchor_date: date


class StrategyScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)
    strategies: list[str] | None = Field(default=None, max_length=50)
    anchor_date: date | None = None
    timeframe: Literal["daily", "weekly", "15m", "1h", "4h"] = "weekly"


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


class SilverBulletStartRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)


class SilverBulletTestRequest(BaseModel):
    anchor_date: date
    symbols: list[str] | None = Field(default=None, max_length=500)


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    classification: dict[str, str] | None = None


class FnoApplyRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=64)


class WatchlistRemoveRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    delete_data: bool = False


class WatchlistRenameRequest(BaseModel):
    old_symbol: str = Field(min_length=1, max_length=80)
    new_symbol: str = Field(min_length=1, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    classification: dict[str, str] | None = None
    delete_old_data: bool = False


WATCHLIST_PATH = ROOT / "config" / "watchlist.txt"
WATCHLIST_CATEGORIES_PATH = ROOT / "config" / "watchlist_categories.json"
OHLC_CACHE_PATH = ROOT / "data" / "state" / "ohlc_cache.json"
TRACKER_STATE_CACHE_PATH = ROOT / "data" / "state" / "tracker_state_cache.json"
def nse_fno_members() -> set[str] | None:
    """NSE F&O base tickers from the local list (config/nse_fno_cache.json).

    Never downloads; refresh it from the watchlist manager. None if never saved.
    """
    return fno_membership.cached_members()


def apply_classification(target: dict[str, str], updates: dict[str, str] | None) -> None:
    """Merge form values into a classification; an empty value clears the field."""
    for key, value in (updates or {}).items():
        key = key.strip()
        if not key:
            continue
        value = (value or "").strip()
        if value:
            target[key] = value
        else:
            target.pop(key, None)


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
        return self.module.load_watchlist_details(str(WATCHLIST_PATH), allow_empty=True, categories_filename=str(WATCHLIST_CATEGORIES_PATH))

    def add_to_watchlist(self, value: str, category_label: str | None = None, classification: dict[str, str] | None = None) -> list[dict[str, str]]:
        try:
            category_info = self.module.categorize_symbol(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        symbol = category_info["symbol"]

        defaults = {
            "asset_class": category_info["asset_class"],
            "exchange": category_info["exchange"],
            "scope": category_info["scope"],
        }
        if category_info["asset_class"] == "equity" and category_info["exchange"] == "NSE":
            # Only tag F&O when the local NSE F&O list confirms it; without a
            # saved list the symbol stays "Equity" until "Refresh F&O from NSE".
            members = nse_fno_members()
            if members is not None:
                is_fno = category_info["base"] in members
                defaults["f_and_o"] = "F&O" if is_fno else "Non-F&O"
                if is_fno:
                    defaults["scope"] = "F&O"
        if category_label and category_label.strip():
            defaults["scope"] = category_label.strip()
        apply_classification(defaults, classification)

        with self.module.watchlist_file_lock(str(WATCHLIST_PATH)):
            entries = self.module.load_watchlist(str(WATCHLIST_PATH), allow_empty=True)
            if any(existing_symbol.upper() == symbol for existing_symbol, _ in entries):
                raise ValueError(f"{symbol} is already in the watchlist")
            self.module.modify_watchlist_file(str(WATCHLIST_PATH), add=symbol)
            categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
            categories[symbol] = defaults
            self.module.save_watchlist_categories(categories, str(WATCHLIST_CATEGORIES_PATH))
        return self.watchlist()

    def remove_from_watchlist(self, value: str) -> list[dict[str, str]]:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("A symbol is required")
        with self.module.watchlist_file_lock(str(WATCHLIST_PATH)):
            if not self.module.modify_watchlist_file(str(WATCHLIST_PATH), remove=symbol):
                raise ValueError(f"{symbol} is not in the watchlist")
            categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
            if categories.pop(symbol, None) is not None:
                self.module.save_watchlist_categories(categories, str(WATCHLIST_CATEGORIES_PATH))
        # Drop any alias mapping for the removed symbol.
        try:
            from market_data.config import load_symbol_aliases, save_symbol_aliases

            alias_map = load_symbol_aliases()
            if alias_map.pop(symbol, None) is not None:
                save_symbol_aliases(alias_map)
        except Exception:  # pragma: no cover - aliases are best-effort
            pass
        return self.watchlist()

    def purge_symbol_data(self, value: str) -> dict[str, int]:
        """Delete everything stored for one symbol: SQLite (OHLC rows + no-data
        markers for every source, ipo_metadata, tv_symbol_cache) and its entries
        in ohlc_cache.json / tracker_state_cache.json. Watchlist files untouched."""
        from market_data import database

        symbol = value.strip().upper()
        return {
            "ohlc_rows": database.delete_ohlc(symbols=[symbol]),
            "ipo_metadata": database.remove_ipo_metadata(symbol),
            "tv_symbol_cache": database.remove_tv_symbol(symbol),
            "ohlc_cache": int(self.module.remove_symbol_from_json_cache(str(OHLC_CACHE_PATH), symbol)),
            "tracker_state": int(self.module.remove_symbol_from_json_cache(str(TRACKER_STATE_CACHE_PATH), symbol)),
        }

    def rename_in_watchlist(self, old_value: str, new_value: str, category: str | None = None, classification: dict[str, str] | None = None) -> list[dict[str, str]]:
        old_symbol = old_value.strip().upper()
        try:
            new_category = self.module.categorize_symbol(new_value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        new_detected = new_category
        new_symbol = new_detected["symbol"]
        try:
            old_detected = self.module.categorize_symbol(old_symbol)
        except ValueError:
            old_detected = {}
        # Moving to a different exchange/asset class: detected fields the user
        # left untouched in the form follow the new symbol instead of the old.
        instrument_changed = any(old_detected.get(key) != new_detected[key] for key in ("exchange", "asset_class"))
        requested = dict(classification or {})
        if category and category.strip():
            requested["scope"] = category.strip()

        with self.module.watchlist_file_lock(str(WATCHLIST_PATH)):
            entries = self.module.load_watchlist(str(WATCHLIST_PATH), allow_empty=True)
            if not any(existing_symbol.upper() == old_symbol for existing_symbol, _ in entries):
                raise ValueError(f"{old_symbol} is not in the watchlist")
            if new_symbol != old_symbol and any(existing_symbol.upper() == new_symbol for existing_symbol, _ in entries):
                raise ValueError(f"{new_symbol} is already in the watchlist")
            self.module.modify_watchlist_file(
                str(WATCHLIST_PATH),
                rename=(old_symbol, new_symbol),
                keep_override=not instrument_changed,
            )
            categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
            old_category = categories.pop(old_symbol, {})
            updated = dict(old_category)
            if instrument_changed:
                for key in ("asset_class", "exchange", "scope"):
                    incoming = (requested.get(key) or "").strip()
                    if not incoming or incoming == old_category.get(key, ""):
                        requested.pop(key, None)
                        updated[key] = new_detected[key]
            apply_classification(updated, requested)
            categories[new_symbol] = updated
            self.module.save_watchlist_categories(categories, str(WATCHLIST_CATEGORIES_PATH))
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
        entries = self.module.load_watchlist(str(WATCHLIST_PATH), allow_empty=True)
        details = {item["symbol"]: item for item in self.module.load_watchlist_details(entries=entries, categories_filename=str(WATCHLIST_CATEGORIES_PATH))}
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
        cache = self.module.OHLCCache(path=str(OHLC_CACHE_PATH))
        scanner = self.module.AdaptiveScanner(
            watchlist=entries,
            data_fetcher=self.create_fetcher(config, cache),
            results_file_prefix=str(ROOT / "logs" / "scan_results"),
            operating_start=self.module.dtime(0, 0),
            operating_end=self.module.dtime(23, 59),
            operating_tz=self.module.IST,
            output_tiers=set(self.module.Tier),
            state_cache=self.module.TrackerStateCache(path=str(TRACKER_STATE_CACHE_PATH)),
        )
        scanner.run_once()
        results = []
        for tracker in scanner.trackers.values():
            if tracker.snapshot is None:
                continue
            row = asdict(tracker.snapshot)
            row.update({"symbol": tracker.symbol, "session": tracker.session.value, "tier": tracker.tier.value, "state": tracker.state.value, "scope": details.get(tracker.symbol, {}).get("scope", "")})
            results.append(row)
        self.last_results = results
        self.last_scan_at = datetime.now().astimezone().isoformat()
        return results

    def historical_test(self, requested_symbols: list[str] | None, anchor_date: date) -> dict[str, Any]:
        requested_date, resolved_date, reason = self.module.resolve_previous_working_date(anchor_date)
        entries = self.module.load_watchlist(str(WATCHLIST_PATH), allow_empty=True)
        details = {item["symbol"]: item for item in self.module.load_watchlist_details(entries=entries, categories_filename=str(WATCHLIST_CATEGORIES_PATH))}
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
        cache = self.module.OHLCCache(path=str(OHLC_CACHE_PATH))
        scanner = self.module.AdaptiveScanner(
            watchlist=entries,
            data_fetcher=self.create_fetcher(config, cache),
            results_file_prefix=str(ROOT / "logs" / "scan_results"),
            operating_start=self.module.dtime(0, 0),
            operating_end=self.module.dtime(23, 59),
            operating_tz=self.module.IST,
            output_tiers=set(self.module.Tier),
            state_cache=self.module.TrackerStateCache(path=str(TRACKER_STATE_CACHE_PATH)),
        )
        scanner.run_once(anchor_date=resolved_date)
        results = []
        for tracker in scanner.trackers.values():
            if tracker.snapshot is None:
                continue
            row = asdict(tracker.snapshot)
            row.update({"symbol": tracker.symbol, "session": tracker.session.value, "tier": tracker.tier.value, "state": tracker.state.value, "scope": details.get(tracker.symbol, {}).get("scope", "")})
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


class SilverBulletLiveScanner:
    """Poll commodity 5-minute bars during the New York AM Silver Bullet window."""

    NEW_YORK = ZoneInfo("America/New_York")
    AM_WINDOW_START_HOUR = 10  # 10:00 New York: AM Silver Bullet window opens
    AM_WINDOW_END_HOUR = 11  # 11:00 New York: window closed, live scan retires
    AUTO_CHECK_SECONDS = 60 * 3  # confirm the live scan is running every 3 minutes
    BAR_SECONDS = 60 * 5  # scan cadence: once per 5-minute bar close
    BAR_CLOSE_DELAY_SECONDS = 20  # let TradingView publish the just-closed bar
    # One last scan just after 11:00 New York evaluates the 10:55 bar, which only
    # closes at 11:00 (the historical test sees it too, via now=11:00).
    FINAL_SCAN_GRACE = timedelta(minutes=5)

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.auto_task: asyncio.Task[None] | None = None
        self.symbols: list[str] | None = None
        self.signals: list[dict[str, Any]] = []
        self.last_check_at: str | None = None
        self.next_check_at: str | None = None
        self.last_error: str | None = None
        self.run_count = 0
        self.scan_date: str | None = None
        # New York date of an explicit user stop, so the in-window fallback does
        # not re-arm a scan the user just stopped (cleared by any start/test).
        self.manual_stop_date: str | None = None

    def start_auto_schedule(self) -> None:
        if self.auto_task is None or self.auto_task.done():
            self.auto_task = asyncio.create_task(self._auto_loop())

    def _seconds_until_next_auto_check(self, now: datetime) -> float:
        """Seconds to the next check inside the New York AM window.

        Before 10:00 New York -> sleep until 10:00. After 11:00 New York -> the
        window is over, so sleep until 10:00 the next day. The delta is measured
        in UTC because subtracting two datetimes that share a tzinfo uses wall
        clock, not elapsed time: across the March DST change a "day" is 23 real
        hours, which would wake this check at 11:00 New York and skip the whole
        morning window.
        """
        window_start = now.replace(
            hour=self.AM_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
        )
        window_end = window_start + timedelta(hours=1)
        if now >= window_end:
            window_start += timedelta(days=1)
        elif now >= window_start and now.weekday() < 5:
            return float(self.AUTO_CHECK_SECONDS)
        while window_start.weekday() >= 5:  # commodities: Mon-Fri only
            window_start += timedelta(days=1)
        remaining = window_start.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        return max(1.0, remaining.total_seconds())

    async def _auto_loop(self) -> None:
        """Confirm every few minutes that a live scan is running in the NY AM window.

        The scan itself is unchanged (5-minute bars polled inside 10:00-11:00 NY);
        this loop only decides *whether* a live scan should be running. It is a
        fallback: an already running scan is never interrupted, and a scan the user
        stopped for this session stays stopped. Once 11:00 New York has passed it
        stops checking for the day and waits for the next one.
        """
        while True:
            try:
                now = datetime.now(self.NEW_YORK)
                manually_stopped = self.manual_stop_date == now.date().isoformat()
                if (
                    now.weekday() < 5
                    and self.AM_WINDOW_START_HOUR <= now.hour < self.AM_WINDOW_END_HOUR
                    and not manually_stopped
                ):
                    stale = (
                        self.task is not None
                        and not self.task.done()
                        and self.scan_date != now.date().isoformat()
                    )
                    if self.task is None or self.task.done() or stale:
                        self.start(None)
                await asyncio.sleep(max(1.0, self._seconds_until_next_auto_check(now)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the fallback alive across transient failures
                self.last_error = f"auto-check: {exc.__class__.__name__}: {exc}"
                await asyncio.sleep(60)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "symbols": self.symbols or [],
            "signals": self.signals,
            "last_check_at": self.last_check_at,
            "next_check_at": self.next_check_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "scan_date": self.scan_date,
        }

    def start(self, symbols: list[str] | None) -> dict[str, Any]:
        self.stop()
        requested = [str(value).strip().upper() for value in (symbols or []) if str(value).strip()]
        if not requested:
            from silver_bullet import is_commodity_symbol

            requested = [
                str(entry["symbol"]).upper()
                for entry in service.watchlist()
                if is_commodity_symbol(str(entry.get("symbol", "")))
            ]
        self.symbols = list(dict.fromkeys(requested))
        self.signals = []
        self.last_error = None
        # The session is anchored to New York wall time (DST aware), so the scan
        # date must come from New York too — not from the host's local date.
        self.scan_date = datetime.now(self.NEW_YORK).date().isoformat()
        self.task = asyncio.create_task(self._loop())
        return self.status()

    async def test(self, anchor_date: date, symbols: list[str] | None) -> dict[str, Any]:
        self.stop()
        requested = [str(value).strip().upper() for value in (symbols or []) if str(value).strip()]
        if not requested:
            from silver_bullet import is_commodity_symbol

            requested = [
                str(entry["symbol"]).upper()
                for entry in service.watchlist()
                if is_commodity_symbol(str(entry.get("symbol", "")))
            ]
        self.symbols = list(dict.fromkeys(requested))
        self.signals = []
        self.scan_date = anchor_date.isoformat()
        historical_now = datetime.combine(anchor_date, time(11, 0), tzinfo=self.NEW_YORK)
        await self._scan(anchor_date, historical_now)
        return self.status()

    def stop(self, manual: bool = False) -> dict[str, Any]:
        """Stop the live scan; ``manual`` marks a stop made by the user.

        A manual stop is remembered for the rest of the New York session so the
        in-window fallback (``_auto_loop``) does not re-arm the scan the user just
        stopped. Internal stops (a ``start``/``test`` restart) clear that marker.
        """
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        self.symbols = None
        self.next_check_at = None
        self.scan_date = None
        self.manual_stop_date = (
            datetime.now(self.NEW_YORK).date().isoformat() if manual else None
        )
        return self.status()

    async def _loop(self) -> None:
        while True:
            now = await self._check()
            if now.weekday() >= 5:
                # Commodities are weekday-only: nothing to watch on Sat/Sun.
                self.last_error = None
                self.next_check_at = None
                return
            if now.hour >= self.AM_WINDOW_END_HOUR:
                # 11:00 New York has passed: the AM window is over for this
                # session, so retire the live scan (status "running" -> False)
                # instead of idling all day. _auto_loop re-arms it at the next
                # 10:00 New York.
                self.next_check_at = None
                return
            # Wake just after the next 5-minute bar close.
            current = datetime.now(self.NEW_YORK)
            offset = (current.minute * 60 + current.second) % self.BAR_SECONDS
            seconds = (self.BAR_SECONDS + self.BAR_CLOSE_DELAY_SECONDS - offset) % self.BAR_SECONDS or self.BAR_SECONDS
            self.next_check_at = (datetime.now(self.NEW_YORK) + timedelta(seconds=seconds)).isoformat()
            await asyncio.sleep(max(1, seconds))

    async def _check(self) -> datetime:
        """Scan once if New York wall time is inside the AM window; return ``now``.

        The caller retires the scan once the window has closed, so the 10:00-11:00
        New York session is never scanned outside its own hours (plus one final
        scan within ``FINAL_SCAN_GRACE`` of 11:00 for the last bar's close).
        """
        now = datetime.now(self.NEW_YORK)
        self.last_check_at = now.isoformat()
        if now.weekday() >= 5:
            self.last_error = None
            return now
        window_end = now.replace(hour=self.AM_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
        if now.hour < self.AM_WINDOW_START_HOUR or now >= window_end + self.FINAL_SCAN_GRACE:
            self.last_error = None
            return now
        await self._scan(now.date(), now)
        return now

    async def _scan(self, scan_date: date, now: datetime) -> None:
        from market_data.sources import tradingview_source
        from silver_bullet import evaluate_am_silver_bullet

        fresh: list[dict[str, Any]] = []
        failures: list[str] = []
        for symbol in self.symbols or []:
            try:
                rows = await asyncio.to_thread(
                    tradingview_source.fetch_timeframe,
                    symbol,
                    scan_date,
                    scan_date,
                    "5m",
                    symbol.split(":", 1)[0] if ":" in symbol else None,
                )
                if not rows:
                    failures.append(f"{symbol}: TradingView returned 0 5m bars for {scan_date}")
                    continue
                signal = evaluate_am_silver_bullet(symbol, rows, trading_date=scan_date, now=now)
                if signal is None:
                    continue
                payload = asdict(signal)
                payload["id"] = f"{signal.symbol}|{signal.direction}|{signal.signal_time}"
                fresh.append(payload)
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
        known = {str(item.get("id")) for item in self.signals}
        self.signals = [*self.signals, *(item for item in fresh if item["id"] not in known)]
        self.signals = self.signals[-100:]
        self.last_error = "; ".join(failures) if failures else None
        self.run_count += 1


class IPOScanner:
    """Periodically detect newly-listed NSE stocks from bhavcopy and register them.

    Automation design
    -----------------
    - Runs on an interval (default hourly). Only acts once the NSE daily bar is
      ready (after 17:00 IST on a trading day), because IPO detection needs the
      final bhavcopy.
    - Builds the *already listed* baseline from the most recent completed NSE
      Universe so established stocks are never mistaken for new listings, then
      scans a trailing ``lookback_days`` window from that baseline for symbols
      that are brand new -> potential IPOs.
    - New candidates are auto-registered (watchlist + category scope=IPO +
      ipo_metadata). OHLC backfill is NOT run automatically to avoid bulk
      downloads; callers can trigger ``/api/market-data/ipo/backfill`` explicitly
      (per repo convention to ask before long-running work).
    - Candidates must pass the IPO eligibility gate in ``market_data.ipo``
      (NSE -> main-board equity master -> ``EQ`` series -> traded recently ->
      liquidity threshold), so bonds, Sovereign Gold Bonds, ETFs/funds and
      rights entitlements are never registered.
    """

    def __init__(self, lookback_days: int = 7) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = 60
        self.lookback_days = max(1, int(lookback_days))
        self.last_ran_at: str | None = None
        self.last_error: str | None = None
        self.run_count = 0

    def start(self) -> dict[str, Any]:
        self.stop()
        self.task = asyncio.create_task(self._loop())
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "interval_minutes": self.interval_minutes,
            "lookback_days": self.lookback_days,
            "last_ran_at": self.last_ran_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = f"{exc.__class__.__name__}: {exc}"
            await asyncio.sleep(self.interval_minutes * 60)

    def run_once(self) -> dict[str, Any]:
        try:
            bar_ready = self.service.module.is_daily_bar_ready(self.service.module.Session.NSE)
        except Exception as exc:  # pragma: no cover - defensive
            self.last_error = f"market-ready check failed: {exc}"
            return {"skipped": True, "reason": self.last_error}
        if not bar_ready:
            self.last_error = "NSE daily bar not ready yet; IPO scan deferred"
            return {"skipped": True, "reason": self.last_error}

        from market_data import ipo as ipo_service
        from market_data.config import db_path

        today = date.today()
        end = today - timedelta(days=1)  # most recent completed trading day
        baseline = ipo_service.known_symbols_from_bhavcopy(end)
        if not baseline:
            self.last_error = "No baseline bhavcopy universe available; IPO scan skipped"
            return {"skipped": True, "reason": self.last_error}
        start = end - timedelta(days=self.lookback_days)
        candidates = ipo_service.discover_new_ipos(start, today, known_symbols=baseline, db_path=db_path())
        registered = ipo_service.register_ipos(candidates, db_path=db_path()) if candidates else []
        self.last_ran_at = datetime.now().astimezone().isoformat()
        self.run_count += 1
        self.last_error = None
        return {
            "baseline_date": end.isoformat(),
            "window_start": start.isoformat(),
            "window_end": today.isoformat(),
            "candidates": candidates,
            "registered": registered,
        }

    @property
    def service(self) -> Any:
        return service


ipo_scanner = IPOScanner()


class LtfConfirmationWatcher:
    """Intraday (1h/15m) CISD confirmation of armed daily setups.

    Arming: once a market's daily bar is final (NSE after the 17:00 IST
    bhavcopy; forex/commodities after the NY 17:00 rollover) the enabled
    LTF-capable strategies run over that market's watchlist symbols for the
    session, and every row carrying an ``ltf_*`` zone is stored as an armed
    setup (data/state/ltf_setups.json via ``ltf_confirmation.LtfSetupStore``).

    Confirmation: each poll fetches intraday bars from TradingView for armed
    symbols only and replays the completed bars through
    ``ltf_confirmation.evaluate_ltf`` (the same pure function a future intraday
    backtest would use). While a market is closed the last fetched bars are
    reused, so closed sessions cost no requests. Transitions (armed, triggered,
    invalidated, expired) are kept as alerts for the UI.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = app_settings.DEFAULTS["automation"]["ltf_confirmation"]["interval_minutes"]
        self.last_check_at: str | None = None
        self.last_error: str | None = None
        self.last_arm: dict[str, str] = {}
        self.alerts: list[dict[str, Any]] = []
        self.run_count = 0
        self._bars: dict[str, Any] = {}
        self._fetched_while_closed: set[str] = set()

    @staticmethod
    def _timeframe() -> str:
        return app_settings.load_settings()["strategy"]["ltf_timeframe"]

    def start(self, interval_minutes: int | None = None) -> dict[str, Any]:
        self.stop()
        if interval_minutes is not None:
            self.interval_minutes = interval_minutes
        self.task = asyncio.create_task(self._loop())
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        return self.status()

    def status(self) -> dict[str, Any]:
        from ltf_confirmation import LtfSetupStore

        # Only live states go to the UI; invalidated/expired stay in the store.
        setups = sorted(
            (s for s in LtfSetupStore().load().values() if s.state in ("armed", "triggered")),
            key=lambda s: s.updated_at or "", reverse=True,
        )
        return {
            "running": self.task is not None and not self.task.done(),
            "timeframe": self._timeframe(),
            "interval_minutes": self.interval_minutes,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "last_arm": dict(self.last_arm),
            "run_count": self.run_count,
            "armed_count": sum(1 for s in setups if s.state == "armed"),
            "alerts": self.alerts[-50:],
            "setups": [{k: v for k, v in asdict(s).items() if k != "events"} for s in setups[:200]],
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # keep the watcher alive across one bad poll
                logging.getLogger(__name__).exception("LTF confirmation poll failed")
                self.last_error = str(exc)
            await asyncio.sleep(self.interval_minutes * 60)

    async def check(self, force_arm: bool = False) -> dict[str, Any]:
        """Arm any newly final sessions, then replay intraday bars for armed setups."""
        self.last_check_at = datetime.now(timezone.utc).isoformat()
        errors = await self._arm_due(force_arm)
        errors += await self._confirm()
        self.last_error = "; ".join(errors) if errors else None
        self.run_count += 1
        return self.status()

    def _alert(self, setup: Any, state: str) -> None:
        self.alerts.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": setup.symbol, "strategy": setup.strategy, "state": state,
            "direction": setup.direction, "entry": setup.entry, "sl": setup.sl,
            "target": setup.target, "note": setup.note,
        })
        self.alerts = self.alerts[-100:]

    async def _arm_due(self, force: bool) -> list[str]:
        from ltf_confirmation import LtfSetupStore, market_for_symbol
        from market_data.service import latest_final_session

        by_market: dict[str, list[str]] = {}
        for entry in service.watchlist():
            symbol = str(entry.get("symbol", "")).strip().upper()
            if symbol and not symbol.startswith("CRYPTO:"):
                by_market.setdefault(market_for_symbol(symbol), []).append(symbol)
        errors: list[str] = []
        for market, symbols in by_market.items():
            source = "NSE" if market == "NSE" else "TRADINGVIEW"
            session = latest_final_session(source, symbol=symbols[0])
            if session is None or (not force and self.last_arm.get(market) == session.isoformat()):
                continue
            try:
                setups = await asyncio.to_thread(strategy_bridge.collect_ltf_setups, symbols, session)
                for setup in LtfSetupStore().arm(setups):
                    self._alert(setup, "armed")
                self.last_arm[market] = session.isoformat()
            except Exception as exc:
                errors.append(f"arm {market}: {exc}")
        return errors

    async def _confirm(self) -> list[str]:
        import ict_scanner  # type: ignore  (src/ is on sys.path via strategy_bridge)
        from ltf_confirmation import LtfSetupStore, bars_from_rows, evaluate_ltf
        from market_data.sources import tradingview_source

        timeframe = self._timeframe()
        store = LtfSetupStore()
        now = datetime.now(timezone.utc)
        by_symbol: dict[str, list[Any]] = {}
        for setup in store.active():
            by_symbol.setdefault(setup.symbol, []).append(setup)

        errors: list[str] = []
        for symbol, setups in by_symbol.items():
            session = ict_scanner.Session.NSE if setups[0].market == "NSE" else ict_scanner.Session.FOREX_24_5
            market_open = ict_scanner.is_market_open(session)
            key = f"{symbol}|{timeframe}"
            start = min(date.fromisoformat(s.signal_date) for s in setups) + timedelta(days=1)
            # Open market: fetch every poll (the completed-bar filter makes
            # repeats harmless). Closed: once, to pick up the session's last bars.
            if start > date.today():
                need_fetch = False  # the session after the signal has not started yet
            elif market_open:
                self._fetched_while_closed.discard(key)
                need_fetch = True
            else:
                need_fetch = key not in self._fetched_while_closed
            if need_fetch:
                try:
                    rows = await asyncio.to_thread(
                        tradingview_source.fetch_timeframe, symbol, start, date.today(), timeframe,
                        symbol.split(":", 1)[0] if ":" in symbol else "NSE",
                    )
                    self._bars[key] = bars_from_rows(rows)
                    if not market_open:
                        self._fetched_while_closed.add(key)
                except Exception as exc:
                    errors.append(f"{symbol}: {exc}")
            bars = self._bars.get(key)
            if bars is None:
                bars = bars_from_rows([])
            for setup in setups:
                changed = store.apply(setup.key, evaluate_ltf(setup, bars, now, timeframe))
                if changed is not None:
                    self._alert(changed, changed.state)
        return errors


ltf_watcher = LtfConfirmationWatcher()


service = ScannerService()
scheduler = ScanScheduler(service)
silver_bullet_scanner = SilverBulletLiveScanner()
app = FastAPI(title="ICT Scanner API", version="1.0.0")


@app.on_event("startup")
async def start_silver_bullet_auto_schedule() -> None:
    """Arm the AM Silver Bullet scheduler on boot.

    The scheduler used to start only when a client fetched /api/silver-bullet, so
    a page load was required before a live scan could run. On startup the loop
    arms a scan immediately when New York wall time is inside the window, or
    sleeps until 10:00 New York otherwise.
    """
    settings = app_settings.load_settings()
    if settings["automation"]["silver_bullet_auto"]["enabled"]:
        silver_bullet_scanner.start_auto_schedule()
    apply_automation(settings, on_boot=True)


@app.on_event("shutdown")
async def stop_silver_bullet_auto_schedule() -> None:
    if silver_bullet_scanner.auto_task is not None:
        silver_bullet_scanner.auto_task.cancel()
        silver_bullet_scanner.auto_task = None
    ltf_watcher.stop()


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
        return {"symbols": service.add_to_watchlist(request.symbol, request.category, request.classification)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/watchlist/fno")
def get_fno_list_info() -> dict[str, Any]:
    """Saved NSE F&O list: when it was last refreshed and how many symbols."""
    return fno_membership.cache_info()


@app.post("/api/watchlist/fno/preview")
def preview_fno_refresh() -> dict[str, Any]:
    """Download NSE's F&O list and show the watchlist changes; writes nothing."""
    try:
        return fno_membership.preview(service.module)
    except Exception as exc:  # network / NSE format / incomplete list
        raise HTTPException(status_code=502, detail=f"Could not load the NSE F&O list: {exc}") from exc


@app.post("/api/watchlist/fno/apply")
def apply_fno_refresh(request: FnoApplyRequest) -> dict[str, Any]:
    """Save the previewed F&O list locally and re-tag the watchlist."""
    try:
        result = fno_membership.apply(service.module, request.preview_id)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**result, "symbols": service.watchlist()}


@app.delete("/api/watchlist")
def remove_watchlist_item(request: WatchlistRemoveRequest) -> dict[str, Any]:
    try:
        symbols = service.remove_from_watchlist(request.symbol)
        purged = service.purge_symbol_data(request.symbol) if request.delete_data else None
        return {"symbols": symbols, "purged": purged}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/watchlist")
def rename_watchlist_item(request: WatchlistRenameRequest) -> dict[str, Any]:
    try:
        symbols = service.rename_in_watchlist(request.old_symbol, request.new_symbol, request.category, request.classification)
        old_symbol = request.old_symbol.strip().upper()
        renamed_away = all(item["symbol"] != old_symbol for item in symbols)
        purged = service.purge_symbol_data(old_symbol) if request.delete_old_data and renamed_away else None
        return {"symbols": symbols, "purged": purged}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
            return await asyncio.to_thread(
                strategy_bridge.run_scan,
                request.symbols,
                request.strategies,
                request.anchor_date,
                request.timeframe,
            )
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


def apply_automation(settings: dict[str, Any], on_boot: bool = False) -> None:
    """Start/stop/retune each background automation to match ``settings``."""
    auto = settings["automation"]

    sched = auto["scan_scheduler"]
    running = scheduler.task is not None and not scheduler.task.done()
    if sched["enabled"]:
        if not running or scheduler.interval_minutes != sched["interval_minutes"]:
            scheduler.start(sched["interval_minutes"], scheduler.symbols)
    elif running:
        scheduler.stop()

    sb = auto["silver_bullet_auto"]
    if sb["enabled"]:
        silver_bullet_scanner.start_auto_schedule()
    elif silver_bullet_scanner.auto_task is not None:
        silver_bullet_scanner.auto_task.cancel()
        silver_bullet_scanner.auto_task = None

    ipo = auto["ipo_scanner"]
    ipo_running = ipo_scanner.task is not None and not ipo_scanner.task.done()
    if ipo["enabled"]:
        changed = (ipo_scanner.interval_minutes, ipo_scanner.lookback_days) != (ipo["interval_minutes"], ipo["lookback_days"])
        ipo_scanner.interval_minutes = ipo["interval_minutes"]
        ipo_scanner.lookback_days = ipo["lookback_days"]
        if not ipo_running or changed:
            ipo_scanner.start()
    elif ipo_running:
        ipo_scanner.stop()

    sync = auto["data_auto_sync"]
    syncer = _auto_sync()
    sync_running = syncer.status()["running"]
    if sync["enabled"]:
        changed = (syncer.lookback_days, syncer.interval_hours) != (sync["lookback_days"], sync["interval_hours"])
        if not sync_running or changed:
            syncer.start(sync["lookback_days"], sync["interval_hours"])
    elif sync_running:
        syncer.stop()

    ltf = auto["ltf_confirmation"]
    ltf_running = ltf_watcher.task is not None and not ltf_watcher.task.done()
    if ltf["enabled"] and (not ltf_running or ltf_watcher.interval_minutes != ltf["interval_minutes"]):
        ltf_watcher.start(ltf["interval_minutes"])
    elif not ltf["enabled"] and ltf_running:
        ltf_watcher.stop()


def _settings_payload(settings: dict[str, Any]) -> dict[str, Any]:
    strategies, master = strategy_bridge.list_strategies()
    return {
        "settings": settings,
        "strategies": strategies,
        "weekly_profiles_master_enabled": master,
        "status": {
            "scan_scheduler": scheduler.status(),
            "silver_bullet": {"auto_armed": silver_bullet_scanner.auto_task is not None and not silver_bullet_scanner.auto_task.done()},
            "ipo_scanner": ipo_scanner.status(),
            "data_auto_sync": _auto_sync().status(),
            "ltf_confirmation": {
                "running": ltf_watcher.task is not None and not ltf_watcher.task.done(),
                "last_check_at": ltf_watcher.last_check_at,
                "last_error": ltf_watcher.last_error,
            },
        },
        "hideable_pages": list(app_settings.HIDEABLE_PAGES),
        "strategy_choices": {key: list(values) for key, values in app_settings.STRATEGY_CHOICES.items()},
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _settings_payload(app_settings.load_settings())


@app.put("/api/settings")
async def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial settings document, persist it, and apply the automation changes."""
    settings = app_settings.save_settings(patch)
    apply_automation(settings)
    return _settings_payload(settings)


@app.get("/api/ltf-confirmation")
def get_ltf_confirmation() -> dict[str, Any]:
    """Armed / triggered intraday-confirmation setups and watcher status."""
    return ltf_watcher.status()


@app.post("/api/ltf-confirmation/check")
async def check_ltf_confirmation() -> dict[str, Any]:
    """Re-arm from the latest final daily session and replay intraday bars now."""
    return await ltf_watcher.check(force_arm=True)


@app.get("/api/schedule")
def get_schedule() -> dict[str, Any]:
    return scheduler.status()


@app.post("/api/schedule/start")
async def start_schedule(request: ScheduleStartRequest) -> dict[str, Any]:
    return scheduler.start(request.interval_minutes, request.symbols)


@app.post("/api/schedule/stop")
async def stop_schedule() -> dict[str, Any]:
    return scheduler.stop()


@app.get("/api/silver-bullet")
async def get_silver_bullet_status() -> dict[str, Any]:
    if app_settings.load_settings()["automation"]["silver_bullet_auto"]["enabled"]:
        silver_bullet_scanner.start_auto_schedule()
    return silver_bullet_scanner.status()


@app.post("/api/silver-bullet/start")
async def start_silver_bullet(request: SilverBulletStartRequest) -> dict[str, Any]:
    return silver_bullet_scanner.start(request.symbols)


@app.post("/api/silver-bullet/test")
async def test_silver_bullet(request: SilverBulletTestRequest) -> dict[str, Any]:
    try:
        return await silver_bullet_scanner.test(request.anchor_date, request.symbols)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/silver-bullet/stop")
async def stop_silver_bullet() -> dict[str, Any]:
    return silver_bullet_scanner.stop(manual=True)


@app.get("/api/ipo-scan")
def get_ipo_scanner_status() -> dict[str, Any]:
    return ipo_scanner.status()


# Scheduling is owned by Settings -> Automation -> IPO scanner (persisted);
# this only runs one scan now.
@app.post("/api/ipo-scan/run-once")
async def run_ipo_scan_once() -> dict[str, Any]:
    return await asyncio.to_thread(ipo_scanner.run_once)


class LiquidityScreenRequest(BaseModel):
    lookback_days: int = Field(default=60, ge=1, le=250)


@app.post("/api/ipo-liquidity/screen")
async def run_liquidity_screen(request: LiquidityScreenRequest) -> dict[str, Any]:
    """Screen all IPO-scope symbols for liquidity and market presence.

    Returns screening results for each symbol. Read-only: nothing is removed.
    """
    try:
        results = await asyncio.to_thread(
            screen_all_ipos,
            lookback_days=request.lookback_days,
        )
        return {"count": len(results), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/markets")
def get_markets() -> dict[str, Any]:
    return {
        "nse": bool(service.module.is_nse_market_open()),
        "forex_commodities": bool(service.module.is_forex_24_5_open()),
    }


