"""Protected Swings — self-contained detection module.

A *protected swing* is a swing high or swing low that is expected to hold while
the current trend continues. It forms in one of two ways:

* **Sweep-based** — price pierces a recent short-term swing extreme (a liquidity
  sweep), then a candle *closes* beyond the **open (body)** of the series of
  same-direction candles that formed that extreme.
* **FVG-based** — price trades into a fair value gap, then a candle closes beyond
  the **open (body)** of the three candles that created the gap.

A swing is only **confirmed** once the qualifying close occurs; before that it is
**anticipated**. After confirmation the swing is **invalidated** if price closes
back beyond the swept extreme for sweep events or the protected level for FVG events.

This module contains *only* point-in-time, deterministic detection logic that
operates on a daily OHLC ``DataFrame`` (columns ``Open/High/Low/Close`` indexed
by ``Date``). It has no dependency on ``all_strategy.py`` so it can be unit-tested
in isolation with a synthetic DataFrame. The thin ``run_protected_swings`` runner
that adapts these primitives to the platform's ``StrategyExecution`` contract
lives in ``all_strategy.py`` (mirroring ``run_weekly_profile``).

Repainting / look-ahead note: every primitive scans only bars up to the last
available index. ``evaluate_protected_swings`` is therefore safe to call on a
frame already truncated to ``as_of_date`` (the backtest engine slices the
``daily_map`` per date, so no future bar ever reaches these functions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

Candle = Tuple[float, float, float, float]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
PROTECTED_SWINGS_LOOKBACK_DAYS = 80
SWING_LEFT_BARS = 2
SWING_RIGHT_BARS = 2
FVG_LOOKBACK_BARS = 30

STATE_NONE = "none"
STATE_ANTICIPATED = "anticipated"
STATE_CONFIRMED = "confirmed"
STATE_INVALIDATED = "invalidated"

MODE_SWEEP = "sweep"
MODE_FVG = "fvg"

# Formation-basis tags (which of the two protected-swing pathways produced an
# event). Distinct from ``mode`` in that these are the consumer-facing labels
# surfaced on each ``ProtectedSwing`` for filtering/UI tagging.
TAG_SWEEP_BASED = "sweep_based"
TAG_FVG_BASED = "fvg_based"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class SwingPoint:
    """A fractal swing high or low identified on the OHLC series."""

    idx: int
    date: object
    high: float
    low: float
    close: float
    is_high: bool  # True = swing high, False = swing low


@dataclass
class FVGap:
    """A 3-candle fair value gap (same definition as ``ict_scanner``)."""

    idx: int             # index of the 3rd (confirming) candle
    fvg_type: str        # "bullish" | "bearish"
    gap_low: float       # bottom of the gap zone
    gap_high: float      # top of the gap zone
    series_start: int    # index of the 1st candle of the 3-candle group
    series_end: int      # index of the 3rd candle (== idx)


@dataclass
class ProtectedSwing:
    """A detected protected-swing candidate with its full lifecycle state."""

    idx: int                  # source index (swing or FVG confirming candle)
    date: object
    direction: int            # +1 bullish (protected low), -1 bearish (protected high)
    swing_level: float        # the swept extreme (low for bullish, high for bearish)
    protected_level: float    # the level a close must get beyond to confirm
    mode: str                 # "sweep" | "fvg"
    sweep_idx: Optional[int] = None
    confirm_idx: Optional[int] = None
    invalidate_idx: Optional[int] = None
    series_start: int = 0
    series_end: int = 0
    state: str = STATE_NONE
    confirm_date: Optional[object] = None
    confirmation_price: Optional[float] = None
    invalidate_date: Optional[object] = None
    # diagnostic
    gap_id: Optional[str] = None
    tag: str = "unknown"  # formation basis: "sweep_based" | "fvg_based"


@dataclass
class ProtectedSwingAnalysis:
    """Aggregate result of scanning one daily series."""

    events: List[ProtectedSwing] = field(default_factory=list)
    active: Optional[ProtectedSwing] = None      # most recent confirmed (live) swing
    anticipated: Optional[ProtectedSwing] = None  # most recent swept, unconfirmed
    bias: int = 0
    note: str = ""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _ensure_daily(daily: pd.DataFrame) -> pd.DataFrame:
    out = daily.copy()
    for col in ("Open", "High", "Low", "Close"):
        if col not in out.columns:
            out[col] = np.nan
    return out


def _ohlc_arrays(daily: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (open, high, low, close) float arrays in positional order."""
    d = _ensure_daily(daily)
    o = pd.to_numeric(d["Open"], errors="coerce").to_numpy(dtype=float)
    h = pd.to_numeric(d["High"], errors="coerce").to_numpy(dtype=float)
    l = pd.to_numeric(d["Low"], errors="coerce").to_numpy(dtype=float)
    c = pd.to_numeric(d["Close"], errors="coerce").to_numpy(dtype=float)
    return o, h, l, c


