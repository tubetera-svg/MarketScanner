"""NSE IPO Liquidity Screener.

Screens IPO-scope symbols in the watchlist for liquidity and market presence,
then decides ADD/KEEP/WATCH/REMOVE per the configured thresholds.

On REMOVE: purges symbol from watchlist.txt, watchlist_categories.json,
and all market_data DB tables (ohlc_daily, ohlc_no_data, ipo_metadata).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from . import database
from .config import (
    SOURCE_NSE,
    LIQUID_AVG_DAILY_VALUE_CR,
    BORDERLINE_AVG_DAILY_VALUE_CR,
    LIQUID_MAX_ZERO_DAYS,
    BORDERLINE_MAX_ZERO_DAYS,
    MARKET_CAP_MIN_CR,
    FREE_FLOAT_MIN_PCT,
    BID_ASK_SPREAD_MAX_PCT,
    LIQUIDITY_LOOKBACK_DAYS,
)
from .service import get_ohlc

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT_DIR / "config" / "watchlist.txt"
CATEGORIES_PATH = ROOT_DIR / "config" / "watchlist_categories.json"
IPO_SCOPE = "IPO"


def _load_categories() -> dict[str, dict[str, str]]:
    try:
        return json.loads(CATEGORIES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_categories(categories: dict[str, dict[str, str]]) -> None:
    CATEGORIES_PATH.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2) + "\n",
        encoding="utf-8",
    )


def _load_watchlist_symbols() -> set[str]:
    """Return all symbols currently in watchlist.txt (upper-case)."""
    if not WATCHLIST_PATH.exists():
        return set()
    with WATCHLIST_PATH.open("r", encoding="utf-8") as f:
        return {line.strip().upper() for line in f if line.strip() and not line.startswith("#")}


def _save_watchlist_symbols(symbols: set[str]) -> None:
    """Overwrite watchlist.txt with the given symbol set (sorted)."""
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WATCHLIST_PATH.open("w", encoding="utf-8") as f:
        for sym in sorted(symbols):
            f.write(f"{sym}\n")


def _get_ipo_scope_symbols() -> list[str]:
    """Return symbols in watchlist that have scope=IPO in categories."""
    categories = _load_categories()
    return [
        sym for sym, meta in categories.items()
        if str(meta.get("scope", "")).upper() == IPO_SCOPE
    ]


def compute_liquidity_metrics(
    symbol: str,
    lookback_days: int = LIQUIDITY_LOOKBACK_DAYS,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Compute liquidity metrics from stored OHLC data.

    Returns dict with:
    - avg_daily_value_cr: average daily traded value in crores
    - zero_trade_days: count of days with zero volume
    - total_days: total trading days in window
    - latest_close: most recent close price
    - bid_ask_spread_pct: estimated from (high-low)/close (proxy)
    - sufficient_data: bool, True if enough history to decide
    """
    end = date.today()
    start = end - timedelta(days=lookback_days * 2)

    # Try with auto_fetch enabled but exclude today (incomplete bar)
    end_fetch = end - timedelta(days=1)
    try:
        result = get_ohlc(SOURCE_NSE, symbol, start, end_fetch, db_path=db_path)
        bars = result.rows
    except Exception as exc:
        log.warning("Failed to fetch OHLC for %s: %s", symbol, exc)
        return {
            "avg_daily_value_cr": 0.0,
            "zero_trade_days": 0,
            "total_days": 0,
            "latest_close": None,
            "bid_ask_spread_pct": None,
            "sufficient_data": False,
        }

    if not bars:
        return {
            "avg_daily_value_cr": 0.0,
            "zero_trade_days": 0,
            "total_days": 0,
            "latest_close": None,
            "bid_ask_spread_pct": None,
            "sufficient_data": False,
        }

    total_value = 0.0
    zero_days = 0
    spreads = []

    for bar in bars:
        vol = bar.get("volume") or 0
        close = bar.get("close") or 0
        high = bar.get("high") or close
        low = bar.get("low") or close

        if vol > 0 and close > 0:
            total_value += vol * close
        else:
            zero_days += 1

        if close > 0:
            spread_pct = ((high - low) / close) * 100
            spreads.append(spread_pct)

    total_days = len(bars)
    avg_daily_value = total_value / total_days if total_days > 0 else 0.0
    avg_daily_value_cr = avg_daily_value / 1e7

    bid_ask_spread_pct = sum(spreads) / len(spreads) if spreads else None

    return {
        "avg_daily_value_cr": round(avg_daily_value_cr, 4),
        "zero_trade_days": zero_days,
        "total_days": total_days,
        "latest_close": bars[-1].get("close") if bars else None,
        "bid_ask_spread_pct": round(bid_ask_spread_pct, 2) if bid_ask_spread_pct else None,
        "sufficient_data": total_days >= max(10, lookback_days // 3),
    }


def fetch_fundamentals(symbol: str) -> dict:
    """Fetch market cap and free float for a symbol.

    Placeholder - integrate with NSE API or cached fundamentals source.
    Returns dict with market_cap_cr, free_float_pct (None if unavailable).
    """
    base = symbol.split(":", 1)[-1] if ":" in symbol else symbol

    try:
        sys.path.insert(0, str(ROOT_DIR / "src"))
        import all_strategy
        if hasattr(all_strategy, "fetch_fundamentals"):
            return all_strategy.fetch_fundamentals(base)
    except Exception as exc:
        log.debug("Fundamentals fetch failed for %s: %s", symbol, exc)

    return {"market_cap_cr": None, "free_float_pct": None}


def _classify_liquidity(metrics: dict) -> str:
    """Classify as LIQUID/BORDERLINE/ILLIQUID based on metrics."""
    avg_val = metrics.get("avg_daily_value_cr", 0)
    zero_days = metrics.get("zero_trade_days", 0)
    spread = metrics.get("bid_ask_spread_pct")

    if avg_val >= LIQUID_AVG_DAILY_VALUE_CR and zero_days <= LIQUID_MAX_ZERO_DAYS:
        if spread is None or spread <= BID_ASK_SPREAD_MAX_PCT:
            return "LIQUID"

    if avg_val >= BORDERLINE_AVG_DAILY_VALUE_CR or zero_days <= BORDERLINE_MAX_ZERO_DAYS:
        if spread is None or spread <= BID_ASK_SPREAD_MAX_PCT * 2:
            return "BORDERLINE"

    return "ILLIQUID"


def _check_market_presence(fundamentals: dict) -> list[str]:
    """Return list of flags from market presence checks."""
    flags = []
    mcap = fundamentals.get("market_cap_cr")
    free_float = fundamentals.get("free_float_pct")

    if mcap is not None and mcap < MARKET_CAP_MIN_CR:
        flags.append("LOW_MARKET_SHARE")
    if free_float is not None and free_float < FREE_FLOAT_MIN_PCT:
        flags.append("LOW_FREE_FLOAT")
    return flags


def screen_symbol(
    symbol: str,
    lookback_days: int = LIQUIDITY_LOOKBACK_DAYS,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Screen one IPO-scope symbol and return decision JSON."""
    metrics = compute_liquidity_metrics(symbol, lookback_days, db_path)
    fundamentals = fetch_fundamentals(symbol)

    liquidity_tier = _classify_liquidity(metrics)
    flags = _check_market_presence(fundamentals)

    if not metrics.get("sufficient_data", False):
        liquidity_tier = "N/A"
        decision = "WATCH"
        reason = f"insufficient data: only {metrics.get('total_days', 0)} trading days in lookback window"
    elif liquidity_tier == "LIQUID" and not flags:
        decision = "KEEP"
        reason = (
            f"LIQUID: avg daily value Rs.{metrics['avg_daily_value_cr']:.2f} cr, "
            f"zero-trade days {metrics['zero_trade_days']}/{metrics['total_days']}"
        )
    elif liquidity_tier == "BORDERLINE" or len(flags) == 1:
        decision = "WATCH"
        flag_str = f", flags: {', '.join(flags)}" if flags else ""
        reason = (
            f"BORDERLINE: avg daily value Rs.{metrics['avg_daily_value_cr']:.2f} cr, "
            f"zero-trade days {metrics['zero_trade_days']}/{metrics['total_days']}{flag_str}"
        )
    else:
        decision = "REMOVE"
        flag_str = f", flags: {', '.join(flags)}" if flags else ""
        illiq_reason = []
        if metrics.get("avg_daily_value_cr", 0) < BORDERLINE_AVG_DAILY_VALUE_CR:
            illiq_reason.append(f"avg value Rs.{metrics['avg_daily_value_cr']:.2f} cr < Rs.{BORDERLINE_AVG_DAILY_VALUE_CR} cr")
        if metrics.get("zero_trade_days", 0) >= 5:
            illiq_reason.append(f"zero-trade days {metrics['zero_trade_days']} >= 5")
        if metrics.get("bid_ask_spread_pct") and metrics["bid_ask_spread_pct"] > BID_ASK_SPREAD_MAX_PCT:
            illiq_reason.append(f"bid-ask spread {metrics['bid_ask_spread_pct']:.1f}% > {BID_ASK_SPREAD_MAX_PCT}%")
        reason = f"ILLIQUID: {', '.join(illiq_reason) if illiq_reason else 'failed liquidity thresholds'}{flag_str}"

    return {
        "symbol": symbol,
        "liquidity_tier": liquidity_tier,
        "flags": flags,
        "decision": decision,
        "reason": reason,
    }


def remove_symbol_everywhere(symbol: str, db_path: Optional[Path | str] = None) -> dict:
    """Remove symbol from watchlist, categories, and all DB tables.

    Returns summary of what was removed.
    """
    sym = symbol.strip().upper()
    removed = {"watchlist": False, "categories": False, "ohlc_daily": 0, "ohlc_no_data": 0, "ipo_metadata": 0, "tv_symbol_cache": 0}

    watchlist_syms = _load_watchlist_symbols()
    if sym in watchlist_syms:
        watchlist_syms.discard(sym)
        _save_watchlist_symbols(watchlist_syms)
        removed["watchlist"] = True
        log.info("Removed %s from watchlist.txt", sym)

    categories = _load_categories()
    if sym in categories:
        del categories[sym]
        _save_categories(categories)
        removed["categories"] = True
        log.info("Removed %s from watchlist_categories.json", sym)

    db_removed = database.remove_symbol_data(sym, source=SOURCE_NSE, db_path=db_path)
    removed["ohlc_daily"] = db_removed.get("ohlc_daily", 0)
    removed["ohlc_no_data"] = db_removed.get("ohlc_no_data", 0)
    removed["ipo_metadata"] = db_removed.get("ipo_metadata", 0)
    removed["tv_symbol_cache"] = db_removed.get("tv_symbol_cache", 0)

    for table, count in db_removed.items():
        if count:
            log.info("Deleted %d %s rows for %s", count, table, sym)

    return removed


def screen_all_ipos(
    lookback_days: int = LIQUIDITY_LOOKBACK_DAYS,
    db_path: Optional[Path | str] = None,
    auto_remove: bool = False,
) -> list[dict]:
    """Screen all IPO-scope symbols in watchlist.

    If auto_remove=True, immediately purges symbols with decision=REMOVE.
    Returns list of screening results for all symbols.
    """
    ipo_symbols = _get_ipo_scope_symbols()
    log.info("Screening %d IPO-scope symbols", len(ipo_symbols))

    results = []
    for sym in ipo_symbols:
        result = screen_symbol(sym, lookback_days, db_path)
        results.append(result)
        log.info(
            "Screened %s: tier=%s decision=%s (%s)",
            sym, result["liquidity_tier"], result["decision"], result["reason"]
        )

        if auto_remove and result["decision"] == "REMOVE":
            removed = remove_symbol_everywhere(sym, db_path)
            result["removed"] = removed

    return results


__all__ = [
    "screen_symbol",
    "screen_all_ipos",
    "compute_liquidity_metrics",
    "remove_symbol_everywhere",
    "IPO_SCOPE",
]