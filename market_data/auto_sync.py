"""Per-market automatic daily OHLC sync.

Each market is synced once its newest daily bar becomes final, using that
market's own cut-off (see ``service.latest_final_session``):

- NSE: after the bhavcopy is published (17:00 IST on a trading day).
- TradingView forex/commodities: after the 17:00 New York daily rollover
  (≈02:30 IST in US summer time, ≈03:30 IST in winter).
- Crypto (CRYPTO:*, 24x7): after the UTC day closes (00:00 UTC = 05:30 IST),
  every day including weekends.
- GIFT Nifty (NSEIX:*): at 03:00 IST the next day (session ends 02:45 IST).

A background thread wakes every ``TICK_SECONDS``; for each market it syncs only
when a newer final session exists than the last one synced. Only missing dates
are fetched (cache-aside via ``get_ohlc``), so re-runs are cheap. Data sync
only — no strategy/scan execution.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Optional

from .config import backdate_lookback_days, load_symbol_aliases, source_enabled, source_flag_name
from .service import is_crypto_symbol, is_nseix_symbol, latest_final_session, resolve_session_source, sync_symbol_range

log = logging.getLogger(__name__)

# Retries for a market whose new bar was not yet available upstream (e.g. a
# late bhavcopy) before giving up on that session until the next one.
MAX_ATTEMPTS_PER_SESSION = 8


class DataAutoSync:
    TICK_SECONDS = 15 * 60

    def __init__(self, entries_loader: Callable[[], list[tuple[str, object]]]) -> None:
        self._entries_loader = entries_loader
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self.lookback_days: int = backdate_lookback_days()
        self.last_run_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.run_count = 0
        # source -> per-market state
        self.markets: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ lifecycle
    def start(self, lookback_days: Optional[int] = None) -> dict[str, Any]:
        self.stop()
        if lookback_days:
            self.lookback_days = max(1, int(lookback_days))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="market-data-auto-sync", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self._thread is not None:
            self._stop.set()
            self._thread = None
        return self.status()

    def status(self) -> dict[str, Any]:
        markets = {}
        for market in sorted(self.markets):
            state = dict(self.markets[market])
            target = latest_final_session(state["source"], symbol=state.get("sample_symbol"))
            state["latest_final_session"] = target.isoformat() if target else None
            markets[market] = state
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "syncing": self._run_lock.locked(),
            "tick_minutes": self.TICK_SECONDS // 60,
            "lookback_days": self.lookback_days,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "markets": markets,
        }

    def _loop(self) -> None:
        stop = self._stop
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("Auto-sync tick failed: %s", exc)
            stop.wait(self.TICK_SECONDS)

    # ------------------------------------------------------------ work
    def run_once(self, now: Optional[datetime] = None) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "auto-sync already running"}
        try:
            # Market = source, except crypto and GIFT Nifty, which share the
            # TradingView source but have their own cut-offs.
            groups: dict[tuple[str, str], list[str]] = {}
            for symbol, session in self._entries_loader():
                source = resolve_session_source(symbol, session)
                market = "CRYPTO" if is_crypto_symbol(symbol) else "NSEIX" if is_nseix_symbol(symbol) else source
                groups.setdefault((market, source), []).append(symbol)
            alias_map = load_symbol_aliases()
            report: dict[str, Any] = {}
            for (market, source), symbols in groups.items():
                report[market] = self._sync_market(market, source, symbols, alias_map, now)
            self.last_run_at = datetime.now().astimezone().isoformat()
            self.run_count += 1
            self.last_error = None
            return report
        finally:
            self._run_lock.release()

    def _sync_market(
        self, market: str, source: str, symbols: list[str], alias_map: dict, now: Optional[datetime]
    ) -> dict[str, Any]:
        state = self.markets.setdefault(
            market, {"source": source, "symbols": 0, "last_synced_session": None, "attempts": 0, "last_result": None}
        )
        state["symbols"] = len(symbols)
        state["sample_symbol"] = symbols[0]
        if not source_enabled(source):
            state["last_result"] = f"{source_flag_name(source)}=false: skipped"
            return {"skipped": True, "reason": state["last_result"]}
        target: Optional[date] = latest_final_session(source, now, symbol=symbols[0])
        if target is None:
            return {"skipped": True, "reason": "no completed session"}
        target_iso = target.isoformat()
        if state["last_synced_session"] == target_iso:
            return {"skipped": True, "reason": f"already synced {target_iso}"}
        if state.get("pending_session") != target_iso:
            state["pending_session"] = target_iso
            state["attempts"] = 0

        ok = failed = have_target = 0
        for symbol in symbols:
            result = sync_symbol_range(
                source,
                symbol,
                target,
                self.lookback_days,
                gate_market_hours=True,
                aliases=alias_map.get(str(symbol).strip().upper()),
            )
            if any(note.startswith("sync failed") for note in result.notes):
                failed += 1
                continue
            ok += 1
            if any(str(row.get("date")) == target_iso for row in result.rows):
                have_target += 1
        state["attempts"] += 1
        state["last_result"] = (
            f"{target_iso}: {ok} ok, {failed} failed, {have_target}/{len(symbols)} have the new bar"
        )
        # Done once the new bar is actually available upstream (NSE bhavcopy is
        # all-or-nothing, so any hit means it is published); otherwise retry on
        # the next tick, up to MAX_ATTEMPTS_PER_SESSION.
        if have_target or state["attempts"] >= MAX_ATTEMPTS_PER_SESSION:
            state["last_synced_session"] = target_iso
            state["pending_session"] = None
        log.info("Auto-sync %s -> %s", market, state["last_result"])
        return {"target": target_iso, "ok": ok, "failed": failed, "have_target": have_target}


__all__ = ["DataAutoSync", "MAX_ATTEMPTS_PER_SESSION"]
