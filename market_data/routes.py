"""FastAPI routes for the market-data layer.

Exposed paths:
    GET  /ohlc            ?source=NSE&symbol=RELIANCE&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
    GET  /api/ohlc        (alias, consistent with other /api endpoints)
    POST /api/market-data/sync   (optional bulk backfill trigger)
    GET  /api/market-data/records   ?scope=watchlist|all&source=&exchange=&q=&start_date=&end_date=&sort=asc|desc&limit=&offset=&symbols=NSE:A,OANDA:B
    GET  /api/market-data/meta      ?scope=watchlist|all
    GET  /api/market-data/watchlist (static symbol list powering the picker UI)

The router is included by api/main.py; all database/fetch logic lives in
market_data.service / market_data.database — nothing source-specific here.
The record browser (records/meta) is strictly READ-ONLY over SQLite: it never
touches market_data.service.get_ohlc or the source modules, so browsing can
never trigger upstream NSE/TradingView requests.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import database
from .config import (
    KNOWN_SOURCES,
    backdate_lookback_days,
    load_symbol_aliases,
    normalize_source,
    save_symbol_aliases,
)
from .errors import FetchError, MarketDataError, NoDataError
from .service import get_ohlc, resolve_session_source, sync_symbol_range

log = logging.getLogger(__name__)

router = APIRouter(tags=["market-data"])


def _serve_ohlc(
    source: str,
    symbol: str,
    start_date: date,
    end_date: date,
    auto_fetch: Optional[bool],
) -> dict:
    try:
        result = get_ohlc(
            source,
            symbol,
            start_date,
            end_date,
            auto_fetch=auto_fetch,
        )
    except NoDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FetchError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": str(exc),
                "rows": exc.rows,
                "missing_dates": exc.missing_dates,
            },
        ) from exc
    except MarketDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result.to_dict()


@router.get("/ohlc")
@router.get("/api/ohlc")
def read_ohlc(
    source: str = Query(..., description="Data source: NSE or TRADINGVIEW"),
    symbol: str = Query(..., min_length=1, max_length=80),
    start_date: date = Query(...),
    end_date: date = Query(...),
    auto_fetch: Optional[bool] = Query(
        default=None,
        description="One-off override of AUTO_FETCH_MISSING_DATA for this request.",
    ),
) -> dict:
    """Daily OHLC rows served from SQLite; missing dates auto-fetched per flags."""
    return _serve_ohlc(source, symbol.upper(), start_date, end_date, auto_fetch)


class SyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=500)
    source: Optional[str] = None
    anchor_date: Optional[date] = None
    lookback_days: Optional[int] = Field(default=None, ge=1, le=120)
    gate_market_hours: bool = Field(
        default=True,
        description="Sync completed sessions only, respecting each source's "
                    "data-availability window: NSE bars are fetched once the "
                    "bhavcopy is published (after 17:00 IST) and commodity/forex "
                    "bars are deferred to the next day. Set false to force a full "
                    "fetch of the current (possibly in-progress) day.",
    )
    use_aliases: bool = Field(
        default=True,
        description="Fall back to configured alias symbols (config/symbol_aliases.json) "
                    "when the primary fetch returns nothing.",
    )


class DeleteRecordsRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=500)
    source: Optional[str] = None
    exchange: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    delete_all: bool = Field(
        default=False,
        description="Required (with no symbol/source/exchange/date filter) to wipe "
                    "the entire market-data table before a full re-sync.",
    )


@router.post("/api/market-data/sync")
def sync_market_data(request: SyncRequest) -> dict:
    """Backfill OHLC into SQLite for symbols (defaults to the watchlist).

    Data synchronisation only — no strategy/scan execution. When
    ``gate_market_hours`` is set, each symbol's in-progress session is skipped
    (sync is allowed only after that exchange's hours). When ``use_aliases`` is
    set, any symbol configured in config/symbol_aliases.json is retried under its
    alias if the primary fetch returns nothing.
    """
    lookback = request.lookback_days or backdate_lookback_days()
    entries = _watchlist_entries()
    if request.symbols:
        wanted = {str(s).strip().upper() for s in request.symbols if str(s).strip()}
        entries = [entry for entry in entries if entry[0].upper() in wanted]
    if not entries:
        raise HTTPException(status_code=404, detail="No symbols resolved for sync")

    anchor = request.anchor_date or date.today()
    alias_map = load_symbol_aliases() if request.use_aliases else {}
    results = []
    for symbol, session in entries:
        source_name = (
            request.source.strip().upper()
            if request.source and request.source.strip().upper() in KNOWN_SOURCES
            else resolve_session_source(symbol, session)
        )
        aliases = alias_map.get(str(symbol).strip().upper()) if request.use_aliases else None
        summary = sync_symbol_range(
            source_name,
            symbol,
            anchor,
            lookback,
            gate_market_hours=request.gate_market_hours,
            aliases=aliases,
        )
        results.append(summary.to_dict())
    return {"anchor_date": anchor.isoformat(), "lookback_days": lookback, "results": results}


@router.delete("/api/market-data/records")
def delete_market_data(request: DeleteRecordsRequest) -> dict:
    """Remove stored OHLC rows (and no-data markers) to enable a re-sync.

    Provide ``symbols`` (and/or source/exchange/date filters) to delete a slice,
    or set ``delete_all=true`` with no filters to wipe the whole table.
    """
    if not request.delete_all and not (
        request.symbols or request.source or request.exchange or request.start_date or request.end_date
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide symbols to delete, or set delete_all=true to clear everything.",
        )
    try:
        deleted = database.delete_ohlc(
            symbols=[str(s).strip().upper() for s in request.symbols if str(s).strip()] or None,
            source=normalize_source(request.source) if request.source else None,
            exchange=str(request.exchange).strip().upper() if request.exchange else None,
            start_date=request.start_date,
            end_date=request.end_date,
            delete_all=request.delete_all,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "deleted": deleted,
        "symbols": [str(s).strip().upper() for s in request.symbols if str(s).strip()],
        "delete_all": bool(request.delete_all),
    }


def _watchlist_entries() -> list[tuple[str, object]]:
    """Best-effort watchlist load (symbol + session) without hard failures."""
    try:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        src_path = str(root / "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)
        import ict_scanner  # type: ignore

        return ict_scanner.load_watchlist(str(root / "config" / "watchlist.txt"))
    except Exception as exc:
        log.warning("Could not load watchlist for sync endpoint: %s", exc)
        return []


# --------------------------------------------------------- record browser API
def _serve_records(
    scope: str,
    source: Optional[str],
    exchange: Optional[str],
    symbol_query: Optional[str],
    date_query: Optional[str],
    start_date: Optional[date],
    end_date: Optional[date],
    sort: str,
    limit: int,
    offset: int,
    selected_symbols: Optional[list[str]] = None,
) -> dict:
    source_name = None
    if source:
        try:
            source_name = normalize_source(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be on or before end_date"
        )

    try:
        watchlist_symbols: Optional[list[str]] = None
        watchlist_size: Optional[int] = None
        notes: list[str] = []
        if scope == "watchlist":
            entries = _watchlist_entries()
            watchlist_symbols = [str(symbol).strip().upper() for symbol, _ in entries]
            watchlist_size = len(watchlist_symbols)
            if not watchlist_symbols:
                notes.append("Watchlist (config/watchlist.txt) is empty: nothing to show.")

        # Explicit selection narrows the scope's universe: intersected with the
        # watchlist when scope=watchlist, free-form when scope=all.
        effective_symbols: Optional[list[str]] = selected_symbols
        if scope == "watchlist":
            if selected_symbols is None:
                effective_symbols = watchlist_symbols
            else:
                allowed = set(watchlist_symbols or [])
                effective_symbols = [s for s in selected_symbols if s in allowed]

        if scope == "watchlist" and selected_symbols is not None:
            dropped = sorted(set(selected_symbols) - set(effective_symbols or []))
            if dropped:
                preview = ", ".join(dropped[:20]) + ("…" if len(dropped) > 20 else "")
                notes.append(f"Ignored (not part of the current scope): {preview}")

        if selected_symbols is not None and not effective_symbols:
            # Explicit selection matched nothing in this scope's universe.
            # query_ohlc_page treats [] as "no symbol filter", so short-circuit to
            # an honest empty page instead of silently returning every row.
            return {
                "scope": scope,
                "total": 0,
                "limit": limit,
                "offset": offset,
                "rows": [],
                "watchlist_size": watchlist_size,
                "watchlist_missing_in_db": (
                    sorted(set(selected_symbols)) if scope == "watchlist" else []
                ),
                "notes": notes + ["None of the requested symbols are part of the current scope."],
            }

        rows, total = database.query_ohlc_page(
            symbols=effective_symbols,
            source=source_name,
            exchange=exchange,
            symbol_contains=symbol_query,
            date_contains=date_query,
            start_date=start_date,
            end_date=end_date,
            order=sort,
            limit=limit,
            offset=offset,
        )

        missing: list[str] = []
        if scope == "watchlist" and effective_symbols is not None:
            present = set(
                database.distinct_symbols(
                    symbols=effective_symbols,
                    source=source_name,
                    exchange=exchange,
                    symbol_contains=symbol_query,
                    date_contains=date_query,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            missing = sorted(set(effective_symbols) - present)

        return {
            "scope": scope,
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": rows,
            "watchlist_size": watchlist_size,
            "watchlist_missing_in_db": missing,
            "notes": notes,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data records error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/market-data/records")
def read_records(
    scope: str = Query("watchlist", pattern="^(watchlist|all)$"),
    source: Optional[str] = Query(None, description="Data source: NSE or TRADINGVIEW."),
    exchange: Optional[str] = Query(None, max_length=40),
    symbol_query: Optional[str] = Query(
        None, alias="q", max_length=80,
        description="Case-insensitive substring of the stored (prefixed) symbol.",
    ),
    date_query: Optional[str] = Query(
        None, alias="date_contains", max_length=40,
        description="Case-insensitive substring of the date (e.g. '2026-08' or '-15').",
    ),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    symbols: Optional[str] = Query(
        None, alias="symbols", max_length=8000,
        description="Comma-separated explicit symbol filter; intersected with the "
                    "watchlist when scope=watchlist.",
    ),
) -> dict:
    """Browse stored daily OHLC rows. Read-only: never fetches upstream."""
    selected: Optional[list[str]] = None
    if symbols is not None and symbols.strip():
        seen: set[str] = set()
        selected = []
        for part in symbols.split(","):
            token = part.strip().upper()
            if token and token not in seen:
                seen.add(token)
                selected.append(token)
        if not selected:
            raise HTTPException(
                status_code=400, detail="symbols parameter contained no valid symbols"
            )
        if len(selected) > 500:
            raise HTTPException(
                status_code=400, detail="At most 500 symbols can be requested at once"
            )
    return _serve_records(
        scope, source, exchange, symbol_query, date_query, start_date, end_date, sort, limit, offset, selected
    )


@router.get("/api/market-data/meta")
def read_meta(scope: str = Query("all", pattern="^(watchlist|all)$")) -> dict:
    """Aggregates powering the records-browser filters and status strip."""
    try:
        symbols: Optional[list[str]] = None
        if scope == "watchlist":
            symbols = [str(symbol).strip().upper() for symbol, _ in _watchlist_entries()]
        _, total = database.query_ohlc_page(symbols=symbols, limit=1, offset=0)
        minimum, maximum = database.date_range(symbols=symbols)
        return {
            "scope": scope,
            "row_count": total,
            "min_date": minimum,
            "max_date": maximum,
            "sources": database.distinct_values("source", symbols=symbols),
            "exchanges": database.distinct_values("exchange", symbols=symbols),
            "rows_per_source": database.rows_per_source(symbols=symbols),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data meta error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/market-data/watchlist")
def read_watchlist() -> dict:
    """Static watchlist symbol list for the browser's symbol picker.

    Pure config read (config/watchlist.txt): touches neither SQLite nor any
    upstream provider, so requesting it never triggers data fetching.
    """
    try:
        symbols = sorted({str(symbol).strip().upper() for symbol, _ in _watchlist_entries()})
        return {"symbols": symbols}
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data watchlist error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class AliasRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=50)


@router.get("/api/market-data/aliases")
def read_aliases() -> dict:
    """All configured symbol-alias fallback mappings (config/symbol_aliases.json)."""
    try:
        return {"aliases": load_symbol_aliases()}
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data aliases error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/api/market-data/aliases")
def set_alias(request: AliasRequest) -> dict:
    """Set (or clear, when `aliases` is empty) the fallback aliases for a symbol."""
    try:
        current = load_symbol_aliases()
        key = request.symbol.strip().upper()
        norm = [value.strip().upper() for value in request.aliases if value.strip()]
        if norm:
            current[key] = norm
        else:
            current.pop(key, None)
        save_symbol_aliases(current)
        return {"aliases": current}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected – log full traceback
        log.exception("Unhandled market-data alias write error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
