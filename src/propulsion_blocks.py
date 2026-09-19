"""Point-in-time propulsion-block detection.

The detector follows the convention used by the strategy runner:

* a contiguous opposite-colour candle series is the originating order block;
* displacement closes beyond that block;
* price retraces into the block and prints an opposite-colour propulsion candle;
* a later displacement close beyond the block confirms the propulsion block;
* a close through the propulsion candle midpoint invalidates it.

The frame passed to :func:`evaluate_propulsion_blocks` must already be truncated
to the evaluation date. No provider or strategy-runner dependency is used here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

PROPULSION_BLOCKS_LOOKBACK_DAYS = 80
STATE_NONE = "none"
STATE_ANTICIPATED = "anticipated"
STATE_CONFIRMED = "confirmed"
STATE_INVALIDATED = "invalidated"


@dataclass
class PropulsionBlock:
    idx: int
    date: object
    direction: int
    order_block_low: float
    order_block_high: float
    order_block_midpoint: float
    propulsion_open: float
    propulsion_high: float
    propulsion_low: float
    mean_threshold: float
    order_block_start: int
    order_block_end: int
    impulse_idx: int
    retrace_idx: int
    confirm_idx: Optional[int] = None
    invalidate_idx: Optional[int] = None
    state: str = STATE_NONE
    confirm_date: Optional[object] = None
    confirmation_price: Optional[float] = None
    invalidate_date: Optional[object] = None


@dataclass
class PropulsionBlockAnalysis:
    events: List[PropulsionBlock] = field(default_factory=list)
    active: Optional[PropulsionBlock] = None
    anticipated: Optional[PropulsionBlock] = None
    bias: int = 0
    note: str = ""


def _ohlc(daily: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    frame = daily.copy()
    for column in ("Open", "High", "Low", "Close"):
        if column not in frame.columns:
            frame[column] = np.nan
    return tuple(
        pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
        for column in ("Open", "High", "Low", "Close")
    )  # type: ignore[return-value]


def _same_colour(o: float, c: float, bullish: bool) -> bool:
    return bool(np.isfinite(o) and np.isfinite(c) and (c > o if bullish else c < o))


def _run_start(o: np.ndarray, c: np.ndarray, end: int, bullish: bool) -> int:
    start = end
    while start > 0 and _same_colour(o[start - 1], c[start - 1], bullish):
        start -= 1
    return start


def _inside_block(o: float, h: float, l: float, c: float, low: float, high: float) -> bool:
    return bool(
        np.isfinite(o) and np.isfinite(h) and np.isfinite(l) and np.isfinite(c)
        and low <= o <= high and low <= c <= high
        and l <= high and h >= low
    )


def _first_after_close(c: np.ndarray, level: float, start: int, bullish: bool) -> Optional[int]:
    for index in range(start, len(c)):
        if np.isfinite(c[index]) and (c[index] > level if bullish else c[index] < level):
            return index
    return None


def _resolve_state(
    c: np.ndarray, confirm_idx: Optional[int], mean_threshold: float, direction: int
) -> tuple[str, Optional[int]]:
    if confirm_idx is None:
        return STATE_ANTICIPATED, None
    for index in range(confirm_idx + 1, len(c)):
        if np.isfinite(c[index]) and (c[index] < mean_threshold if direction > 0 else c[index] > mean_threshold):
            return STATE_INVALIDATED, index
    return STATE_CONFIRMED, None


def _candidate(
    daily: pd.DataFrame, end: int, bullish: bool
) -> Optional[PropulsionBlock]:
    o, h, l, c = _ohlc(daily)
    n = len(c)
    impulse = end + 1
    if impulse >= n or not _same_colour(o[impulse], c[impulse], bullish):
        return None

    start = _run_start(o, c, end, not bullish)
    block_low = float(np.nanmin(l[start:end + 1]))
    block_high = float(np.nanmax(h[start:end + 1]))
    if not np.isfinite(block_low) or not np.isfinite(block_high):
        return None
    if not (c[impulse] > block_high if bullish else c[impulse] < block_low):
        return None
    order_block_midpoint = float((block_low + block_high) / 2.0)

    retrace = None
    for index in range(impulse + 1, n):
        if _inside_block(o[index], h[index], l[index], c[index], block_low, block_high) and _same_colour(o[index], c[index], not bullish):
            retrace = index
            break
    if retrace is None:
        return None

    confirm = _first_after_close(c, block_high if bullish else block_low, retrace + 1, bullish)
    mean_threshold = float((h[retrace] + l[retrace]) / 2.0)
    state, invalidate = _resolve_state(c, confirm, mean_threshold, 1 if bullish else -1)
    return PropulsionBlock(
        idx=retrace,
        date=daily.index[retrace],
        direction=1 if bullish else -1,
        order_block_low=block_low,
        order_block_high=block_high,
        order_block_midpoint=order_block_midpoint,
        propulsion_open=float(o[retrace]),
        propulsion_high=float(h[retrace]),
        propulsion_low=float(l[retrace]),
        mean_threshold=mean_threshold,
        order_block_start=start,
        order_block_end=end,
        impulse_idx=impulse,
        retrace_idx=retrace,
        confirm_idx=confirm,
        invalidate_idx=invalidate,
        state=state,
        confirm_date=daily.index[confirm] if confirm is not None else None,
        confirmation_price=float(c[confirm]) if confirm is not None else None,
        invalidate_date=daily.index[invalidate] if invalidate is not None else None,
    )


def evaluate_propulsion_blocks(
    daily: pd.DataFrame,
    lookback: int = PROPULSION_BLOCKS_LOOKBACK_DAYS,
) -> PropulsionBlockAnalysis:
    if daily.empty or len(daily) < 5:
        return PropulsionBlockAnalysis(note="insufficient_data")
    frame = daily.iloc[-lookback:].copy() if len(daily) > lookback else daily.copy()
    o, _h, _l, c = _ohlc(frame)
    events: List[PropulsionBlock] = []
    for end in range(len(frame) - 1):
        if _same_colour(o[end], c[end], False):
            candidate = _candidate(frame, end, True)
            if candidate is not None:
                events.append(candidate)
        if _same_colour(o[end], c[end], True):
            candidate = _candidate(frame, end, False)
            if candidate is not None:
                events.append(candidate)

    unique = {}
    for event in events:
        unique[(event.direction, event.order_block_start, event.retrace_idx)] = event
    events = list(unique.values())
    confirmed = [event for event in events if event.state == STATE_CONFIRMED]
    anticipated = [event for event in events if event.state == STATE_ANTICIPATED]
    active = max(confirmed, key=lambda event: event.confirm_idx or -1, default=None)
    pending = max(anticipated, key=lambda event: event.retrace_idx, default=None)
    bias = active.direction if active is not None else 0
    note = "no active propulsion block"
    if active is not None:
        note = (
            f"{'bullish' if active.direction > 0 else 'bearish'} propulsion block | "
            f"mean={active.mean_threshold:.2f} | confirmed={active.confirm_date}"
        )
    elif pending is not None:
        note = (
            f"anticipated {'bullish' if pending.direction > 0 else 'bearish'} "
            f"propulsion block | mean={pending.mean_threshold:.2f}; awaiting confirmation"
        )
    return PropulsionBlockAnalysis(events, active, pending, bias, note)


__all__ = [
    "PROPULSION_BLOCKS_LOOKBACK_DAYS",
    "STATE_NONE",
    "STATE_ANTICIPATED",
    "STATE_CONFIRMED",
    "STATE_INVALIDATED",
    "PropulsionBlock",
    "PropulsionBlockAnalysis",
    "evaluate_propulsion_blocks",
]