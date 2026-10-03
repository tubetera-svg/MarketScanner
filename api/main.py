from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ICT_PATH = ROOT / "src" / "ict_scanner.py"
for _extra_path in (str(ROOT), str(ROOT / "api"), str(ROOT / "src")):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

import app_settings  # noqa: E402  (persisted automation / show-hide settings)
import auth  # noqa: E402  (admin sign-in, read-only guest role)
import fno_membership  # noqa: E402  (NSE F&O list cache + watchlist F&O re-check)
import news_calendar  # noqa: E402  (TradingView/ForexFactory news + EIA inventory times, fetched once per IST day)
import price_alerts  # noqa: E402  (chart-popup price alerts, in-app delivery)
import push  # noqa: E402  (Telegram / ntfy pushes shared by the automations)
import strategy_bridge  # noqa: E402  (strategy profiles panel: lives in the api folder)
from market_data import favorites, nse_holidays  # noqa: E402  (starred symbols shared by scanner / watchlist / IPO pages)
from market_data.automation_state import AutomationState  # noqa: E402  (restart-safe "already done" markers)
from market_data.routes import router as market_data_router, _auto_sync  # noqa: E402
from market_data.service import ensure_backdate_data, ist_today  # noqa: E402
from market_data.timeutil import DISPLAY_TIMEZONES, fmt_display, parse_instant, utc_now  # noqa: E402
from market_data.liquidity_screener import screen_all_ipos  # noqa: E402


class StrategyScanRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, max_length=500)
    strategies: list[str] | None = Field(default=None, max_length=50)
    anchor_date: date | None = None
    timeframe: Literal["daily", "weekly", "15m", "1h", "4h"] = "weekly"
    include_context: bool = True
    include_bias: bool = True


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


class FavoriteRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)


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

    def __init__(self, state: AutomationState | None = None) -> None:
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
        self.pusher = push.Pusher("Silver Bullet")
        # Signal ids already pushed this New York date; outlives start()/stop() so a
        # re-armed scan does not re-send the morning's signals.
        self._pushed: tuple[str, set[str]] = ("", set())
        # With ``state`` the manual stop and pushed ids also survive an API restart.
        self._state = state
        if state is not None:
            saved = state.load()
            self.manual_stop_date = saved.get("manual_stop_date") or None
            self._pushed = (str(saved.get("pushed_date") or ""), set(saved.get("pushed_ids") or []))

    def _save_state(self) -> None:
        if self._state is not None:
            self._state.save({
                "manual_stop_date": self.manual_stop_date,
                "pushed_date": self._pushed[0],
                "pushed_ids": sorted(self._pushed[1]),
            })

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
        manual_stop_date = datetime.now(self.NEW_YORK).date().isoformat() if manual else None
        if manual_stop_date != self.manual_stop_date:
            self.manual_stop_date = manual_stop_date
            self._save_state()
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
        fresh = await self._scan(now.date(), now)
        if self._pushed[0] != now.date().isoformat():
            self._pushed = (now.date().isoformat(), set())
        unsent = [s for s in fresh if s.get("id") not in self._pushed[1]]
        if unsent:
            self._pushed[1].update(s.get("id") for s in unsent)
            self._save_state()
        zone = app_settings.display_timezone()
        await self.pusher.push(
            f"{s['symbol']} {s['direction']} @ {fmt_display(s['signal_time'], zone)}; entry {s['entry']:g}, SL {s['stop_loss']:g}, TP {s['target']:g}"
            for s in unsent
        )
        return now

    async def _scan(self, scan_date: date, now: datetime) -> list[dict[str, Any]]:
        """Scan the symbols for ``scan_date``; returns the signals not seen before."""
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
        fresh = [item for item in fresh if item["id"] not in known]
        self.signals = [*self.signals, *fresh][-100:]
        self.last_error = "; ".join(failures) if failures else None
        self.run_count += 1
        return fresh


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

    def __init__(self, lookback_days: int = 7, state: AutomationState | None = None) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = 60
        self.lookback_days = max(1, int(lookback_days))
        self.last_error: str | None = None
        self.run_count = 0
        # With ``state`` the last successful scan survives a restart, so the loop
        # waits out the rest of the interval instead of scanning again at once.
        self._state = state
        self.last_ran_at: str | None = (state.load().get("last_ran_at") or None) if state is not None else None

    def _first_wait_seconds(self) -> float:
        """Seconds left of the interval since the last successful scan (0 = run now)."""
        if not self.last_ran_at:
            return 0.0
        last = parse_instant(self.last_ran_at)  # legacy naive values are IST
        if last is None:
            return 0.0
        elapsed = (utc_now() - last).total_seconds()
        return max(0.0, self.interval_minutes * 60 - elapsed)

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
        wait = self._first_wait_seconds()
        if wait > 0:
            await asyncio.sleep(wait)
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

        today = ist_today()
        end = today - timedelta(days=1)  # most recent completed trading day
        baseline = ipo_service.known_symbols_from_bhavcopy(end)
        if not baseline:
            self.last_error = "No baseline bhavcopy universe available; IPO scan skipped"
            return {"skipped": True, "reason": self.last_error}
        start = end - timedelta(days=self.lookback_days)
        candidates = ipo_service.discover_new_ipos(start, today, known_symbols=baseline, db_path=db_path())
        registered = ipo_service.register_ipos(candidates, db_path=db_path()) if candidates else []
        self.last_ran_at = utc_now().isoformat(timespec="seconds")
        if self._state is not None:
            self._state.save({"last_ran_at": self.last_ran_at})
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


