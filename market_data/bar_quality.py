"""Bar integrity checks (AGENTS.md §2a "Data integrity before signals").

A bad bar is dropped before it can be stored or form a level - never repaired:

- a price that is missing, non-numeric, NaN/inf, zero or negative;
- ``high < low``;
- ``open`` or ``close`` outside ``[low, high]``;
- duplicate timestamps: identical copies collapse to one, copies that disagree
  are all dropped (which one is right is unknown).

A dropped bar becomes "no data" for that date (a gap), never a flat price.
Applied where bars enter the app: ``database.upsert_ohlc`` (storing),
``database.query_ohlc*`` (reading rows stored before this check existed),
``tradingview_source.fetch_timeframe`` / ``fetch_recent_bars`` (intraday and
live bars that are never stored) and the live scanner's TradingView frames
(``clean_frame``).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

PRICE_FIELDS = ("open", "high", "low", "close")
# Float noise allowance when comparing open/close with the high-low range.
REL_TOL = 1e-9
MAX_SAMPLES = 3

# Same rules as bar_problem, for SQL (Status page counts).
SQL_INVALID = (
    "open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL "
    "OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 "
    "OR high < low OR open > high OR open < low OR close > high OR close < low"
)


def bar_problem(open_: Any, high: Any, low: Any, close: Any) -> Optional[str]:
    """Why this bar must be rejected, or None when it is valid."""
    try:
        values = [float(v) for v in (open_, high, low, close)]
    except (TypeError, ValueError):
        return "missing/non-numeric price"
    if not all(math.isfinite(v) for v in values):
        return "NaN/inf price"
    o, h, l, c = values
    if min(values) <= 0:
        return "zero/negative price"
    tol = REL_TOL * h
    if h < l - tol:
        return "high < low"
    if not l - tol <= o <= h + tol:
        return "open outside high-low"
    if not l - tol <= c <= h + tol:
        return "close outside high-low"
    return None


def _report(context: str, rejected: list[str]) -> None:
    if rejected:
        where = f" for {context}" if context else ""
        log.warning("Rejected %d bad bar(s)%s: %s%s", len(rejected), where,
                    "; ".join(rejected[:MAX_SAMPLES]), " ..." if len(rejected) > MAX_SAMPLES else "")


def clean_rows(
    rows: Iterable[dict],
    context: str = "",
    key_fields: Sequence[str] = ("source", "exchange", "symbol", "date"),
) -> list[dict]:
    """Rows with bad bars and conflicting duplicates removed (order kept).

    Rows are dicts with lower-case ``open/high/low/close``; duplicates are
    rows sharing every ``key_fields`` value present in the row.
    """
    rejected: list[str] = []
    valid: list[dict] = []
    for row in rows:
        problem = bar_problem(*(row.get(field) for field in PRICE_FIELDS))
        if problem:
            rejected.append(f"{row.get('symbol', '')} {row.get('date')} {problem}".strip())
        else:
            valid.append(row)
    groups: dict[tuple, list[dict]] = {}
    for row in valid:
        groups.setdefault(tuple(row.get(field) for field in key_fields), []).append(row)
    out: list[dict] = []
    seen: set[tuple] = set()
    for row in valid:
        key = tuple(row.get(field) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        copies = groups[key]
        prices = {tuple(float(c[field]) for field in PRICE_FIELDS) for c in copies}
        if len(prices) > 1:
            rejected.append(f"{row.get('symbol', '')} {row.get('date')} {len(copies)} conflicting copies".strip())
            continue
        out.append(row)
    _report(context, rejected)
    return out


def clean_frame(df: Any, context: str = "") -> Any:
    """A pandas OHLC frame (``open``/``Open`` columns, time index) with bad bars removed."""
    if df is None or len(df) == 0:
        return df
    import pandas as pd

    columns = {str(name).lower(): name for name in df.columns}
    if not all(field in columns for field in PRICE_FIELDS):
        return df
    prices = df[[columns[field] for field in PRICE_FIELDS]].apply(pd.to_numeric, errors="coerce")
    problems = [bar_problem(*values) for values in prices.itertuples(index=False, name=None)]
    rejected = [f"{label} {problem}" for label, problem in zip(df.index, problems) if problem]
    keep = [problem is None for problem in problems]

    # Duplicate timestamps among the valid bars: identical -> keep the first,
    # disagreeing -> drop every copy.
    copies: dict[Any, set[tuple]] = {}
    for position, label in enumerate(df.index):
        if keep[position]:
            copies.setdefault(label, set()).add(tuple(prices.iloc[position]))
    conflicting = {label for label, values in copies.items() if len(values) > 1}
    rejected.extend(f"{label} conflicting copies" for label in conflicting)
    seen: set[Any] = set()
    for position, label in enumerate(df.index):
        if not keep[position]:
            continue
        if label in conflicting or label in seen:
            keep[position] = False
        seen.add(label)
    _report(context, rejected)
    return df if all(keep) else df[keep]
