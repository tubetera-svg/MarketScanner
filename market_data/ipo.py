"""IPO discovery, backfill and watchlist registration for NSE stocks.

Data source: the NSE full-market daily bhavcopy (same files already used by the
NSE source). A stock is treated as a *newly listed* IPO when its EQ symbol
appears in a bhavcopy file on a date where it was not present in a supplied (or
derived) baseline of already-listed symbols, and it is not already tracked in
the local database / watchlist.

Key functions
-------------
- ``discover_new_ipos(start, end, known_symbols)`` - scan a bhavcopy window and
  return symbol / listing_date / listing_price (the listing day's OPEN price).
- ``backfill_ipo_history(symbol, listing_date, end_date)`` - store daily OHLC
  from the listing date onwards into ``ohlc_daily``.
- ``register_ipo(entry)`` - append the symbol to ``config/watchlist.txt``, set
  its category (scope=IPO) in ``config/watchlist_categories.json`` and persist
  IPO metadata in the ``ipo_metadata`` table.

Important limitation (behavioural contract)
------------------------------------------
Discovery is only meaningful when the ``known_symbols`` baseline excludes the
symbols being discovered. If it defaults to the currently-tracked DB/watchlist
universe, every established-but-untracked NSE stock will look like an IPO on
its first appearance in the scanned window. For a *going-forward* detector, pass
``known_symbols = known_symbols_from_bhavcopy(latest_date)`` so the baseline is
the full EQ universe as of a recent date (recommended for automation). For a
*human-curated* catalog, pass the exact pre-existing set.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from . import database
from .config import SOURCE_NSE, source_enabled

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT_DIR / "config" / "watchlist.txt"
CATEGORIES_PATH = ROOT_DIR / "config" / "watchlist_categories.json"

IPO_SCOPE = "IPO"

_all_strategy = None


def _load_all_strategy():
    global _all_strategy
    if _all_strategy is None:
        src_path = str(ROOT_DIR / "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)
        import all_strategy  # type: ignore

        _all_strategy = all_strategy
    return _all_strategy


def _download_bhavcopy(trade_date: date) -> Optional[object]:
    """Mirror nse_source: return the raw bhavcopy DataFrame, if available, else None.

    The return is the raw object from all_strategy's ``_download_bhavcopy_for_date``.
    The caller resolves columns via ``all_strategy._find_column``.
    """
    if not source_enabled(SOURCE_NSE):
        log.warning("IPO scan skipped (FETCH_NSE_DATA disabled)")
        return None
    mod = _load_all_strategy()
    return mod._download_bhavcopy_for_date(trade_date)


def known_symbols_from_bhavcopy(trade_date: date) -> set[str]:
    """Return the full NSE EQ universe present in one bhavcopy file.

    Used to build a realistic *already-listed* baseline for going-forward IPO
    detection so established stocks are not mistaken for new listings.
    """
    df = _download_bhavcopy(trade_date)
    result: set[str] = set()
    if df is None or getattr(df, "empty", True):
        return result
    mod = _load_all_strategy()
    cols = df.columns
    symbol_col = mod._find_column(cols, ["SYMBOL"])
    series_col = mod._find_column(cols, ["SERIES"])
    if not symbol_col or not series_col:
        return result
    mask = df[series_col].astype(str).str.strip().str.upper() == "EQ"
    try:
        values = df.loc[mask, symbol_col]
    except KeyError:  # pragma: no cover - defensive
        return result
    for value in values:
        text = str(value).strip().upper()
        if text:
            result.add(text)
    return result


def _bhavcopy_rows(trade_date: date) -> dict[str, dict]:
    """Map base-symbol -> {open, high, low, close, volume} for EQ rows on a date.

    Returns an empty dict when the file is unavailable. Column resolution mirrors
    ``nse_source.fetch_daily`` so both code paths agree on field names.
    """
    df = _download_bhavcopy(trade_date)
    out: dict[str, dict] = {}
    if df is None or getattr(df, "empty", True):
        return out
    mod = _load_all_strategy()
    cols = df.columns
    find = mod._find_column
    symbol_col = find(cols, ["SYMBOL"])
    series_col = find(cols, ["SERIES"])
    open_col = find(cols, ["OPEN_PRICE", "OPEN"])
    high_col = find(cols, ["HIGH_PRICE", "HIGH"])
    low_col = find(cols, ["LOW_PRICE", "LOW"])
    close_col = find(cols, ["CLOSE_PRICE", "CLOSE"])
    volume_col = find(cols, ["TTL_TRD_QNTY", "TOTAL_TRADED_QUANTITY", "VOLUME"])
    if not all([symbol_col, series_col, open_col, high_col, low_col, close_col]):
        return out
    subset = df[df[series_col].astype(str).str.strip().str.upper() == "EQ"]
    for _, record in subset.iterrows():
        text = str(record[symbol_col]).strip().upper()
        if not text:
            continue
        try:
            out[text] = {
                "open": float(record[open_col]),
                "high": float(record[high_col]),
                "low": float(record[low_col]),
                "close": float(record[close_col]),
                "volume": float(record[volume_col]) if volume_col else None,
            }
        except (TypeError, ValueError):
            continue
    return out


def discover_new_ipos(
    start_date: date,
    end_date: date,
    *,
    known_symbols: Optional[Iterable[str]] = None,
    db_path: Optional[Path | str] = None,
) -> list[dict]:
    """Scan bhavcopy in [start_date, end_date] for newly-appearing EQ symbols.

    A symbol is reported as a candidate IPO on the first date it appears in the
    window only if it is NOT in ``known_symbols`` and NOT already tracked in the
    database (ohlc_daily source=NSE) or the ipo_metadata table.

    listing_date is that first appearance date; listing_price is the OPEN price
    on that day.

    Known-symbols contract: for correct results pass a baseline snapshot (see
    module docstring). If ``known_symbols`` is None the baseline is derived from
    what is already in the local DB + watchlist, which under-detects established
    untracked stocks.
    """
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    baseline: set[str] = set(known_symbols or ())
    if not baseline:
        # Derive a baseline from everything already tracked/synced so an already
        # stored symbol is never re-reported.
        for row in database.query_ipo_metadata(db_path=db_path):
            baseline.add(str(row["symbol"]).split(":", 1)[-1])
        for symbol in database.distinct_symbols(source=SOURCE_NSE):
            baseline.add(str(symbol).split(":", 1)[-1])
        baseline.update(_watchlist_base_symbols())

    first_seen: dict[str, date] = {}
    price_on_first: dict[str, float] = {}
    mod = _load_all_strategy()
    current = start_date
    holidays = set(getattr(mod, "NSE_HOLIDAYS", set()))
    while current <= end_date:
        if current.weekday() < 5 and current not in holidays:
            rows = _bhavcopy_rows(current)
            for base, quote in rows.items():
                if base in baseline:
                    continue
                if base not in first_seen:
                    first_seen[base] = current
                    price_on_first[base] = quote["open"]
        current += timedelta(days=1)

    results = [
        {
            "symbol": f"NSE:{base}",
            "exchange": "NSE",
            "source": SOURCE_NSE,
            "listing_date": first_seen[base].isoformat(),
            "listing_price": price_on_first[base],
        }
        for base in sorted(first_seen, key=lambda b: first_seen[b])
    ]
    log.info(
        "IPO discovery %s..%s: %d new candidate(s)", start_date.isoformat(),
        end_date.isoformat(), len(results),
    )
    return results


def backfill_ipo_history(
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Fetch+store daily OHLC for an NSE IPO from listing date to end_date.

    Uses ``nse_source.fetch_daily`` (only the listed window, so nothing before
    listing is attempted). Returns a summary of stored rows.
    """
    from .sources.nse_source import fetch_daily

    base = str(symbol).split(":", 1)[-1]
    rows = fetch_daily(f"NSE:{base}", start_date, end_date)
    stored = 0
    if rows:
        stored = database.upsert_ohlc(rows, db_path=db_path)
    dates = [row["date"] for row in rows]
    return {
        "symbol": f"NSE:{base}",
        "stored_new": stored,
        "rows": len(rows),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
    }


