"""Data-freshness report for the Status page (``GET /api/health/details``).

Read-only: it only queries ``ohlc_daily`` / ``ohlc_no_data`` and never fetches.
For each watchlist symbol it compares the stored bars with the trading days the
market should have produced:

- ``expected_last``: the market's latest *final* daily bar
  (``service.latest_final_session``, each market's own cut-off and zone).
- ``behind``: expected sessions after the last stored bar, up to ``expected_last``.
- ``missing``: expected sessions inside the window (``WINDOW_DAYS``, from the
  symbol's first stored bar at the earliest) with no bar and no ``ohlc_no_data``
  marker. NSE uses the NSE holiday calendar; forex/commodities only skip
  weekends, so their exchange holidays show up as a few missing days
  (``TV_GAP_TOLERANCE``) before the symbol is flagged.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import bar_quality, database, service
from .config import SOURCE_NSE, db_path as resolve_db_path
from .sources import nse_source, tradingview_source

WINDOW_DAYS = 90
TV_GAP_TOLERANCE = 3
MISSING_SAMPLE = 5

log = logging.getLogger(__name__)


def _market_key(source: str, symbol: str) -> str:
    if source == SOURCE_NSE:
        return "nse"
    if service.is_crypto_symbol(symbol):
        return "crypto"
    if service.is_nseix_symbol(symbol):
        return "nseix"
    return "tradingview"


def _expected_dates(market: str, start: date, end: date) -> list[date]:
    if start > end:
        return []
    if market == "nse":
        return nse_source.expected_trading_dates(start, end)
    if market == "crypto":
        return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return tradingview_source.expected_trading_dates(start, end)


def source_summary(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    conn = database.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM ohlc_daily GROUP BY source ORDER BY source"
        ).fetchall()
        no_data = dict(conn.execute("SELECT source, COUNT(*) FROM ohlc_no_data GROUP BY source").fetchall())
        # Stored rows that fail the integrity check (filtered on every read).
        invalid = dict(conn.execute(
            f"SELECT source, COUNT(*) FROM ohlc_daily WHERE {bar_quality.SQL_INVALID} GROUP BY source").fetchall())
    finally:
        conn.close()
    return [{"source": str(r[0]), "rows": int(r[1]), "symbols": int(r[2]), "latest_date": r[3],
             "no_data_markers": int(no_data.get(r[0], 0)), "invalid_rows": int(invalid.get(r[0], 0))} for r in rows]


def database_info() -> dict[str, Any]:
    if database.turso_url():
        return {"backend": "turso", "size_mb": None}
    path = resolve_db_path()
    try:
        size = round(path.stat().st_size / 1_048_576, 1)
    except OSError:
        size = None
    return {"backend": "sqlite", "size_mb": size}


def symbol_freshness(
    watchlist: Iterable[dict[str, Any]],
    now: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Per-symbol freshness rows plus a status count, worst first."""
    now = now or datetime.now(timezone.utc)
    entries = []
    for item in watchlist:
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol:
            source = service.resolve_session_source(symbol, item.get("session"))
            entries.append((symbol, source, _market_key(source, symbol), item))
    window_start = service.market_today(SOURCE_NSE, now=now) - timedelta(days=WINDOW_DAYS)

    # One pass over the window's bars (a few rows per symbol per day) - bars from
    # any source count, so an NSE stock filled from TradingView is not "missing".
    stored: dict[str, set[str]] = {}
    no_data: dict[str, set[str]] = {}
    bounds: dict[str, tuple[str, str]] = {}
    wanted = {symbol for symbol, *_ in entries}
    conn = database.connect(db_path)
    try:
        for row in conn.execute("SELECT symbol, date FROM ohlc_daily WHERE date >= ?", (window_start.isoformat(),)):
            if row[0] in wanted:
                stored.setdefault(row[0], set()).add(str(row[1]))
        for row in conn.execute("SELECT symbol, date FROM ohlc_no_data WHERE date >= ?", (window_start.isoformat(),)):
            if row[0] in wanted:
                no_data.setdefault(row[0], set()).add(str(row[1]))
        for row in conn.execute("SELECT symbol, MIN(date), MAX(date) FROM ohlc_daily GROUP BY symbol"):
            if row[0] in wanted:
                bounds[row[0]] = (str(row[1]), str(row[2]))
    finally:
        conn.close()

    expected_last: dict[str, Optional[date]] = {}
    calendars: dict[str, list[date]] = {}
    rows = []
    for symbol, source, market, item in entries:
        if market not in expected_last:
            last = service.latest_final_session(source, now=now, symbol=symbol)
            expected_last[market] = last
            calendars[market] = _expected_dates(market, window_start, last) if last else []
        last_expected = expected_last[market]
        first, last = bounds.get(symbol, (None, None))
        have = stored.get(symbol, set())
        skip = no_data.get(symbol, set())
        calendar = [d.isoformat() for d in calendars[market]]
        in_range = [d for d in calendar if first is not None and d >= first]
        missing = [d for d in in_range if d not in have and d not in skip and (last is None or d <= last)]
        behind = sum(1 for d in calendar if last is not None and d > last and d not in skip)
        if last is None:
            status = "empty"
        elif behind > 0:
            status = "stale"
        elif len(missing) > (0 if market == "nse" else TV_GAP_TOLERANCE):
            status = "gaps"
        else:
            status = "ok"
        rows.append({
            "symbol": symbol,
            "source": source,
            "market": market,
            "scope": item.get("scope") or item.get("asset_class") or "",
            "first_date": first,
            "last_date": last,
            "expected_last": last_expected.isoformat() if last_expected else None,
            "behind": behind,
            "missing": len(missing),
            "missing_sample": missing[:MISSING_SAMPLE],
            "no_data_days": len(skip),
            "status": status,
        })
    order = {"empty": 0, "stale": 1, "gaps": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["behind"], -r["missing"], r["symbol"]))
    counts = {key: sum(1 for r in rows if r["status"] == key) for key in order}
    return {
        "window_start": window_start.isoformat(),
        "window_days": WINDOW_DAYS,
        "expected_last": {market: (day.isoformat() if day else None) for market, day in expected_last.items()},
        "counts": counts,
        "symbols": rows,
    }