def _collect_run(o: np.ndarray, c: np.ndarray, anchor: int, down: bool) -> List[int]:
    """Maximal run of same-direction candles ending at the nearest qualifying
    candle at or before ``anchor``.

    ``down=True`` collects down-close candles (close <= open); ``down=False``
    collects up-close candles (close >= open). The run is walked backward from
    the anchor so that the swing extreme itself is always included.
    """
    n = len(c)
    if n == 0 or anchor < 0 or anchor >= n:
        return []
    up = not down
    # Find the end of the run: the largest index <= anchor that qualifies.
    k = anchor
    while k >= 0 and not ((c[k] < o[k]) if down else (c[k] > o[k])):
        # treat a flat candle (close == open) as qualifying for neither direction
        # unless it is the anchor itself (so the swing is never lost).
        if k == anchor:
            break
        k -= 1
    if k < 0:
        return [anchor]
    run = [k]
    j = k - 1
    while j >= 0 and ((c[j] < o[j]) if down else (c[j] > o[j])):
        run.append(j)
        j -= 1
    run.reverse()
    if anchor not in run:
        run.append(anchor)
    return run


def _collect_prior_run(o: np.ndarray, c: np.ndarray, anchor: int, down: bool) -> List[int]:
    """Collect the contiguous same-direction candles ending at ``anchor``.

    Unlike ``_collect_run``, this returns no series when the anchor candle is
    not in the requested direction. It is used when the candle immediately
    before a pierce must start the confirmation series.
    """
    if anchor < 0 or anchor >= len(c):
        return []
    qualifies = (c[anchor] < o[anchor]) if down else (c[anchor] > o[anchor])
    if not qualifies:
        return []
    run = [anchor]
    j = anchor - 1
    while j >= 0 and ((c[j] < o[j]) if down else (c[j] > o[j])):
        run.append(j)
        j -= 1
    run.reverse()
    return run


def _first_after(arr: np.ndarray, level: float, after_idx: int, above: bool, inclusive: bool = False) -> Optional[int]:
    """First index after (or at) ``after_idx`` where ``arr[i]`` is above/below
    ``level`` (``above=True`` -> arr[i] > level; ``above=False`` -> arr[i] < level).

    If ``inclusive=True``, the bar at ``after_idx`` itself is considered.
    If ``inclusive=False`` (default for backward compatibility), only bars
    strictly after ``after_idx`` are considered.
    """
    if after_idx is None or (inclusive and after_idx >= len(arr)) or (not inclusive and after_idx >= len(arr) - 1):
        return None
    if inclusive:
        slice_start = after_idx
    else:
        slice_start = after_idx + 1

    if above:
        hits = np.flatnonzero(arr[slice_start:] > level)
    else:
        hits = np.flatnonzero(arr[slice_start:] < level)
    if len(hits) == 0:
        return None
    return int(hits[0]) + slice_start


def _first_le_after(arr: np.ndarray, level: float, after_idx: int, high: bool) -> Optional[int]:
    """First index strictly after ``after_idx`` where a candle's low (high)
    reaches into the ``<= level`` zone. Used for "price trades into the FVG".
    """
    lo = arr[after_idx + 1:]
    if high:
        hits = np.flatnonzero(arr[after_idx + 1:] >= level)  # high >= level (into gap from below)
    else:
        hits = np.flatnonzero(arr[after_idx + 1:] <= level)  # low <= level
    if len(hits) == 0:
        return None
    return int(hits[0]) + (after_idx + 1)


