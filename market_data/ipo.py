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

Eligibility gate (what counts as an IPO here)
--------------------------------------------
A candidate is only ever reported/registered when *all* of these hold:

1. **NSE** - the qualified symbol is ``NSE:<BASE>`` (this module only scans the
   NSE bhavcopy).
2. **Equity** - the symbol is present in the NSE main-board equity master
   (``EQUITY_L.csv`` via ``market_data.equity_master``), which by construction
   excludes ETFs/index funds, Sovereign Gold Bonds, dated government securities
   and rights entitlements. When the master cannot be fetched (offline), a
   symbol-pattern fallback (``NON_IPO_SYMBOL_RE``) is used instead.
3. **Main board** - the bhavcopy series is ``EQ`` (never SME ``SM``/``ST``,
   trade-to-trade ``BE``/``BZ``, debt ``GB``/``GS``/``SG``, ``IV``/``RR``/``E1``).
4. **Traded recently** - it appears in the bhavcopy (which only lists symbols
   that traded) on at least ``IPO_MIN_ACTIVE_RATIO`` of the scanned sessions
   since its first appearance.
5. **Liquidity threshold** - average daily traded value since listing is at
   least ``IPO_MIN_AVG_DAILY_VALUE_CR`` crore (sourced from bhavcopy
   ``TURNOVER_LACS``, else ``volume * close``).

``discover_new_ipos`` applies the whole gate to every candidate; ``register_ipo``
re-applies the instrument-type part (1-3) so a non-equity instrument can never
enter the IPO tracker even if it is registered directly. Deletion of tracked
entries (e.g. delisting / symbol rename / family reclassification) is handled
by the backfill/scrub scripts and the liquidity screener and is NOT gated by
these five conditions - this eligibility logic is for *adding*, not deleting.


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
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from . import database, equity_master
from .config import (
    IPO_MIN_ACTIVE_RATIO,
    IPO_MIN_AVG_DAILY_VALUE_CR,
    LIQUIDITY_LOOKBACK_DAYS,
    SOURCE_NSE,
    source_enabled,
)

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT_DIR / "config" / "watchlist.txt"
CATEGORIES_PATH = ROOT_DIR / "config" / "watchlist_categories.json"

IPO_SCOPE = "IPO"

# --- eligibility constants -------------------------------------------------
#: Bhavcopy series that can host a genuine *main-board* equity IPO. SME (SM/ST),
#: trade-to-trade (BE/BZ), debt (GB/GS/SG) and IV/RR/E1 are never eligible.
MAIN_BOARD_SERIES = frozenset({"EQ"})

#: Non-equity instrument families that still trade in the ``EQ`` series, used as
#: the offline fallback when the NSE equity master is unavailable. Deliberately
#: narrow (e.g. no bare ``LIC``/``ICICI`` prefixes) so real equities such as
#: ``LICHSGFIN`` or ``ICICIGI`` are never excluded:
#:   * rights entitlements      -> ANOND-RE, DUCON-RE1
#:   * dated govt securities    -> 628GS2032, 74GS2035, 79GR2024
#:   * sovereign gold bonds     -> SGBDEC26, SGBOCT27VI
#:   * ETFs / index funds       -> BANKETFADD, NIF10GETF, LIQUIDBETA, NIFTYBEES
NON_IPO_SYMBOL_RE = re.compile(
    r"(?:"
    r"-RE\d*$"
    r"|^\d{1,3}(?:GS|GR|SG)\d{4}[A-Z]?$"
    r"|^SGB[A-Z]{3}\d{2}[A-Z]{0,2}$"
    r"|ETF[A-Z]{0,3}\d{0,4}$"
    r"|GETF$"
    r"|BEES$"
    r"|BETA$"
    r"|ADD$"
    r")"
)

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


def ipo_family_ineligibility_reason(symbol: str) -> Optional[str]:
    """Return why ``symbol`` is not a main-board equity instrument, else ``None``.

    Pattern-only check (no network): catches instrument families that trade in
    the ``EQ`` series but are not equity IPOs - rights entitlements, dated
    government securities, Sovereign Gold Bonds and ETFs/index funds.
    """
    base = str(symbol).strip().upper().split(":", 1)[-1]
    if not base:
        return "empty symbol"
    if NON_IPO_SYMBOL_RE.search(base):
        return (
            f"'{base}' is a non-equity instrument "
            "(bond / SGB / ETF-fund / rights entitlement)"
        )
    return None


