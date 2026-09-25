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

import strategy_bridge  # noqa: E402  (strategy profiles panel: lives in the api folder)
from market_data.routes import router as market_data_router  # noqa: E402
from market_data.service import ensure_backdate_data  # noqa: E402
from market_data.liquidity_screener import screen_all_ipos, remove_symbol_everywhere  # noqa: E402


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


class WatchlistRemoveRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)


class WatchlistRenameRequest(BaseModel):
    old_symbol: str = Field(min_length=1, max_length=80)
    new_symbol: str = Field(min_length=1, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    classification: dict[str, str] | None = None


WATCHLIST_CATEGORIES_PATH = ROOT / "config" / "watchlist_categories.json"


def save_watchlist_categories(categories: dict[str, dict[str, str]]) -> None:
    WATCHLIST_CATEGORIES_PATH.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2) + "\n",
        encoding="utf-8",
    )


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
        return self.module.load_watchlist_details(str(ROOT / "config" / "watchlist.txt"))

    def add_to_watchlist(self, value: str, category_label: str | None = None, classification: dict[str, str] | None = None) -> list[dict[str, str]]:
        try:
            category_info = self.module.categorize_symbol(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        symbol = category_info["symbol"]

        path = ROOT / "config" / "watchlist.txt"
        entries = self.module.load_watchlist(str(path))
        if any(existing_symbol.upper() == symbol for existing_symbol, _ in entries):
            raise ValueError(f"{symbol} is already in the watchlist")

        with path.open("a", encoding="utf-8") as file:
            file.write(f"{symbol}\n")
        categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
        defaults = {
            "asset_class": category_info["asset_class"],
            "exchange": category_info["exchange"],
            "scope": category_info["scope"],
            "f_and_o": "",
            "sector": "",
            "industry": "",
            "index": "",
            "market_cap": "",
            "liquidity": "",
            "price_range": "",
            "theme": "",
        }
        if category_label and category_label.strip():
            defaults["scope"] = category_label.strip()
        if classification:
            defaults.update({key.strip(): value.strip() for key, value in classification.items() if key.strip() and value.strip()})
        categories[symbol] = defaults
        save_watchlist_categories(categories)
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
        categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
        if categories.pop(symbol, None) is not None:
            save_watchlist_categories(categories)
        # Drop any alias mapping for the removed symbol.
        try:
            from market_data.config import load_symbol_aliases, save_symbol_aliases

            alias_map = load_symbol_aliases()
            if alias_map.pop(symbol, None) is not None:
                save_symbol_aliases(alias_map)
        except Exception:  # pragma: no cover - aliases are best-effort
            pass
        return self.watchlist()

    def rename_in_watchlist(self, old_value: str, new_value: str, category: str | None = None, classification: dict[str, str] | None = None) -> list[dict[str, str]]:
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
        if new_symbol != old_symbol and any(existing_symbol.upper() == new_symbol for existing_symbol, _ in entries):
            raise ValueError(f"{new_symbol} is already in the watchlist")
        with path.open("w", encoding="utf-8") as file:
            for existing_symbol, _ in entries:
                file.write(f"{(new_symbol if existing_symbol.upper() == old_symbol else existing_symbol)}\n")
        categories = self.module.load_watchlist_categories(str(WATCHLIST_CATEGORIES_PATH))
        old_category = categories.pop(old_symbol, {})
        new_category = dict(old_category)
        if category and category.strip():
            new_category["scope"] = category.strip()
        if classification:
            new_category.update({key.strip(): value.strip() for key, value in classification.items() if key.strip() and value.strip()})
        categories[new_symbol] = new_category
        save_watchlist_categories(categories)
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
        details = {item["symbol"]: item for item in self.module.load_watchlist_details(str(ROOT / "config" / "watchlist.txt"))}
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
            row.update({"symbol": tracker.symbol, "session": tracker.session.value, "tier": tracker.tier.value, "state": tracker.state.value, "scope": details.get(tracker.symbol, {}).get("scope", "")})
            results.append(row)
        self.last_results = results
        self.last_scan_at = datetime.now().astimezone().isoformat()
        return results

    def historical_test(self, requested_symbols: list[str] | None, anchor_date: date) -> dict[str, Any]:
        requested_date, resolved_date, reason = self.module.resolve_previous_working_date(anchor_date)
        entries = self.module.load_watchlist(str(ROOT / "config" / "watchlist.txt"))
        details = {item["symbol"]: item for item in self.module.load_watchlist_details(str(ROOT / "config" / "watchlist.txt"))}
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
    """Poll commodity 15-minute bars during the New York AM Silver Bullet window."""

    NEW_YORK = ZoneInfo("America/New_York")
    AM_WINDOW_START_HOUR = 10  # 10:00 New York: AM Silver Bullet window opens
    AM_WINDOW_END_HOUR = 11  # 11:00 New York: window closed, live scan retires
    AUTO_CHECK_SECONDS = 60 * 3  # confirm the live scan is running every 3 minutes (scan poll cadence)

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
        elif now >= window_start:
            return float(self.AUTO_CHECK_SECONDS)
        remaining = window_start.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        return max(1.0, remaining.total_seconds())

    async def _auto_loop(self) -> None:
        """Confirm every few minutes that a live scan is running in the NY AM window.

        The scan itself is unchanged (15-minute bars polled inside 10:00-11:00 NY);
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
                    self.AM_WINDOW_START_HOUR <= now.hour < self.AM_WINDOW_END_HOUR
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
            if now.hour >= self.AM_WINDOW_END_HOUR:
                # 11:00 New York has passed: the AM window is over for this
                # session, so retire the live scan (status "running" -> False)
                # instead of idling all day. _auto_loop re-arms it at the next
                # 10:00 New York.
                self.next_check_at = None
                return
            seconds = 180 - (datetime.now(self.NEW_YORK).second % 180)
            self.next_check_at = (datetime.now(self.NEW_YORK) + timedelta(seconds=seconds)).isoformat()
            await asyncio.sleep(max(1, seconds))

    async def _check(self) -> datetime:
        """Scan once if New York wall time is inside the AM window; return ``now``.

        The caller retires the scan once the window has closed, so the 10:00-11:00
        New York session is never scanned outside its own hours.
        """
        now = datetime.now(self.NEW_YORK)
        self.last_check_at = now.isoformat()
        if now.hour < self.AM_WINDOW_START_HOUR or now.hour >= self.AM_WINDOW_END_HOUR:
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
                    "15m",
                    symbol.split(":", 1)[0] if ":" in symbol else None,
                )
                if not rows:
                    failures.append(f"{symbol}: TradingView returned 0 15m bars for {scan_date}")
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
    silver_bullet_scanner.start_auto_schedule()


@app.on_event("shutdown")
async def stop_silver_bullet_auto_schedule() -> None:
    if silver_bullet_scanner.auto_task is not None:
        silver_bullet_scanner.auto_task.cancel()
        silver_bullet_scanner.auto_task = None


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


@app.delete("/api/watchlist")
def remove_watchlist_item(request: WatchlistRemoveRequest) -> dict[str, Any]:
    try:
        return {"symbols": service.remove_from_watchlist(request.symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/watchlist")
def rename_watchlist_item(request: WatchlistRenameRequest) -> dict[str, Any]:
    try:
        return {"symbols": service.rename_in_watchlist(request.old_symbol, request.new_symbol, request.category, request.classification)}
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


@app.get("/api/weekly-profile-tracker")
def weekly_profile_tracker(
    symbols: Optional[str] = None,
    all_: Optional[str] = None,
) -> dict[str, Any]:
    """Return tracked weekly-profile setups (cross-scan).

    Defaults to the last ``DEFAULT_RETENTION_DAYS`` days of setups. Pass
    ``?all=true`` to return the full unbounded store (mainly for debugging).

    Pass ``symbols`` (comma-separated) to restrict to a watchlist subset —
    used by the frontend to show only the symbols a strategy scan ran over.
    """
    try:
        from weekly_profile_tracker import (
            ACTIVE_STATES,
            DEFAULT_RETENTION_DAYS,
            ProfileTrackerStore,
        )

        store = ProfileTrackerStore()
        if all_ and str(all_).strip().lower() in {"1", "true", "yes"}:
            setups = store.all_setups()
        else:
            setups = store.recent_setups(DEFAULT_RETENTION_DAYS)
            # Automatic maintenance: drop older records so the JSON store does not
            # grow unboundedly.
            removed = store.prune_before(days=DEFAULT_RETENTION_DAYS)

        if symbols:
            wanted = {token.strip().upper() for token in symbols.split(",") if token.strip()}
            setups = [s for s in setups if str(s.get("symbol", "")).upper() in wanted]
        active = [s for s in setups if s.get("state") in ACTIVE_STATES]
        body: dict[str, Any] = {
            "active": active,
            "setups": setups,
            "active_count": len(active),
        }
        if all_ and str(all_).strip().lower() in {"1", "true", "yes"}:
            body["source"] = "all"
        else:
            body["source"] = "recent"
            body["pruned"] = removed if not symbols else None
        return body
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


class CrossScanTrackerUpdate(BaseModel):
    enabled: bool


@app.get("/api/cross-scan-tracker")
def get_cross_scan_tracker() -> dict[str, Any]:
    """Return whether the cross-scan setup tracker is enabled."""
    return strategy_bridge.get_tracker_settings()


@app.put("/api/cross-scan-tracker")
def update_cross_scan_tracker(request: CrossScanTrackerUpdate) -> dict[str, Any]:
    """Toggle the cross-scan setup tracker on/off (persisted)."""
    settings = strategy_bridge.set_cross_scan_tracker(request.enabled)
    return settings


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


class IPOScannerRequest(BaseModel):
    lookback_days: int = Field(default=7, ge=1, le=90)


@app.get("/api/ipo-scan")
def get_ipo_scanner_status() -> dict[str, Any]:
    return ipo_scanner.status()


@app.post("/api/ipo-scan/start")
async def start_ipo_scanner(request: IPOScannerRequest) -> dict[str, Any]:
    ipo_scanner.lookback_days = max(1, int(request.lookback_days))
    return ipo_scanner.start()


@app.post("/api/ipo-scan/stop")
async def stop_ipo_scanner() -> dict[str, Any]:
    return ipo_scanner.stop()


@app.post("/api/ipo-scan/run-once")
async def run_ipo_scan_once() -> dict[str, Any]:
    return await asyncio.to_thread(ipo_scanner.run_once)


class LiquidityScreenRequest(BaseModel):
    lookback_days: int = Field(default=60, ge=1, le=250)
    auto_remove: bool = False


@app.get("/api/ipo-liquidity/status")
async def get_liquidity_status() -> dict[str, Any]:
    """Get cached/latest liquidity screening results."""
    # Could add persistent storage later; for now just indicate endpoint exists
    return {"status": "ready", "endpoints": ["/api/ipo-liquidity/screen", "/api/ipo-liquidity/remove"]}


@app.post("/api/ipo-liquidity/screen")
async def run_liquidity_screen(request: LiquidityScreenRequest) -> dict[str, Any]:
    """Screen all IPO-scope symbols for liquidity and market presence.

    Returns screening results for each symbol. If auto_remove=true, symbols
    with REMOVE decision are purged from watchlist, categories, and database.
    """
    try:
        results = await asyncio.to_thread(
            screen_all_ipos,
            lookback_days=request.lookback_days,
            auto_remove=request.auto_remove,
        )
        return {"count": len(results), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/ipo-liquidity/remove")
async def remove_illiquid_symbol(request: WatchlistRemoveRequest) -> dict[str, Any]:
    """Manually remove a symbol from watchlist, categories, and all DB tables."""
    try:
        removed = await asyncio.to_thread(
            remove_symbol_everywhere,
            request.symbol,
        )
        return {"symbol": request.symbol.upper(), "removed": removed}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/markets")
def get_markets() -> dict[str, Any]:
    return {
        "nse": bool(service.module.is_nse_market_open()),
        "forex_commodities": bool(service.module.is_forex_24_5_open()),
    }