# ---------------------------------------------------------------------------
# 1. Swing-point (fractal) detection
# ---------------------------------------------------------------------------
def detect_swing_points(
    daily: pd.DataFrame,
    left: int = SWING_LEFT_BARS,
    right: int = SWING_RIGHT_BARS,
) -> List[SwingPoint]:
    """Fractal swing highs/lows: a high that is the max in [i-left, i+right]
    (and strictly greater than at least one neighbour to break plateaus), and
    symmetrically for lows.

    These are the *candidate* short-term swing extremes that a sweep may later
    target.
    """
    d = _ensure_daily(daily)
    h = pd.to_numeric(d["High"], errors="coerce").to_numpy(dtype=float)
    l = pd.to_numeric(d["Low"], errors="coerce").to_numpy(dtype=float)
    c = pd.to_numeric(d["Close"], errors="coerce").to_numpy(dtype=float)
    idx = d.index
    n = len(h)
    out: List[SwingPoint] = []
    if n < left + right + 2:
        return out
    for i in range(left, n - right):
        lo = i - left
        hi = i + right
        window_h = h[lo:hi + 1]
        window_l = l[lo:hi + 1]
        is_high = False
        is_low = False
        if np.isfinite(h[i]) and h[i] == window_h.max():
            if (i > 0 and h[i] > h[i - 1]) or (i + 1 < n and h[i] > h[i + 1]):
                is_high = True
        if np.isfinite(l[i]) and l[i] == window_l.min():
            if (i > 0 and l[i] < l[i - 1]) or (i + 1 < n and l[i] < l[i + 1]):
                is_low = True
        if is_high:
            out.append(SwingPoint(i, idx[i], float(h[i]), float(l[i]), float(c[i]), True))
        if is_low:
            out.append(SwingPoint(i, idx[i], float(h[i]), float(l[i]), float(c[i]), False))
    return out


# ---------------------------------------------------------------------------
# 2. FVG detection (3-candle, mirrors ict_scanner.detect_bullish/bearish_fvg)
# ---------------------------------------------------------------------------
def find_fvgs(
    daily: pd.DataFrame,
    lookback: int = FVG_LOOKBACK_BARS,
) -> List[FVGap]:
    """Scan the recent window for 3-candle fair value gaps.

    Bullish: candle1.high < candle3.low with candle2 bullish (close>open).
    Bearish: candle1.low  > candle3.high with candle2 bearish (close<open).

    This is the same *definition* used by ``ict_scanner.detect_bullish_fvg`` /
    ``detect_bearish_fvg`` but applied across the whole recent window so that
    every gap — not only the most recent — can be evaluated for a protected
    swing.
    """
    d = _ensure_daily(daily)
    o = pd.to_numeric(d["Open"], errors="coerce").to_numpy(dtype=float)
    h = pd.to_numeric(d["High"], errors="coerce").to_numpy(dtype=float)
    l = pd.to_numeric(d["Low"], errors="coerce").to_numpy(dtype=float)
    c = pd.to_numeric(d["Close"], errors="coerce").to_numpy(dtype=float)
    n = len(c)
    gaps: List[FVGap] = []
    if n < 3:
        return gaps
    start = max(2, n - lookback)
    for i in range(2, n):
        if i < start:
            continue
        c1h, c1l = h[i - 2], l[i - 2]
        o2, h2, l2, c2 = o[i - 1], h[i - 1], l[i - 1], c[i - 1]
        c3h, c3l = h[i], l[i]
        if np.isfinite(c1h) and np.isfinite(c3l) and c1h < c3l and c2 > o2:
            gaps.append(FVGap(i, "bullish", float(c1h), float(c3l), i - 2, i))
        if np.isfinite(c1l) and np.isfinite(c3h) and c1l > c3h and c2 < o2:
            gaps.append(FVGap(i, "bearish", float(c3h), float(c1l), i - 2, i))
    return gaps


