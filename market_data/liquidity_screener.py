"""NSE IPO Liquidity Screener.

Screens IPO-scope symbols in the watchlist for liquidity and market presence,
then decides ADD/KEEP/WATCH/REMOVE per the configured thresholds.

Screening never deletes; removal is ``ipo.remove_ipo_completely`` via the IPO
page review (DELETE /api/market-data/ipo).
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .config import (
    SOURCE_NSE,
    LIQUID_AVG_DAILY_VALUE_CR,
    BORDERLINE_AVG_DAILY_VALUE_CR,
    LIQUID_MAX_ZERO_DAYS,
    BORDERLINE_MAX_ZERO_DAYS,
    MARKET_CAP_MIN_CR,
    FREE_FLOAT_MIN_PCT,
    LIQUIDITY_LOOKBACK_DAYS,
)
from . import state_store
from .service import get_ohlc, ist_today

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CATEGORIES_PATH = ROOT_DIR / "config" / "watchlist_categories.json"
IPO_SCOPE = "IPO"


def _load_categories() -> dict[str, dict[str, str]]:
    try:
        return json.loads(state_store.read_text(CATEGORIES_PATH))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


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
    - median_daily_value_cr: median daily traded value in crores (used for the
      tier - one listing-day or block-deal spike can inflate the average)
    - zero_trade_days: count of days with zero volume
    - circuit_locked_days: traded days with high == low (stuck at a price band,
      one-sided book - effectively untradeable for one side)
    - total_days: total trading days in window
    - latest_close: most recent close price
    - avg_daily_range_pct: mean (high-low)/close. Informational only - this is
      volatility, not a bid-ask spread (bhavcopy has no quotes), so it does not
      affect the liquidity tier.
    - sufficient_data: bool, True if enough history to decide
    """
    end = ist_today()
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
            "median_daily_value_cr": 0.0,
            "zero_trade_days": 0,
            "circuit_locked_days": 0,
            "total_days": 0,
            "latest_close": None,
            "avg_daily_range_pct": None,
            "sufficient_data": False,
        }

    if not bars:
        return {
            "avg_daily_value_cr": 0.0,
            "median_daily_value_cr": 0.0,
            "zero_trade_days": 0,
            "circuit_locked_days": 0,
            "total_days": 0,
            "latest_close": None,
            "avg_daily_range_pct": None,
            "sufficient_data": False,
        }

    values = []
    zero_days = 0
    locked_days = 0
    ranges = []

    for bar in bars:
        vol = bar.get("volume") or 0
        close = bar.get("close") or 0
        high = bar.get("high") or close
        low = bar.get("low") or close

        if vol > 0 and close > 0:
            values.append(vol * close)
            if high == low:
                locked_days += 1
        else:
            values.append(0.0)
            zero_days += 1

        if close > 0:
            ranges.append(((high - low) / close) * 100)

    total_days = len(bars)
    avg_daily_value_cr = sum(values) / total_days / 1e7 if total_days > 0 else 0.0
    median_daily_value_cr = statistics.median(values) / 1e7 if values else 0.0

    avg_daily_range_pct = sum(ranges) / len(ranges) if ranges else None

    return {
        "avg_daily_value_cr": round(avg_daily_value_cr, 4),
        "median_daily_value_cr": round(median_daily_value_cr, 4),
        "zero_trade_days": zero_days,
        "circuit_locked_days": locked_days,
        "total_days": total_days,
        "latest_close": bars[-1].get("close") if bars else None,
        "avg_daily_range_pct": round(avg_daily_range_pct, 2) if avg_daily_range_pct else None,
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


def _illiquid_days(metrics: dict) -> int:
    """Zero-volume days plus circuit-locked days."""
    return metrics.get("zero_trade_days", 0) + metrics.get("circuit_locked_days", 0)


def _describe(metrics: dict) -> str:
    return (
        f"median daily value Rs.{metrics['median_daily_value_cr']:.2f} cr "
        f"(avg Rs.{metrics['avg_daily_value_cr']:.2f} cr), "
        f"zero-trade days {metrics['zero_trade_days']}/{metrics['total_days']}, "
        f"circuit-locked days {metrics['circuit_locked_days']}"
    )


def _classify_liquidity(metrics: dict) -> str:
    """Classify as LIQUID/BORDERLINE/ILLIQUID on median value and illiquid days."""
    median_val = metrics.get("median_daily_value_cr", 0)
    illiquid_days = _illiquid_days(metrics)

    if median_val >= LIQUID_AVG_DAILY_VALUE_CR and illiquid_days <= LIQUID_MAX_ZERO_DAYS:
        return "LIQUID"

    if median_val >= BORDERLINE_AVG_DAILY_VALUE_CR or illiquid_days <= BORDERLINE_MAX_ZERO_DAYS:
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
        reason = f"LIQUID: {_describe(metrics)}"
    elif liquidity_tier == "BORDERLINE" or len(flags) == 1:
        decision = "WATCH"
        flag_str = f", flags: {', '.join(flags)}" if flags else ""
        reason = f"BORDERLINE: {_describe(metrics)}{flag_str}"
    else:
        decision = "REMOVE"
        flag_str = f", flags: {', '.join(flags)}" if flags else ""
        illiq_reason = []
        if metrics.get("median_daily_value_cr", 0) < BORDERLINE_AVG_DAILY_VALUE_CR:
            illiq_reason.append(
                f"median value Rs.{metrics['median_daily_value_cr']:.2f} cr < Rs.{BORDERLINE_AVG_DAILY_VALUE_CR} cr"
            )
        if _illiquid_days(metrics) > BORDERLINE_MAX_ZERO_DAYS:
            illiq_reason.append(
                f"zero-trade + circuit-locked days {_illiquid_days(metrics)} > {BORDERLINE_MAX_ZERO_DAYS}"
            )
        reason = f"ILLIQUID: {', '.join(illiq_reason) if illiq_reason else 'failed liquidity thresholds'}{flag_str}"

    return {
        "symbol": symbol,
        "liquidity_tier": liquidity_tier,
        "flags": flags,
        "decision": decision,
        "reason": reason,
    }


def screen_all_ipos(
    lookback_days: int = LIQUIDITY_LOOKBACK_DAYS,
    db_path: Optional[Path | str] = None,
) -> list[dict]:
    """Screen all IPO-scope symbols in watchlist (read-only, removes nothing).

    Returns list of screening results for all symbols. Deletion is explicit via
    the IPO page review (DELETE /api/market-data/ipo).
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

    return results


__all__ = [
    "screen_symbol",
    "screen_all_ipos",
    "compute_liquidity_metrics",
    "IPO_SCOPE",
]