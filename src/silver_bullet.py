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
        # The market-data layer stores TradingView bars as naive IST wall time.
        # Attach IST before converting so the NY session window stays correct.
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
) -> SilverBulletSignal | None:
    """Return the first confirmed signal visible at ``now`` for one session.

    The 09:00-10:00 range and 10:00-11:00 New York window are evaluated using
    completed bars only. A later bar cannot alter an already returned signal.
    """
    if not is_commodity_symbol(symbol):
        return None
    frame = _frame_for_today(rows, trading_date)
    if frame.empty:
        return None

    current = _timestamp(now or datetime.now(NEW_YORK))
    if current is None:
        return None
    # Exclude the currently forming 15-minute candle; its close is not known yet.
    frame = frame[frame["timestamp"] + pd.Timedelta(minutes=15) <= current]
    range_bars = frame[(frame["timestamp"].dt.time >= time(9, 0)) & (frame["timestamp"].dt.time < time(10, 0))]
    window = frame[(frame["timestamp"].dt.time >= time(10, 0)) & (frame["timestamp"].dt.time < time(11, 0))]
    if len(range_bars) == 0 or len(window) == 0:
        return None

    range_high = float(range_bars["high"].max())
    range_low = float(range_bars["low"].min())
    for _, bar in window.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        if low < range_low and close > range_low:
            return SilverBulletSignal(
                symbol=symbol.upper(), direction="bullish", signal_time=bar["timestamp"].isoformat(),
                range_high=range_high, range_low=range_low, entry=close,
                stop_loss=low, target=range_high,
                note="10:00-11:00 NY sell-side sweep and displacement back above 09:00 range low",
            )
        if high > range_high and close < range_high:
            return SilverBulletSignal(
                symbol=symbol.upper(), direction="bearish", signal_time=bar["timestamp"].isoformat(),
                range_high=range_high, range_low=range_low, entry=close,
                stop_loss=high, target=range_low,
                note="10:00-11:00 NY buy-side sweep and displacement back below 09:00 range high",
            )
    return None