# ---------------------------------------------------------------------------
# 3. Sweep / confirmation detection
# ---------------------------------------------------------------------------
def detect_liquidity_sweep(
    daily: pd.DataFrame,
    swing_idx: int,
    is_high: bool,
) -> Optional[int]:
    """First bar *after* ``swing_idx`` whose extreme pierces the swing point.

    ``is_high=True`` (bearish sweep): a bar whose high > swing high.
    ``is_high=False`` (bullish sweep): a bar whose low < swing low.
    Returns the index or ``None`` if the swing was never pierced.
    """
    o, h, l, c = _ohlc_arrays(daily)
    if is_high:
        return _first_after(h, h[swing_idx], swing_idx, above=True)
    return _first_after(l, l[swing_idx], swing_idx, above=False)


def confirm_close(
    daily: pd.DataFrame,
    protected_level: float,
    after_idx: int,
    above: bool,
    inclusive: bool = True,
) -> Optional[int]:
    """First bar at or after (or strictly after) ``after_idx`` whose close pierces
    ``protected_level``.

    The bar at ``after_idx`` itself is included (same-bar confirmation),
    in addition to subsequent bars.

    ``above=True`` (bullish confirm): close > protected_level.
    ``above=False`` (bearish confirm): close < protected_level.

    ``inclusive=False`` requires a distinct close after the sweep/pierce bar.
    """
    o, h, l, c = _ohlc_arrays(daily)
    return _first_after(c, protected_level, after_idx, above=above, inclusive=inclusive)


def invalidate_close(
    daily: pd.DataFrame,
    protected_level: float,
    after_idx: int,
    above: bool,
) -> Optional[int]:
    """First bar *strictly after* ``after_idx`` whose close pierces ``protected_level``
    in the *opposite* direction to confirmation (the protection breaking).
    """
    o, h, l, c = _ohlc_arrays(daily)
    return _first_after(c, protected_level, after_idx, above=above, inclusive=False)


# ---------------------------------------------------------------------------
# 4. Candidate builders + lifecycle resolution
# ---------------------------------------------------------------------------
def _series_extremes(daily: pd.DataFrame, run: List[int], for_low: bool) -> float:
    o, h, l, c = _ohlc_arrays(daily)
    if not run:
        return np.nan
    if for_low:
        return float(np.nanmax(h[run]))  # protection level for a protected low = series HIGH
    return float(np.nanmin(l[run]))      # protection level for a protected high = series LOW


def _build_sweep_candidate(
    daily: pd.DataFrame,
    swing: SwingPoint,
) -> Optional[ProtectedSwing]:
    """Build a sweep-based protected-swing candidate from a swing point.

    Returns ``None`` when the swing has never been swept (no activation).
    """
    o, h, l, c = _ohlc_arrays(daily)
    idx = daily.index
    j = swing.idx
    if swing.is_high:
        direction = -1  # bearish protected high
        swing_level = swing.high
        sweep_idx = detect_liquidity_sweep(daily, j, is_high=True)
        if sweep_idx is None:
            return None
        run = _collect_run(o, c, sweep_idx, down=False) or [sweep_idx]
        protected_level = float(np.nanmin(o[run]))  # body: lowest open of the green sweep series
        confirm_idx = confirm_close(daily, protected_level, sweep_idx, above=False, inclusive=False)
        swing_break_idx = _first_after(c, swing_level, sweep_idx, above=True)
        if swing_break_idx is not None and (confirm_idx is None or swing_break_idx <= confirm_idx):
            confirm_idx = None
        invalidate_idx = (
            invalidate_close(daily, swing_level, confirm_idx, above=True)
            if confirm_idx is not None
            else None
        )
        gap_id = None
    else:
        direction = 1  # bullish protected low
        swing_level = swing.low
        sweep_idx = detect_liquidity_sweep(daily, j, is_high=False)
        if sweep_idx is None:
            return None
        run = _collect_run(o, c, sweep_idx, down=True) or [sweep_idx]
        protected_level = float(np.nanmax(o[run]))  # body: highest open of the red sweep series
        confirm_idx = confirm_close(daily, protected_level, sweep_idx, above=True, inclusive=False)
        swing_break_idx = _first_after(c, swing_level, sweep_idx, above=False)
        if swing_break_idx is not None and (confirm_idx is None or swing_break_idx <= confirm_idx):
            confirm_idx = None
        invalidate_idx = (
            invalidate_close(daily, swing_level, confirm_idx, above=False)
            if confirm_idx is not None
            else None
        )
        gap_id = None

    state = _lifecycle_state(daily, confirm_idx, invalidate_idx)
    return ProtectedSwing(
        idx=j,
        date=idx[j],
        direction=direction,
        swing_level=swing_level,
        protected_level=protected_level,
        mode=MODE_SWEEP,
        sweep_idx=sweep_idx,
        confirm_idx=confirm_idx,
        invalidate_idx=invalidate_idx,
        series_start=int(run[0]),
        series_end=int(run[-1]),
        state=state,
        confirm_date=(idx[confirm_idx] if confirm_idx is not None else None),
        confirmation_price=(float(c[confirm_idx]) if confirm_idx is not None else None),
         invalidate_date=(idx[invalidate_idx] if invalidate_idx is not None else None),
         gap_id=gap_id,
         tag=TAG_SWEEP_BASED,
     )


