"""One-pass NSE IPO discovery + full-history backfill (last 3 years).

Efficient design: each trading day's bhavcopy is downloaded exactly ONCE and
cached on disk (data/bhavcopy_cache/YYYY-MM-DD.pkl). In the same pass we:
  1. Build the baseline universe from the first trading day of the window.
  2. Detect first-appearance EQ symbols (IPOs) with exact listing date/price.
  3. Accumulate every IPO's daily OHLC rows from listing day to today.
  4. Register IPOs (watchlist + categories scope=IPO + ipo_metadata) and store
     OHLC into the SQLite DB in bulk.

Run per batch (resumable; cached dates are never re-downloaded):

  python scripts/ipo_full_history.py --months 2023-09            # one month
  python scripts/ipo_full_history.py --months 2023-09 2023-10    # several
  python scripts/ipo_full_history.py --months all                # whole window
"""

from __future__ import annotations

import argparse
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

CACHE_DIR = ROOT / "data" / "bhavcopy_cache"

# The shared IPO eligibility gate (market_data.ipo) owns these rules:
# the main-board EQ series and the non-equity instrument families (rights
# entitlements, dated government securities, Sovereign Gold Bonds, ETFs/funds).
from market_data import database, ipo as ipo_service  # noqa: E402

IPO_SERIES = set(ipo_service.MAIN_BOARD_SERIES)
NON_IPO_SYMBOL_RE = ipo_service.NON_IPO_SYMBOL_RE


def is_ipo_candidate(symbol: str, quote: dict) -> bool:
    """True when a freshly-appeared symbol looks like a genuine equity IPO.

    Delegates to ``market_data.ipo.ipo_ineligibility_reason``: NSE -> main-board
    equity master -> ``EQ`` series -> not a bond/SGB/ETF/fund/rights family.
    Trading activity and liquidity for a candidate are still validated later by
    ``validate_tracked_ipo``.
    """
    return ipo_service.ipo_ineligibility_reason(
        symbol, series=str(quote.get("series", "")) or None
    ) is None


import all_strategy as mod  # noqa: E402


