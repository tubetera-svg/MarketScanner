"""Point-in-time AM Silver Bullet detection for commodity symbols."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import re
from zoneinfo import ZoneInfo

import pandas as pd


NEW_YORK = ZoneInfo("America/New_York")
INDIA = ZoneInfo("Asia/Kolkata")
COMMODITY_SYMBOL_RE = re.compile(
    r"(?:GOLD|XAU|SILVER|XAG|OIL|CRUDE|NATURALGAS|NATGAS|COPPER|PLATINUM|PALLADIUM|"
    r"WHEAT|CORN|SOYBEAN|COCOA|COFFEE|SUGAR|COTTON)",
    re.IGNORECASE,
)
COMMODITY_EXCHANGES = {"COMEX", "NYMEX", "CME", "CBOT", "ICE", "MCX"}


@dataclass(frozen=True)
class SilverBulletSignal:
    symbol: str
    direction: str
    signal_time: str
    range_high: float
    range_low: float
    entry: float
    stop_loss: float
    target: float
    note: str


def is_commodity_symbol(symbol: str) -> bool:
    value = str(symbol or "").strip().upper()
    if ":" not in value:
        return False
    exchange, base = value.split(":", 1)
    return exchange in COMMODITY_EXCHANGES or bool(COMMODITY_SYMBOL_RE.search(base))


def _timestamp(value: object) -> pd.Timestamp | None:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        # TradingView rows carry UTC instants; a naive value is the legacy IST
        # wall-time convention. Attach IST before converting so the NY session
        # window stays correct either way.
        parsed = parsed.tz_localize(INDIA)
    return parsed.tz_convert(NEW_YORK)


def _frame_for_today(rows: list[dict], trading_date: date) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    frame = pd.DataFrame(rows)
    if "date" not in frame:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    frame["timestamp"] = frame["date"].map(_timestamp)
    frame = frame.dropna(subset=["timestamp"])
    frame = frame[frame["timestamp"].dt.date == trading_date].sort_values("timestamp")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"])


def evaluate_am_silver_bullet(
    symbol: str,
    rows: list[dict],
    *,
    trading_date: date,
    now: datetime | None = None,
    bar_minutes: int = 5,
) -> SilverBulletSignal | None:
    """Return the first confirmed signal visible at ``now`` for one session.

    Range = the 09:00-10:00 New York hour. Inside 10:00-11:00 New York, once one
    side of the range is swept, the setup confirms on the first fair value gap
    whose middle (displacement) candle is the sweep bar or later and whose third
    candle closes back inside the range. Entry is the FVG edge nearest price,
    the stop is the swing extreme since the sweep, and the target is the
    opposite side of the range. A single bar sweeping both sides, or a sweep of
    the target side before confirmation, voids the session. Completed bars only,
    so a later bar cannot alter an already returned signal.
    """
    if not is_commodity_symbol(symbol):
        return None
    frame = _frame_for_today(rows, trading_date)
    if frame.empty:
        return None

    current = _timestamp(now or datetime.now(NEW_YORK))
    if current is None:
        return None
    # Exclude the currently forming candle; its close is not known yet.
    frame = frame[frame["timestamp"] + pd.Timedelta(minutes=bar_minutes) <= current]
    range_bars = frame[(frame["timestamp"].dt.time >= time(9, 0)) & (frame["timestamp"].dt.time < time(10, 0))]
    window = frame[(frame["timestamp"].dt.time >= time(10, 0)) & (frame["timestamp"].dt.time < time(11, 0))]
    if len(range_bars) == 0 or len(window) == 0:
        return None

    range_high = float(range_bars["high"].max())
    range_low = float(range_bars["low"].min())
    bars = window[["timestamp", "high", "low", "close"]].to_dict("records")
    swept: str | None = None
    sweep_index = 0
    for index, bar in enumerate(bars):
        took_low = float(bar["low"]) < range_low
        took_high = float(bar["high"]) > range_high
        if took_low and took_high:
            return None  # both liquidity pools taken in one bar: no draw left
        if swept is None:
            if not (took_low or took_high):
                continue
            swept, sweep_index = ("bullish", index) if took_low else ("bearish", index)
        elif (swept == "bullish" and took_high) or (swept == "bearish" and took_low):
            return None  # target side ran before the setup confirmed
        # FVG = candles (index-2, index-1, index); the displacement candle
        # (index-1) must be the sweep bar or later.
        if index < 2 or index - 1 < sweep_index:
            continue
        first, third = bars[index - 2], bar
        if swept == "bullish" and float(third["low"]) > float(first["high"]) and float(third["close"]) > range_low:
            fvg_low, fvg_high = float(first["high"]), float(third["low"])
            return SilverBulletSignal(
                symbol=symbol.upper(), direction="bullish", signal_time=third["timestamp"].isoformat(),
                range_high=range_high, range_low=range_low, entry=fvg_high,
                stop_loss=min(float(item["low"]) for item in bars[sweep_index:index + 1]), target=range_high,
                note=f"10:00-11:00 NY sell-side sweep, displacement FVG {fvg_low:g}-{fvg_high:g} back above 09:00 range low",
            )
        if swept == "bearish" and float(third["high"]) < float(first["low"]) and float(third["close"]) < range_high:
            fvg_low, fvg_high = float(third["high"]), float(first["low"])
            return SilverBulletSignal(
                symbol=symbol.upper(), direction="bearish", signal_time=third["timestamp"].isoformat(),
                range_high=range_high, range_low=range_low, entry=fvg_low,
                stop_loss=max(float(item["high"]) for item in bars[sweep_index:index + 1]), target=range_low,
                note=f"10:00-11:00 NY buy-side sweep, displacement FVG {fvg_low:g}-{fvg_high:g} back below 09:00 range high",
            )
    return None
