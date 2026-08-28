"""SQLite storage layer for daily OHLC market data.

Design notes
------------
- SQLite only (stdlib `sqlite3`), no external database.
- Database file: data/market_data.db (override with MARKET_DATA_DB_PATH).
- WAL journal mode + NORMAL synchronous for safe concurrent readers.
- Unique constraint on (source, exchange, symbol, date) prevents duplicates;
  inserts use ON CONFLICT DO NOTHING so re-fetching never overwrites/duplicates.
- Index on (source, symbol, date) keeps range queries fast.
- Writes go through a module lock + short-lived connections, batched via
  `executemany` inside one transaction.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .config import db_path as resolve_db_path

log = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlc_daily (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, exchange, symbol, date)
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ohlc_source_symbol_date "
    "ON ohlc_daily (source, symbol, date);",
    "CREATE INDEX IF NOT EXISTS idx_ohlc_symbol_date "
    "ON ohlc_daily (symbol, date);",
    "CREATE TABLE IF NOT EXISTS ohlc_no_data (\n"
    "        id         INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "        source     TEXT NOT NULL,\n"
    "        symbol     TEXT NOT NULL,\n"
    "        exchange   TEXT NOT NULL,\n"
    "        date       TEXT NOT NULL,\n"
    "        checked_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
    "        UNIQUE (source, exchange, symbol, date)\n"
    "    );",
)

_ROW_COLUMNS = ("source", "symbol", "exchange", "date", "open", "high", "low", "close", "volume")


def connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Open a connection with WAL enabled and sane busy timeout."""
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: Optional[Path | str] = None) -> None:
    """Create the schema if needed. Idempotent and safe to call often."""
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            for statement in _INDEXES:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()