def load_bhavcopy(trade_date: date):
    """Download-once, disk-cached full bhavcopy DataFrame (or None)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{trade_date.isoformat()}.pkl"
    if path.exists():
        with open(path, "rb") as fh:
            return pickle.load(fh)
    df = mod._download_bhavcopy_for_date(trade_date)
    if df is None or getattr(df, "empty", True):
        df = None
    with open(path, "wb") as fh:
        pickle.dump(df, fh)
    return df


def bhavcopy_all_rows(trade_date: date) -> dict[str, dict]:
    """All-series rows merged per symbol (EQ preferred when duplicated).

    Using the full series universe for the baseline avoids false IPO positives
    from series migrations (e.g. SM/ST SME stocks moving into EQ).
    """
    df = load_bhavcopy(trade_date)
    if df is None:
        return {}
    cols = df.columns
    find = mod._find_column
    s_col = find(cols, ["SYMBOL"])
    ser_col = find(cols, ["SERIES"])
    o_col = find(cols, ["OPEN_PRICE", "OPEN"])
    h_col = find(cols, ["HIGH_PRICE", "HIGH"])
    l_col = find(cols, ["LOW_PRICE", "LOW"])
    c_col = find(cols, ["CLOSE_PRICE", "CLOSE"])
    v_col = find(cols, ["TTL_TRD_QNTY", "TOTAL_TRADED_QUANTITY", "VOLUME"])
    if not all([s_col, ser_col, o_col, h_col, l_col, c_col]):
        return {}
    out: dict[str, dict] = {}
    for _, rec in df.iterrows():
        sym = str(rec[s_col]).strip().upper()
        if not sym:
            continue
        try:
            quote = {
                "date": trade_date.isoformat(),
                "open": float(rec[o_col]),
                "high": float(rec[h_col]),
                "low": float(rec[l_col]),
                "close": float(rec[c_col]),
                "volume": float(rec[v_col]) if v_col else None,
            }
        except (TypeError, ValueError):
            continue
        series = str(rec[ser_col]).strip().upper() if ser_col else ""
        prev = out.get(sym)
        # prefer EQ rows; keep first seen otherwise
        if prev is None or (series == "EQ" and prev.get("series") != "EQ"):
            quote["series"] = series
            out[sym] = quote
    return out


def month_trading_days(year: int, month: int) -> list[date]:
    holidays = set(getattr(mod, "NSE_HOLIDAYS", set()))
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days, cur = [], date(year, month, 1)
    while cur < nxt:
        if cur.weekday() < 5 and cur not in holidays and cur <= date.today():
            days.append(cur)
        cur += timedelta(days=1)
    return days


def run_batch(year: int, month: int, baseline: set[str], state: dict) -> set[str]:
    """Scan one month; extends `baseline`; updates `state` accumulators."""
    days = month_trading_days(year, month)
    if not days:
        return baseline
    new_this_batch: dict[str, dict] = {}
    for d in days:
        rows = bhavcopy_all_rows(d)
        if not state.get("seeded"):
            # First trading day of the invocation: seed the baseline universe
            # only — existing stocks are NOT IPOs. OHLC for already-tracked
            # IPOs is still accumulated below.
            baseline.update(rows)
            state["seeded"] = True
            print(f"[{year}-{month:02d}] baseline seeded from {d} "
                  f"({len(rows)} equity-like symbols)")
        else:
            for sym, quote in rows.items():
                if sym not in baseline:
                    baseline.add(sym)
                    if (sym not in new_this_batch
                            and is_ipo_candidate(sym, quote)):
                        new_this_batch[sym] = {**quote,
                                               "listing_date": d.isoformat()}
        # accumulate OHLC for every IPO found so far (listing day onwards)
        for sym, quote in rows.items():
            if sym in new_this_batch or sym in state["ipos"]:
                row = {k: v for k, v in quote.items() if k != "series"}
                state["ohlc"].append({"source": "NSE", "symbol": f"NSE:{sym}",
                                      "exchange": "NSE", **row})
        state["days_done"] += 1
    for sym, first in new_this_batch.items():
        if sym in state["ipos"]:
            continue
        state["ipos"][sym] = first
        state["new_registered"].append(sym)
        print(f"  IPO {sym}: listed {first['listing_date']} @ {first['open']}")
    print(f"[{year}-{month:02d}] {len(days)} trading days, "
          f"{len(new_this_batch)} new IPO(s)")
    return baseline


def run_rebuild() -> None:
    """Full rebuild from cached bhavcopy: wipe, detect, register, backfill."""
    cached = sorted(CACHE_DIR.glob("*.pkl"))
    print(f"Rebuild from {len(cached)} cached bhavcopy files")
    days = [p.stem for p in cached]

    # wipe existing IPO tracking
    metas = database.query_ipo_metadata()
    symbols = [str(m["symbol"]) for m in metas]
    if symbols:
        n1 = sum(database.remove_ipo_metadata(s) for s in symbols)
        n2 = database.delete_ohlc(symbols=symbols, source="NSE")
        print(f"wiped {n1} metadata, {n2} ohlc rows")
        base = {s.split(":", 1)[-1] for s in symbols}
        wl = ROOT / "config" / "watchlist.txt"
        lines = [ln for ln in wl.read_text().splitlines()
                 if ln.strip() and ln.split(":", 1)[-1].strip().upper() not in base]
        wl.write_text("\n".join(lines) + "\n")
        import json
        cat_path = ROOT / "config" / "watchlist_categories.json"
        cats = json.loads(cat_path.read_text())
        cats = {k: v for k, v in cats.items() if k not in symbols}
        cat_path.write_text(json.dumps(cats, indent=2))

    baseline: set[str] = set()
    state: dict = {"ipos": {}, "ohlc": [], "days_done": 0, "new_registered": []}
    for d in days:
        rows = bhavcopy_all_rows(date.fromisoformat(d))
        if not baseline:
            # seed universe from first cached day, EQUITY-LIKE series only
            baseline.update(s for s, q in rows.items()
                            if q.get("series") in IPO_SERIES)
            state["days_done"] = 1
            print(f"baseline from {d}: {len(baseline)} equity-like symbols")
            continue
        for sym, quote in rows.items():
            if sym not in baseline:
                baseline.add(sym)
                if sym not in state["ipos"] and is_ipo_candidate(sym, quote):
                    state["ipos"][sym] = {**quote, "listing_date": d}
            if sym in state["ipos"]:
                row = {k: v for k, v in quote.items() if k != "series"}
                state["ohlc"].append({"source": "NSE", "symbol": f"NSE:{sym}",
                                      "exchange": "NSE", **row})
        state["days_done"] += 1

    print(f"scan done: {state['days_done']} days, "
          f"{len(state['ipos'])} IPO candidates")
    # validation + registration happen in main via shared helpers
    state["new_registered"] = list(state["ipos"])
    register_and_store(state)


def register_and_store(state: dict) -> None:
    for sym in state["new_registered"]:
        first = state["ipos"][sym]
        try:
            ipo_service.register_ipo({
                "symbol": f"NSE:{sym}", "exchange": "NSE", "source": "NSE",
                "listing_date": first["listing_date"],
                "listing_price": first.get("open"),
                "series": first.get("series"),
            })
        except ValueError as exc:
            # Eligibility gate rejected the candidate (bond/SGB/ETF/fund/rights
            # or non-EQ series) - never let it into the IPO tracker.
            state["ipos"].pop(sym, None)
            print(f"  skipped {sym}: {exc}")
    known = set(state["ipos"])
    rows = [r for r in state["ohlc"] if str(r["symbol"]).split(":", 1)[-1] in known]
    if rows:
        stored = 0
        for i in range(0, len(rows), 5000):
            stored += database.upsert_ohlc(rows[i:i + 5000])
        print(f"Stored {stored} OHLC rows for {len(known)} IPO(s)")


def validate_tracked_ipo(ipo: dict, symbol_dates: dict[str, list[str]],
                         all_dates: list[str],
                         check_days: int = 12, min_ratio: float = 0.5) -> str:
    """Validate one IPO via post-listing trading activity.

    Modern NSE bhavcopy only lists stocks that TRADED, so illiquid old stocks
    flicker in/out and look like new listings. A genuine IPO trades on most
    of the trading days right after listing. Returns 'pass' | 'fail' |
    'insufficient'.
    """
    sym = str(ipo["symbol"]).split(":", 1)[-1]
    listing = str(ipo["listing_date"])
    after = [d for d in all_dates if d > listing]
    if len(after) < 5:
        return "insufficient"
    traded = set(symbol_dates.get(sym, []))
    window = after[:check_days]
    hits = sum(1 for d in window if d in traded)
    return "pass" if hits / len(window) >= min_ratio else "fail"


def run_validation() -> None:
    """Re-validate every tracked IPO from cached bhavcopy; drop failures."""
    cached = sorted(CACHE_DIR.glob("*.pkl"))
    symbol_dates: dict[str, list[str]] = {}
    for p in cached:
        try:
            df = pickle.load(open(p, "rb"))
        except Exception:
            continue
        if df is None:
            continue
        d = p.stem
        for s in df["SYMBOL"].astype(str).str.strip():
            symbol_dates.setdefault(s, []).append(d)
    all_dates = sorted(p.stem for p in cached)
    metadata = database.query_ipo_metadata()
    removed = kept = insuff = 0
    for meta in metadata:
        verdict = validate_tracked_ipo(meta, symbol_dates, all_dates)
        if verdict == "fail":
            sym = str(meta["symbol"])
            database.remove_ipo_metadata(sym)
            database.delete_ohlc(symbols=[sym], source="NSE")
            base = sym.split(":", 1)[-1]
            wl = ROOT / "config" / "watchlist.txt"
            lines = [ln for ln in wl.read_text().splitlines()
                     if ln.strip() and ln.split(":", 1)[-1].strip().upper() != base]
            wl.write_text("\n".join(lines) + "\n")
            import json
            cat_path = ROOT / "config" / "watchlist_categories.json"
            cats = json.loads(cat_path.read_text())
            cats.pop(sym, None)
            cat_path.write_text(json.dumps(cats, indent=2))
            removed += 1
        elif verdict == "insufficient":
            insuff += 1
        else:
            kept += 1
    print(f"Validation: kept {kept}, removed {removed}, "
          f"insufficient data {insuff}")


def scrub_renames_and_flickers() -> None:
    """Drop tracked IPOs whose symbol existed in bhavcopy before its listing.

    Renames (e.g. ADANITRANS -> ADANIENSOL) and illiquid stocks that missed
    the scan's seed day both look like IPOs. A true IPO has NO rows before its
    listing date; check each candidate against the bhavcopy ~14 days earlier.
    """
    metas = database.query_ipo_metadata()
    pre_dates = sorted({(date.fromisoformat(str(m["listing_date"]))
                         - timedelta(days=14)).isoformat()
                        for m in metas})
    print(f"Pre-date files to check: {len(pre_dates)}")
    pre_syms: dict[str, set[str]] = {}
    for d in pre_dates:
        dd = date.fromisoformat(d)
        # roll back to a weekday if needed
        while dd.weekday() >= 5:
            dd -= timedelta(days=1)
        rows = bhavcopy_all_rows(dd)
        pre_syms[d] = set(rows)
        print(f"  {dd}: {len(rows)} symbols")
    removed = 0
    for m in metas:
        sym = str(m["symbol"]).split(":", 1)[-1]
        listing = str(m["listing_date"])
        pre = (date.fromisoformat(listing) - timedelta(days=14)).isoformat()
        if sym in pre_syms.get(pre, set()):
            qualified = f"NSE:{sym}"
            database.remove_ipo_metadata(qualified)
            database.delete_ohlc(symbols=[qualified], source="NSE")
            wl = ROOT / "config" / "watchlist.txt"
            lines = [ln for ln in wl.read_text().splitlines()
                     if ln.strip() and ln.split(":", 1)[-1].strip().upper() != sym]
            wl.write_text("\n".join(lines) + "\n")
            import json
            cat_path = ROOT / "config" / "watchlist_categories.json"
            cats = json.loads(cat_path.read_text())
            cats.pop(qualified, None)
            cat_path.write_text(json.dumps(cats, indent=2))
            removed += 1
    print(f"Scrub removed {removed} non-IPO entries")


def _drop_tracked(symbols: list[str]) -> None:
    """Remove symbols from ipo_metadata, ohlc, watchlist and categories.
    Deletion is unconditional — there is no protection gate on removal.
    """
    import json
    drop: list[str] = list(symbols)
    candidates = [s for s in symbols if database.remove_ipo_metadata(s)]
    database.delete_ohlc(symbols=symbols, source="NSE")
    base = {s.split(":", 1)[-1].strip().upper() for s in symbols}
    wl = ROOT / "config" / "watchlist.txt"
    lines = [ln for ln in wl.read_text().splitlines()
             if ln.strip() and ln.split(":", 1)[-1].strip().upper() not in base]
    wl.write_text("\n".join(lines) + "\n")
    cat_path = ROOT / "config" / "watchlist_categories.json"
    cats = json.loads(cat_path.read_text())
    for s in symbols:
        cats.pop(s, None)
    cat_path.write_text(json.dumps(cats, indent=2))
    print(f"Dropped {len(candidates)} tracked non-IPO instruments")


def scrub_non_ipo_instruments() -> None:
    """Drop tracked entries that are not genuine equity IPOs.

    Catches the classes the historical detection filter could miss:
      * rights entitlements (ST/BE series)   -> ANOND-RE, DUCON-RE1
      * govt-securities / SGB codes          -> 628GS2032, SGBDEC26
      * ETFs and index funds                 -> BANKETFADD, LIQUIDBETA
    Uses the cached bhavcopy (when present) to confirm the series each symbol
    traded under; the instrument-family test comes from market_data.ipo.
    """
    series_by_symbol: dict[str, set[str]] = {}
    for p in sorted(CACHE_DIR.glob("*.pkl")):
        try:
            df = pickle.load(open(p, "rb"))
        except Exception:
            continue
        if df is None:
            continue
        for _, rec in df.iterrows():
            sym = str(rec["SYMBOL"]).strip().upper()
            series_by_symbol.setdefault(sym, set()).add(
                str(rec["SERIES"]).strip().upper())

    to_drop: list[str] = []
    for meta in database.query_ipo_metadata():
        qualified = str(meta["symbol"])
        base = qualified.split(":", 1)[-1].strip().upper()
        observed = series_by_symbol.get(base)
        if ipo_service.ipo_family_ineligibility_reason(base) or (
                observed and not (observed & IPO_SERIES)):
            to_drop.append(qualified)
    _drop_tracked(to_drop)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", default=None,
                    help="YYYY-MM values, or 'all' (last 3 years)")
    ap.add_argument("--validate-only", action="store_true",
                    help="re-validate tracked IPOs from cache, drop failures")
    ap.add_argument("--rebuild", action="store_true",
                    help="wipe IPO tracking and rebuild from cached bhavcopy")
    ap.add_argument("--no-backfill", action="store_true",
                    help="register only, skip OHLC storage")
    ap.add_argument("--scrub-only", action="store_true",
                    help="only re-scrub tracked IPOs (rename/flicker check)")
    ap.add_argument("--clean-junk", action="store_true",
                    help="drop non-IPO instruments (GS bonds, rights "
                         "entitlements) from tracking")
    args = ap.parse_args()

    if args.clean_junk:
        scrub_non_ipo_instruments()
        return

    if args.scrub_only:
        scrub_renames_and_flickers()
        return

    if args.validate_only:
        run_validation()
        return
    if args.rebuild:
        run_rebuild()
        run_validation()
        return
    if not args.months:
        ap.error("--months is required unless --validate-only")

    today = date.today()
    if args.months == ["all"]:
        start = today - timedelta(days=3 * 366)
        months = []
        y, m = start.year, start.month
        while (y, m) <= (today.year, today.month):
            months.append((y, m))
            m += 1
            if m == 13:
                y, m = y + 1, 1
    else:
        months = []
        for spec in args.months:
            y, m = spec.split("-")
            months.append((int(y), int(m)))

    state: dict = {"ipos": {}, "ohlc": [], "days_done": 0,
                   "new_registered": [], "seeded": False}

    # Skip IPOs already tracked (from previous batches).
    for row in database.query_ipo_metadata():
        base = str(row["symbol"]).split(":", 1)[-1]
        state["ipos"][base] = {"symbol": base,
                               "listing_date": row["listing_date"],
                               "open": row.get("listing_price") or 0}
    print(f"Already tracked IPOs: {len(state['ipos'])}")

    # Pre-seed the baseline from the newest cached bhavcopy BEFORE the batch
    # window, so a stock listing on the batch's first day is still detected
    # (it is already absent from that earlier snapshot).
    batch_days = [d for _y, _m in months for d in month_trading_days(_y, _m)]
    first_day = min(batch_days) if batch_days else None
    baseline: set[str] = set()
    if first_day:
        prior = [p for p in sorted(CACHE_DIR.glob("*.pkl"))
                 if p.stem < first_day.isoformat()]
        if prior:
            seed_rows = bhavcopy_all_rows(date.fromisoformat(prior[-1].stem))
            baseline.update(seed_rows)
            state["seeded"] = True
            print(f"Pre-seeded baseline from {prior[-1].stem} "
                  f"({len(seed_rows)} equity-like symbols)")

    for y, m in months:
        baseline = run_batch(y, m, baseline, state)

    print(f"\nScan complete: {state['days_done']} trading-day files, "
          f"{len(state['new_registered'])} new IPO(s) this run")

    # Register new IPOs (watchlist + categories + ipo_metadata).
    for sym in state["new_registered"]:
        first = state["ipos"][sym]
        try:
            ipo_service.register_ipo({
                "symbol": f"NSE:{sym}", "exchange": "NSE", "source": "NSE",
                "listing_date": first["listing_date"],
                "listing_price": first.get("open"),
                "series": first.get("series"),
            })
        except ValueError as exc:
            state["ipos"].pop(sym, None)
            print(f"  skipped {sym}: {exc}")

    if args.no_backfill:
        return

    # Store OHLC for ALL known IPOs found in this window's files
    # (new + previously registered, so earlier batches complete too).
    known = set(state["ipos"])
    rows = [r for r in state["ohlc"]
            if str(r["symbol"]).split(":", 1)[-1] in known]
    if rows:
        stored = 0
        CHUNK = 5000
        for i in range(0, len(rows), CHUNK):
            stored += database.upsert_ohlc(rows[i:i + CHUNK])
        print(f"Stored {stored} OHLC rows for {len(known)} IPO(s)")


if __name__ == "__main__":
    main()
