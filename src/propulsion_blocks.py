"""Point-in-time propulsion-block detection.

The detector follows the convention used by the strategy runner:

* a contiguous opposite-colour candle series is the originating order block;
* displacement closes beyond that block;
* price trades into the block and prints an opposite-colour propulsion candle
  (its wick must reach the block, its close must not go through it; the latest
  such candle before displacement is the propulsion block);
* a later displacement close beyond both the block and the propulsion candle's
  extreme confirms the propulsion block;
* a close through the propulsion candle's mean threshold invalidates it, whether
  that close lands before or after confirmation;
* a block that price closed through before the retrace is not a valid block.

The mean threshold is the midpoint of the propulsion candle's full range
(``MEAN_MODE_RANGE``, default) or of its body (``MEAN_MODE_BODY``); the source
material does not pin it down, so the runner takes it from app settings. A range
midpoint beyond the candle's open falls back to the body midpoint so the stop
never sits on the wrong side of the entry reference.

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

MEAN_MODE_RANGE = "range"  # (high + low) / 2 of the propulsion candle
MEAN_MODE_BODY = "body"    # (open + close) / 2 of the propulsion candle
MEAN_MODES = (MEAN_MODE_RANGE, MEAN_MODE_BODY)


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


def _trades_into_block(h: float, l: float, c: float, low: float, high: float, bullish: bool) -> bool:
    """Propulsion candle wicks into the order block without closing through it.

    Its open may sit outside the block (it trades *into* the block from the
    displacement side); only the close must stay on the block's far side.
    """
    if not (np.isfinite(h) and np.isfinite(l) and np.isfinite(c)):
        return False
    return bool(l <= high and c >= low) if bullish else bool(h >= low and c <= high)


def _mean_threshold(o: float, h: float, l: float, c: float, bullish: bool, mean_mode: str) -> float:
    """Mean threshold of the propulsion candle, always on the stop side of its open.

    In range mode a long wick on the displacement side can push the midpoint
    beyond the open (above it for a bullish block), which would put the stop on
    the wrong side of the entry reference; fall back to the body midpoint then.
    """
    body = float((o + c) / 2.0)
    if mean_mode == MEAN_MODE_BODY:
        return body
    mean = float((h + l) / 2.0)
    if (mean >= o) if bullish else (mean <= o):
        return body
    return mean


def _breaches(close: float, mean_threshold: float, bullish: bool) -> bool:
    return bool(np.isfinite(close) and (close < mean_threshold if bullish else close > mean_threshold))


def _candidate(
    daily: pd.DataFrame, end: int, bullish: bool, mean_mode: str = MEAN_MODE_RANGE
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

    def qualifies(index: int) -> bool:
        return _same_colour(o[index], c[index], not bullish) and _trades_into_block(
            h[index], l[index], c[index], block_low, block_high, bullish
        )

    # The propulsion block is the last opposite-colour candle that trades into
    # the order block before displacement. A later qualifying candle re-anchors
    # it, unless the current one was already invalidated by a mean breach.
    retrace: Optional[int] = None
    mean_threshold = float("nan")
    confirm: Optional[int] = None
    invalidate: Optional[int] = None
    for index in range(impulse + 1, n):
        if retrace is None:
            if np.isfinite(c[index]) and (c[index] < block_low if bullish else c[index] > block_high):
                return None  # block closed through before any retrace: no longer valid
            if qualifies(index):
                retrace = index
                mean_threshold = _mean_threshold(o[index], h[index], l[index], c[index], bullish, mean_mode)
            continue
        if _breaches(c[index], mean_threshold, bullish):
            invalidate = index
            break
        if qualifies(index):
            retrace = index
            mean_threshold = _mean_threshold(o[index], h[index], l[index], c[index], bullish, mean_mode)
            continue
        # Displacement must clear both the order block and the propulsion candle.
        level = max(block_high, h[retrace]) if bullish else min(block_low, l[retrace])
        if np.isfinite(c[index]) and (c[index] > level if bullish else c[index] < level):
            confirm = index
            break
    if retrace is None:
        return None
    if confirm is not None:
        for index in range(confirm + 1, n):
            if _breaches(c[index], mean_threshold, bullish):
                invalidate = index
                break
    if invalidate is not None:
        state = STATE_INVALIDATED
    elif confirm is not None:
        state = STATE_CONFIRMED
    else:
        state = STATE_ANTICIPATED
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
    mean_mode: str = MEAN_MODE_RANGE,
) -> PropulsionBlockAnalysis:
    if daily.empty or len(daily) < 5:
        return PropulsionBlockAnalysis(note="insufficient_data")
    # Indices are shifted back by ``offset`` so they stay positions in ``daily``
    # (the runner compares ``confirm_idx == len(frame) - 1``).
    offset = max(len(daily) - lookback, 0)
    frame = daily.iloc[offset:].copy()
    o, _h, _l, c = _ohlc(frame)
    events: List[PropulsionBlock] = []
    for end in range(len(frame) - 1):
        if _same_colour(o[end], c[end], False):
            candidate = _candidate(frame, end, True, mean_mode)
            if candidate is not None:
                events.append(candidate)
        if _same_colour(o[end], c[end], True):
            candidate = _candidate(frame, end, False, mean_mode)
            if candidate is not None:
                events.append(candidate)

    unique = {}
    for event in events:
        unique[(event.direction, event.order_block_start, event.retrace_idx)] = event
    events = list(unique.values())
    if offset:
        for event in events:
            for name in ("idx", "order_block_start", "order_block_end", "impulse_idx",
                         "retrace_idx", "confirm_idx", "invalidate_idx"):
                value = getattr(event, name)
                if value is not None:
                    setattr(event, name, int(value) + offset)
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
    "MEAN_MODE_RANGE",
    "MEAN_MODE_BODY",
    "MEAN_MODES",
    "PropulsionBlock",
    "PropulsionBlockAnalysis",
    "evaluate_propulsion_blocks",
]