def _build_fvg_candidate(
    daily: pd.DataFrame,
    gap: FVGap,
) -> Optional[ProtectedSwing]:
    """Build an FVG-based protected-swing candidate from a fair value gap.

    Returns ``None`` when price has never traded into the gap.
    """
    o, h, l, c = _ohlc_arrays(daily)
    idx = daily.index
    i = gap.idx  # 3rd (confirming) candle of the 3-candle group
    if gap.fvg_type == "bullish":
        direction = 1
        # price trades into the gap: a bar whose low reaches the gap zone
        entry_idx = _first_le_after(l, gap.gap_high, i, high=False)
        if entry_idx is None:
            return None
        sweep_idx = entry_idx
        series = _collect_prior_run(o, c, entry_idx - 1, down=True)
        if not series:
            return None
        swing_level = float(np.nanmin(l[series]))
        protected_level = float(np.nanmax(o[series]))  # body: highest open of red series
        confirm_idx = confirm_close(daily, protected_level, sweep_idx, above=True)
        invalidate_idx = (
            invalidate_close(daily, protected_level, confirm_idx, above=False)
            if confirm_idx is not None
            else None
        )
    else:
        direction = -1
        entry_idx = _first_le_after(h, gap.gap_low, i, high=True)
        if entry_idx is None:
            return None
        sweep_idx = entry_idx
        series = _collect_prior_run(o, c, entry_idx - 1, down=False)
        if not series:
            return None
        swing_level = float(np.nanmax(h[series]))
        protected_level = float(np.nanmin(o[series]))  # body: lowest open of green series
        confirm_idx = confirm_close(daily, protected_level, sweep_idx, above=False)
        invalidate_idx = (
            invalidate_close(daily, protected_level, confirm_idx, above=True)
            if confirm_idx is not None
            else None
        )
    state = _lifecycle_state(daily, confirm_idx, invalidate_idx)
    return ProtectedSwing(
        idx=i,
        date=idx[i],
        direction=direction,
        swing_level=swing_level,
        protected_level=protected_level,
        mode=MODE_FVG,
        sweep_idx=sweep_idx,
        confirm_idx=confirm_idx,
        invalidate_idx=invalidate_idx,
        series_start=int(series[0]),
        series_end=int(series[-1]),
        state=state,
        confirm_date=(idx[confirm_idx] if confirm_idx is not None else None),
        confirmation_price=(float(c[confirm_idx]) if confirm_idx is not None else None),
         invalidate_date=(idx[invalidate_idx] if invalidate_idx is not None else None),
         gap_id=f"{gap.fvg_type}:{i}",
         tag=TAG_FVG_BASED,
     )


def _lifecycle_state(daily: pd.DataFrame, confirm_idx: Optional[int], invalidate_idx: Optional[int]) -> str:
    """Resolve the terminal-ish state given the bars currently available.

    The caller evaluates on a frame already truncated to ``as_of_date``, so the
    last index is the latest *known* close — this keeps the state point-in-time.
    """
    n = len(daily)
    if confirm_idx is None:
        return STATE_ANTICIPATED
    if invalidate_idx is not None and invalidate_idx <= n - 1:
        return STATE_INVALIDATED
    return STATE_CONFIRMED