REFETCH_LIMIT = 200  # bad rows handled per click (bounded work on small hosts)


def refetch_invalid(source: Optional[str] = None, db_path: Optional[str] = None) -> dict[str, Any]:
    """Status-page "Re-fetch": replace stored bars that fail the integrity check.

    Each bad row is deleted and its date fetched again from its source
    (``service.get_ohlc``, honouring the FETCH_* flags). A bar that is still
    bad is rejected again by ``upsert_ohlc`` and stays a gap. Returns what was
    fixed and what is still missing.
    """
    where, params = bar_quality.SQL_INVALID, []
    if source:
        where, params = f"source = ? AND ({where})", [str(source).strip().upper()]
    conn = database.connect(db_path)
    try:
        bad = [dict(row) for row in conn.execute(
            f"SELECT source, exchange, symbol, date, open, high, low, close FROM ohlc_daily WHERE {where} "
            f"ORDER BY source, symbol, date LIMIT {REFETCH_LIMIT}", params).fetchall()]
    finally:
        conn.close()
    groups: dict[tuple[str, str, str], list[str]] = {}
    for row in bad:
        groups.setdefault((row["source"], row["exchange"], row["symbol"]), []).append(str(row["date"]))
    if bad:
        log.warning("Re-fetching %d bad stored bar(s): %s", len(bad), bad[:5])
    fixed, missing, errors = [], [], []
    for (src, exchange, symbol), dates in groups.items():
        for day in dates:
            database.delete_ohlc(symbols=[symbol], source=src, exchange=exchange, start_date=day, end_date=day, db_path=db_path)
        try:
            result = service.get_ohlc(src, symbol, min(dates), max(dates), auto_fetch=True, db_path=db_path)
            got = {str(row["date"]) for row in result.rows}
        except Exception as exc:  # provider disabled / blocked / no data: the dates stay gaps
            errors.append(f"{symbol}: {exc}")
            got = set()
        fixed += [f"{symbol} {day}" for day in dates if day in got]
        missing += [f"{symbol} {day}" for day in dates if day not in got]
    return {"checked": len(bad), "fixed": fixed, "still_missing": missing, "errors": errors,
            "more": len(bad) == REFETCH_LIMIT}
