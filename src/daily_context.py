"""Display-only daily context for strategy rows (TTrades Tier 1 ideas).

Nothing here gates or alters a signal: ``run_strategies`` appends these
``ctx_*`` columns to every strategy's rows so the UI/CSV can show them.

All values are point-in-time for the latest bar of ``daily`` (the signal bar):
ADR averages only the bars *before* it, and the "follow-through" checks judge
the previous bar's wick / equilibrium by the latest bar, never a later one.

* ADR exhaustion  - ``ctx_adr`` (mean High-Low of the prior ``ADR_PERIOD``
  bars) and ``ctx_adr_used_pct`` (latest bar's range as % of it).
* Wick quality    - opposing wick as % of the latest bar's range (lower wick
  for bullish rows, upper for bearish) and a small/medium/large class.
* Candle type     - latest bar vs previous bar (Next Day Model): inside,
  outside, close beyond PDH/PDL (continuation) or swept-and-rejected
  (reversal), with the implied next-day bias and the continuation streak.
* Wick 50% rule   - when the previous bar was a sweep-and-reject, its wick
  midpoint and whether the latest bar respected or closed through it.
* EQ follow-through - whether the latest bar held the upper/lower half of
  the previous bar's range (Candle 4 behaviour).
* Liquidity map   - previous day/week/month high-low and the nearest unswept
  3-bar swing high above / swing low below the latest close.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

import pandas as pd

ADR_PERIOD = 20
SMALL_WICK_PCT = 25.0   # opposing wick <= 25% of range supports expansion
LARGE_WICK_PCT = 50.0   # opposing wick >= 50% of range: wait for the next candle
PHASE_CHANGE_STREAK = 3  # consecutive continuation closures before a phase change is likely
_SWING_LOOKBACK = 60

CANDLE_BIAS = {
    "close_above_pdh": "bullish",
    "swept_pdl_rejected": "bullish",
    "close_below_pdl": "bearish",
    "swept_pdh_rejected": "bearish",
    "inside": "neutral",
    "outside": "neutral",
}

CONTEXT_COLUMNS = [
    "ctx_adr", "ctx_adr_used_pct", "ctx_opposing_wick_pct", "ctx_wick_class",
    "ctx_candle_type", "ctx_next_day_bias", "ctx_cont_streak", "ctx_phase_change",
    "ctx_prev_wick_mid", "ctx_prev_wick_status", "ctx_prev_eq", "ctx_prev_eq_status",
    "ctx_pdh", "ctx_pdl", "ctx_pwh", "ctx_pwl", "ctx_pmh", "ctx_pml",
    "ctx_draw_above", "ctx_draw_below",
]


def _r(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def _ohlc(bar: pd.Series) -> tuple[float, float, float, float]:
    return float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"])


def candle_type(prev: pd.Series, cur: pd.Series) -> str:
    """Classify ``cur`` against ``prev``'s range (Next Day Model closure types)."""
    _, ph, pl, _ = _ohlc(prev)
    _, h, l, c = _ohlc(cur)
    took_high, took_low = h > ph, l < pl
    if took_high and took_low:
        return "outside"
    if took_high:
        return "close_above_pdh" if c > ph else "swept_pdh_rejected"
    if took_low:
        return "close_below_pdl" if c < pl else "swept_pdl_rejected"
    return "inside"


def wick_pcts(bar: pd.Series) -> tuple[Optional[float], Optional[float]]:
    """(upper wick %, lower wick %) of the bar's High-Low range."""
    o, h, l, c = _ohlc(bar)
    rng = h - l
    if rng <= 0:
        return None, None
    return (h - max(o, c)) / rng * 100.0, (min(o, c) - l) / rng * 100.0


