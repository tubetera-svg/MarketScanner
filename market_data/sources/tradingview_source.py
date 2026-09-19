"""TradingView (tvDatafeed) daily OHLC source — used for commodities/forex.

Reuses the same tvDatafeed library and connection style as
`src/ict_scanner.py::TvDatafeedFetcher` (anonymous TvDatafeed() session),
but is intentionally a standalone module so source-specific logic stays
in one place. Symbols are exchange-qualified like the rest of this project:
"OANDA:XAUUSD", "CAPITALCOM:NATURALGAS", "FOREXCOM:USOIL", ...
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Optional

from ..config import SOURCE_TRADINGVIEW

log = logging.getLogger(__name__)

SOURCE_NAME = SOURCE_TRADINGVIEW

_DEFAULT_EXCHANGE = os.environ.get("TRADINGVIEW_DEFAULT_EXCHANGE", "NSE")

_client = None  # lazily created shared TvDatafeed session


def split_symbol(symbol: str) -> tuple[str, str]:
    """'FOREXCOM:USOIL' -> ('FOREXCOM', 'USOIL'); bare symbol uses default exchange."""
    value = str(symbol).strip().upper()
    if ":" in value:
        exchange, sym = value.split(":", 1)
        return exchange.strip(), sym.strip()
    return _DEFAULT_EXCHANGE.upper(), value


def _get_client():
    global _client
    if _client is None:
        try:
            from tvDatafeed import TvDatafeed
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "TradingView source requires tvDatafeed. "
                "Install with: pip install --upgrade --no-cache-dir "
                "git+https://github.com/rongardF/tvdatafeed.git"
            ) from exc
        log.info("Connecting to TradingView (shared tvDatafeed session)...")
        _client = TvDatafeed()
        log.info("TradingView session established.")
    return _client


def expected_trading_dates(start_date: date, end_date: date) -> list[date]:
    """Weekdays only (forex/commodities trade Mon-Fri). Holiday gaps are tolerated."""
    days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def fetch_timeframe(
    symbol: str,
    start_date: date,
    end_date: date,
    timeframe: str = "1d",
    exchange: Optional[str] = None,
    store_symbol: Optional[str] = None,
) -> list[dict]:
    """Fetch OHLC bars for a supported timeframe from TradingView.

    Intraday rows retain their timestamp in ``date``; higher timeframes use
    their bar date.
    """
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    # A symbol already carrying its own exchange (e.g. an alias like
    # 'FOREXCOM:XAUUSD') must keep that exchange, otherwise alias fallback would
    # just re-query the original (failing) feed. Only a *bare* symbol inherits the
    # caller-supplied `exchange`.
    exchange_name, sym = split_symbol(symbol)
    if exchange and ":" not in symbol:
        exchange_name = str(exchange).strip().upper()

    # `store_symbol` lets an alias fetch (e.g. FOREXCOM:XAUUSD) be recorded under
    # the original watchlist symbol (e.g. OANDA:XAUUSD). The stored `exchange`
    # must match `store_symbol`'s own exchange so later cache lookups (which
    # filter by that exchange) can find the row.
    if store_symbol:
        qualified = str(store_symbol).strip().upper()
        store_exchange, _ = split_symbol(qualified)
    else:
        qualified = f"{exchange_name}:{sym}"
        store_exchange = exchange_name

    tv = _get_client()
    from tvDatafeed import Interval  # imported after client creation for clear errors

    interval_by_name = {
        "15m": Interval.in_15_minute,
        "1h": Interval.in_1_hour,
        "4h": Interval.in_4_hour,
        "1d": Interval.in_daily,
        "1w": Interval.in_weekly,
    }
    normalized_timeframe = str(timeframe).strip().lower()
    if normalized_timeframe not in interval_by_name:
        raise ValueError(f"Unsupported TradingView timeframe: {timeframe}")

    span_days = (end_date - start_date).days + 1
    bars_per_day = {"15m": 30, "1h": 8, "4h": 2, "1d": 1, "1w": 0.2}[normalized_timeframe]
    n_bars = min(max(int(span_days * bars_per_day * 2 + 10), 30), 5000)

    log.info("Fetching %s:%s %s bars (%d) for %s..%s",
             exchange_name, sym, normalized_timeframe, n_bars, start_date.isoformat(), end_date.isoformat())
    df = tv.get_hist(symbol=sym, exchange=exchange_name, interval=interval_by_name[normalized_timeframe], n_bars=n_bars)
    if df is None or len(df) == 0:
        raise RuntimeError(f"TradingView returned no daily data for {exchange_name}:{sym}")

    rows: list[dict] = []
    for index, row in df.iterrows():
        bar_day = getattr(index, "date", lambda: index)()
        if not isinstance(bar_day, date):
            continue
        if bar_day < start_date or bar_day > end_date:
            continue
        volume = row.get("volume")
        rows.append(
            {
                "source": SOURCE_NAME,
                "symbol": qualified,
                "exchange": store_exchange,
                "date": index.isoformat() if normalized_timeframe in {"15m", "1h", "4h"} else bar_day.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": None if volume is None else float(volume),
            }
        )
    rows.sort(key=lambda item: item["date"])
    log.info("TradingView returned %d %s bars for %s:%s in range.",
             len(rows), normalized_timeframe, exchange_name, sym)
    return rows


def fetch_daily(
    symbol: str,
    start_date: date,
    end_date: date,
    exchange: Optional[str] = None,
    store_symbol: Optional[str] = None,
) -> list[dict]:
    return fetch_timeframe(symbol, start_date, end_date, "1d", exchange, store_symbol)