def _watchlist_base_symbols() -> set[str]:
    try:
        entries = load_entries()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not load watchlist for IPO baseline: %s", exc)
        return set()
    return {str(symbol).split(":", 1)[-1].upper() for symbol, _ in entries}


def _load_watchlist_categories() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(CATEGORIES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_watchlist_categories(categories: dict[str, dict[str, str]]) -> None:
    CATEGORIES_PATH.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2) + "\n",
        encoding="utf-8",
    )


def load_entries() -> list[tuple[str, object]]:
    import ict_scanner  # type: ignore

    return ict_scanner.load_watchlist(str(WATCHLIST_PATH))


def register_ipo(
    entry: dict,
    *,
    db_path: Optional[Path | str] = None,
    issue_price: Optional[float] = None,
) -> dict:
    """Persist one IPO everywhere it needs to live:

    - append ``NSE:<BASE>`` to config/watchlist.txt (idempotent)
    - set scope=IPO in config/watchlist_categories.json
    - upsert a row in the ipo_metadata table

    ``entry`` must contain symbol, listing_date and listing_price (as produced by
    discover_new_ipos). Returns a summary dict.
    """
    symbol = str(entry["symbol"]).strip().upper()
    listing_date = str(entry["listing_date"]).strip()
    listing_price = entry.get("listing_price")

    existing = {sym.upper() for sym, _ in load_entries()}
    if symbol not in existing:
        WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WATCHLIST_PATH.open("a", encoding="utf-8") as file:
            file.write(f"{symbol}\n")

    categories = _load_watchlist_categories()
    existing_cat = categories.get(symbol, {})
    defaults = {
        "asset_class": "equity",
        "exchange": "NSE",
        "scope": IPO_SCOPE,
        "f_and_o": "",
        "sector": "",
        "industry": "",
        "index": "",
        "market_cap": "",
        "liquidity": "",
        "price_range": "",
        "theme": "",
        "listing_date": listing_date,
    }
    defaults.update({k: v for k, v in existing_cat.items() if v})
    if listing_price is not None:
        defaults["listing_price"] = str(listing_price)
    if issue_price is not None:
        defaults["issue_price"] = str(issue_price)
    categories[symbol] = defaults
    _save_watchlist_categories(categories)

    database.upsert_ipo_metadata(
        {
            "symbol": symbol,
            "exchange": "NSE",
            "source": SOURCE_NSE,
            "listing_date": listing_date,
            "listing_price": listing_price,
            "issue_price": issue_price,
        },
        db_path=db_path,
    )

    return {"symbol": symbol, "listing_date": listing_date, "listing_price": listing_price}