def ipo_ineligibility_reason(
    symbol: str,
    *,
    series: Optional[str] = None,
    equity_master_map: Optional[dict[str, str]] = None,
    allow_master_lookup: bool = True,
) -> Optional[str]:
    """Return the reason ``symbol`` cannot be an NSE main-board IPO, else ``None``.

    Conditions 1-3 of the module docstring: NSE exchange, main-board ``EQ``
    series, and equity-instrument membership verified against the NSE equity
    master (with the pattern fallback when the master is unavailable).
    """
    raw = str(symbol).strip().upper()
    if ":" in raw and raw.split(":", 1)[0] != "NSE":
        return f"exchange '{raw.split(':', 1)[0]}' is not NSE"
    base = raw.split(":", 1)[-1]
    if not base:
        return "empty symbol"

    if series is not None and str(series).strip().upper() not in MAIN_BOARD_SERIES:
        return f"series '{str(series).strip().upper()}' is not main-board EQ"

    family_reason = ipo_family_ineligibility_reason(base)
    if family_reason:
        return family_reason

    master = equity_master_map
    if master is None and allow_master_lookup:
        master = equity_master.load_equity_master()
    if master:
        master_series = master.get(base)
        if master_series is None:
            return f"'{base}' is not in the NSE main-board equity master"
        if master_series != "EQ":
            return f"'{base}' is master series '{master_series}', not main-board EQ"
    return None


def is_ipo_eligible(symbol: str, **kwargs) -> bool:
    """True when ``symbol`` may be treated as an NSE main-board equity IPO."""
    return ipo_ineligibility_reason(symbol, **kwargs) is None


def ipo_trading_ineligibility_reason(
    *,
    traded_sessions: int,
    total_sessions: int,
    avg_daily_value_cr: Optional[float],
    min_active_ratio: float = IPO_MIN_ACTIVE_RATIO,
    min_avg_daily_value_cr: float = IPO_MIN_AVG_DAILY_VALUE_CR,
) -> Optional[str]:
    """Return why a candidate fails the traded-recently/liquidity gate, else ``None``.

    ``traded_sessions``/``total_sessions`` come from the bhavcopy scan (the file
    only lists symbols that actually traded). Missing turnover data means the
    liquidity test is skipped rather than failed.
    """
    if total_sessions > 0:
        ratio = traded_sessions / total_sessions
        if ratio < min_active_ratio:
            return (
                f"traded on {traded_sessions}/{total_sessions} sessions "
                f"(needs >= {min_active_ratio:.0%})"
            )
    if avg_daily_value_cr is not None and avg_daily_value_cr < min_avg_daily_value_cr:
        return (
            f"avg daily traded value Rs.{avg_daily_value_cr:.2f} cr "
            f"< Rs.{min_avg_daily_value_cr:.2f} cr"
        )
    return None