def wick_class(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct <= SMALL_WICK_PCT:
        return "small"
    if pct >= LARGE_WICK_PCT:
        return "large"
    return "medium"


def continuation_streak(daily: pd.DataFrame) -> int:
    """Consecutive continuation closures ending at the latest bar (+up / -down)."""
    streak = 0
    for i in range(len(daily) - 1, 0, -1):
        kind = candle_type(daily.iloc[i - 1], daily.iloc[i])
        step = 1 if kind == "close_above_pdh" else (-1 if kind == "close_below_pdl" else 0)
        if step == 0 or (streak and (step > 0) != (streak > 0)):
            break
        streak += step
    return streak


def _prior_period_extremes(daily: pd.DataFrame, freq: str) -> tuple[Optional[float], Optional[float]]:
    """High/low of the last *completed* week ("W") or month ("M") before the latest bar."""
    idx = pd.to_datetime(daily.index)
    periods = idx.to_period(freq)
    current = periods[-1]
    earlier = periods < current
    if not earlier.any():
        return None, None
    prior = periods[earlier].max()
    if not (periods < prior).any():
        return None, None  # history starts inside the prior period: extremes would be partial
    block = daily[periods == prior]
    return float(block["High"].max()), float(block["Low"].min())


def unswept_draws(daily: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """Nearest unswept 3-bar swing high above / swing low below the latest close.

    A swing at bar i needs bar i+1 to exist, so only bars up to the latest are
    used; "unswept" means no later bar (latest included) traded beyond it.
    """
    frame = daily.iloc[-_SWING_LOOKBACK:]
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    close = float(frame["Close"].iloc[-1])
    above: Optional[float] = None
    below: Optional[float] = None
    n = len(frame)
    for i in range(1, n - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and highs[i] > close:
            if highs[i + 1:].max() <= highs[i] and (above is None or highs[i] < above):
                above = float(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and lows[i] < close:
            if lows[i + 1:].min() >= lows[i] and (below is None or lows[i] > below):
                below = float(lows[i])
    return above, below


def compute_daily_context(daily: pd.DataFrame) -> Dict[str, object]:
    """Side-independent context for the latest bar. ``ctx_opposing_wick_pct`` /
    ``ctx_wick_class`` are filled per row by :func:`annotate_frame`."""
    out: Dict[str, object] = {col: None for col in CONTEXT_COLUMNS}
    if daily is None or len(daily) < 2:
        return out
    daily = daily[["Open", "High", "Low", "Close"]].dropna()
    if len(daily) < 2:
        return out
    prev, cur = daily.iloc[-2], daily.iloc[-1]
    _, ph, pl, _ = _ohlc(prev)
    po, _, _, pc = _ohlc(prev)
    _, h, l, c = _ohlc(cur)

    prior = daily.iloc[:-1].tail(ADR_PERIOD)
    adr = float((prior["High"] - prior["Low"]).mean())
    out["ctx_adr"] = _r(adr)
    out["ctx_adr_used_pct"] = _r((h - l) / adr * 100.0) if adr > 0 else None

    upper, lower = wick_pcts(cur)
    out["_upper_wick_pct"], out["_lower_wick_pct"] = upper, lower

    kind = candle_type(prev, cur)
    out["ctx_candle_type"] = kind
    out["ctx_next_day_bias"] = CANDLE_BIAS[kind]
    streak = continuation_streak(daily)
    out["ctx_cont_streak"] = streak
    out["ctx_phase_change"] = abs(streak) >= PHASE_CHANGE_STREAK

    if len(daily) >= 3:
        prev_kind = candle_type(daily.iloc[-3], prev)
        if prev_kind == "swept_pdh_rejected":
            mid = (max(po, pc) + ph) / 2.0
            out["ctx_prev_wick_mid"] = _r(mid)
            out["ctx_prev_wick_status"] = "closed_through" if c > mid else "respected"
        elif prev_kind == "swept_pdl_rejected":
            mid = (min(po, pc) + pl) / 2.0
            out["ctx_prev_wick_mid"] = _r(mid)
            out["ctx_prev_wick_status"] = "closed_through" if c < mid else "respected"

    eq = (ph + pl) / 2.0
    out["ctx_prev_eq"] = _r(eq)
    out["ctx_prev_eq_status"] = "held_upper" if l >= eq else ("held_lower" if h <= eq else "traded_through")

    out["ctx_pdh"], out["ctx_pdl"] = _r(ph), _r(pl)
    wh, wl = _prior_period_extremes(daily, "W")
    mh, ml = _prior_period_extremes(daily, "M")
    out["ctx_pwh"], out["ctx_pwl"] = _r(wh), _r(wl)
    out["ctx_pmh"], out["ctx_pml"] = _r(mh), _r(ml)
    above, below = unswept_draws(daily)
    out["ctx_draw_above"], out["ctx_draw_below"] = _r(above), _r(below)
    return out


def annotate_frame(frame: pd.DataFrame, contexts: Dict[str, Dict[str, object]], side: Optional[int]) -> pd.DataFrame:
    """Return ``frame`` with ``CONTEXT_COLUMNS`` appended per symbol.

    ``side`` is +1 / -1 for the bullish / bearish split; ``None`` falls back to
    each row's ``direction`` column (0 or missing leaves the wick columns empty).
    """
    if frame is None or "symbol" not in frame.columns:
        return frame
    frame = frame.copy()
    for col in CONTEXT_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
        frame[col] = frame[col].astype(object)
    for idx, row in frame.iterrows():
        ctx = contexts.get(str(row["symbol"]).upper())
        if not ctx:
            continue
        for col in CONTEXT_COLUMNS:
            frame.at[idx, col] = ctx.get(col)
        direction = side
        if direction is None:
            try:
                direction = int(row.get("direction") or 0)
            except (TypeError, ValueError):
                direction = 0
        if direction:
            pct = ctx.get("_lower_wick_pct") if direction > 0 else ctx.get("_upper_wick_pct")
            frame.at[idx, "ctx_opposing_wick_pct"] = _r(pct) if pct is not None else None
            frame.at[idx, "ctx_wick_class"] = wick_class(pct)
    return frame