def register_ipos(
    entries: Iterable[dict],
    *,
    db_path: Optional[Path | str] = None,
) -> list[dict]:
    """Register multiple IPO entries (see register_ipo). Returns per-symbol summary."""
    return [register_ipo(entry, db_path=db_path) for entry in entries]


def ipo_performance(
    db_path: Optional[Path | str] = None,
    reference_date: Optional[date | str] = None,
) -> list[dict]:
    """IPO performance snapshot: listing vs today (or a reference date) and the
    highest/lowest prices recorded since listing.

    For each row in ipo_metadata this computes, from ohlc_daily:
      - current_price      : the close of the latest stored bar (<= reference date)
      - high_since_listing : max(high) since the listing date
      - low_since_listing  : min(low) since the listing date
      - pct_vs_listing     : (current - listing_price) / listing_price * 100

    ``reference_date`` defaults to today. Symbols with no stored OHLC are still
    returned (with None metrics) so the UI always sees the full IPO universe.
    """
    ref = (
        reference_date
        if isinstance(reference_date, date)
        else date.fromisoformat(reference_date)
        if reference_date
        else date.today()
    )
    metadata = database.query_ipo_metadata(db_path=db_path)
    rows = database.query_ohlc_multi(SOURCE_NSE, [m["symbol"] for m in metadata],
                                     start_date=None, end_date=ref, db_path=db_path)
    out = []
    for meta in metadata:
        symbol = str(meta["symbol"])
        bars = [b for b in rows.get(symbol, []) if b["date"] <= ref.isoformat()]
        item = {
            "symbol": symbol,
            "exchange": str(meta.get("exchange", "NSE")).upper(),
            "listing_date": meta["listing_date"],
            "listing_price": meta.get("listing_price"),
            "issue_price": meta.get("issue_price"),
        }
        if bars:
            item["latest_date"] = bars[-1]["date"]
            item["current_price"] = bars[-1]["close"]
            high = max(float(b["high"]) for b in bars)
            low = min(float(b["low"]) for b in bars)
            item["high_since_listing"] = high
            item["low_since_listing"] = low
            if item["listing_price"]:
                item["pct_vs_listing"] = round(
                    (item["current_price"] - float(item["listing_price"]))
                    / float(item["listing_price"]) * 100, 2,
                )
        else:
            item["latest_date"] = None
            item["current_price"] = None
            item["high_since_listing"] = None
            item["low_since_listing"] = None
            item["pct_vs_listing"] = None
        out.append(item)
    out.sort(key=lambda i: (i["listing_date"], i["symbol"]))
    return out


