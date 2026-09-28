#!/usr/bin/env python3
"""CLI script to screen NSE IPO watchlist symbols for liquidity and market presence.

Read-only: nothing is removed. Delete IPOs from the IPO page ("Review list").

Usage:
    python scripts/screen_ipo_liquidity.py [--lookback-days N] [--symbol SYMBOL]
    python scripts/screen_ipo_liquidity.py --help

Options:
    --lookback-days N   Lookback window in days (default: 60 from config)
    --symbol SYMBOL     Screen a single symbol instead of all IPO-scope symbols
    --json              Output results as JSON array
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

from market_data.liquidity_screener import screen_symbol, screen_all_ipos

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

    lookback = args.lookback_days

    if args.symbol:
        symbol = args.symbol.strip().upper()
        log.info("Screening single symbol: %s", symbol)
        results = [screen_symbol(symbol, lookback_days=lookback or 60)]
    else:
        log.info("Screening all IPO-scope symbols...")
        results = screen_all_ipos(lookback_days=lookback or 60)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(f"{r['symbol']:20s} | {r['liquidity_tier']:10s} | {r['decision']:6s} | {r['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())