"""Delete non-equity instruments (bonds / SGB / ETFs / funds / rights
entitlements) that were mis-tracked as NSE main-board IPOs.

Removes each symbol from **watchlist.txt**, **watchlist_categories.json** and
every market-data table that holds it (``ohlc_daily`` history, ``ohlc_no_data``,
``ipo_metadata``, ``tv_symbol_cache``) via the existing
``market_data.ipo.remove_ipo_completely`` helper - so the ``/ipo`` tracker,
the watchlist and the historic OHLC store all lose the symbol together.

Usage:
    python scripts/remove_non_ipo_symbols.py                # apply
    python scripts/remove_non_ipo_symbols.py --dry-run      # preview only
    python scripts/remove_non_ipo_symbols.py --audit        # list tracked
                                                           # entries failing the
                                                           # new eligibility gate

The symbols below were flagged from the live NSE bhavcopy: dated government
securities (``####GS####``), Sovereign Gold Bonds (``SGB*``), ETF / index-fund
units and rights entitlements (``*-RE``). The eligibility gate in
``market_data.ipo`` now rejects these families automatically, so this list is a
one-off back-cleanup.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from market_data import database  # noqa: E402
from market_data import ipo as ipo_service  # noqa: E402
from market_data import equity_master  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,  # keep PowerShell from treating progress logs as errors
)
log = logging.getLogger("remove_non_ipo_symbols")

GOVERNMENT_SECURITIES = """
628GS2032 633GS2035 636GS2031 645GS2029 657GS2033A 662GS2051 664GS2027
664GS2035 667GS2035 679GS2034 679GS2034A 692GS2039 695GS2061 706GS2046
710GS2029 716GS2050 717GS2028 717GS2030 718GS2033 719GS2060 723GS2039
736GS2052 74GS2035 757GS2033 75GS2034 772GS2055 795GS2032 813GS2045
824GS2033 826GS2027 828GS2027 832GS2032 83GS2042 883GS2041
""".split()

SOVEREIGN_GOLD_BONDS = """
SGBDEC26 SGBFEB27 SGBOCT27 SGBOCT27VI SGBSEP27
""".split()

ETF_AND_FUNDS = """



BANKETFADD GOLDETFADD ITETFADD LIQUIDBETA NIF10GETF NIF5GETF NIFITETF SILVRETF
""".split()

RIGHTS_ENTITLEMENTS = """
ANOND-RE DUCON-RE1 JAYKAY-RE1 MPEL-RE RATNA-RE VHLTD-RE1
""".split()

FLAGGED_GROUPS: dict[str, list[str]] = {
    "government securities": GOVERNMENT_SECURITIES,
    "SGB / gold bonds": SOVEREIGN_GOLD_BONDS,
    "ETFs / non-stock funds": ETF_AND_FUNDS,
    "rights / RE instruments": RIGHTS_ENTITLEMENTS,
}


def audit_tracked() -> None:
    """Report tracked IPOs that fail the new eligibility gate."""
    rows = database.query_ipo_metadata(source="NSE")
    family_hits: list[tuple[str, str]] = []
    master_absent: list[str] = []
    master = equity_master.load_equity_master()
    for row in rows:
        symbol = str(row["symbol"]).strip().upper()
        base = symbol.split(":", 1)[-1]
        family_reason = ipo_service.ipo_family_ineligibility_reason(base)
        if family_reason:
            family_hits.append((symbol, family_reason))
        elif master and base not in master:
            master_absent.append(symbol)

    print(f"Tracked IPOs: {len(rows)}")
    print(f"Non-equity instrument families still tracked: {len(family_hits)}")
    for symbol, reason in family_hits:
        print(f"   {symbol:20s} {reason}")
    print(
        f"Absent from the NSE equity master: {len(master_absent)} "
        "(delisted / renamed / SME / newly listed - review manually)"
    )
    for symbol in master_absent[:20]:
        print(f"   {symbol}")
    if len(master_absent) > 20:
        print(f"   ... and {len(master_absent) - 20} more")


def remove_flagged(dry_run: bool) -> int:
    """Delete the flagged symbols everywhere; returns the number processed."""
    total = 0
    table_totals: dict[str, int] = {}
    for group, symbols in FLAGGED_GROUPS.items():
        print(f"\n== {group} ({len(symbols)}) ==")
        for base in symbols:
    symbol = f"NSE:{base}"
    if dry_run:
        present = [
            name for name, found in (
                ("watchlist", _in_watchlist(base)),
                ("categories", _in_categories(symbol)),
                ("ipo_metadata", bool(database.query_ipo_metadata(
                    symbols=[symbol], source="NSE"))),
                ("ohlc_daily", bool(database.query_ohlc(
                    "NSE", symbol, None, None))),
            ) if found
        ]
        print(f"   {symbol:20s} would remove from: {', '.join(present) or 'nothing'}")
    else:
        removed = ipo_service.remove_ipo_completely(symbol)
        detail = ", ".join(f"{name}={count}" for name, count in removed.items())
        print(f"   {symbol:20s} {detail}")
        for name, count in removed.items():
            if isinstance(count, int):
                table_totals[name] = table_totals.get(name, 0) + count
    total += 1

    if not dry_run:
        print("\nTotals removed:")
        for name, count in sorted(table_totals.items()):
            print(f"   {name:16s} {count}")
    print(f"\n{'Would process' if dry_run else 'Processed'} {total} symbol(s)")
    return total


def _in_watchlist(base: str) -> bool:
    path = ROOT_DIR / "config" / "watchlist.txt"
    if not path.exists():
        return False
    wanted = f"NSE:{base}".upper()
    return any(
        line.strip().upper() == wanted
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _in_categories(symbol: str) -> bool:
    import json

    path = ROOT_DIR / "config" / "watchlist_categories.json"
    try:
        return symbol.upper() in json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be removed without changing anything")
    parser.add_argument("--audit", action="store_true",
                        help="only list tracked entries failing the eligibility gate")
    args = parser.parse_args()

    if args.audit:
        audit_tracked()
        return 0
    remove_flagged(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