def run_ipo_backfill(
    limits: Optional[float] = None,
    *,
    days: int = 3 * 366,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Backfill OHLC history for every tracked IPO from listing_date up to today.

    Returns a per-symbol summary plus overall counts. ``days`` caps how far back
    to fetch for any single symbol (defence against an old/bad listing_date),
    and ``limits`` is accepted for signature compatibility (unused)."""
    end = date.today()
    metadata = database.query_ipo_metadata(db_path=db_path)
    results = []
    ok = failed = rows = 0
    for meta in metadata:
        symbol = str(meta["symbol"])
        listing = date.fromisoformat(meta["listing_date"])
        start = max(listing, end - __import__("datetime").timedelta(days=int(days)))
        try:
            summary = backfill_ipo_history(symbol, start, end, db_path=db_path)
            results.append(summary)
            if summary["rows"]:
                ok += 1
                rows += summary["rows"]
            else:
                failed += 1
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            results.append({"symbol": symbol, "error": str(exc)})
    return {"synced": ok, "failed": failed, "rows": rows, "results": results}


def sync_new_ipos(
    start_date: date,
    end_date: date,
    *,
    known_symbols: Optional[Iterable[str]] = None,
    backfill: bool = True,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Discover new NSE IPOs, register them in watchlist/categories/db, then
    optionally backfill their OHLC history. Returns a combined summary."""
    candidates = discover_new_ipos(
        start_date, end_date, known_symbols=known_symbols, db_path=db_path
    )
    registered = register_ipos(candidates, db_path=db_path) if candidates else []
    backfill_summary = (
        run_ipo_backfill(db_path=db_path)
        if backfill
        else {"synced": 0, "failed": 0, "rows": 0, "results": []}
    )
    # backfill only covers symbols already in ipo_metadata (registered above)
    return {
        "window": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "discovered": candidates,
        "registered": registered,
        "backfill": backfill_summary,
        "known_symbols_provided": bool(known_symbols),
    }


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


def _load_watchlist_categories() -> dict[str, dict[str, str]]:
    try:
        return json.loads(CATEGORIES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_watchlist_categories(categories: dict[str, dict[str, str]]) -> None:
    CATEGORIES_PATH.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2) + "\n",
        encoding="utf-8",
    )


def remove_from_watchlist(symbol: str) -> bool:
    """Remove a symbol from watchlist.txt. Returns True if was present and removed."""
    sym = str(symbol).strip().upper()
    symbols = _load_watchlist_symbols()
    if sym in symbols:
        symbols.discard(sym)
        _save_watchlist_symbols(symbols)
        log.info("Removed %s from watchlist.txt", sym)
        return True
    return False


def remove_from_categories(symbol: str) -> bool:
    """Remove a symbol from watchlist_categories.json. Returns True if was present and removed."""
    sym = str(symbol).strip().upper()
    categories = _load_watchlist_categories()
    if sym in categories:
        del categories[sym]
        _save_watchlist_categories(categories)
        log.info("Removed %s from watchlist_categories.json", sym)
        return True
    return False


def remove_ipo_completely(
    symbol: str,
    *,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Remove an IPO symbol from watchlist, categories, and all DB tables.

    Returns summary of what was removed.
    """
    from . import database
    from .config import SOURCE_NSE

    sym = str(symbol).strip().upper()
    removed = {
        "watchlist": False,
        "categories": False,
        "ohlc_daily": 0,
        "ohlc_no_data": 0,
        "ipo_metadata": 0,
        "tv_symbol_cache": 0,
    }

    removed["watchlist"] = remove_from_watchlist(sym)
    removed["categories"] = remove_from_categories(sym)

    db_removed = database.remove_symbol_data(sym, source=SOURCE_NSE, db_path=db_path)
    removed.update(db_removed)

    for table, count in db_removed.items():
        if count:
            log.info("Deleted %d %s rows for %s", count, table, sym)

    return removed


__all__ = [
    "IPO_SCOPE",
    "backfill_ipo_history",
    "discover_new_ipos",
    "ipo_performance",
    "known_symbols_from_bhavcopy",
    "load_entries",
    "register_ipo",
    "register_ipos",
    "remove_from_watchlist",
    "remove_from_categories",
    "remove_ipo_completely",
    "run_ipo_backfill",
    "sync_new_ipos",
]