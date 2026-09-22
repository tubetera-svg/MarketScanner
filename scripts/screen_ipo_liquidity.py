#!/usr/bin/env python3
"""CLI script to screen NSE IPO watchlist symbols for liquidity and market presence.

Usage:
    python scripts/screen_ipo_liquidity.py [--auto-remove] [--lookback-days N] [--symbol SYMBOL]
    python scripts/screen_ipo_liquidity.py --help

Options:
    --auto-remove       Immediately remove symbols with REMOVE decision
    --lookback-days N   Lookback window in days (default: 60 from config)
    --symbol SYMBOL     Screen a single symbol instead of all IPO-scope symbols
    --json              Output results as JSON array
    --dry-run           Show what would be removed without actually removing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from market_data.liquidity_screener import screen_symbol, screen_all_ipos, remove_symbol_everywhere

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Screen NSE IPO watchlist symbols for liquidity and market presence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--auto-remove",
        action="store_true",
        help="Immediately remove symbols with REMOVE decision from watchlist and DB",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without actually removing (implies --auto-remove)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Lookback window in days (default: from config LIQUIDITY_LOOKBACK_DAYS=60)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Screen a single symbol (e.g. NSE:ANKITMETAL) instead of all IPO-scope symbols",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON array",
    )
    args = parser.parse_args()

    if args.dry_run:
        args.auto_remove = True

    lookback = args.lookback_days

    if args.symbol:
        symbol = args.symbol.strip().upper()
        log.info("Screening single symbol: %s", symbol)
        result = screen_symbol(symbol, lookback_days=lookback or 60)
        results = [result]

        if args.auto_remove and result["decision"] == "REMOVE":
            if args.dry_run:
                log.info("DRY-RUN: Would remove %s", symbol)
                result["would_remove"] = True
            else:
                removed = remove_symbol_everywhere(symbol)
                result["removed"] = removed
                log.info("Removed %s: %s", symbol, removed)
    else:
        log.info("Screening all IPO-scope symbols...")
        results = screen_all_ipos(lookback_days=lookback or 60, auto_remove=args.auto_remove and not args.dry_run)

        if args.auto_remove and args.dry_run:
            for result in results:
                if result["decision"] == "REMOVE":
                    result["would_remove"] = True
                    log.info("DRY-RUN: Would remove %s (%s)", result["symbol"], result["reason"])

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(f"{r['symbol']:20s} | {r['liquidity_tier']:10s} | {r['decision']:6s} | {r['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())