"""
ICT Daily Bias — Adaptive Scanner
==================================

UPDATED VERSION

Implements:
  - Section 19: Adaptive Scanning Frequency
  - Section 20: Stock Scanning Priority
  - Section 21: NSE Market-Day Rule
  - Daily + Weekly directional bias
  - PDH / PDL / PWH / PWL liquidity
  - Intraday liquidity-sweep detection
  - Bullish / bearish FVG detection
  - Displacement detection
  - FVG-based POI detection
  - ICT-style setup confirmation
  - Entry zone / SL / TP1 / TP2
  - Risk/reward filtering
  - Adaptive 15m / 5m / 1m monitoring
  - Repeated Tier A / Tier B alerts (UNCHANGED)
  - Repeated Tier A + Liquidity Event alerts (UNCHANGED)
  - TradingView tvDatafeed support
  - OANDA v20 REST support
  - NSE and Forex/commodity session handling
  - Daily result files

IMPORTANT:
  This is a rule-based ICT-style scanner, not an execution engine.
  The exact definitions of FVG, displacement, liquidity sweep and POI
  are configurable approximations and should be backtested before
  being used for live trading.

WATCHLIST:
    RELIANCE
    OANDA:XAUUSD
    CAPITALCOM:NATURALGAS
    SOME_SYMBOL|nse
    SOME_SYMBOL|forex

TradingView:
    pip install --upgrade --no-cache-dir git+https://github.com/rongardF/tvdatafeed.git

OANDA:
    pip install requests

Environment:
    OANDA_API_KEY=xxxxx

QUICK CONFIG GUIDE — where to change common settings
=====================================================
  Scan frequency per tier (A/B/C/D)
      -> TIER_INTERVAL_SEC dict, below.
         (Tier.A: 5min, Tier.B: 15min, Tier.C: 30min, Tier.D: 60min)

  Scan frequency per live setup state
  (far from POI / approaching / liquidity event / confirmed)
      -> CHECK_INTERVALS_SEC dict, below.

  Minimum time between checks for NSE symbols
  (floor applied on top of the two intervals above;
   forex/commodities are NOT floored and keep the
   intervals above as-is)
      -> NSE_MIN_CHECK_INTERVAL_SEC constant, below.

  Which tiers get written to the results file
      -> AdaptiveScanner(output_tiers={...}) — see __main__ below.
         Defaults to {Tier.A} (Tier A only).

  How many bars are pulled per fetch (daily/weekly/intraday)
      -> ICTConfig(daily_bars=..., weekly_bars=..., intraday_bars=...)
         — see ict_config in __main__ below.

  ICT thresholds (POI proximity, displacement size, min RR, etc.)
      -> ICTConfig(...) — see ict_config in __main__ below.

  Operating window (hours the scanner runs at all)
      -> OPERATING_START / OPERATING_END / OPERATING_TZ in __main__.

  Results file size cap (prepend read/rewrite cost)
      -> AdaptiveScanner(max_results_file_bytes=...) in __main__.

  Cache file locations (daily/weekly OHLC + tracker scheduling state)
      -> OHLCCache(path=...) / TrackerStateCache(path=...) in __main__.
         Default: ohlc_cache.json / tracker_state_cache.json next to
         this script.
"""

from __future__ import annotations

import os
import json
import time
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import re

# ==================================================
# OPTIONAL WINDOWS ALERT SUPPORT
# ==================================================

try:
    import winsound
except ImportError:
    winsound = None

# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

log = logging.getLogger("ict_scanner")

# ==================================================
# TIMEZONES
# ==================================================

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ==================================================
# MARKET SESSION TYPES
# ==================================================


class Session(Enum):
    NSE = "nse"
    FOREX_24_5 = "forex_24_5"
    CRYPTO_24_7 = "crypto_24_7"


FOREX_24_5_PREFIXES = {
    "OANDA",
    "FOREXCOM",
    "CAPITALCOM",
    "FXCM",
    "PEPPERSTONE",
    "IC MARKETS",
    "ICMARKETS",
    "SAXO",
    "TVC",
    "CURRENCYCOM",
}


COMMODITY_RE = re.compile(r"(?:NATURALGAS|UKOIL|USOIL|XAUUSD|XAGUSD|COPPER|SILVER|GOLD)", re.I)


def detect_session(symbol: str) -> Session:
    """
    Auto-detect session type.

    Examples:
        OANDA:XAUUSD          -> FOREX_24_5
        CAPITALCOM:NATURALGAS -> FOREX_24_5
        CRYPTO:BTCUSD         -> CRYPTO_24_7 (24x7 instrument)
        NSE:RELIANCE          -> NSE
        RELIANCE              -> NSE
    """
    if ":" in symbol:
        prefix = symbol.split(":", 1)[0].strip().upper()
        if prefix in FOREX_24_5_PREFIXES:
            return Session.FOREX_24_5
        if prefix == "NSE":
            return Session.NSE
        if prefix == "CRYPTO":
            return Session.CRYPTO_24_7
    return Session.NSE


def categorize_symbol(raw: str) -> dict:
    """Normalize and classify an exchange-qualified symbol.

    Returns the canonical ``EXCHANGE:BASE`` symbol together with its detected
    session, exchange, asset class and the UI scope group it maps to. Raises
    ``ValueError`` when the input is not in ``EXCHANGE:BASE`` form.

    Scope mapping (aligned with the frontend watchlist groups):
        crypto      -> "Crypto"
        commodity   -> "Commodities"
        forex       -> "Forex"
        equity/etc  -> "F&O"
    """
    symbol = raw.strip().upper()
    if not symbol or ":" not in symbol or any(char.isspace() for char in symbol):
        raise ValueError(
            "Use an exchange-qualified symbol, for example NSE:INFY, OANDA:XAUUSD or CRYPTO:BTCUSD"
        )
    exchange, base = symbol.split(":", 1)
    exchange = exchange.strip()
    base = base.strip()
    if not base:
        raise ValueError(f"Missing ticker after '{exchange}:' in '{raw}'")

    session = detect_session(symbol)
    if exchange == "CRYPTO":
        asset_class = "crypto"
    elif session == Session.FOREX_24_5:
        asset_class = "commodity" if COMMODITY_RE.search(base) else "forex"
    elif exchange == "NSE":
        asset_class = "equity"
    else:
        asset_class = "unknown"

    if asset_class == "crypto":
        scope = "Crypto"
    elif asset_class == "commodity":
        scope = "Commodities"
    elif asset_class == "forex":
        scope = "Forex"
    else:
        scope = "F&O"

    return {
        "symbol": symbol,
        "exchange": exchange,
        "base": base,
        "session": session.value,
        "asset_class": asset_class,
        "scope": scope,
    }


# ==================================================
# ALERT
#
# IMPORTANT:
# Alert behavior remains intentionally repetitive.
#
# Tier A / Tier B:
#     Alert every qualifying scan.
#
# Tier A + Liquidity Event:
#     Additional alert every qualifying scan.
#
# This preserves the original alert logic.
# ==================================================


def play_alert():
    """
    Play the same Ring04 alert used by the original scanner.

    On Windows this uses winsound.
    On non-Windows systems it logs an ALERT instead.
    """
    if winsound is None:
        log.warning("ALERT")
        return
    try:
        winsound.PlaySound(r"C:\Windows\Media\Ring04.wav", winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        log.warning(f"Could not play alert sound: {e}")


# ==================================================
# NSE MARKET-DAY CONFIG
# ==================================================

NSE_OPEN = dtime(9, 15)
NSE_CLOSE = dtime(15, 30)
# NSE bhavcopy for a trading day is published only after the market closes;
# treat the day's final daily bar as available for sync/analysis from this time.
NSE_BHAVCOPY_READY = dtime(17, 0)

NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
    date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14),
    date(2026, 5, 1), date(2026, 5, 27), date(2026, 6, 26),
    date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 9), date(2026, 11, 24), date(2026, 12, 25),
}


def resolve_previous_working_date(requested_date: date) -> tuple[date, date, Optional[str]]:
    resolved_date = requested_date
    while resolved_date.weekday() >= 5 or resolved_date in NSE_HOLIDAYS:
        resolved_date -= timedelta(days=1)
    if resolved_date == requested_date:
        return requested_date, resolved_date, None
    reason = "weekend" if requested_date.weekday() >= 5 else "NSE holiday"
    return requested_date, resolved_date, reason


