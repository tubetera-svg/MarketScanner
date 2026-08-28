"""NSE Bhavcopy daily OHLC source — used for NSE stocks.

Deliberately reuses the existing downloader in `src/all_strategy.py`
(`_download_bhavcopy_for_date`, including its in-memory per-date cache and
NSE holiday calendar) instead of duplicating any fetching logic. This module
only adapts the downloaded full-market bhavcopy file into OHLC rows and can
target an explicit list of missing dates so only what is absent gets fetched.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..config import SOURCE_NSE

log = logging.getLogger(__name__)

SOURCE_NAME = SOURCE_NSE
EXCHANGE = "NSE"

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_all_strategy = None


def _load_all_strategy():
    """Import src/all_strategy.py once (same pattern as main.py / api/main.py)."""
    global _all_strategy
    if _all_strategy is None:
        src_path = str(_ROOT_DIR / "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)
        import all_strategy  # type: ignore

        _all_strategy = all_strategy
    return _all_strategy


def strip_symbol_prefix(symbol: str) -> str:
    """'NSE:RELIANCE' -> 'RELIANCE'."""
    value = str(symbol).strip().upper()
    return value.split(":", 1)[1].strip() if ":" in value else value


def expected_trading_dates(start_date: date, end_date: date) -> list[date]:
    """Weekdays minus the NSE holiday calendar from all_strategy."""
    mod = _load_all_strategy()
    holidays = set(getattr(mod, "NSE_HOLIDAYS", set()))
    days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5 and current not in holidays:
            days.append(current)
        current += timedelta(days=1)
    return days


def fetch_daily(
    symbol: str,
    start_date: date,
    end_date: date,
    dates: Optional[list[date]] = None,
    store_symbol: Optional[str] = None,
) -> list[dict]:
    """Fetch daily bars for one symbol from NSE bhavcopy files.

    Args:
        dates: explicit trading dates to download. Only these files are hit,
            enabling precise missing-date backfill. Falls back to every
            expected trading date in [start_date, end_date].
    """
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    sym = strip_symbol_prefix(symbol)
    # Rows are stored under the exchange-qualified key ('NSE:INFY') so they line
    # up with watchlist entries and get_ohlc()'s cache lookups, which pass the
    # prefixed form through untouched. `store_symbol` lets an alias fetch (e.g.
    # BSE:INFY) be stored under the original watchlist symbol.
    qualified = str(store_symbol).strip().upper() if store_symbol else f"{EXCHANGE}:{sym}"
    mod = _load_all_strategy()
    wanted = {sym}
    find_column = mod._find_column
    get_bhavcopy = mod._download_bhavcopy_for_date

    rows: list[dict] = []
    for trade_date in dates or expected_trading_dates(start_date, end_date):
        if trade_date < start_date or trade_date > end_date:
            continue
        df = get_bhavcopy(trade_date)
        if df is None or df.empty:
            log.debug("No bhavcopy for %s", trade_date.isoformat())
            continue

        cols = df.columns
        symbol_col = find_column(cols, ["SYMBOL"])
        series_col = find_column(cols, ["SERIES"])
        open_col = find_column(cols, ["OPEN_PRICE", "OPEN"])
        high_col = find_column(cols, ["HIGH_PRICE", "HIGH"])
        low_col = find_column(cols, ["LOW_PRICE", "LOW"])
        close_col = find_column(cols, ["CLOSE_PRICE", "CLOSE"])
        volume_col = find_column(cols, ["TTL_TRD_QNTY", "TOTAL_TRADED_QUANTITY", "VOLUME"])
        if not all([symbol_col, series_col, open_col, high_col, low_col, close_col]):
            continue

        match = df[
            (df[series_col].astype(str).str.strip().str.upper() == "EQ")
            & (df[symbol_col].astype(str).str.strip().str.upper().isin(wanted))
        ]
        if match.empty:
            continue
        record = match.iloc[0]
        try:
            rows.append(
                {
                    "source": SOURCE_NAME,
                    "symbol": qualified,
                    "exchange": EXCHANGE,
                    "date": trade_date.isoformat(),
                    "open": float(record[open_col]),
                    "high": float(record[high_col]),
                    "low": float(record[low_col]),
                    "close": float(record[close_col]),
                    "volume": float(record[volume_col]) if volume_col else None,
                }
            )
        except (TypeError, ValueError) as exc:
            log.warning("Skipping bhavcopy row for %s on %s: %s", sym, trade_date, exc)

    rows.sort(key=lambda item: item["date"])
    log.info("NSE bhavcopy returned %d bars for %s (%s..%s).",
             len(rows), sym, start_date.isoformat(), end_date.isoformat())
    return rows
