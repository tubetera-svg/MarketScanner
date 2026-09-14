"""Resolve app symbols (``NSE:ACHYUT``) to TradingView symbols, cached in SQLite.

Why this exists
---------------
TradingView's embed widget renders "This symbol doesn't exist" when handed a
symbol the exchange mapping does not know. Many tracked IPOs are SME listings
that TradingView only carries under ``BSE:`` (e.g. ``ACHYUT``). Rather than
hard-coding a mapping, the symbol-search endpoint is queried once per symbol and
the result is cached in the ``tv_symbol_cache`` table, so a page view costs zero
network calls after the first resolution.

Resolution order for a base symbol ``X``: ``NSE:X`` then ``BSE:X``, then None
(recorded as a negative cache hit so it is not retried on every request).

This module only ever reaches out to TradingView's public symbol search; it is
never used by the read-only market-data record browser.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Iterable, Optional, Sequence

from . import database

log = logging.getLogger(__name__)

_SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/v3/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tradingview.com/",
    "Origin": "https://www.tradingview.com",
}
PREFERRED_EXCHANGES = ("NSE", "BSE")
# Only Indian equity symbols need the NSE->BSE fallback. Every other exchange
# (OANDA, CAPITALCOM, FOREXCOM, CRYPTO, NSEIX, MCX, ...) already maps 1:1 to a
# TradingView exchange, so those symbols are passed straight through untouched.
INDIAN_EQUITY_EXCHANGES = {"NSE", "BSE"}
# Politeness delay between live lookups (seconds) so bulk resolution is gentle.
LOOKUP_DELAY = 0.4


def strip_exchange(symbol: str) -> str:
    """'NSE:ACHYUT' -> 'ACHYUT' (unchanged when no exchange prefix)."""
    value = str(symbol).strip().upper()
    return value.split(":", 1)[1].strip() if ":" in value else value


def split_exchange(symbol: str) -> tuple[str, str]:
    """('NSE:ACHYUT') -> ('NSE', 'ACHYUT'); ('ACHYUT') -> ('', 'ACHYUT')."""
    value = str(symbol).strip().upper()
    if ":" in value:
        exchange, base = value.split(":", 1)
        return exchange.strip(), base.strip()
    return "", value


def is_passthrough_symbol(symbol: str) -> bool:
    """True when the symbol needs no NSE/BSE lookup on TradingView.

    Anything that is not an NSE/BSE equity symbol (OANDA, CAPITALCOM,
    CRYPTO, bare symbols without an exchange prefix, ...) already maps 1:1
    to a TradingView symbol, so the app symbol is used as-is.
    """
    exchange, _base = split_exchange(symbol)
    if not exchange:
        return True
    return exchange not in PREFERRED_EXCHANGES


def _search(base: str) -> list[str]:
    """Query TradingView symbol search; return candidate 'EXCHANGE:SYMBOL' hits.

    Raises the underlying error type on network/WAF failure so callers can tell
    "not listed" (empty list) apart from "lookup unavailable".
    """
    query = urllib.parse.quote(base, safe="")
    url = f"{_SEARCH_URL}?text={query}&hl=1&exchange=&lang=en&search_type=undefined&domain=production"
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    hits: list[str] = []
    for entry in payload.get("symbols", []):
        exchange = str(entry.get("exchange", "")).strip().upper()
        raw = (
            str(entry.get("symbol", ""))
            .replace("<em>", "")
            .replace("</em>", "")
            .strip()
            .upper()
        )
        if raw != base:
            continue  # only an exact symbol match counts
        if exchange not in PREFERRED_EXCHANGES:
            continue
        value = f"{exchange}:{raw}"
        if value not in hits:
            hits.append(value)
    return hits


def pick_symbol(hits: Iterable[str]) -> Optional[str]:
    """Prefer NSE over BSE from the candidate hits; None when nothing matches."""
    by_exchange = {hit.split(":", 1)[0]: hit for hit in hits}
    for exchange in PREFERRED_EXCHANGES:
        if exchange in by_exchange:
            return by_exchange[exchange]
    return None


def resolve_tv_symbol(
    symbol: str,
    *,
    refresh: bool = False,
    db_path: Optional[str] = None,
) -> dict:
    """Resolve one app symbol to a TradingView symbol (cache-first).

    Returns ``{"symbol", "tv_symbol", "exchange", "cached"}`` where ``tv_symbol``
    is None when TradingView does not carry the symbol on NSE or BSE.
    """
    key = str(symbol).strip().upper()
    if not key:
        raise ValueError("symbol is required")

    # Non-Indian exchanges map straight to TradingView; nothing to resolve and
    # nothing to cache (the app symbol is already the TradingView symbol).
    if is_passthrough_symbol(key):
        exchange, _base = split_exchange(key)
        return {"symbol": key, "tv_symbol": key, "exchange": exchange,
                "cached": False, "passthrough": True}

    if not refresh:
        cached = database.query_tv_symbol([key], db_path=db_path).get(key)
        if cached:
            return {**cached, "cached": True}

    base = strip_exchange(key)
    tv_symbol: Optional[str] = None
    try:
        hits = _search(base)
        tv_symbol = pick_symbol(hits)
    except Exception:  # network/WAF failure – do not poison the cache
        log.warning("TradingView lookup failed for %s", key, exc_info=True)
        return {"symbol": key, "tv_symbol": None, "exchange": None,
                "cached": False, "error": True}

    exchange = tv_symbol.split(":", 1)[0] if tv_symbol else None
    database.upsert_tv_symbol(
        {"symbol": key, "tv_symbol": tv_symbol, "exchange": exchange},
        db_path=db_path,
    )
    return {"symbol": key, "tv_symbol": tv_symbol, "exchange": exchange,
            "cached": False}


def resolve_tv_symbols(
    symbols: Sequence[str],
    *,
    refresh: bool = False,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> dict[str, dict]:
    """Batch resolve, hitting the network only for uncached symbols.

    ``limit`` caps how many live lookups one call performs, so a page rendering
    1,500 rows cannot trigger a 1,500-request burst; unresolved symbols are
    simply retried on the next call.
    """
    keys = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not keys:
        return {}

    out: dict[str, dict] = {}
    if not refresh:
        for key, cached in database.query_tv_symbol(keys, db_path=db_path).items():
            out[key] = {**cached, "cached": True}

    pending = [key for key in keys if key not in out]
    if refresh:
        pending = keys
    for index, key in enumerate(pending[:limit]):
        if index:
            time.sleep(LOOKUP_DELAY)
        resolved = resolve_tv_symbol(key, refresh=True, db_path=db_path)
        resolved["cached"] = False
        out[key] = resolved
    return out