def is_nse_market_open(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return NSE_OPEN <= now.time() <= NSE_CLOSE


def is_fresh_nse_day(last_analysis_date: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """
    True once per NSE trading day at/after market open.
    """
    now = now or datetime.now(IST)
    if last_analysis_date is None:
        return True
    return now.date() != last_analysis_date.date() and now.time() >= NSE_OPEN


# ==================================================
# FOREX / COMMODITIES
# ==================================================

FOREX_DAILY_ROLLOVER = dtime(17, 0)


def is_forex_24_5_open(now: Optional[datetime] = None) -> bool:
    """
    Forex / commodities:
        Sunday 17:00 ET -> Friday 17:00 ET
    """
    now = (now or datetime.now(NY)).astimezone(NY)
    wd = now.weekday()
    t = now.time()
    if wd == 5:
        return False
    if wd == 6:
        return t >= FOREX_DAILY_ROLLOVER
    if wd == 4:
        return t < FOREX_DAILY_ROLLOVER
    return True


def _forex_day_key(dt: datetime):
    dt_ny = dt.astimezone(NY)
    return (dt_ny - timedelta(hours=17)).date()


def is_fresh_forex_day(last_analysis_date: Optional[datetime], now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(NY)
    if last_analysis_date is None:
        return True
    return _forex_day_key(now) != _forex_day_key(last_analysis_date)


def _crypto_day_key(dt: datetime):
    return dt.astimezone(UTC).date()


def is_fresh_crypto_day(last_analysis_date: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True once per UTC calendar day (crypto trades 24x7, no session breaks)."""
    now = now or datetime.now(UTC)
    if last_analysis_date is None:
        return True
    return _crypto_day_key(now) != _crypto_day_key(last_analysis_date)


def is_market_open(session: Session, now: Optional[datetime] = None) -> bool:
    if session == Session.FOREX_24_5:
        return is_forex_24_5_open(now)
    if session == Session.CRYPTO_24_7:
        return True
    return is_nse_market_open(now)


def is_fresh_trading_day(
    session: Session, last_analysis_date: Optional[datetime], now: Optional[datetime] = None
) -> bool:
    if session == Session.FOREX_24_5:
        return is_fresh_forex_day(last_analysis_date, now)
    if session == Session.CRYPTO_24_7:
        return is_fresh_crypto_day(last_analysis_date, now)
    return is_fresh_nse_day(last_analysis_date, now)


def is_daily_bar_ready(session: Session, now: Optional[datetime] = None) -> bool:
    """Whether the current day's *final* daily bar is available to sync/analyze.

    - NSE: the bhavcopy for the day is published only after market close
      (treated as ready from ``NSE_BHAVCOPY_READY`` / 17:00 IST onward, on a
      trading day). Before that the day's bar is still in-progress/unavailable,
      so a sync should defer it to the last completed session.
    - FOREX_24_5 (commodities/forex): the in-progress day's bar is never treated
      as final — it only becomes available the next day — so a sync always defers
      to the last completed session.
    - CRYPTO_24_7: trades 24x7; the in-progress UTC day's bar is likewise only
      final after the UTC day closes, so a sync defers to the last completed day.
    """
    if session == Session.NSE:
        now = now or datetime.now(IST)
        if now.weekday() >= 5 or now.date() in NSE_HOLIDAYS:
            return False
        return now.time() >= NSE_BHAVCOPY_READY
    return False  # FOREX_24_5 / CRYPTO_24_7: today's bar finalizes next session/day


# ==================================================
# SECTION 19 — ADAPTIVE SCANNING STATES
# ==================================================


class ScanState(Enum):
    FAR_FROM_POI = "far_from_poi"
    APPROACHING_POI = "approaching_poi"
    LIQUIDITY_EVENT = "liquidity_event"
    CONFIRMED_TRADE = "confirmed_trade"
    INVALIDATED = "invalidated"


CHECK_INTERVALS_SEC = {
    ScanState.FAR_FROM_POI: 15 * 60,
    ScanState.APPROACHING_POI: 5 * 60,
    ScanState.LIQUIDITY_EVENT: 1 * 60,
    ScanState.CONFIRMED_TRADE: 2 * 60,
    ScanState.INVALIDATED: 15 * 60,
}

# ==================================================
# SECTION 20 — PRIORITY TIERS
# ==================================================


class Tier(Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


TIER_INTERVAL_SEC = {
    # A/B unchanged — actively monitored.
    Tier.A: 5 * 60,
    Tier.B: 15 * 60,
    # ------------------------------------------
    # C/D previously mapped to None, which meant
    # "checked exactly once, ever" (due_for_check
    # never resets last_checked, even on a fresh
    # trading day) — a Neutral-bias stock would
    # never be reclassified again, even after a
    # breakout. Daily/weekly bias here is price-
    # relative to PDH/PDL/PWH/PWL, so it can flip
    # from Neutral to directional intraday — that
    # flip is exactly what should promote a stock
    # to Tier A. Finite, longer intervals keep
    # Neutral names cheap to scan without going
    # permanently blind to them.
    # ------------------------------------------
    Tier.C: 30 * 60,
    Tier.D: 60 * 60,
}

# ------------------------------------------
# NSE MINIMUM CHECK INTERVAL
#
# Floor applied only to NSE symbols: no NSE
# symbol is checked more often than this, even
# if its tier/state interval (e.g. the 1-minute
# LIQUIDITY_EVENT state) would normally allow it.
# Forex/commodity symbols are unaffected and keep
# their full adaptive 1–60 min range as-is.
# ------------------------------------------
NSE_MIN_CHECK_INTERVAL_SEC = 5 * 60


def classify_tier(near_poi: bool, has_dol: bool, strong_bias: bool, clear_bias: bool, has_poi: bool) -> Tier:
    if not has_poi and not has_dol:
        return Tier.D
    if not clear_bias:
        return Tier.C
    if near_poi and has_dol and strong_bias:
        return Tier.A
    return Tier.B


# ==================================================
# ICT CONFIGURATION
# ==================================================


@dataclass
class ICTConfig:

    # Optional historical test date. When set, fetchers use the previous
    # working NSE day when the requested date is a weekend or holiday.
    anchor_date: Optional[date] = None

    # POI proximity
    approaching_poi_pct: float = 0.005
    tier_a_poi_pct: float = 0.01

    # Displacement
    displacement_lookback: int = 10
    displacement_body_multiplier: float = 1.5

    # Liquidity sweep tolerance
    liquidity_tolerance: float = 0.0005

    # FVG lookback
    fvg_lookback: int = 30

    # FVG must be reasonably recent
    max_fvg_age_bars: int = 20

    # Minimum RR
    minimum_rr: float = 2.0

    # Swing lookback
    swing_len: int = 5

    # Intraday timeframe
    intraday_timeframe: str = "5m"

    # ------------------------------------------
    # BAR COUNTS (data pulled per request)
    #
    # These only need to cover:
    #   - fvg_lookback / displacement_lookback
    #   - roughly one trading session, so
    #     "today's" high/low is meaningful
    #
    # Previously hardcoded to 150 intraday bars
    # (~12.5h of 5m data — more than 1.5x an NSE
    # session). Reduced and made configurable.
    # ------------------------------------------
    intraday_bars: int = 100
    daily_bars: int = 20
    weekly_bars: int = 5
    historical_bars: int = 5000


# ==================================================
# MARKET STRUCTURE SNAPSHOT
# ==================================================


@dataclass
class StructureSnapshot:

    # Current market price
    price: float

    # Major liquidity references
    pdh: float
    pdl: float
    pwh: float
    pwl: float

    # Bias
    daily_bias: str = "Neutral"
    weekly_bias: str = "Neutral"
    bias_strength: str = "weak"

    # ICT POI
    poi_price: Optional[float] = None
    poi_low: Optional[float] = None
    poi_high: Optional[float] = None
    poi_type: Optional[str] = None

    # Draw on liquidity
    dol: Optional[float] = None

    # Liquidity event
    liquidity_swept: Optional[str] = None

    # Confirmation components
    displacement_confirmed: bool = False
    fvg_formed: bool = False

    # Entry
    entry: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None

    # Risk management
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None

    # Confirmation
    trade_confirmed: bool = False
    setup_invalidated: bool = False

    # Diagnostics
    confirmation_reason: Optional[str] = None
    risk_reward: Optional[float] = None


# ==================================================
# CANDLE HELPERS
# ==================================================

Candle = tuple[float, float, float, float]


def candle_body(candle: Candle) -> float:
    o, h, l, c = candle
    return abs(c - o)


def candle_range(candle: Candle) -> float:
    o, h, l, c = candle
    return h - l


def candle_bullish(candle: Candle) -> bool:
    return candle[3] > candle[0]


def candle_bearish(candle: Candle) -> bool:
    return candle[3] < candle[0]


# ==================================================
# FVG DETECTION
# ==================================================


def detect_bullish_fvg(candles: list[Candle], lookback: int = 30) -> Optional[tuple[float, float, int]]:
    """
    Bullish FVG:

        Candle 1 high < Candle 3 low

    Returns:

        (fvg_low, fvg_high, bars_ago)
    """
    if len(candles) < 3:
        return None
    start = max(2, len(candles) - lookback)
    for i in range(len(candles) - 1, start - 1, -1):
        c1 = candles[i - 2]
        c2 = candles[i - 1]
        c3 = candles[i]
        _, h1, _, _ = c1
        o2, h2, l2, c2_close = c2
        _, _, l3, _ = c3
        if h1 < l3 and c2_close > o2:
            bars_ago = len(candles) - 1 - i
            return h1, l3, bars_ago
    return None


def detect_bearish_fvg(candles: list[Candle], lookback: int = 30) -> Optional[tuple[float, float, int]]:
    """
    Bearish FVG:

        Candle 1 low > Candle 3 high
    """
    if len(candles) < 3:
        return None
    start = max(2, len(candles) - lookback)
    for i in range(len(candles) - 1, start - 1, -1):
        c1 = candles[i - 2]
        c2 = candles[i - 1]
        c3 = candles[i]
        _, _, l1, _ = c1
        o2, h2, l2, c2_close = c2
        _, h3, _, _ = c3
        if l1 > h3 and c2_close < o2:
            bars_ago = len(candles) - 1 - i
            return h3, l1, bars_ago
    return None


# ==================================================
# LIQUIDITY SWEEP DETECTION
# ==================================================


def detect_liquidity_sweep(
    candles: list[Candle], pdh: float, pdl: float, pwh: float, pwl: float, tolerance: float = 0.0005
) -> Optional[str]:
    """
    Detect a liquidity raid/reclaim on the most recent candle.

    Bullish liquidity sweep:
        price trades below PDL/PWL
        and closes back above it.

    Bearish liquidity sweep:
        price trades above PDH/PWH
        and closes back below it.

    EQH/EQL detection is not included here because equal-high/equal-low
    clustering needs a dedicated swing-point algorithm.
    """
    if not candles:
        return None
    _, h, l, c = candles[-1]

    def slightly_beyond(price: float, level: float) -> bool:
        return abs(price - level) / max(abs(level), 1e-9) <= tolerance

    # Sell-side liquidity: candle must actually trade through the level
    # (or come within `tolerance` of it) and close back above it.
    if (l < pdl or slightly_beyond(l, pdl)) and c > pdl:
        return "PDL"
    if (l < pwl or slightly_beyond(l, pwl)) and c > pwl:
        return "PWL"

    # Buy-side liquidity: candle must trade through the level (or
    # within `tolerance`) and close back below it.
    if (h > pdh or slightly_beyond(h, pdh)) and c < pdh:
        return "PDH"
    if (h > pwh or slightly_beyond(h, pwh)) and c < pwh:
        return "PWH"
    return None


# ==================================================
# DISPLACEMENT DETECTION
# ==================================================


def detect_displacement(candles: list[Candle], lookback: int = 10, body_multiplier: float = 1.5) -> bool:
    """
    Displacement is approximated by a candle body significantly larger
    than the recent average candle body.
    """
    if len(candles) < lookback + 1:
        return False
    historical = candles[-(lookback + 1) : -1]
    bodies = [candle_body(c) for c in historical if candle_body(c) > 0]
    if not bodies:
        return False
    average_body = sum(bodies) / len(bodies)
    current_body = candle_body(candles[-1])
    if average_body <= 0:
        return False
    return current_body >= (average_body * body_multiplier)


def detect_directional_displacement(
    candles: list[Candle], bias: str, lookback: int = 10, body_multiplier: float = 1.5
) -> bool:
    """
    Displacement that actually supports `bias`.

    detect_displacement() only measures candle body size vs. the recent
    average — it says nothing about direction. Used alone, a large candle
    moving AGAINST the resolved bias would still satisfy "displacement
    confirmed" for that bias, which can produce a false trade confirmation
    (e.g. a big down-candle counted as "displacement" for a Bullish setup).
    This wraps detect_displacement() and additionally requires the most
    recent candle to close in the bias's own direction.
    """
    if not detect_displacement(candles, lookback, body_multiplier):
        return False
    if not candles:
        return False
    last = candles[-1]
    if bias == "Bullish":
        return candle_bullish(last)
    if bias == "Bearish":
        return candle_bearish(last)
    return False


# ==================================================
# SWING DETECTION
# ==================================================


def calculate_swing_high(candles: list[Candle], swing_len: int) -> float:
    if not candles:
        return 0.0
    selected = candles[-swing_len:]
    return max(c[1] for c in selected)


def calculate_swing_low(candles: list[Candle], swing_len: int) -> float:
    if not candles:
        return 0.0
    selected = candles[-swing_len:]
    return min(c[2] for c in selected)


# ==================================================
# RR CALCULATION
# ==================================================


def calculate_rr(entry: Optional[float], stop: Optional[float], target: Optional[float]) -> Optional[float]:
    if entry is None or stop is None or target is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    reward = target - entry
    # A target on the wrong side of entry (e.g. below entry on a long)
    # is not a reward at all — abs() would previously mask this as a
    # positive-looking RR. Treat it as invalid rather than silently
    # flipping its sign.
    if (stop < entry and reward <= 0) or (stop > entry and reward >= 0):
        return None
    return abs(reward) / risk


# ==================================================
# CONFIRMATION LOGIC
# ==================================================


def determine_trade_confirmation(
    bias: str,
    liquidity_swept: Optional[str],
    displacement: bool,
    fvg: Optional[tuple[float, float, int]],
    price: float,
    minimum_rr: float,
    max_fvg_age_bars: int,
    entry: Optional[float],
    stop_loss: Optional[float],
    target: Optional[float],
) -> tuple[bool, Optional[str], Optional[float]]:
    if not liquidity_swept:
        return False, None, None
    if not displacement:
        return (False, "Liquidity swept, but no displacement", None)
    if not fvg:
        return (False, "Liquidity sweep + displacement, but no FVG", None)
    fvg_low, fvg_high, bars_ago = fvg
    if bars_ago > max_fvg_age_bars:
        return (False, f"FVG is too old ({bars_ago} bars, max {max_fvg_age_bars})", None)

    # Require price to still be reasonably close to the FVG.
    fvg_mid = (fvg_low + fvg_high) / 2
    fvg_range = max(abs(fvg_high - fvg_low), abs(price) * 0.0005)
    near_fvg = fvg_low - fvg_range <= price <= fvg_high + fvg_range
    if not near_fvg:
        return (False, "FVG exists but price is no longer near the POI", None)
    bullish_sweep = liquidity_swept in ("PDL", "PWL", "EQL")
    bearish_sweep = liquidity_swept in ("PDH", "PWH", "EQH")
    if bias == "Bullish" and not bullish_sweep:
        return (False, "Liquidity event conflicts with bullish bias", None)
    if bias == "Bearish" and not bearish_sweep:
        return (False, "Liquidity event conflicts with bearish bias", None)
    rr = calculate_rr(entry, stop_loss, target)
    # A missing/invalid RR (no valid target beyond entry, or a
    # backwards target) must block confirmation rather than silently
    # skip the minimum-RR filter — previously only "rr below minimum"
    # was rejected, so a None rr sailed through unfiltered.
    if rr is None:
        return (False, "No valid target / RR could not be determined", None)
    if rr < minimum_rr:
        return (False, f"RR={rr:.2f} below minimum {minimum_rr:.2f}", rr)
    if bias == "Bullish":
        return (True, "Sell-side sweep + bullish displacement + bullish FVG", rr)
    if bias == "Bearish":
        return (True, "Buy-side sweep + bearish displacement + bearish FVG", rr)
    return False, "Neutral bias", rr


# ==================================================
# DATA FETCHER BASE
# ==================================================


class DataFetcher:
    def get_ohlc(self, symbol: str, anchor_date: Optional[date] = None) -> StructureSnapshot:
        raise NotImplementedError("Wire this up to your market data source.")


# ==================================================
# OHLC / WEEKLY CACHE
#
# Daily and weekly candles are static once that day/week
# has closed, so there is no need to re-request them from
# the data source on every scan (only the intraday candles
# actually change). This cache persists parsed candles to a
# JSON file, keyed by symbol + granularity + the calendar
# day/ISO week they belong to, so a fetch is only ever made
# again once that day/week rolls over.
# ==================================================


def _cache_period_keys(symbol: str) -> tuple[str, str]:
    """
    Returns (daily_key, weekly_key) for the trading day that
    `symbol` currently belongs to, using the same day/week
    boundaries as the rest of the session logic (NSE = IST
    calendar day, forex/commodities = NY 17:00 rollover day).
    """
    session = detect_session(symbol)
    if session == Session.FOREX_24_5:
        now = datetime.now(NY)
        day_key = str(_forex_day_key(now))
    elif session == Session.CRYPTO_24_7:
        now = datetime.now(UTC)
        day_key = str(_crypto_day_key(now))
    else:
        now = datetime.now(IST)
        day_key = now.strftime("%Y-%m-%d")
    iso_year, iso_week, _ = now.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    return day_key, week_key


class OHLCCache:
    """
    Simple JSON-backed cache for parsed daily/weekly candles.

    One shared instance should be passed to every fetcher so
    that a symbol's daily/weekly bars are only fetched once per
    day/week, no matter how many times it's scanned.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "ohlc_cache.json"
        )
        self._data: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning(f"Could not read OHLC cache ({e}); starting fresh.")
                self._data = {}

    def _save(self):
        try:
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, self.path)
        except OSError as e:
            log.warning(f"Could not write OHLC cache: {e}")

    def get(self, symbol: str, granularity: str, period_key: str) -> Optional[list[Candle]]:
        entry = self._data.get(symbol, {}).get(granularity)
        if entry and entry.get("period_key") == period_key:
            return [tuple(c) for c in entry["candles"]]
        return None

    def set(self, symbol: str, granularity: str, period_key: str, candles: list[Candle]):
        self._data.setdefault(symbol, {})[granularity] = {
            "period_key": period_key,
            "candles": [list(c) for c in candles],
        }
        self._save()


# ==================================================
# OANDA
# ==================================================

OANDA_SYMBOL_MAP = {
    "XAUUSD": "XAU_USD",
    "XAGUSD": "XAG_USD",
    "NATURALGAS": "NATGAS_USD",
    "UKOIL": "BCO_USD",
    "USOIL": "WTICO_USD",
}


class OandaDataFetcher(DataFetcher):
    def __init__(
        self,
        api_key: Optional[str] = None,
        environment: str = "practice",
        config: Optional[ICTConfig] = None,
        swing_len: int = 5,
        symbol_map: Optional[dict] = None,
        cache: Optional[OHLCCache] = None,
    ):
        self.cache = cache or OHLCCache()
        self.api_key = api_key or os.environ.get("OANDA_API_KEY")
        if not self.api_key:
            raise ValueError("No OANDA API key found. " "Pass api_key=... or set " "OANDA_API_KEY.")
        self.base_url = (
            "https://api-fxpractice.oanda.com" if environment == "practice" else "https://api-fxtrade.oanda.com"
        )
        self.swing_len = swing_len
        self.config = config or ICTConfig()
        self.symbol_map = {**OANDA_SYMBOL_MAP, **(symbol_map or {})}
        try:
            import requests

            self._requests = requests
        except ImportError as e:
            raise ImportError("OANDA fetcher requires requests.\n" "Install with:\n" "pip install requests") from e

    def _to_instrument(self, symbol: str) -> str:
        raw = symbol.split(":", 1)[1] if ":" in symbol else symbol
        raw = raw.strip().upper()
        if raw in self.symbol_map:
            return self.symbol_map[raw]
        if len(raw) == 6 and raw.isalpha():
            return f"{raw[:3]}_{raw[3:]}"
        raise ValueError(f"Don't know how to map '{symbol}' " f"to an OANDA instrument.")

    def _get_candles(
        self, instrument: str, granularity: str, count: int, anchor_date: Optional[date] = None
    ) -> list:
        url = f"{self.base_url}/v3/instruments/" f"{instrument}/candles"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        params = {"granularity": granularity, "count": count, "price": "M"}
        if anchor_date:
            lookback_days = count * 7 if granularity == "W" else count * 2 if granularity == "D" else 0
            start_date = anchor_date - timedelta(days=lookback_days)
            start = datetime.combine(start_date, dtime.min, IST).astimezone(timezone.utc)
            end = datetime.combine(anchor_date + timedelta(days=1), dtime.min, IST).astimezone(timezone.utc)
            params.update({"from": start.isoformat(), "to": end.isoformat()})
        response = self._requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("candles", [])

    @staticmethod
    def _candle_ohlc(candle: dict) -> Candle:
        mid = candle["mid"]
        return (float(mid["o"]), float(mid["h"]), float(mid["l"]), float(mid["c"]))

    def get_ohlc(self, symbol: str, anchor_date: Optional[date] = None) -> StructureSnapshot:
        instrument = self._to_instrument(symbol)
        anchor_date = anchor_date or self.config.anchor_date
        requested_date, anchor_date, fallback_reason = (
            resolve_previous_working_date(anchor_date) if anchor_date else (None, None, None)
        )
        if requested_date:
            log.info(f"[{symbol}] Historical anchor: {requested_date} -> {anchor_date} ({fallback_reason or 'working day'})")
        use_cache = anchor_date is None
        daily_key, weekly_key = _cache_period_keys(symbol)

        # ------------------------------------------
        # DAILY / WEEKLY — cached, static per day/week
        # ------------------------------------------
        daily = self.cache.get(symbol, "D", daily_key) if use_cache else None
        if daily is None:
            daily_raw = self._get_candles(instrument, "D", max(self.swing_len * 2 + 3, self.config.daily_bars), anchor_date)
            daily = [self._candle_ohlc(c) for c in daily_raw if c.get("complete", True)]
            if use_cache:
                self.cache.set(symbol, "D", daily_key, daily)

        weekly = self.cache.get(symbol, "W", weekly_key) if use_cache else None
        if weekly is None:
            weekly_raw = self._get_candles(instrument, "W", self.config.weekly_bars, anchor_date)
            weekly = [self._candle_ohlc(c) for c in weekly_raw if c.get("complete", True)]
            if use_cache:
                self.cache.set(symbol, "W", weekly_key, weekly)

        # ------------------------------------------
        # INTRADAY — always fetched fresh
        # ------------------------------------------
        intraday_raw = self._get_candles(instrument, "M5", self.config.intraday_bars, anchor_date)
        intraday = [self._candle_ohlc(c) for c in intraday_raw]
        if len(daily) < 2:
            raise RuntimeError(f"Not enough daily candles for {instrument}")
        if len(intraday) < 10:
            raise RuntimeError(f"Not enough intraday candles for {instrument}")

        # Previous completed day. Historical requests include the anchor day.
        daily_index = -2 if anchor_date else -1
        if len(daily) < (3 if anchor_date else 2):
            raise RuntimeError(f"Not enough daily candles before {anchor_date or 'today'} for {instrument}")
        pd_o, pd_h, pd_l, pd_c = daily[daily_index]

        # Current price from latest intraday candle
        _, _, _, price = intraday[-1]

        # Previous completed week
        weekly_index = -2 if anchor_date else -1
        if len(weekly) >= (3 if anchor_date else 2):
            pw_o, pw_h, pw_l, pw_c = weekly[weekly_index]
        else:
            pw_h = pd_h
            pw_l = pd_l

        # ------------------------------------------
        # DAILY BIAS
        # ------------------------------------------
        pd_body_hi = max(pd_o, pd_c)
        pd_body_lo = min(pd_o, pd_c)

        # Current intraday high / low
        today_high = max(c[1] for c in intraday)
        today_low = min(c[2] for c in intraday)
        bias = "Neutral"
        if price > pd_h or (today_low < pd_l and price >= pd_body_lo):
            bias = "Bullish"
        if price < pd_l or (today_high > pd_h and price <= pd_body_hi):
            bias = "Bearish"
        weekly_bias = "Neutral"
        if price > pw_h:
            weekly_bias = "Bullish"
        elif price < pw_l:
            weekly_bias = "Bearish"
        bias_strength = "strong" if (bias != "Neutral" and bias == weekly_bias) else "weak"

        # ------------------------------------------
        # SWINGS
        # ------------------------------------------
        swing_candles = daily[-self.swing_len :]
        swing_high = calculate_swing_high(swing_candles, self.swing_len)
        swing_low = calculate_swing_low(swing_candles, self.swing_len)

        # ------------------------------------------
        # FVG / POI
        #
        # Only the FVG direction matching the resolved
        # bias is ever used, so only detect that one
        # (previously computed both every scan and
        # discarded whichever didn't match).
        # ------------------------------------------
        selected_fvg = None
        if bias == "Bullish":
            selected_fvg = detect_bullish_fvg(intraday, self.config.fvg_lookback)
        elif bias == "Bearish":
            selected_fvg = detect_bearish_fvg(intraday, self.config.fvg_lookback)
        poi_low = None
        poi_high = None
        poi_price = None
        poi_type = None
        if selected_fvg:
            poi_low = selected_fvg[0]
            poi_high = selected_fvg[1]
            poi_price = (poi_low + poi_high) / 2
            poi_type = "FVG"

        # ------------------------------------------
        # LIQUIDITY SWEEP
        # ------------------------------------------
        liquidity_swept = detect_liquidity_sweep(
            intraday, pd_h, pd_l, pw_h, pw_l, self.config.liquidity_tolerance
        )

        # ------------------------------------------
        # DISPLACEMENT
        #
        # Requires the displacement candle to actually move in
        # the resolved bias's direction (see
        # detect_directional_displacement).
        # ------------------------------------------
        displacement = detect_directional_displacement(
            intraday, bias, self.config.displacement_lookback, self.config.displacement_body_multiplier
        )
        fvg_formed = selected_fvg is not None

        # ------------------------------------------
        # DOL
        # ------------------------------------------
        dol = None
        if bias == "Bullish":
            dol = pd_h
        elif bias == "Bearish":
            dol = pd_l

        # ------------------------------------------
        # ENTRY
        # ------------------------------------------
        entry = None
        entry_low = None
        entry_high = None
        if poi_low is not None and poi_high is not None:
            entry_low = poi_low
            entry_high = poi_high
            entry = (poi_low + poi_high) / 2

        # ------------------------------------------
        # SL / TP
        #
        # pd_h/pd_l (and pw_h/pw_l) are only valid TARGETS if they
        # still sit beyond entry in the trade direction. Bias often
        # turns Bullish/Bearish precisely because price has already
        # traded through pd_h/pd_l — in that case the entry (formed
        # during/after that break) can end up on the far side of
        # pd_h/pd_l, which would make it a backwards target (e.g. a
        # "TP1" below entry on a long). Skip it rather than emit a
        # target that isn't actually ahead of the trade.
        # ------------------------------------------
        stop_loss = None
        tp1 = None
        tp2 = None
        if bias == "Bullish":
            stop_loss = swing_low
            if entry is not None and pd_h > entry:
                tp1 = pd_h
            if weekly_bias == "Bullish" and entry is not None and pw_h > entry:
                tp2 = pw_h
        elif bias == "Bearish":
            stop_loss = swing_high
            if entry is not None and pd_l < entry:
                tp1 = pd_l
            if weekly_bias == "Bearish" and entry is not None and pw_l < entry:
                tp2 = pw_l

        # ------------------------------------------
        # CONFIRMATION
        # ------------------------------------------
        trade_confirmed = False
        confirmation_reason = None
        risk_reward = None
        if entry is not None:
            trade_confirmed, confirmation_reason, risk_reward = determine_trade_confirmation(
                bias=bias,
                liquidity_swept=liquidity_swept,
                displacement=displacement,
                fvg=selected_fvg,
                price=price,
                minimum_rr=self.config.minimum_rr,
                max_fvg_age_bars=self.config.max_fvg_age_bars,
                entry=entry,
                stop_loss=stop_loss,
                target=tp1,
            )

        # ------------------------------------------
        # INVALIDATION
        # ------------------------------------------
        setup_invalidated = False
        if bias == "Bullish":
            if stop_loss is not None and price < stop_loss:
                setup_invalidated = True
        elif bias == "Bearish":
            if stop_loss is not None and price > stop_loss:
                setup_invalidated = True

        # ------------------------------------------
        # TP2 DUPLICATE PROTECTION
        # ------------------------------------------
        if tp2 is not None and tp1 is not None and abs(tp2 - tp1) < 1e-9:
            tp2 = None
        return StructureSnapshot(
            price=price,
            pdh=pd_h,
            pdl=pd_l,
            pwh=pw_h,
            pwl=pw_l,
            daily_bias=bias,
            weekly_bias=weekly_bias,
            bias_strength=bias_strength,
            poi_price=poi_price,
            poi_low=poi_low,
            poi_high=poi_high,
            poi_type=poi_type,
            dol=dol,
            liquidity_swept=liquidity_swept,
            displacement_confirmed=displacement,
            fvg_formed=fvg_formed,
            entry=entry,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            trade_confirmed=trade_confirmed,
            setup_invalidated=setup_invalidated,
            confirmation_reason=confirmation_reason,
            risk_reward=risk_reward,
        )


# ==================================================
# TRADINGVIEW DATA FETCHER
# ==================================================


class TvDatafeedFetcher(DataFetcher):
    # When the primary exchange's anonymous TradingView feed is limited/empty
    # for a symbol (e.g. OANDA metals), retry the same instrument on these
    # common forex/commodity exchanges before giving up.
    FALLBACK_EXCHANGES = ["FOREXCOM", "CAPITALCOM", "TVC", "CURRENCYCOM", "SAXO", "ICMARKETS"]

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        config: Optional[ICTConfig] = None,
        cache: Optional[OHLCCache] = None,
    ):
        try:
            from tvDatafeed import TvDatafeed, Interval
        except ImportError as e:
            raise ImportError(
                "TradingView fetcher requires tvDatafeed.\n\n"
                "Install with:\n"
                "pip install --upgrade --no-cache-dir "
                "git+https://github.com/rongardF/tvdatafeed.git"
            ) from e
        self.Interval = Interval
        self.config = config or ICTConfig()
        self.cache = cache or OHLCCache()
        log.info("Connecting to TradingView...")
        self.tv = TvDatafeed(username, password) if username else TvDatafeed()
        log.info("TradingView session established.")

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str]:
        if ":" in symbol:
            exchange, sym = symbol.split(":", 1)
        else:
            exchange = "NSE"
            sym = symbol
        return (exchange.strip().upper(), sym.strip().upper())

    def _get_hist(self, sym: str, exchange: str, interval, n_bars: int):
        """Fetch bars, falling back across exchanges for the same instrument.

        OANDA's anonymous TradingView feed frequently returns no/limited data
        for metals (XAUUSD/XAGUSD); retrying on FOREXCOM/CAPITALCOM etc. keeps
        those symbols analysable. Returns the first non-empty frame, or raises
        the last underlying error if every exchange fails.
        """
        exchanges = [exchange] + [e for e in self.FALLBACK_EXCHANGES if e != exchange]
        last_exc: Optional[Exception] = None
        for ex in exchanges:
            try:
                df = self.tv.get_hist(symbol=sym, exchange=ex, interval=interval, n_bars=n_bars)
            except Exception as exc:  # noqa: BLE001 - try next exchange
                last_exc = exc
                log.warning("[%s] TradingView fetch failed on %s: %s", sym, ex, exc)
                continue
            if df is not None and len(df) > 0:
                if ex != exchange:
                    log.info("[%s] Using fallback exchange %s (primary %s unavailable).", sym, ex, exchange)
                return df
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"TradingView returned no data for {exchange}:{sym} (and fallback exchanges)")

    @staticmethod
    def _df_to_candles(df) -> list[Candle]:
        if df is None or len(df) == 0:
            return []
        candles = []
        for _, row in df.iterrows():
            candles.append((float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])))
        return candles

    @staticmethod
    def _filter_to_anchor(df, anchor_date: Optional[date], exact: bool = False):
        if anchor_date is None or df is None or len(df) == 0:
            return df
        if exact:
            mask = [getattr(value, "date", lambda: value)() == anchor_date for value in df.index]
        else:
            mask = [getattr(value, "date", lambda: value)() <= anchor_date for value in df.index]
        return df.loc[mask]

    def get_ohlc(self, symbol: str, anchor_date: Optional[date] = None) -> StructureSnapshot:
        exchange, sym = self._split_symbol(symbol)
        anchor_date = anchor_date or self.config.anchor_date
        requested_date, anchor_date, fallback_reason = (
            resolve_previous_working_date(anchor_date) if anchor_date else (None, None, None)
        )
        if requested_date:
            log.info(f"[{symbol}] Historical anchor: {requested_date} -> {anchor_date} ({fallback_reason or 'working day'})")
        use_cache = anchor_date is None
        daily_key, weekly_key = _cache_period_keys(symbol)

        # ------------------------------------------
        # DAILY / WEEKLY — cached, static per day/week
        # ------------------------------------------
        daily = self.cache.get(symbol, "D", daily_key) if use_cache else None
        if daily is None:
            daily_bars = max(self.config.swing_len * 2 + 5, self.config.daily_bars, self.config.historical_bars if anchor_date else 0)
            daily_df = self._get_hist(sym, exchange, self.Interval.in_daily, daily_bars)
            daily_df = self._filter_to_anchor(daily_df, anchor_date)
            if daily_df is None or len(daily_df) < 2:
                raise RuntimeError(f"TradingView returned insufficient " f"daily data for {exchange}:{sym}")
            daily = self._df_to_candles(daily_df)
            if use_cache:
                self.cache.set(symbol, "D", daily_key, daily)

        weekly = self.cache.get(symbol, "W", weekly_key) if use_cache else None
        if weekly is None:
            weekly_bars = max(self.config.weekly_bars, self.config.historical_bars // 5 if anchor_date else 0)
            weekly_df = self._get_hist(sym, exchange, self.Interval.in_weekly, weekly_bars)
            weekly_df = self._filter_to_anchor(weekly_df, anchor_date)
            weekly = self._df_to_candles(weekly_df)
            if use_cache:
                self.cache.set(symbol, "W", weekly_key, weekly)

        # ------------------------------------------
        # INTRADAY — always fetched fresh
        # ------------------------------------------
        intraday_bars = max(self.config.intraday_bars, self.config.historical_bars if anchor_date else 0)
        intraday_df = self._get_hist(sym, exchange, self.Interval.in_5_minute, intraday_bars)
        intraday_df = self._filter_to_anchor(intraday_df, anchor_date, exact=anchor_date is not None)
        if intraday_df is None or len(intraday_df) < 10:
            raise RuntimeError(f"TradingView returned insufficient " f"intraday data for {exchange}:{sym}")
        intraday = self._df_to_candles(intraday_df)

        # ------------------------------------------
        # PREVIOUS DAY
        # ------------------------------------------
        if len(daily) < (3 if anchor_date else 2):
            raise RuntimeError(f"TradingView returned insufficient daily context before {anchor_date or 'today'}")
        prev = daily[-2]
        pd_o, pd_h, pd_l, pd_c = prev

        # Current market price
        price = intraday[-1][3]

        # ------------------------------------------
        # PREVIOUS WEEK
        # ------------------------------------------
        if len(weekly) >= (3 if anchor_date else 2):
            pw = weekly[-2]
            pw_o, pw_h, pw_l, pw_c = pw
        else:
            pw_h = pd_h
            pw_l = pd_l

        # ------------------------------------------
        # CURRENT INTRADAY RANGE
        # ------------------------------------------
        today_high = max(c[1] for c in intraday)
        today_low = min(c[2] for c in intraday)

        # ------------------------------------------
        # DAILY BIAS
        # ------------------------------------------
        pd_body_hi = max(pd_o, pd_c)
        pd_body_lo = min(pd_o, pd_c)
        bias = "Neutral"
        if price > pd_h or (today_low < pd_l and price >= pd_body_lo):
            bias = "Bullish"
        if price < pd_l or (today_high > pd_h and price <= pd_body_hi):
            bias = "Bearish"

        # ------------------------------------------
        # WEEKLY BIAS
        # ------------------------------------------
        weekly_bias = "Neutral"
        if price > pw_h:
            weekly_bias = "Bullish"
        elif price < pw_l:
            weekly_bias = "Bearish"
        bias_strength = "strong" if (bias != "Neutral" and bias == weekly_bias) else "weak"

        # ------------------------------------------
        # SWING
        # ------------------------------------------
        swing_candles = daily[-self.config.swing_len :]
        swing_high = calculate_swing_high(swing_candles, self.config.swing_len)
        swing_low = calculate_swing_low(swing_candles, self.config.swing_len)

        # ------------------------------------------
        # FVG
        #
        # Only the direction matching bias is used, so
        # only detect that one.
        # ------------------------------------------
        selected_fvg = None
        if bias == "Bullish":
            selected_fvg = detect_bullish_fvg(intraday, self.config.fvg_lookback)
        elif bias == "Bearish":
            selected_fvg = detect_bearish_fvg(intraday, self.config.fvg_lookback)
        poi_low = None
        poi_high = None
        poi_price = None
        poi_type = None
        if selected_fvg:
            poi_low = selected_fvg[0]
            poi_high = selected_fvg[1]
            poi_price = (poi_low + poi_high) / 2
            poi_type = "FVG"

        # ------------------------------------------
        # LIQUIDITY SWEEP
        # ------------------------------------------
        liquidity_swept = detect_liquidity_sweep(intraday, pd_h, pd_l, pw_h, pw_l, self.config.liquidity_tolerance)

        # ------------------------------------------
        # DISPLACEMENT
        #
        # Requires the displacement candle to actually move in
        # the resolved bias's direction (see
        # detect_directional_displacement).
        # ------------------------------------------
        displacement = detect_directional_displacement(
            intraday, bias, self.config.displacement_lookback, self.config.displacement_body_multiplier
        )
        fvg_formed = selected_fvg is not None

        # ------------------------------------------
        # DOL
        # ------------------------------------------
        dol = None
        if bias == "Bullish":
            dol = pd_h
        elif bias == "Bearish":
            dol = pd_l

        # ------------------------------------------
        # ENTRY
        # ------------------------------------------
        entry = None
        entry_low = None
        entry_high = None
        if poi_low is not None and poi_high is not None:
            entry_low = poi_low
            entry_high = poi_high
            entry = (poi_low + poi_high) / 2

        # ------------------------------------------
        # STOP / TARGETS
        #
        # pd_h/pd_l (and pw_h/pw_l) are only valid TARGETS if they
        # still sit beyond entry in the trade direction. Bias often
        # turns Bullish/Bearish precisely because price has already
        # traded through pd_h/pd_l — skip the level rather than emit
        # a target that isn't actually ahead of the trade.
        # ------------------------------------------
        stop_loss = None
        tp1 = None
        tp2 = None
        if bias == "Bullish":
            stop_loss = swing_low
            if entry is not None and pd_h > entry:
                tp1 = pd_h
            if weekly_bias == "Bullish" and entry is not None and pw_h > entry:
                tp2 = pw_h
        elif bias == "Bearish":
            stop_loss = swing_high
            if entry is not None and pd_l < entry:
                tp1 = pd_l
            if weekly_bias == "Bearish" and entry is not None and pw_l < entry:
                tp2 = pw_l

        # ------------------------------------------
        # CONFIRMATION
        # ------------------------------------------
        trade_confirmed, confirmation_reason, risk_reward = (
            determine_trade_confirmation(
                bias=bias,
                liquidity_swept=liquidity_swept,
                displacement=displacement,
                fvg=selected_fvg,
                price=price,
                minimum_rr=self.config.minimum_rr,
                max_fvg_age_bars=self.config.max_fvg_age_bars,
                entry=entry,
                stop_loss=stop_loss,
                target=tp1,
            )
            if entry is not None
            else (False, None, None)
        )

        # ------------------------------------------
        # INVALIDATION
        # ------------------------------------------
        setup_invalidated = False
        if bias == "Bullish":
            if stop_loss is not None and price < stop_loss:
                setup_invalidated = True
        elif bias == "Bearish":
            if stop_loss is not None and price > stop_loss:
                setup_invalidated = True

        # ------------------------------------------
        # TP2 DUPLICATE
        # ------------------------------------------
        if tp2 is not None and tp1 is not None and abs(tp2 - tp1) < 1e-9:
            tp2 = None
        return StructureSnapshot(
            price=price,
            pdh=pd_h,
            pdl=pd_l,
            pwh=pw_h,
            pwl=pw_l,
            daily_bias=bias,
            weekly_bias=weekly_bias,
            bias_strength=bias_strength,
            poi_price=poi_price,
            poi_low=poi_low,
            poi_high=poi_high,
            poi_type=poi_type,
            dol=dol,
            liquidity_swept=liquidity_swept,
            displacement_confirmed=displacement,
            fvg_formed=fvg_formed,
            entry=entry,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            trade_confirmed=trade_confirmed,
            setup_invalidated=setup_invalidated,
            confirmation_reason=confirmation_reason,
            risk_reward=risk_reward,
        )


# ==================================================
# PER-STOCK TRACKER
# ==================================================


@dataclass
class StockTracker:
    symbol: str
    session: Session = Session.NSE
    state: ScanState = ScanState.FAR_FROM_POI
    tier: Tier = Tier.C
    last_checked: Optional[datetime] = None
    last_daily_analysis: Optional[datetime] = None
    snapshot: Optional[StructureSnapshot] = None

    def due_for_check(self, now: datetime) -> bool:

        # First check always allowed.
        if self.last_checked is None:
            return True
        interval = TIER_INTERVAL_SEC.get(self.tier)

        # A tier explicitly mapped to None in TIER_INTERVAL_SEC
        # is intentionally never (re)checked. With the default
        # config this no longer applies to any tier — see the
        # comment on TIER_INTERVAL_SEC.
        if interval is None:
            return False
        elapsed = (now - self.last_checked).total_seconds()
        state_interval = CHECK_INTERVALS_SEC[self.state]
        effective_interval = min(interval, state_interval)

        # NSE floor: never check an NSE symbol more often
        # than NSE_MIN_CHECK_INTERVAL_SEC, even if tier/state
        # would otherwise allow tighter polling (e.g. the
        # 1-minute LIQUIDITY_EVENT state). Forex/commodity
        # symbols are untouched and keep their normal
        # adaptive interval.
        if self.session == Session.NSE:
            effective_interval = max(effective_interval, NSE_MIN_CHECK_INTERVAL_SEC)

        return elapsed >= effective_interval

    def update_state(self, snap: StructureSnapshot):
        near_poi = snap.poi_price is not None and abs(snap.price - snap.poi_price) / max(abs(snap.price), 1e-9) < 0.005
        if snap.setup_invalidated:
            self.state = ScanState.INVALIDATED
        elif snap.trade_confirmed:
            self.state = ScanState.CONFIRMED_TRADE
        elif snap.liquidity_swept is not None:
            self.state = ScanState.LIQUIDITY_EVENT
        elif near_poi:
            self.state = ScanState.APPROACHING_POI
        else:
            self.state = ScanState.FAR_FROM_POI

    def update_tier(self, snap: StructureSnapshot):
        near_poi = snap.poi_price is not None and abs(snap.price - snap.poi_price) / max(abs(snap.price), 1e-9) < 0.01
        clear_bias = snap.daily_bias in ("Bullish", "Bearish")
        strong_bias = snap.bias_strength == "strong"
        self.tier = classify_tier(
            near_poi=near_poi,
            has_dol=snap.dol is not None,
            strong_bias=strong_bias,
            clear_bias=clear_bias,
            has_poi=snap.poi_price is not None,
        )


# ==================================================
# WATCHLIST LOADER
# ==================================================


def load_watchlist(filename: str = "watchlist.txt") -> list[tuple[str, Session]]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Watchlist file not found: {path}\n\n" f"Create '{filename}' next to this script.")
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if "|" in line:
                sym_part, override = line.split("|", 1)
                sym_part = sym_part.strip().upper()
                override = override.strip().lower()
                if override == "nse":
                    session = Session.NSE
                elif override in ("forex", "forex_24_5", "24x5", "commodities"):
                    session = Session.FOREX_24_5
                elif override in ("crypto", "crypto_24_7"):
                    session = Session.CRYPTO_24_7
                else:
                    log.warning(f"Unknown session override " f"'{override}' for " f"'{sym_part}'. " f"Auto-detecting.")
                    session = detect_session(sym_part)
            else:
                sym_part = line.upper()
                session = detect_session(sym_part)
            entries.append((sym_part, session))
    if not entries:
        raise ValueError(f"Watchlist '{path}' is empty.")
    log.info(
        f"Loaded {len(entries)} symbols: "
        # f"{[(s, sess.value) for s, sess in entries]}"
    )
    return entries


# ==================================================
# FORMATTER
# ==================================================


def _fmt(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:g}"


def _fmt_rr(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


# ==================================================
# TRACKER STATE CACHE
#
# Persists each symbol's tier/state/last_checked/
# last_daily_analysis to disk so a script restart
# doesn't lose adaptive scheduling. Without this, every
# restart treats every symbol as "never checked" again
# (StockTracker defaults), which both re-triggers a
# fetch for every single symbol immediately (a startup
# burst) and forgets that a symbol was already
# classified Neutral/C/D recently, defeating the point
# of the longer C/D recheck intervals above.
# ==================================================


class TrackerStateCache:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "tracker_state_cache.json"
        )

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Could not read tracker state cache ({e}); starting fresh.")
            return {}

    def save(self, trackers: dict):
        payload = {}
        for symbol, t in trackers.items():
            payload[symbol] = {
                "tier": t.tier.value,
                "state": t.state.value,
                "last_checked": t.last_checked.isoformat() if t.last_checked else None,
                "last_daily_analysis": t.last_daily_analysis.isoformat() if t.last_daily_analysis else None,
            }
        try:
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, self.path)
        except OSError as e:
            log.warning(f"Could not write tracker state cache: {e}")

    @staticmethod
    def apply(tracker: StockTracker, saved: dict):
        try:
            tracker.tier = Tier(saved["tier"])
            tracker.state = ScanState(saved["state"])
            if saved.get("last_checked"):
                tracker.last_checked = datetime.fromisoformat(saved["last_checked"])
            if saved.get("last_daily_analysis"):
                tracker.last_daily_analysis = datetime.fromisoformat(saved["last_daily_analysis"])
        except (KeyError, ValueError) as e:
            log.warning(f"Skipping malformed cached state for " f"{tracker.symbol}: {e}")


