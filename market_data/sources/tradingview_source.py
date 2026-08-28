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


def fetch_daily(
    symbol: str,
    start_date: date,
    end_date: date,
    exchange: Optional[str] = None,
    store_symbol: Optional[str] = None,
) -> list[dict]:
    """Fetch daily bars covering [start_date, end_date] from TradingView.

    Returns rows shaped like DB records (date='YYYY-MM-DD'). Raises RuntimeError
    when tvDatafeed is unavailable or returns nothing usable.
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

    span_days = (end_date - start_date).days + 1
    # Extra buffer so weekends/holidays inside the window still yield enough bars.
    n_bars = min(max(span_days * 2 + 10, 30), 5000)

    tv = _get_client()
    from tvDatafeed import Interval  # imported after client creation for clear errors

    log.info("Fetching %s:%s daily bars (%d) for %s..%s",
             exchange_name, sym, n_bars, start_date.isoformat(), end_date.isoformat())
    df = tv.get_hist(symbol=sym, exchange=exchange_name, interval=Interval.in_daily, n_bars=n_bars)
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
                "date": bar_day.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": None if volume is None else float(volume),
            }
        )
    rows.sort(key=lambda item: item["date"])
    log.info("TradingView returned %d daily bars for %s:%s in range.",
             len(rows), exchange_name, sym)
    return rows
