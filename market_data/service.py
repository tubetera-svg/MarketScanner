"""Reusable OHLC retrieval service (cache-aside over SQLite).

get_ohlc(source, symbol, start_date, end_date):
    1. Check SQLite first.
    2. If every expected trading date in the range is stored -> return from
       SQLite; no API call.
    3. Otherwise fetch ONLY the missing dates from the correct source module
       (NSE: only missing bhavcopy files; TradingView: one call for the
       missing window).
    4. Store fetched rows in SQLite (duplicates impossible via UNIQUE +
       ON CONFLICT DO NOTHING).
    5. Return the complete requested range read back from SQLite.
    6. When AUTO_FETCH_MISSING_DATA is false, or the source flag is off,
       return local data only / raise a descriptive error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import database
from .config import (
    SOURCE_NSE,
    SOURCE_TRADINGVIEW,
    auto_fetch_missing,
    load_symbol_aliases,
    normalize_source,
    source_enabled,
    source_flag_name,
)
from .errors import FetchError, NoDataError

log = logging.getLogger(__name__)

FetchCallable = Callable[[dict], list[dict]]

_FETCHERS: dict[str, FetchCallable] = {}


@dataclass
class OhlcResult:
    source: str
    symbol: str
    exchange: str
    start_date: str
    end_date: str
    rows: list[dict] = field(default_factory=list)
    cached: bool = False
    fetched_new: int = 0
    missing_dates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "count": len(self.rows),
            "cached": self.cached,
            "fetched_new": self.fetched_new,
            "missing_dates": self.missing_dates,
            "notes": self.notes,
            "rows": self.rows,
        }


def _default_fetchers() -> dict[str, FetchCallable]:
    """Wire real sources lazily so importing this package stays cheap."""
    global _FETCHERS
    if not _FETCHERS:
        from .sources import nse_source, tradingview_source

        def fetch_nse(spec: dict) -> list[dict]:
            return nse_source.fetch_daily(
                spec["symbol"], spec["start"], spec["end"], dates=spec.get("dates"),
                store_symbol=spec.get("store_symbol"),
            )

        def fetch_tradingview(spec: dict) -> list[dict]:
            return tradingview_source.fetch_daily(
                spec["symbol"], spec["start"], spec["end"], exchange=spec.get("exchange"),
                store_symbol=spec.get("store_symbol"),
            )

        _FETCHERS = {SOURCE_NSE: fetch_nse, SOURCE_TRADINGVIEW: fetch_tradingview}
    return _FETCHERS


def register_fetcher(source: str, fetcher: FetchCallable) -> None:
    """Replace/override a source fetcher (used by tests and bootstrap)."""
    _FETCHERS[normalize_source(source)] = fetcher


def reset_fetchers() -> None:
    """Clear fetcher overrides so real sources are wired again."""
    global _FETCHERS
    _FETCHERS = {}


def expected_trading_dates(source: str, start: date, end: date) -> list[date]:
    """Expected daily bars between start and end (never in the future)."""
    today = date.today()
    end = min(end, today)
    if start > end:
        return []
    if normalize_source(source) == SOURCE_NSE:
        from .sources import nse_source

        return nse_source.expected_trading_dates(start, end)
    from .sources import tradingview_source

    return tradingview_source.expected_trading_dates(start, end)


def _exchange_for(source_name: str, symbol: str) -> str:
    if source_name == SOURCE_NSE:
        return "NSE"
    from .sources import tradingview_source

    exchange, _ = tradingview_source.split_symbol(symbol)
    return exchange


def resolve_session_source(symbol: str, session: object = None) -> str:
    """Map a watchlist entry ('NSE:INFY', 'OANDA:XAUUSD', session) to a source."""
    value = str(symbol or "").strip().upper()
    session_text = str(getattr(session, "value", session) or "").strip().lower()
    if ":" in value:
        prefix = value.split(":", 1)[0].strip()
        return SOURCE_NSE if prefix == "NSE" else SOURCE_TRADINGVIEW
    return SOURCE_NSE if session_text in {"nse", ""} else SOURCE_TRADINGVIEW


def get_ohlc(
    source: str,
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    *,
    auto_fetch: Optional[bool] = None,
    aliases: Optional[list[str]] = None,
    db_path=None,
) -> OhlcResult:
    """Return daily OHLC for symbol between start_date and end_date (inclusive).

    See module docstring for the exact cache-aside behaviour.
    """
    source_name = normalize_source(source)
    start = date.fromisoformat(str(start_date).strip()) if isinstance(start_date, str) else start_date
    end = date.fromisoformat(str(end_date).strip()) if isinstance(end_date, str) else end_date
    if start > end:
        raise ValueError("start_date must be on or before end_date")

    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    exchange = _exchange_for(source_name, sym)

    database.init_db(db_path)

    rows = database.query_ohlc(source_name, sym, start, end, exchange=exchange, db_path=db_path)
    have = {row["date"] for row in rows}
    expected = expected_trading_dates(source_name, start, end)
    known_no_data = database.no_data_dates(
        source_name, sym, start, end, exchange=exchange, db_path=db_path
    )
    missing = [
        day
        for day in expected
        if day.isoformat() not in have and day.isoformat() not in known_no_data
    ]

    result = OhlcResult(
        source=source_name,
        symbol=sym,
        exchange=exchange,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        rows=rows,
        cached=not missing,
        missing_dates=[day.isoformat() for day in missing],
    )
    if not missing:
        return result

    # ---- Data is missing: decide whether we may fetch -------------------
    may_auto_fetch = auto_fetch_missing() if auto_fetch is None else bool(auto_fetch)
    if not may_auto_fetch:
        result.notes.append("AUTO_FETCH_MISSING_DATA=false: returning local data only.")
        if not result.rows:
            raise NoDataError(
                f"No local OHLC for {source_name} {sym} between {start} and {end}; "
                f"automatic fetching is disabled via AUTO_FETCH_MISSING_DATA=false.",
                detail={"missing_dates": result.missing_dates},
            )
        return result

    if not source_enabled(source_name):
        flag = source_flag_name(source_name)
        result.notes.append(f"{flag}=false: fetching from this source is disabled.")
        if not result.rows:
            raise NoDataError(
                f"No local OHLC for {source_name} {sym} between {start} and {end}; "
                f"fetching disabled via {flag}.",
                detail={"missing_dates": result.missing_dates},
            )
        return result

    # ---- Fetch only what is missing -------------------------------------
    # Try the primary symbol first, then any configured aliases (stored under
    # the original symbol) so a symbol rename/relisting upstream doesn't break
    # sync. Aliases are only attempted when the primary raises or returns no
    # rows for the requested window.
    candidates = [sym] + [a for a in (aliases or []) if a and a.strip().upper() != sym]
    fetched_rows: list[dict] = []
    last_exc: Exception | None = None
    for candidate in candidates:
        spec = {
            "symbol": candidate,
            "exchange": exchange,
            "start": min(missing),
            "end": max(missing),
            "dates": list(missing),
            "store_symbol": sym,
        }
        try:
            rows = _default_fetchers()[source_name](spec)
        except Exception as exc:
            last_exc = exc
            log.warning("Fetch candidate %s failed for %s (%s): %s", candidate, sym, source_name, exc)
            continue
        in_range = [row for row in rows if start.isoformat() <= str(row["date"]) <= end.isoformat()]
        if in_range:
            fetched_rows = in_range
            if candidate != sym:
                result.notes.append(
                    f"Fetched via alias {candidate} (primary {sym} returned no data)."
                )
            break
    if not fetched_rows:
        exc = last_exc or RuntimeError("no upstream data for any candidate")
        log.error("Fetching %s data for %s failed: %s", source_name, sym, exc)
        raise FetchError(
            f"Failed to fetch {source_name} data for {sym}: {exc}",
            rows=result.rows,
            missing_dates=result.missing_dates,
        ) from exc

    in_range = fetched_rows
    inserted = database.upsert_ohlc(in_range, db_path=db_path)

    still_missing = [
        day.isoformat()
        for day in missing
        if day.isoformat() not in {str(row["date"]) for row in in_range}
    ]
    if still_missing:
        # Remember confirmed holidays/gaps so later calls do not re-hit the API.
        database.mark_no_data(
            [
                {"source": source_name, "symbol": sym, "exchange": exchange, "date": day}
                for day in still_missing
            ],
            db_path=db_path,
        )

    result.rows = database.query_ohlc(
        source_name, sym, start, end, exchange=exchange, db_path=db_path
    )
    result.fetched_new = inserted
    result.missing_dates = still_missing
    result.cached = False
    if still_missing:
        result.notes.append(
            "Some dates had no upstream data (likely market holidays): "
            + ", ".join(still_missing)
        )
    log.info(
        "get_ohlc %s %s %s..%s -> %d rows (%d new, %d unresolved)",
        source_name, sym, start, end, len(result.rows), inserted, len(still_missing),
    )
    return result


def session_for_source(source: str) -> Optional[object]:
    """Map a data source to its ict_scanner.Session (for market-hours checks)."""
    name = normalize_source(source)
    if name not in (SOURCE_NSE, SOURCE_TRADINGVIEW):
        return None
    try:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        if str(root / "src") not in sys.path:
            sys.path.insert(0, str(root / "src"))
        import ict_scanner  # type: ignore

        return ict_scanner.Session.NSE if name == SOURCE_NSE else ict_scanner.Session.FOREX_24_5
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not resolve session for source %s: %s", source, exc)
        return None


def _last_completed_trading_day(source: str, end_date: date) -> date | None:
    """Latest expected trading day strictly before `end_date` (used for gating).

    When a market is still open, the in-progress session's bar is incomplete, so
    sync is deferred to this date until the exchange's hours close.
    """
    today = date.today()
    horizon = min(end_date, today)
    # Scan the last ~2 weeks of expected sessions to find the most recent one
    # that closed before today (the in-progress session is excluded).
    candidates = expected_trading_dates(source, horizon - timedelta(days=12), horizon)
    completed = [day for day in candidates if day < today]
    return completed[-1] if completed else None


def sync_symbol_range(
    source: str,
    symbol: str,
    end_date: date | str,
    lookback_days: int = 14,
    *,
    gate_market_hours: bool = False,
    aliases: Optional[list[str]] = None,
    db_path=None,
) -> OhlcResult:
    """Ensure the last `lookback_days` calendar days up to end_date are stored.

    Used by the backdate-test hook and the bootstrap CLI. Failures are logged,
    never raised, so callers (e.g. historical tests) keep running.

    - ``gate_market_hours``: when True, the window is shrunk to the last
      *completed* session unless the current day's final bar is already
      available. NSE bars become available once the bhavcopy is published
      (after 17:00 IST); commodity/forex bars are deferred to the next day so an
      incomplete in-progress bar is never stored.
    - ``aliases``: fallback symbols attempted (stored under the original symbol)
      when the primary fetch returns nothing.
    """
    end = date.fromisoformat(str(end_date).strip()) if isinstance(end_date, str) else end_date
    start = end - timedelta(days=max(1, lookback_days) - 1)
    gated = False
    if gate_market_hours:
        session = session_for_source(source)
        if session is not None:
            try:
                import ict_scanner  # type: ignore

                if ict_scanner.is_daily_bar_ready(session):
                    # The day's final bar is published/available (e.g. NSE bhavcopy
                    # after 17:00 IST). Make sure an earlier "no data" marker does
                    # not permanently block today's bar from being re-fetched.
                    try:
                        database.clear_no_data(
                            source_name,
                            str(symbol).upper(),
                            end,
                            exchange=_exchange_for(source_name, str(symbol)),
                            db_path=db_path,
                        )
                    except Exception:  # pragma: no cover - defensive
                        pass
                else:
                    # Bar not final yet: sync only up to the last completed session
                    # (NSE before bhavcopy is ready, commodities/forex -> next day).
                    completed = _last_completed_trading_day(source, end)
                    if completed is not None and completed < end:
                        end = completed
                        gated = True
                        log.info(
                            "Sync gated for %s (%s): daily bar not final yet, deferring to %s",
                            symbol, source, end,
                        )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Market-hours gating skipped for %s: %s", symbol, exc)
    try:
        result = get_ohlc(source, symbol, start, end, aliases=aliases, db_path=db_path)
        if gated:
            result.notes.append(
                "Synced up to the last completed session (market still open; "
                "today's bar will be backfilled after the exchange closes)."
            )
        return result
    except Exception as exc:
        log.warning("sync_symbol_range failed for %s %s: %s", source, symbol, exc)
        return OhlcResult(
            source=normalize_source(source),
            symbol=str(symbol).upper(),
            exchange=_exchange_for(normalize_source(source), str(symbol)),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            notes=[f"sync failed: {exc}"],
        )


def ensure_backdate_data(
    entries: list[tuple[str, object]],
    anchor_date: date | str,
    *,
    lookback_days: Optional[int] = None,
    db_path=None,
) -> dict:
    """Fetch+store OHLC for watchlist entries before a backdate test runs.

    For every (symbol, session) entry, ensures the last `lookback_days`
    calendar days up to anchor_date exist in SQLite (fetching only what is
    missing). Individual failures are logged and reported, never raised.
    """
    from .config import backdate_lookback_days as _default_lookback

    window = lookback_days or _default_lookback()
    alias_map = load_symbol_aliases()
    synced, failed = [], []
    for symbol, session in entries:
        source_name = resolve_session_source(symbol, session)
        summary = sync_symbol_range(
            source_name,
            symbol,
            anchor_date,
            window,
            aliases=alias_map.get(str(symbol).strip().upper()),
            db_path=db_path,
        )
        if any(note.startswith("sync failed") for note in summary.notes):
            failed.append({"symbol": symbol, "source": source_name, "error": "; ".join(summary.notes)})
        else:
            synced.append(
                {
                    "symbol": symbol,
                    "source": source_name,
                    "rows": len(summary.rows),
                    "fetched_new": summary.fetched_new,
                }
            )
    log.info(
        "Backdate data sync for %s: %d ok, %d failed (window=%dd)",
        anchor_date, len(synced), len(failed), window,
    )
    return {"anchor_date": str(anchor_date), "lookback_days": window, "synced": synced, "failed": failed}


__all__ = [
    "OhlcResult",
    "ensure_backdate_data",
    "get_ohlc",
    "register_fetcher",
    "reset_fetchers",
    "resolve_session_source",
    "session_for_source",
    "sync_symbol_range",
]