# ==================================================
# ADAPTIVE SCANNER
# ==================================================


class AdaptiveScanner:
    def __init__(
        self,
        watchlist: list,
        data_fetcher: Optional[DataFetcher] = None,
        on_confirmed_trade: Optional[Callable[[StockTracker], None]] = None,
        results_file_prefix: str = "scan_results",
        operating_start: dtime = dtime(9, 0),
        operating_end: dtime = dtime(23, 0),
        operating_tz: ZoneInfo = IST,
        output_tiers: Optional[set[Tier]] = None,
        max_results_file_bytes: int = 2_000_000,
        state_cache: Optional[TrackerStateCache] = None,
    ):
        # ------------------------------------------
        # RESULTS FILE SIZE CAP
        #
        # _write_results_file() prepends by reading the
        # whole existing file and rewriting it, so cost
        # grows with the file across a trading day. This
        # caps how much old content is kept/re-written each
        # cycle instead of letting it grow unbounded.
        # ------------------------------------------
        self.max_results_file_bytes = max_results_file_bytes
        # ------------------------------------------
        # OUTPUT TIER FILTER
        #
        # Controls which tiers are written to the
        # results file. Defaults to Tier A only.
        # Scanning, alerting, and classification for
        # every tier is unaffected — this only filters
        # what gets written out.
        # ------------------------------------------
        self.output_tiers = output_tiers if output_tiers is not None else {Tier.A}
        self.trackers: dict[str, StockTracker] = {}
        for entry in watchlist:
            if isinstance(entry, tuple):
                sym, session = entry
            else:
                sym = entry
                session = detect_session(entry)
            self.trackers[sym] = StockTracker(symbol=sym, session=session)

        # ------------------------------------------
        # RESTORE PERSISTED SCHEDULING STATE
        # ------------------------------------------
        self.state_cache = state_cache or TrackerStateCache()
        saved_state = self.state_cache.load()
        for symbol, tracker in self.trackers.items():
            if symbol in saved_state:
                TrackerStateCache.apply(tracker, saved_state[symbol])
        if saved_state:
            log.info(f"Restored cached scheduling state for " f"{len(saved_state)} symbol(s).")

        self.data_fetcher = data_fetcher or DataFetcher()
        self.on_confirmed_trade = on_confirmed_trade
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.results_file_prefix = results_file_prefix
        self.operating_start = operating_start
        self.operating_end = operating_end
        self.operating_tz = operating_tz
        self._was_outside_window = False

    # ==================================================
    # OPERATING WINDOW
    # ==================================================
    def is_within_operating_window(self, now: Optional[datetime] = None) -> bool:
        now = (now or datetime.now(self.operating_tz)).astimezone(self.operating_tz)
        return self.operating_start <= now.time() <= self.operating_end

    # ==================================================
    # RESULTS PATH
    # ==================================================
    def _results_path_for_today(self):
        today_str = datetime.now(self.operating_tz).strftime("%Y-%m-%d")
        filename = f"{self.results_file_prefix}_" f"{today_str}.txt"
        return os.path.join(self.script_dir, filename)

    # ==================================================
    # SINGLE SCAN
    # ==================================================
    def run_once(self, anchor_date: Optional[date] = None):
        configured_anchor = getattr(getattr(self.data_fetcher, "config", None), "anchor_date", None)
        anchor_date = anchor_date or configured_anchor
        historical_run = anchor_date is not None
        if historical_run:
            requested_date, anchor_date, fallback_reason = resolve_previous_working_date(anchor_date)
            log.info(
                f"Historical ICT scan: requested {requested_date}, "
                f"using {anchor_date} ({fallback_reason or 'working day'})."
            )
        if not historical_run and not self.is_within_operating_window():
            if not self._was_outside_window:
                log.info(
                    f"Outside operating window "
                    f"({self.operating_start}–"
                    f"{self.operating_end} "
                    f"{self.operating_tz}) — "
                    f"pausing scans."
                )
                self._was_outside_window = True
            return
        if self._was_outside_window:
            log.info("Entered operating window — " "resuming scans.")
            self._was_outside_window = False
        for tracker in self.trackers.values():

            # ------------------------------------------
            # SYMBOL SESSION TIME
            # ------------------------------------------
            now = datetime.now(IST) if tracker.session == Session.NSE else datetime.now(NY)

            # ------------------------------------------
            # MARKET OPEN?
            # ------------------------------------------
            if not historical_run and not (
                is_market_open(tracker.session, now) or is_daily_bar_ready(tracker.session, now)
            ):
                continue
            if historical_run:
                now = datetime.combine(anchor_date, dtime(15, 30), IST)

            # ------------------------------------------
            # DAILY RESET
            #
            # IMPORTANT:
            # We do NOT mark the daily analysis as
            # completed until data fetching succeeds.
            # ------------------------------------------
            fresh_day = is_fresh_trading_day(tracker.session, tracker.last_daily_analysis, now)
            if fresh_day:
                log.info(f"[{tracker.symbol}] " f"Fresh {tracker.session.value} " f"trading day detected.")

            # ------------------------------------------
            # CHECK DUE?
            # ------------------------------------------
            if not historical_run and not tracker.due_for_check(now):
                continue

            # ------------------------------------------
            # FETCH DATA
            # ------------------------------------------
            try:
                log.info(f"[{tracker.symbol}] " f"Fetching market structure...")
                snap = self.data_fetcher.get_ohlc(tracker.symbol, anchor_date=anchor_date)
            except NotImplementedError as e:
                log.warning(f"[{tracker.symbol}] " f"Data fetcher not implemented: " f"{e}")
                continue
            except Exception as e:
                log.error(f"[{tracker.symbol}] " f"get_ohlc() failed: {e}", exc_info=True)

                # IMPORTANT:
                # If this was the first analysis of
                # the trading day, do not consume it.
                continue

            # ------------------------------------------
            # SUCCESSFUL DATA FETCH
            # ------------------------------------------
            tracker.snapshot = snap
            tracker.last_checked = now
            if fresh_day:
                tracker.last_daily_analysis = now
                tracker.state = ScanState.FAR_FROM_POI

            # ------------------------------------------
            # PREVIOUS STATE/TIER
            # ------------------------------------------
            prev_tier = tracker.tier
            prev_state = tracker.state

            # ------------------------------------------
            # UPDATE TIER
            # ------------------------------------------
            tracker.update_tier(snap)

            # ------------------------------------------
            # UPDATE STATE
            # ------------------------------------------
            tracker.update_state(snap)

            # ------------------------------------------
            # LOG CHANGES
            # ------------------------------------------
            if tracker.tier != prev_tier:
                log.info(f"[{tracker.symbol}] " f"Tier changed: " f"{prev_tier.value} -> " f"{tracker.tier.value}")
            if tracker.state != prev_state:
                interval = CHECK_INTERVALS_SEC[tracker.state]
                log.info(
                    f"[{tracker.symbol}] "
                    f"State changed: "
                    f"{prev_state.value} -> "
                    f"{tracker.state.value} "
                    f"(interval now "
                    f"{interval // 60} min)"
                )

            # ==================================================
            # ALERT LOGIC
            #
            # DO NOT CHANGE:
            #
            # Tier A alerts repeatedly.
            # ==================================================
            if tracker.tier == Tier.A:
                play_alert()
                log.warning(
                    f"🔔 ALERT: "
                    f"[{tracker.symbol}] "
                    f"TIER {tracker.tier.value} — "
                    f"High priority, monitor closely!"
                )

            # ==================================================
            # ALERT LOGIC
            #
            # DO NOT CHANGE:
            #
            # Tier A + liquidity event gets another alert.
            # ==================================================
            if tracker.tier == Tier.A and tracker.state == ScanState.LIQUIDITY_EVENT:
                play_alert()
                log.info(
                    f"[{tracker.symbol}] " f"TIER A + LIQUIDITY EVENT — " f"eligible for immediate entry analysis."
                )

            # ------------------------------------------
            # RESULT
            # ------------------------------------------
            log.info(
                f"[{tracker.symbol}] RESULT | "
                f"Bias={snap.daily_bias} | "
                f"Weekly={snap.weekly_bias} | "
                f"Strength={snap.bias_strength} | "
                f"Tier={tracker.tier.value} | "
                f"State={tracker.state.value} | "
                f"Price={_fmt(snap.price)} | "
                f"POI={snap.poi_type or 'N/A'}:"
                f"{_fmt(snap.poi_price)} | "
                f"Sweep={snap.liquidity_swept or 'None'} | "
                f"Disp="
                f"{'YES' if snap.displacement_confirmed else 'NO'} | "
                f"FVG="
                f"{'YES' if snap.fvg_formed else 'NO'} | "
                f"Entry={_fmt(snap.entry)} | "
                f"EntryZone="
                f"{_fmt(snap.entry_low)}-"
                f"{_fmt(snap.entry_high)} | "
                f"SL={_fmt(snap.stop_loss)} | "
                f"TP1={_fmt(snap.tp1)} | "
                f"TP2={_fmt(snap.tp2)} | "
                f"RR={_fmt_rr(snap.risk_reward)} | "
                f"Confirmed="
                f"{'YES' if snap.trade_confirmed else 'NO'}"
            )

            # ------------------------------------------
            # CONFIRMATION REASON
            # ------------------------------------------
            if snap.confirmation_reason:
                log.info(f"[{tracker.symbol}] " f"Setup: " f"{snap.confirmation_reason}")

            # ------------------------------------------
            # CONFIRMED TRADE CALLBACK
            # ------------------------------------------
            if snap.trade_confirmed and self.on_confirmed_trade:
                self.on_confirmed_trade(tracker)

        # ------------------------------------------
        # WRITE RESULTS
        # ------------------------------------------
        self._write_results_file()

        # ------------------------------------------
        # PERSIST SCHEDULING STATE
        #
        # So a restart resumes each symbol's tier/state
        # timing instead of re-treating every symbol as
        # "never checked" (see TrackerStateCache).
        # ------------------------------------------
        self.state_cache.save(self.trackers)

    # ==================================================
    # RESULTS FILE
    # ==================================================
    def _write_results_file(self):
        now_ist, now_ny = datetime.now(IST), datetime.now(NY)
        live_trackers = [
            t
            for t in self.trackers.values()
            if is_market_open(t.session, (now_ist if t.session == Session.NSE else now_ny))
            and t.tier in self.output_tiers
        ]
        if not live_trackers:
            return
        lines = []
        lines.append("=" * 150)
        lines.append(
            "ICT Daily Bias Scanner — "
            "Results as of "
            f"{datetime.now(self.operating_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        lines.append("=" * 150)
        header = (
            f"{'Symbol':<24} "
            f"{'Bias':<9} "
            f"{'Week':<9} "
            f"{'Tier':<5} "
            f"{'State':<18} "
            f"{'Price':>10} "
            f"{'POI':>10} "
            f"{'Entry':>10} "
            f"{'SL':>10} "
            f"{'TP1':>10} "
            f"{'TP2':>10} "
            f"{'RR':>7} "
            f"{'Sweep':<8} "
            f"{'Conf':<5}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        # Sort final displayed output by tier priority only.
        # Existing scanning, alert, classification, and tracking logic is unchanged.
        tier_priority = {Tier.A: 0, Tier.B: 1, Tier.C: 2, Tier.D: 3}
        live_trackers.sort(key=lambda tracker: tier_priority.get(tracker.tier, 99))
        for tracker in live_trackers:
            snap = tracker.snapshot
            if snap is None:
                lines.append(
                    f"{tracker.symbol:<24} "
                    f"{'—':<9} "
                    f"{'—':<9} "
                    f"{tracker.tier.value:<5} "
                    f"{'not yet checked':<18} "
                    f"{'N/A':>10} "
                    f"{'N/A':>10} "
                    f"{'N/A':>10} "
                    f"{'N/A':>10} "
                    f"{'N/A':>10} "
                    f"{'N/A':>10} "
                    f"{'N/A':>7} "
                    f"{'N/A':<8} "
                    f"{'N/A':<5}"
                )
                continue
            lines.append(
                f"{tracker.symbol:<24} "
                f"{snap.daily_bias:<9} "
                f"{snap.weekly_bias:<9} "
                f"{tracker.tier.value:<5} "
                f"{tracker.state.value:<18} "
                f"{_fmt(snap.price):>10} "
                f"{_fmt(snap.poi_price):>10} "
                f"{_fmt(snap.entry):>10} "
                f"{_fmt(snap.stop_loss):>10} "
                f"{_fmt(snap.tp1):>10} "
                f"{_fmt(snap.tp2):>10} "
                f"{_fmt_rr(snap.risk_reward):>7} "
                f"{(snap.liquidity_swept or 'None'):<8} "
                f"{('YES' if snap.trade_confirmed else 'NO'):<5}"
            )
        lines.append("-" * len(header))
        # lines.append("ICT confirmation model:")
        # lines.append("Liquidity sweep -> displacement -> " "directional FVG -> POI proximity -> " "RR filter.")
        # lines.append("Bullish: sell-side liquidity sweep " "(PDL/PWL) + bullish displacement + bullish FVG.")
        # lines.append("Bearish: buy-side liquidity sweep " "(PDH/PWH) + bearish displacement + bearish FVG.")
        # lines.append("Entry = midpoint of detected FVG/POI.")
        # lines.append("TP1 = PDH for bullish setups / " "PDL for bearish setups.")
        # lines.append("TP2 = PWH/PWL only when weekly bias " "agrees with daily bias.")
        # lines.append("RR filter defaults to minimum 2.0R.")
        lines.append("")
        new_block = "\n".join(lines)
        results_path = self._results_path_for_today()
        try:
            existing = ""
            if os.path.exists(results_path):
                with open(results_path, "r", encoding="utf-8") as f:
                    existing = f.read()
                # Cap retained history so this read+rewrite
                # stays bounded instead of growing across the
                # whole trading day.
                if len(existing) > self.max_results_file_bytes:
                    existing = existing[: self.max_results_file_bytes] + "\n...(older results trimmed)...\n"
            with open(results_path, "w", encoding="utf-8") as f:
                f.write(new_block + "\n" + existing)
            log.info(f"Results prepended to " f"{results_path}")
        except OSError as e:
            log.error(f"Failed to write results file " f"{results_path}: {e}")

    # ==================================================
    # FOREVER LOOP
    # ==================================================
    def run_forever(self, poll_seconds: int = 60):
        log.info(
            f"Starting adaptive scanner loop "
            f"(Ctrl+C to stop). "
            f"Operating window: "
            f"{self.operating_start}–"
            f"{self.operating_end} "
            f"{self.operating_tz}, "
            f"polling every "
            f"{poll_seconds}s."
        )
        try:
            while True:
                self.run_once()
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            log.info("Scanner stopped.")


# ==================================================
# EXAMPLE / MAIN
# ==================================================

if __name__ == "__main__":

    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)

    watchlist = load_watchlist(os.path.join(root_dir, "config", "watchlist.txt"))

    # ------------------------------------------
    # CONFIRMED TRADE CALLBACK
    # ------------------------------------------
    def handle_confirmed_trade(tracker: StockTracker):
        s = tracker.snapshot
        if s is None:
            return
        log.warning(
            "🚨 CONFIRMED ICT SETUP | "
            f"{tracker.symbol} | "
            f"Bias={s.daily_bias} | "
            f"Weekly={s.weekly_bias} | "
            f"POI={s.poi_type}:{_fmt(s.poi_price)} | "
            f"Sweep={s.liquidity_swept} | "
            f"Entry={_fmt(s.entry)} | "
            f"SL={_fmt(s.stop_loss)} | "
            f"TP1={_fmt(s.tp1)} | "
            f"TP2={_fmt(s.tp2)} | "
            f"RR={_fmt_rr(s.risk_reward)}"
        )

    # ------------------------------------------
    # ICT CONFIG
    # ------------------------------------------
    ict_config = ICTConfig(
        # Price is considered "approaching" POI
        # within 0.5%.
        approaching_poi_pct=0.005,
        # Tier A proximity.
        tier_a_poi_pct=0.01,
        # Displacement requires body >= 1.5x
        # average recent body.
        displacement_lookback=10,
        displacement_body_multiplier=1.5,
        # Sweep tolerance.
        liquidity_tolerance=0.0005,
        # Search latest 30 intraday candles
        # for FVG.
        fvg_lookback=30,
        # Minimum acceptable RR.
        minimum_rr=2.0,
        # Daily swing lookback.
        swing_len=5,
        intraday_timeframe="5m",
        # ------------------------------------------
        # Bar counts pulled per request.
        # intraday_bars=100 covers roughly one NSE
        # session (~75 x 5m bars) plus room for the
        # FVG (30) and displacement (10) lookbacks —
        # down from the previous hardcoded 150.
        # ------------------------------------------
        intraday_bars=100,
        daily_bars=20,
        weekly_bars=5,
    )

    # ------------------------------------------
    # SHARED OHLC CACHE
    #
    # Daily/weekly candles are static once that
    # day/week closes, so one cache is shared across
    # fetchers and persisted to ohlc_cache.json — a
    # symbol's daily/weekly bars are fetched at most
    # once per day/week no matter how often it's
    # scanned.
    # ------------------------------------------
    ohlc_cache = OHLCCache()

    # ------------------------------------------
    # DATA SOURCE
    #
    # Priority:
    #
    # 1. TradingView
    # 2. OANDA
    # 3. Stub
    # ------------------------------------------
    fetcher = None
    try:
        fetcher = TvDatafeedFetcher(config=ict_config, cache=ohlc_cache)
        log.info("Using TvDatafeedFetcher " "(TradingView).")
    except ImportError as e:
        log.warning(str(e))
        if os.environ.get("OANDA_API_KEY"):
            fetcher = OandaDataFetcher(environment="practice", config=ict_config, cache=ohlc_cache)
            log.info("Falling back to " "OandaDataFetcher.")
        else:
            fetcher = DataFetcher()
            log.warning("Neither tvDatafeed nor " "OANDA_API_KEY is available. " "Running with stub DataFetcher.")

    # ------------------------------------------
    # OPERATING WINDOW
    # ------------------------------------------
    OPERATING_START = dtime(9, 0)
    OPERATING_END = dtime(23, 0)
    OPERATING_TZ = IST

    # IMPORTANT:
    #
    # Changed from 300 to 60.
    #
    # Your state machine has a 1-minute
    # LIQUIDITY_EVENT state.
    #
    # A 5-minute outer loop would make
    # 1-minute monitoring impossible.
    #
    POLL_SECONDS = 300

    # ------------------------------------------
    # CREATE SCANNER
    # ------------------------------------------
    # ------------------------------------------
    # OUTPUT TIER FILTER
    #
    # Only Tier A setups get written to the results
    # file. Change/extend to e.g. {Tier.A, Tier.B}
    # to include more tiers in the output.
    # ------------------------------------------
    OUTPUT_TIERS = {Tier.A}

    scanner = AdaptiveScanner(
        watchlist=watchlist,
        data_fetcher=fetcher,
        on_confirmed_trade=(handle_confirmed_trade),
        results_file_prefix=os.path.join(root_dir, "logs", "scan_results"),
        operating_start=(OPERATING_START),
        operating_end=(OPERATING_END),
        operating_tz=(OPERATING_TZ),
        output_tiers=OUTPUT_TIERS,
    )

    # ------------------------------------------
    # START
    # ------------------------------------------
    scanner.run_forever(poll_seconds=POLL_SECONDS)

    # For one scan instead:
    #
    # scanner.run_once()