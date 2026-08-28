"""Lightweight local SQLite market-data layer.

Public API:
    from market_data import get_ohlc
    get_ohlc(source="NSE", symbol="RELIANCE",
             start_date="2026-08-11", end_date="2026-08-25")
"""

from .config import (
    FLAG_AUTO_FETCH_MISSING,
    FLAG_FETCH_NSE,
    FLAG_FETCH_TRADINGVIEW,
    SOURCE_NSE,
    SOURCE_TRADINGVIEW,
)
from .database import init_db, query_ohlc, upsert_ohlc
from .errors import FetchError, MarketDataError, NoDataError, SourceDisabledError
from .service import get_ohlc, sync_symbol_range

__all__ = [
    "FLAG_AUTO_FETCH_MISSING",
    "FLAG_FETCH_NSE",
    "FLAG_FETCH_TRADINGVIEW",
    "SOURCE_NSE",
    "SOURCE_TRADINGVIEW",
    "FetchError",
    "MarketDataError",
    "NoDataError",
    "SourceDisabledError",
    "get_ohlc",
    "init_db",
    "query_ohlc",
    "sync_symbol_range",
    "upsert_ohlc",
]