ipo_scanner = IPOScanner(state=AutomationState("ipo_scanner"))


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

    def __init__(self, state: AutomationState | None = None) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = app_settings.DEFAULTS["automation"]["ltf_confirmation"]["interval_minutes"]
        self.last_check_at: str | None = None
        self.last_error: str | None = None
        # market -> last armed session; with ``state`` it survives a restart, so an
        # already armed session is not re-armed (the manual check still forces it).
        self._state = state
        saved = state.load().get("last_arm", {}) if state is not None else {}
        self.last_arm: dict[str, str] = {str(k): str(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
        # "symbol|timeframe" -> {"through": last session fetched, "from": fetch start}
        # for the one fetch made while the market is closed; persisted so a restart
        # does not refetch every armed symbol. Bars themselves stay in memory only.
        saved_closed = state.load().get("closed_fetch", {}) if state is not None else {}
        self._closed_fetch: dict[str, dict[str, str]] = (
            {str(k): dict(v) for k, v in saved_closed.items() if isinstance(v, dict)}
            if isinstance(saved_closed, dict) else {}
        )
        self.alerts: list[dict[str, Any]] = []
        self.run_count = 0
        self._bars: dict[str, Any] = {}
        self.pusher = push.Pusher("LTF confirmation")

    def _save_state(self) -> None:
        if self._state is not None:
            self._state.save({"last_arm": self.last_arm, "closed_fetch": self._closed_fetch})

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
        alerts_before = len(self.alerts)
        errors = await self._arm_due(force_arm)
        errors += await self._confirm()
        self.last_error = "; ".join(errors) if errors else None
        self.run_count += 1
        # Same events the scanner page plays its LTF sound for.
        await self.pusher.push(
            f"{a['symbol']} {a['strategy']} {a['direction']} triggered; entry {a['entry']}, SL {a['sl']}, TP {a['target']}"
            for a in self.alerts[alerts_before:] if a["state"] == "triggered"
        )
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
                self._save_state()
            except Exception as exc:
                errors.append(f"arm {market}: {exc}")
        return errors

    async def _confirm(self) -> list[str]:
        import ict_scanner  # type: ignore  (src/ is on sys.path via strategy_bridge)
        from ltf_confirmation import STATE_EXPIRED, LtfSetupStore, bars_from_rows, evaluate_ltf
        from market_data import service as md_service
        from market_data.sources import tradingview_source

        timeframe = self._timeframe()
        store = LtfSetupStore()
        now = datetime.now(timezone.utc)
        by_symbol: dict[str, list[Any]] = {}
        for setup in store.active():
            by_symbol.setdefault(setup.symbol, []).append(setup)
        closed_before = dict(self._closed_fetch)
        # Markers only for symbols that still have armed setups: no back-dated entries.
        self._closed_fetch = {k: v for k, v in self._closed_fetch.items() if k.split("|", 1)[0] in by_symbol}

        errors: list[str] = []
        for symbol, setups in by_symbol.items():
            nse = setups[0].market == "NSE"
            session = ict_scanner.Session.NSE if nse else ict_scanner.Session.FOREX_24_5
            market_open = ict_scanner.is_market_open(session)
            key = f"{symbol}|{timeframe}"
            start = min(date.fromisoformat(s.signal_date) for s in setups) + timedelta(days=1)
            # Open market: fetch every poll (the completed-bar filter makes
            # repeats harmless). Closed: once per session, to pick up its last bars.
            today = ist_today()  # TV intraday bars are labelled in IST
            sessions = md_service.expected_trading_dates("NSE" if nse else "TRADINGVIEW", start, today, symbol)
            marker = {"through": sessions[-1].isoformat(), "from": start.isoformat()} if sessions else None
            if marker is None:
                need_fetch = False  # no session since the signal yet (weekend/holiday/not started)
            elif market_open:
                self._closed_fetch.pop(key, None)
                need_fetch = True
            else:
                done = self._closed_fetch.get(key) or {}
                # A setup armed later with an earlier start needs its own fetch.
                covered = done.get("through") == marker["through"] and str(done.get("from") or "~") <= marker["from"]
                need_fetch = not covered
            fetched = False
            if need_fetch:
                try:
                    rows = await asyncio.to_thread(
                        tradingview_source.fetch_timeframe, symbol, start, today, timeframe,
                        symbol.split(":", 1)[0] if ":" in symbol else "NSE",
                    )
                    self._bars[key] = bars_from_rows(rows)
                    fetched = True
                except Exception as exc:
                    errors.append(f"{symbol}: {exc}")
            bars = self._bars.get(key)
            for setup in setups:
                result = evaluate_ltf(setup, bars if bars is not None else bars_from_rows([]), now, timeframe)
                # Without bars in memory (closed-market fetch skipped after a restart)
                # the stored state already is the replay result; only the date-based
                # expiry can still change it.
                if bars is None and result["state"] != STATE_EXPIRED:
                    continue
                changed = store.apply(setup.key, result)
                if changed is not None:
                    self._alert(changed, changed.state)
            if fetched and not market_open and marker is not None:
                self._closed_fetch[key] = marker
        if self._closed_fetch != closed_before:
            self._save_state()
        return errors


ltf_watcher = LtfConfirmationWatcher(AutomationState("ltf_confirmation"))
price_alert_watcher = price_alerts.PriceAlertWatcher()


service = ScannerService()
silver_bullet_scanner = SilverBulletLiveScanner(AutomationState("silver_bullet"))
app = FastAPI(title="ICT Scanner API", version="1.0.0")


@app.on_event("startup")
async def start_silver_bullet_auto_schedule() -> None:
    """Arm the AM Silver Bullet scheduler on boot.

    The scheduler used to start only when a client fetched /api/silver-bullet, so
    a page load was required before a live scan could run. On startup the loop
    arms a scan immediately when New York wall time is inside the window, or
    sleeps until 10:00 New York otherwise.
    """
    apply_automation(app_settings.load_settings(), on_boot=True)  # arms the scheduler when enabled


@app.on_event("shutdown")
async def stop_silver_bullet_auto_schedule() -> None:
    if silver_bullet_scanner.auto_task is not None:
        silver_bullet_scanner.auto_task.cancel()
        silver_bullet_scanner.auto_task = None
    ltf_watcher.stop()
    price_alert_watcher.stop()


# --- Read-only guests (api/auth.py) ------------------------------------------
# Guests may only read (GET), except the routes in _GUEST_WRITES; reads that
# belong to a page the admin has not opened to guests are refused too. Any
# other write is admin-only, including routes added later.
_GUEST_PAGE_READS = {
    "watchlist": ("/api/market-data/records", "/api/market-data/meta", "/api/market-data/aliases",
                  "/api/market-data/watchlist", "/api/market-data/turso-sync", "/api/watchlist/fno"),
    "ipo": ("/api/market-data/ipo", "/api/ipo-scan", "/api/ipo-liquidity"),
    "alerts": ("/api/price-alerts",),
}
# path -> page that must be open to guests (None = always allowed)
_GUEST_WRITES: dict[str, str | None] = {
    "/api/auth/login": None,
    "/api/strategy-scan": "scanner",
    "/api/strategy-scan/cancel": "scanner",
    "/api/backtest": "backtest",
}
# Query flags that force remote fetches (TradingView/NSE/news) - admin only.
_GUEST_BLOCKED_FLAGS = ("refresh", "auto_fetch")
GUEST_DENIED = "Read-only guest access: sign in as admin to do this."
_access_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _access_settings() -> dict[str, Any]:
    """Settings 'access' block, cached briefly (read on every guest request)."""
    now = monotonic()
    if _access_cache["value"] is None or now - _access_cache["at"] > 10:
        _access_cache.update(at=now, value=app_settings.load_settings()["access"])
    return _access_cache["value"]


def _guest_allowed(method: str, path: str, query: Any) -> bool:
    pages = set(_access_settings()["guest_pages"])
    if method in ("GET", "HEAD"):
        for page, prefixes in _GUEST_PAGE_READS.items():
            if page not in pages and any(path == p or path.startswith(p + "/") for p in prefixes):
                return False
        return not any(str(query.get(flag, "")).lower() in ("1", "true", "yes") for flag in _GUEST_BLOCKED_FLAGS)
    if path not in _GUEST_WRITES:
        return False
    page = _GUEST_WRITES[path]
    return page is None or page in pages


@app.middleware("http")
async def access_control(request: Request, call_next: Any) -> Any:
    if request.method == "OPTIONS":  # CORS preflight
        return await call_next(request)
    role = await asyncio.to_thread(auth.role_for, request)
    request.state.role = role
    path = request.url.path.rstrip("/") or "/"
    if role == "guest" and not await asyncio.to_thread(_guest_allowed, request.method, path, request.query_params):
        return JSONResponse({"detail": GUEST_DENIED}, status_code=403)
    return await call_next(request)


def _role(request: Request) -> str:
    return getattr(request.state, "role", "guest")


app.add_middleware(
    CORSMiddleware,
    # CORS_ORIGINS: comma-separated extra origins for hosted frontends (e.g. Vercel).
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"]
    + [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
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


@app.get("/api/favorites")
def get_favorites() -> dict[str, Any]:
    return {"symbols": favorites.load_favorites()}


@app.post("/api/favorites")
def add_favorite(request: FavoriteRequest) -> dict[str, Any]:
    try:
        return {"symbols": favorites.add_favorite(request.symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/favorites")
def remove_favorite(request: FavoriteRequest) -> dict[str, Any]:
    try:
        return {"symbols": favorites.remove_favorite(request.symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        favorites.remove_favorites([request.symbol])
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
        if renamed_away:
            favorites.rename_favorite(old_symbol, service.module.categorize_symbol(request.new_symbol)["symbol"])
        purged = service.purge_symbol_data(old_symbol) if request.delete_old_data and renamed_away else None
        return {"symbols": symbols, "purged": purged}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


_strategy_scan_active = False
# Who is running the current strategy scan: role, ip, symbol count, start time and
# whether an admin scan stopped it (guest scans give way to the admin).
_scan_owner: dict[str, Any] = {}
_guest_last_scan: dict[str, float] = {}  # ip -> monotonic start of its last scan
ADMIN_PREEMPT_WAIT_SECONDS = 30


def _guest_scan_request(request: StrategyScanRequest, ip: str) -> StrategyScanRequest:
    """Apply the guest limits (Settings -> Guest access) to a scan request."""
    access = _access_settings()
    cooldown = access["guest_scan_cooldown_seconds"]
    wait = int(cooldown - (monotonic() - _guest_last_scan.get(ip, -1e9)))
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"Guests can scan once every {cooldown} s - try again in {wait} s.")
    watchlist = {str(item.get("symbol", "")).upper() for item in service.watchlist()}
    symbols = list(dict.fromkeys(str(s).strip().upper() for s in request.symbols or [] if str(s).strip()))
    if any(symbol not in watchlist for symbol in symbols):
        raise HTTPException(status_code=403, detail="Guests can only scan watchlist symbols.")
    limit = access["guest_max_symbols"]
    if len(symbols) > limit:
        raise HTTPException(status_code=400, detail=f"Guests can scan up to {limit} symbols at a time ({len(symbols)} selected) - narrow the selection, e.g. with a group filter.")
    if request.anchor_date and not access["guest_past_dates"] and request.anchor_date < ist_today():
        raise HTTPException(status_code=403, detail="Past-date scans are admin only.")
    enabled = {item["name"] for item in strategy_bridge.list_strategies()[0] if item["enabled"]}
    strategies = [name for name in request.strategies or [] if name in enabled] or None  # None = every enabled strategy
    extra = bool(access["guest_extra_info"])
    return request.model_copy(update={
        "symbols": symbols,
        "strategies": strategies,
        "include_context": request.include_context and extra,
        "include_bias": request.include_bias and extra,
    })


def _scan_busy_detail() -> str:
    if not _strategy_scan_active:
        return "Market data is syncing - try again in a minute."
    started = int(monotonic() - _scan_owner.get("started", monotonic()))
    return f"Another scan is running ({_scan_owner.get('symbols', '?')} symbols, started {started} s ago) - try again shortly."


@app.post("/api/strategy-scan")
async def run_strategy_scan(request: StrategyScanRequest, http_request: Request) -> dict[str, Any]:
    global _strategy_scan_active
    role, ip = _role(http_request), auth.client_ip(http_request)
    if role == "guest":
        request = _guest_scan_request(request, ip)
    if service.lock.locked():
        # The admin goes first: stop a running guest scan (it checks the flag per symbol).
        if role == "admin" and _strategy_scan_active and _scan_owner.get("role") == "guest":
            _scan_owner["preempted"] = True
            strategy_bridge.cancel_scan()
            try:
                await asyncio.wait_for(service.lock.acquire(), timeout=ADMIN_PREEMPT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=409, detail="A guest scan is still stopping - try again in a few seconds.")
            service.lock.release()
        else:
            raise HTTPException(status_code=409, detail=_scan_busy_detail())
    if service.lock.locked():  # someone else got in between
        raise HTTPException(status_code=409, detail=_scan_busy_detail())
    async with service.lock:
        strategy_bridge.reset_cancel()
        _strategy_scan_active = True
        _scan_owner.clear()
        _scan_owner.update(role=role, ip=ip, symbols=len(request.symbols or []), started=monotonic(), preempted=False)
        if role == "guest":
            _guest_last_scan[ip] = monotonic()
        scan = asyncio.ensure_future(asyncio.to_thread(
            strategy_bridge.run_scan,
            request.symbols,
            request.strategies,
            request.anchor_date,
            request.timeframe,
            request.include_context,
            request.include_bias,
        ))
        try:
            # Page reload / tab close drops the connection: stop the worker thread
            # (it checks the flag per symbol) instead of letting it run to completion.
            while not scan.done():
                await asyncio.wait({scan}, timeout=0.5)
                if not scan.done() and await http_request.is_disconnected():
                    strategy_bridge.cancel_scan()
                    logging.getLogger(__name__).info("Strategy scan cancelled: client disconnected")
            return scan.result()
        except strategy_bridge.ScanCancelled:
            if _scan_owner.get("preempted"):
                raise HTTPException(status_code=409, detail="Scan stopped: the admin started a scan. Try again in a minute.")
            raise HTTPException(status_code=499, detail="Strategy scan cancelled")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            _strategy_scan_active = False
            strategy_bridge.reset_cancel()


@app.post("/api/strategy-scan/cancel")
def cancel_strategy_scan(http_request: Request) -> dict[str, Any]:
    """Stop the running strategy scan (Stop button / page unload beacon). Guests can only stop their own."""
    if not _strategy_scan_active:
        return {"cancelled": False}
    if _role(http_request) == "guest" and (_scan_owner.get("role") != "guest" or _scan_owner.get("ip") != auth.client_ip(http_request)):
        raise HTTPException(status_code=403, detail=GUEST_DENIED)
    strategy_bridge.cancel_scan()
    return {"cancelled": True}


@app.post("/api/backtest")
async def run_backtest_endpoint(request: BacktestRequest, http_request: Request) -> dict[str, Any]:
    """Run a backtest over the selected strategies + symbol universe.

    Ensures the required historical window is present in SQLite (best-effort,
    never blocks on failure), then replays each strategy point-in-time via
    ``src/backtest`` and returns per-strategy + combined reports.
    """
    if _role(http_request) == "guest":
        # Same limits as a guest scan (watchlist only, symbol cap, cooldown, enabled strategies).
        limited = _guest_scan_request(StrategyScanRequest(symbols=request.symbols, strategies=request.strategies), auth.client_ip(http_request))
        if not limited.strategies:
            raise HTTPException(status_code=403, detail="Guests can only backtest enabled strategies.")
        request = request.model_copy(update={"symbols": limited.symbols, "strategies": limited.strategies})
        _guest_last_scan[auth.client_ip(http_request)] = monotonic()
    if service.lock.locked():
        raise HTTPException(status_code=409, detail=_scan_busy_detail())
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
        "generated_at": utc_now().isoformat(timespec="seconds"),
    }


def apply_automation(settings: dict[str, Any], on_boot: bool = False) -> None:
    """Start/stop/retune each background automation to match ``settings``."""
    auto = settings["automation"]

    sb = auto["silver_bullet_auto"]
    silver_bullet_scanner.pusher.enabled = sb["push"]
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
    ltf_watcher.pusher.enabled = ltf["push"]
    ltf_running = ltf_watcher.task is not None and not ltf_watcher.task.done()
    if ltf["enabled"] and (not ltf_running or ltf_watcher.interval_minutes != ltf["interval_minutes"]):
        ltf_watcher.start(ltf["interval_minutes"])
    elif not ltf["enabled"] and ltf_running:
        ltf_watcher.stop()

    alerts = auto["price_alerts"]
    price_alert_watcher.near_pct = alerts["near_pct"]
    price_alert_watcher.pusher.enabled = alerts["push"]
    if alerts["enabled"] and (not price_alert_watcher.running or price_alert_watcher.interval_minutes != alerts["interval_minutes"]):
        price_alert_watcher.start(alerts["interval_minutes"])
    elif not alerts["enabled"] and price_alert_watcher.running:
        price_alert_watcher.stop()


def _settings_payload(settings: dict[str, Any]) -> dict[str, Any]:
    strategies, master = strategy_bridge.list_strategies()
    return {
        "settings": settings,
        "strategies": strategies,
        "weekly_profiles_master_enabled": master,
        "status": {
            "silver_bullet": {
                "auto_armed": silver_bullet_scanner.auto_task is not None and not silver_bullet_scanner.auto_task.done(),
                "last_push_error": silver_bullet_scanner.pusher.last_error,
            },
            "ipo_scanner": ipo_scanner.status(),
            "data_auto_sync": _auto_sync().status(),
            "ltf_confirmation": {
                "running": ltf_watcher.task is not None and not ltf_watcher.task.done(),
                "last_check_at": ltf_watcher.last_check_at,
                "last_error": ltf_watcher.last_error,
                "last_push_error": ltf_watcher.pusher.last_error,
            },
            "price_alerts": {
                "running": price_alert_watcher.running,
                "last_check_at": price_alert_watcher.last_check_at,
                "last_error": price_alert_watcher.last_error,
                "push_channels": push.channels(),
                "last_push_error": price_alert_watcher.pusher.last_error,
            },
        },
        "hideable_pages": list(app_settings.HIDEABLE_PAGES),
        "strategy_choices": {key: list(values) for key, values in app_settings.STRATEGY_CHOICES.items()},
        "news_currencies": list(app_settings.NEWS_CURRENCIES),
        "sound_choices": list(app_settings.SOUND_CHOICES),
        "display_timezones": [{"value": value, "label": label} for value, label in DISPLAY_TIMEZONES.items()],
        # Info only: the NSE (CM segment) calendar shared by every NSE-session consumer.
        "nse_holidays": nse_holidays.info(),
    }


@app.get("/api/settings")
def get_settings(http_request: Request) -> dict[str, Any]:
    settings = app_settings.load_settings()
    if _role(http_request) == "guest":
        # Display-only subset: no automation status, push channels or errors.
        return {"settings": {key: settings[key] for key in ("ui", "sounds", "access")}}
    return _settings_payload(settings)


@app.put("/api/settings")
async def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial settings document, persist it, and apply the automation changes."""
    settings = app_settings.save_settings(patch)
    _access_cache["value"] = None  # guest rules pick up the change at once
    apply_automation(settings)
    return _settings_payload(settings)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class PasswordRequest(BaseModel):
    password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=200)


@app.get("/api/auth/me")
def auth_me(http_request: Request) -> dict[str, Any]:
    """Current role (admin/guest), whether it comes from localhost, and the guest rules the UI follows."""
    return {
        "role": _role(http_request),
        "local": auth.is_local(http_request),
        "password_set": auth.password_set(),
        "access": _access_settings(),
    }


@app.post("/api/auth/login")
def auth_login(request: LoginRequest, http_request: Request) -> dict[str, Any]:
    ip = auth.client_ip(http_request)
    wait = auth.login_blocked_for(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Too many wrong passwords - try again in {max(1, wait // 60)} min.")
    if not auth.password_set():
        raise HTTPException(status_code=409, detail="No admin password is set yet - set one in Settings > Access on localhost, then push settings to Turso.")
    ok = auth.check_password(request.password)
    auth.record_login(ip, ok)
    if not ok:
        raise HTTPException(status_code=401, detail="Wrong password")
    days = _access_settings()["session_days"]
    return {"token": auth.issue_token(days), "days": days}


@app.put("/api/auth/password")
def auth_set_password(request: PasswordRequest, http_request: Request) -> dict[str, Any]:
    """Admin only (middleware). Replaces the signing key, so every other device is signed out;
    a remote admin gets a fresh token back to stay signed in."""
    try:
        auth.set_password(request.password)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if auth.is_local(http_request):
        return {"token": None}
    return {"token": auth.issue_token(_access_settings()["session_days"])}


@app.get("/api/news/high-impact")
def get_high_impact_news(refresh: bool = False) -> dict[str, Any]:
    """High-impact events + Crude/NatGas release times (TradingView, ForexFactory fallback; cached per IST day)."""
    currencies = app_settings.load_settings()["news"]["currencies"]
    return news_calendar.get_events(currencies, refresh=refresh)


@app.get("/api/price-alerts")
def get_price_alerts(http_request: Request) -> dict[str, Any]:
    """All price alerts, recent trigger events, and watcher status (guests: no push/error details)."""
    status = price_alert_watcher.status()
    if _role(http_request) == "guest":
        for key in ("push_enabled", "push_channels", "last_push_error", "last_error"):
            status.pop(key, None)
    return status


@app.post("/api/price-alerts")
def create_price_alert(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return price_alerts.create(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/price-alerts/batch")
def create_price_alerts(body: dict[str, Any]) -> dict[str, Any]:
    """Create several alerts at once (e.g. a scanner setup's levels); all or nothing."""
    try:
        return {"alerts": price_alerts.create_many(list(body.get("alerts") or []))}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/price-alerts/bulk")
def bulk_price_alerts(body: dict[str, Any]) -> dict[str, int]:
    """Pause / resume (re-arm) / delete many alerts: {"ids": [...], "action": ...}."""
    try:
        return {"count": price_alerts.bulk([str(i) for i in body.get("ids") or []], str(body.get("action")))}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/price-alerts/test-push")
async def test_price_alert_push() -> dict[str, Any]:
    """Send a test message to the configured push channels (env vars)."""
    channels = push.channels()
    if not channels:
        raise HTTPException(status_code=400, detail="No push channel configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or NTFY_TOPIC")
    errors = await asyncio.to_thread(push.send, "Test message from Market Scanner.", "Push test")
    return {"channels": channels, "errors": errors}


@app.put("/api/price-alerts/{alert_id}")
def update_price_alert(alert_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        alert = price_alerts.update(alert_id, body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


@app.delete("/api/price-alerts/{alert_id}")
def delete_price_alert(alert_id: str) -> dict[str, bool]:
    if not price_alerts.delete(alert_id):
        raise HTTPException(status_code=404, detail="alert not found")
    return {"deleted": True}


@app.post("/api/price-alerts/check")
async def check_price_alerts() -> dict[str, Any]:
    """Evaluate all active alerts now (ignores the interval)."""
    return await price_alert_watcher.check(force=True)


@app.get("/api/ltf-confirmation")
def get_ltf_confirmation() -> dict[str, Any]:
    """Armed / triggered intraday-confirmation setups and watcher status."""
    return ltf_watcher.status()


@app.post("/api/ltf-confirmation/check")
async def check_ltf_confirmation() -> dict[str, Any]:
    """Re-arm from the latest final daily session and replay intraday bars now."""
    return await ltf_watcher.check(force_arm=True)


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


