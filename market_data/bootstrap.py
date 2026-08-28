"""CLI: initial load of daily OHLC into data/market_data.db.

Stores the last N calendar days (default 14 - two weeks) for every watchlist
symbol, respecting the FETCH_TRADINGVIEW_DATA / FETCH_NSE_DATA /
AUTO_FETCH_MISSING_DATA flags.

Usage:
    python -m market_data.bootstrap                # whole watchlist, 14 days
    python -m market_data.bootstrap --days 10 --source NSE
    python -m market_data.bootstrap --symbols RELIANCE,INFY --days 7
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("market_data.bootstrap")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap SQLite market-data store")
    parser.add_argument("--days", type=int, default=14, help="Calendar days to backfill (default 14)")
    parser.add_argument("--source", default=None, choices=["NSE", "TRADINGVIEW"], help="Limit to one source")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols (default: watchlist)")
    parser.add_argument("--as-of", default="", help="Anchor date YYYY-MM-DD (default: today)")
    return parser.parse_args(argv)


def load_entries(root: Path) -> list[tuple[str, object]]:
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    import ict_scanner  # type: ignore

    return ict_scanner.load_watchlist(str(root / "config" / "watchlist.txt"))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent

    sys.path.insert(0, str(root))
    from market_data.config import backdate_lookback_days
    from market_data.service import resolve_session_source, sync_symbol_range

    entries = load_entries(root)
    if args.symbols.strip():
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        entries = [(sym, sess) for sym, sess in entries if sym.upper() in wanted]

    anchor = args.as_of.strip() or date.today().isoformat()
    days = args.days if args.days and args.days != 14 else backdate_lookback_days()

    ok = failed = 0
    for index, (symbol, session) in enumerate(entries, start=1):
        source_name = args.source or resolve_session_source(symbol, session)
        summary = sync_symbol_range(source_name, symbol, anchor, days)
        if any(note.startswith("sync failed") for note in summary.notes):
            failed += 1
        else:
            ok += 1
        log.info(
            "[%d/%d] %-24s (%s) rows=%d new=%d missing=%s",
            index, len(entries), symbol, source_name,
            len(summary.rows), summary.fetched_new, summary.missing_dates or "-",
        )

    log.info("Done. stored-ok=%d failed=%d db=%s", ok, failed, root / "data" / "market_data.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