# ---------------------------------------------------------------------------
# 5. Top-level evaluation
# ---------------------------------------------------------------------------
def _dedupe_events(events: List[ProtectedSwing]) -> List[ProtectedSwing]:
    seen: set = set()
    out: List[ProtectedSwing] = []
    for ev in events:
        key = (ev.direction, ev.mode, round(ev.protected_level, 8), ev.series_start, ev.series_end)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def evaluate_protected_swings(
    daily: pd.DataFrame,
    swing_left: int = SWING_LEFT_BARS,
    swing_right: int = SWING_RIGHT_BARS,
    fvg_lookback: int = FVG_LOOKBACK_BARS,
    swing_lookback: int = PROTECTED_SWINGS_LOOKBACK_DAYS,
) -> ProtectedSwingAnalysis:
    """Point-in-time evaluation of protected swings on a daily OHLC frame.

    ``daily`` must be truncated to the desired ``as_of_date`` (the backtest
    engine and the live runner both do this). Returns the full event timeline,
    the currently-active (most-recent confirmed, non-invalidated) swing, the most
    recent anticipated swing, and the implied bias.
    """
    d = _ensure_daily(daily)
    if d.empty or len(d) < 5:
        return ProtectedSwingAnalysis(events=[], active=None, anticipated=None, bias=0, note="insufficient_data")

    # Restrict the scan to the recent window (keeps older noise out).
    if len(d) > swing_lookback:
        d = d.iloc[-swing_lookback:].copy()

    events: List[ProtectedSwing] = []

    for swing in detect_swing_points(d, left=swing_left, right=swing_right):
        ev = _build_sweep_candidate(d, swing)
        if ev is not None:
            events.append(ev)

    for gap in find_fvgs(d, lookback=fvg_lookback):
        ev = _build_fvg_candidate(d, gap)
        if ev is not None:
            events.append(ev)

    events = _dedupe_events(events)

    confirmed = [e for e in events if e.state == STATE_CONFIRMED]
    confirmed.sort(key=lambda e: (e.confirm_idx if e.confirm_idx is not None else -1), reverse=True)
    active = confirmed[0] if confirmed else None

    anticipated = [e for e in events if e.state == STATE_ANTICIPATED]
    anticipated.sort(key=lambda e: (e.sweep_idx if e.sweep_idx is not None else -1), reverse=True)
    anticip = anticipated[0] if anticipated else None

    bias = active.direction if active is not None else 0

    parts: List[str] = []
    if active is not None:
        level_name = "fvg" if active.mode == MODE_FVG else "swing"
        parts.append(
            f"{('bullish' if active.direction > 0 else 'bearish')} protected "
            f"{'low' if active.direction > 0 else 'high'} via {active.mode} | "
            f"protect={active.protected_level:.2f} {level_name}={active.swing_level:.2f} | "
            f"confirmed={active.confirm_date}"
        )
    if anticip is not None and active is None:
        parts.append(
            f"anticipated {('bullish' if anticip.direction > 0 else 'bearish')} "
            f"protected {('low' if anticip.direction > 0 else 'high')} via {anticip.mode} "
            f"@ protect={anticip.protected_level:.2f}; awaiting confirmation"
        )
    note = "; ".join(parts) if parts else "no active protected swing"
    return ProtectedSwingAnalysis(
        events=events,
        active=active,
        anticipated=anticip,
        bias=bias,
        note=note,
    )


# ---------------------------------------------------------------------------
# Public primitive used by tests / UI consumers
# ---------------------------------------------------------------------------
__all__ = [
    "ProtectedSwing",
    "ProtectedSwingAnalysis",
    "SwingPoint",
    "FVGap",
    "Candle",
    "PROTECTED_SWINGS_LOOKBACK_DAYS",
    "SWING_LEFT_BARS",
    "SWING_RIGHT_BARS",
    "FVG_LOOKBACK_BARS",
    "STATE_NONE",
    "STATE_ANTICIPATED",
    "STATE_CONFIRMED",
    "STATE_INVALIDATED",
    "MODE_SWEEP",
    "MODE_FVG",
    "TAG_SWEEP_BASED",
    "TAG_FVG_BASED",
    "detect_swing_points",
    "find_fvgs",
    "detect_liquidity_sweep",
    "confirm_close",
    "invalidate_close",
    "evaluate_protected_swings",
]