def _bhavcopy_rows(trade_date: date) -> dict[str, dict]:
    """Map base-symbol -> EQ quote on a date.

    Fields: ``open``, ``high``, ``low``, ``close``, ``volume``, ``series`` and
    ``turnover_cr`` (daily traded value in rupees crore, from bhavcopy
    ``TURNOVER_LACS`` when present, else ``volume * close``). Only ``EQ`` series
    rows are returned (NSE main board). Returns an empty dict when the file is
    unavailable. Column resolution mirrors ``nse_source.fetch_daily`` so both
    code paths agree on field names.
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
    turnover_col = find(cols, ["TURNOVER_LACS"])
    if not all([symbol_col, series_col, open_col, high_col, low_col, close_col]):
        return out
    subset = df[df[series_col].astype(str).str.strip().str.upper() == "EQ"]
    for _, record in subset.iterrows():
        text = str(record[symbol_col]).strip().upper()
        if not text:
            continue
        try:
            close = float(record[close_col])
            volume = float(record[volume_col]) if volume_col else None
            turnover_cr = None
            if turnover_col:
                try:
                    turnover_cr = float(record[turnover_col]) / 100.0
                except (TypeError, ValueError):
                    turnover_cr = None
            if turnover_cr is None and volume is not None and close:
                turnover_cr = volume * close / 1e7
            out[text] = {
                "open": float(record[open_col]),
                "high": float(record[high_col]),
                "low": float(record[low_col]),
                "close": close,
                "volume": volume,
                "turnover_cr": turnover_cr,
                "series": "EQ",
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
    include_rejected: bool = False,
) -> list[dict]:
    """Scan bhavcopy in [start_date, end_date] for newly-appearing EQ symbols.

    A symbol is reported as a candidate IPO on the first date it appears in the
    window only if it is NOT in ``known_symbols`` and NOT already tracked in the
    database (ohlc_daily source=NSE) or the ipo_metadata table.

    listing_date is that first appearance date; listing_price is the OPEN price
    on that day.

    Every candidate must pass the full eligibility gate described in the module
    docstring (NSE / main-board equity / EQ series / traded recently / liquidity
    threshold). Rejected candidates are logged and dropped; pass
    ``include_rejected=True`` to also receive them as entries flagged
    ``"eligible": False`` with a ``"reject_reason"``.

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
    # base -> {"first_index": int, "traded": int, "value_sum": float, "value_days": int}
    # first_index is the 0-based index of the listing day among scanned sessions,
    # so "sessions since listing" can be derived even when the symbol stops
    # trading (the bhavcopy only lists symbols that traded).
    window_stats: dict[str, dict] = {}
    mod = _load_all_strategy()
    current = start_date
    scanned = 0
    holidays = set(getattr(mod, "NSE_HOLIDAYS", set()))
    while current <= end_date:
        if current.weekday() < 5 and current not in holidays:
            rows = _bhavcopy_rows(current)
            scanned += 1
            for base, quote in rows.items():
                if base in baseline:
                    continue
                if base not in first_seen:
                    first_seen[base] = current
                    price_on_first[base] = quote["open"]
                stats = window_stats.setdefault(
                    base,
                    {"first_index": scanned - 1, "traded": 0, "value_sum": 0.0,
                     "value_days": 0},
                )
                # The bhavcopy only lists symbols that actually traded today.
                stats["traded"] += 1
                turnover = quote.get("turnover_cr")
                if turnover is not None:
                    stats["value_sum"] += float(turnover)
                    stats["value_days"] += 1
        current += timedelta(days=1)

    master = equity_master.load_equity_master()
    accepted: list[dict] = []
    rejected: list[dict] = []
    for base in sorted(first_seen, key=lambda b: (first_seen[b], b)):
        stats = window_stats.get(base, {})
        value_days = int(stats.get("value_days", 0))
        avg_value_cr = (stats.get("value_sum", 0.0) / value_days) if value_days else None
        sessions_since_listing = max(scanned - int(stats.get("first_index", 0)), 1)
        reason = ipo_ineligibility_reason(
            f"NSE:{base}", series="EQ", equity_master_map=master
        )
        if not reason:
            reason = ipo_trading_ineligibility_reason(
                traded_sessions=int(stats.get("traded", 0)),
                total_sessions=sessions_since_listing,
                avg_daily_value_cr=avg_value_cr,
            )
        entry = {
            "symbol": f"NSE:{base}",
            "exchange": "NSE",
            "source": SOURCE_NSE,
            "listing_date": first_seen[base].isoformat(),
            "listing_price": price_on_first[base],
            "eligible": reason is None,
        }
        if reason:
            entry["reject_reason"] = reason
            rejected.append(entry)
            log.info("IPO candidate %s rejected: %s", base, reason)
        else:
            accepted.append(entry)

    log.info(
        "IPO discovery %s..%s: %d eligible, %d rejected",
        start_date.isoformat(),
        end_date.isoformat(),
        len(accepted),
        len(rejected),
    )
    return accepted + rejected if include_rejected else accepted


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

    Raises ``ValueError`` when the symbol is not an NSE main-board equity
    (bonds / SGB / ETFs / funds / rights entitlements / SME / non-EQ series), so
    this class of instrument can never enter the IPO tracker even when
    registered directly. ``entry["series"]`` is honoured when supplied.
    """
    symbol = str(entry["symbol"]).strip().upper()
    listing_date = str(entry["listing_date"]).strip()
    listing_price = entry.get("listing_price")

    reason = ipo_ineligibility_reason(symbol, series=entry.get("series"))
    if reason:
        raise ValueError(f"{symbol} is not an eligible NSE main-board IPO: {reason}")

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
    """Register multiple IPO entries (see register_ipo). Returns per-symbol summary.

    Ineligible symbols are skipped (never partially written) and reported as
    ``{"symbol": ..., "registered": False, "reason": ...}`` so a batch scan
    cannot be aborted by one bad candidate.
    """
    summaries: list[dict] = []
    for entry in entries:
        try:
            summary = register_ipo(entry, db_path=db_path)
        except ValueError as exc:
            symbol = str(entry.get("symbol", "")).strip().upper()
            log.info("IPO registration skipped for %s: %s", symbol, exc)
            summaries.append({"symbol": symbol, "registered": False, "reason": str(exc)})
            continue
        summary["registered"] = True
        summaries.append(summary)
    return summaries


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
    optionally backfill their OHLC history. Returns a combined summary.

    ``skipped`` lists every candidate the eligibility gate rejected (with the
    reason); only eligible candidates are registered.
    """
    candidates = discover_new_ipos(
        start_date,
        end_date,
        known_symbols=known_symbols,
        db_path=db_path,
        include_rejected=True,
    )
    eligible = [item for item in candidates if item.get("eligible", True)]
    skipped = [item for item in candidates if not item.get("eligible", True)]
    registered = register_ipos(eligible, db_path=db_path) if eligible else []
    backfill_summary = (
        run_ipo_backfill(db_path=db_path)
        if backfill
        else {"synced": 0, "failed": 0, "rows": 0, "results": []}
    )
    # backfill only covers symbols already in ipo_metadata (registered above)
    return {
        "window": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "discovered": eligible,
        "skipped": skipped,
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

    Returns summary of what was removed. (Unconditional removal: the five-part
    eligibility gate is for *adding* IPOs, not deleting - see module docstring.)
    """
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
    "MAIN_BOARD_SERIES",
    "NON_IPO_SYMBOL_RE",
    "backfill_ipo_history",
    "discover_new_ipos",
    "ipo_family_ineligibility_reason",
    "ipo_ineligibility_reason",
    "ipo_performance",
    "ipo_trading_ineligibility_reason",
    "is_ipo_eligible",
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