def upsert_ohlc(rows: Iterable[dict], db_path: Optional[Path | str] = None) -> int:
    """Batch-insert rows; existing (source, exchange, symbol, date) rows are kept.

    Each row needs: source, symbol, exchange, date ('YYYY-MM-DD'),
    open, high, low, close and optional volume.
    Returns the number of NEW rows stored.
    """
    payload = []
    for row in rows:
        try:
            payload.append(
                (
                    str(row["source"]).strip().upper(),
                    str(row["symbol"]).strip().upper(),
                    str(row["exchange"]).strip().upper(),
                    str(row["date"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    None if row.get("volume") is None else float(row["volume"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping invalid OHLC row %r: %s", row, exc)
    if not payload:
        return 0

    statement = """
        INSERT INTO ohlc_daily
            (source, symbol, exchange, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, exchange, symbol, date) DO NOTHING
    """

    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            before = conn.total_changes
            conn.executemany(statement, payload)
            inserted = conn.total_changes - before
            conn.commit()
            log.info("Stored %d new OHLC rows (%d submitted).", inserted, len(payload))
            return inserted
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()


def query_ohlc_multi(
    source: str,
    symbols: Sequence[str],
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    exchange: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> dict[str, list[dict]]:
    """Return stored rows for many symbols in a single query, grouped by symbol.

    Used by the strategy layer to fetch the historical window for a whole symbol
    universe at once instead of issuing one round-trip per symbol. Rows are
    returned date-ascending per symbol, matching the ordering of ``query_ohlc``.
    """
    wanted = [str(value).strip().upper() for value in symbols if str(value).strip()]
    if not wanted:
        return {}
    clauses, params = _record_filters(
        symbols=wanted,
        source=source,
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(_ROW_COLUMNS)} FROM ohlc_daily{where} "
        f"ORDER BY symbol ASC, date ASC"
    )
    conn = connect(db_path)
    try:
        grouped: dict[str, list[dict]] = {}
        for row in conn.execute(sql, params).fetchall():
            record = dict(row)
            grouped.setdefault(str(record["symbol"]), []).append(record)
        return grouped
    finally:
        conn.close()


def query_ohlc(
    source: str,
    symbol: str,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    exchange: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> list[dict]:
    """Return stored rows ordered by date. Dates may be `date` or 'YYYY-MM-DD'."""
    clauses = ["source = ?", "symbol = ?"]
    params: list[object] = [str(source).strip().upper(), str(symbol).strip().upper()]
    if start_date is not None:
        clauses.append("date >= ?")
        params.append(_to_date_text(start_date))
    if end_date is not None:
        clauses.append("date <= ?")
        params.append(_to_date_text(end_date))
    if exchange:
        clauses.append("exchange = ?")
        params.append(str(exchange).strip().upper())

    sql = (
        f"SELECT {', '.join(_ROW_COLUMNS)} FROM ohlc_daily "
        f"WHERE {' AND '.join(clauses)} ORDER BY date ASC"
    )
    conn = connect(db_path)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


_SYMBOL_CHUNK = 400  # keep IN()-groups safely under SQLite's host-parameter ceiling


def _record_filters(
    *,
    symbols: Optional[Sequence[str]] = None,
    source: Optional[str] = None,
    exchange: Optional[str] = None,
    symbol_contains: Optional[str] = None,
    date_contains: Optional[str] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
) -> tuple[list[str], list[object]]:
    """Shared WHERE builder for the read-only record-browsing queries.

    Every clause appends its parameters in order, so clauses/params always stay
    aligned. Long symbol lists are split into chunked OR-groups of IN(...) to
    respect SQLite's host-parameter limit.
    """
    clauses: list[str] = []
    params: list[object] = []
    if source:
        clauses.append("source = ?")
        params.append(str(source).strip().upper())
    if exchange:
        clauses.append("exchange = ?")
        params.append(str(exchange).strip().upper())
    if symbol_contains:
        clauses.append("symbol LIKE ?")
        params.append(f"%{str(symbol_contains).strip().upper()}%")
    if date_contains:
        clauses.append("date LIKE ?")
        params.append(f"%{str(date_contains).strip()}%")
    if start_date is not None:
        clauses.append("date >= ?")
        params.append(_to_date_text(start_date))
    if end_date is not None:
        clauses.append("date <= ?")
        params.append(_to_date_text(end_date))
    if symbols:
        wanted = [str(value).strip().upper() for value in symbols if str(value).strip()]
        groups: list[str] = []
        for index in range(0, len(wanted), _SYMBOL_CHUNK):
            chunk = wanted[index:index + _SYMBOL_CHUNK]
            groups.append("symbol IN (%s)" % ", ".join("?" * len(chunk)))
            params.extend(chunk)
        if groups:
            clauses.append("(" + " OR ".join(groups) + ")")
    return clauses, params


def query_ohlc_page(
    *,
    symbols: Optional[Sequence[str]] = None,
    source: Optional[str] = None,
    exchange: Optional[str] = None,
    symbol_contains: Optional[str] = None,
    date_contains: Optional[str] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
    db_path: Optional[Path | str] = None,
) -> tuple[list[dict], int]:
    """One page of ohlc_daily rows plus the total count for the same filter set.

    Pure read helper backing GET /api/market-data/records — it NEVER fetches
    from upstream sources. Returns ([row dicts in _ROW_COLUMNS order], total).
    """
    direction = "ASC" if str(order).strip().lower() == "asc" else "DESC"
    clauses, params = _record_filters(
        symbols=symbols,
        source=source,
        exchange=exchange,
        symbol_contains=symbol_contains,
        date_contains=date_contains,
        start_date=start_date,
        end_date=end_date,
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    conn = connect(db_path)
    try:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM ohlc_daily{where}", params).fetchone()[0]
        )
        sql = (
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM ohlc_daily{where} "
            f"ORDER BY symbol ASC, date {direction} LIMIT ? OFFSET ?"
        )
        rows = [
            dict(row)
            for row in conn.execute(sql, [*params, limit, offset]).fetchall()
        ]
        return rows, total
    finally:
        conn.close()


def distinct_symbols(
    *,
    symbols: Optional[Sequence[str]] = None,
    source: Optional[str] = None,
    exchange: Optional[str] = None,
    symbol_contains: Optional[str] = None,
    date_contains: Optional[str] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    db_path: Optional[Path | str] = None,
) -> list[str]:
    """Distinct stored symbols matching the record filters (coverage checks)."""
    clauses, params = _record_filters(
        symbols=symbols,
        source=source,
        exchange=exchange,
        symbol_contains=symbol_contains,
        date_contains=date_contains,
        start_date=start_date,
        end_date=end_date,
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT symbol FROM ohlc_daily{where} ORDER BY symbol", params
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def existing_dates(
    source: str,
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    exchange: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> set[str]:
    """Set of 'YYYY-MM-DD' strings already stored inside the inclusive range."""
    sql = "SELECT date FROM ohlc_daily WHERE source = ? AND symbol = ? AND date >= ? AND date <= ?"
    params: list[object] = [
        str(source).strip().upper(),
        str(symbol).strip().upper(),
        _to_date_text(start_date),
        _to_date_text(end_date),
    ]
    if exchange:
        sql += " AND exchange = ?"
        params.append(str(exchange).strip().upper())
    conn = connect(db_path)
    try:
        return {row[0] for row in conn.execute(sql, params)}
    finally:
        conn.close()


def mark_no_data(rows: Sequence[dict], db_path: Optional[Path | str] = None) -> int:
    """Remember dates where the upstream source confirmed there is no bar.

    Prevents repeatedly hitting the API for genuine market holidays.
    Rows need: source, symbol, exchange, date.
    """
    payload = [
        (
            str(row["source"]).strip().upper(),
            str(row["symbol"]).strip().upper(),
            str(row["exchange"]).strip().upper(),
            str(row["date"]),
        )
        for row in rows
    ]
    if not payload:
        return 0
    statement = """
        INSERT INTO ohlc_no_data (source, symbol, exchange, date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (source, exchange, symbol, date) DO NOTHING
    """
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            before = conn.total_changes
            conn.executemany(statement, payload)
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()


def clear_no_data(
    source: str,
    symbol: str,
    date: date | str,
    exchange: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> int:
    """Drop a previously cached 'no data' marker so the date can be re-fetched.

    Used when a daily bar becomes available after an earlier sync wrongly
    concluded there was no data (e.g. the NSE bhavcopy publishes after a sync
    ran). Returns the number of markers removed.
    """
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            before = conn.total_changes
            conn.execute(
                "DELETE FROM ohlc_no_data "
                "WHERE source = ? AND symbol = ? AND date = ?",
                (
                    str(source).strip().upper(),
                    str(symbol).strip().upper(),
                    _to_date_text(date),
                ),
            )
            removed = conn.total_changes - before
            if removed:
                conn.commit()
            return removed
        finally:
            conn.close()


def no_data_dates(
    source: str,
    symbol: str,
    start_date: date | str,
    end_date: date | str,
    exchange: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> set[str]:
    """Dates previously confirmed to have no data for this instrument."""
    sql = (
        "SELECT date FROM ohlc_no_data "
        "WHERE source = ? AND symbol = ? AND date >= ? AND date <= ?"
    )
    params: list[object] = [
        str(source).strip().upper(),
        str(symbol).strip().upper(),
        _to_date_text(start_date),
        _to_date_text(end_date),
    ]
    if exchange:
        sql += " AND exchange = ?"
        params.append(str(exchange).strip().upper())
    conn = connect(db_path)
    try:
        return {row[0] for row in conn.execute(sql, params)}
    finally:
        conn.close()


def count_rows(db_path: Optional[Path | str] = None) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM ohlc_daily").fetchone()[0])
    finally:
        conn.close()


def delete_ohlc(
    *,
    symbols: Optional[Sequence[str]] = None,
    source: Optional[str] = None,
    exchange: Optional[str] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    delete_all: bool = False,
    db_path: Optional[Path | str] = None,
) -> int:
    """Delete stored OHLC rows (and their no-data markers) matching the filters.

    Pass ``delete_all=True`` with no symbol/source/exchange/date filter to wipe
    the entire table — used to clear bad data ahead of a re-sync. Returns the
    number of ``ohlc_daily`` rows removed.
    """
    if not delete_all and not (symbols or source or exchange or start_date or end_date):
        return 0
    clauses, params = _record_filters(
        symbols=symbols,
        source=source,
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            before = conn.total_changes
            conn.execute(f"DELETE FROM ohlc_no_data{where}", params)
            conn.execute(f"DELETE FROM ohlc_daily{where}", params)
            deleted = conn.total_changes - before
            conn.commit()
            log.info("Deleted %d OHLC rows%s", deleted, where and f" ({where})" or "")
            return deleted
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()


def distinct_values(
    column: str,
    db_path: Optional[Path | str] = None,
    *,
    symbols: Optional[Sequence[str]] = None,
) -> list[str]:
    """Distinct values of 'source' or 'exchange', sorted (filter dropdowns)."""
    allowed = {"source", "exchange"}
    if column not in allowed:
        raise ValueError(f"column must be one of {sorted(allowed)}")
    clauses, params = _record_filters(symbols=symbols)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM ohlc_daily{where} ORDER BY {column}", params
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]
    finally:
        conn.close()


def date_range(
    db_path: Optional[Path | str] = None,
    *,
    symbols: Optional[Sequence[str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """(MIN(date), MAX(date)) across ohlc_daily; (None, None) when empty."""
    clauses, params = _record_filters(symbols=symbols)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect(db_path)
    try:
        row = conn.execute(f"SELECT MIN(date), MAX(date) FROM ohlc_daily{where}", params).fetchone()
        if row is None or row[0] is None:
            return (None, None)
        return (str(row[0]), str(row[1]))
    finally:
        conn.close()


def rows_per_source(
    db_path: Optional[Path | str] = None,
    *,
    symbols: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Row counts grouped by source, ordered by source name."""
    clauses, params = _record_filters(symbols=symbols)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect(db_path)
    try:
        return [
            {"source": str(row[0]), "rows": int(row[1])}
            for row in conn.execute(
                f"SELECT source, COUNT(*) FROM ohlc_daily{where} GROUP BY source ORDER BY source",
                params,
            )
        ]
    finally:
        conn.close()


def _to_date_text(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    date.fromisoformat(text)  # validate 'YYYY-MM-DD'
    return text

