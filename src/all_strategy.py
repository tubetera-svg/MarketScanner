from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd
from protected_swings import (
    PROTECTED_SWINGS_LOOKBACK_DAYS as _PS_LOOKBACK,
    STATE_ANTICIPATED,
    STATE_CONFIRMED,
    STATE_NONE,
    ProtectedSwingAnalysis,
    _collect_prior_run,
    _first_after,
    _ohlc_arrays,
    detect_swing_points,
    evaluate_protected_swings,
    find_fvgs,
)
from daily_context import annotate_frame, compute_daily_context
from propulsion_blocks import (
    PROPULSION_BLOCKS_LOOKBACK_DAYS as _PB_LOOKBACK,
    PropulsionBlockAnalysis,
    evaluate_propulsion_blocks,
)

log = logging.getLogger(__name__)


_BHAVCOPY_CACHE: Dict[str, Optional[pd.DataFrame]] = {}
_BHAVCOPY_CACHE_MAX = 256
_SYMBOL_DAILY_CACHE: Dict[tuple[str, int, tuple[str, ...]], Dict[str, pd.DataFrame]] = {}
_SYMBOL_DAILY_CACHE_MAX = 64


def _cache_bhavcopy(key: str, value: Optional[pd.DataFrame]) -> None:
    """Store a bhavcopy frame, evicting the oldest entry when over capacity."""
    if len(_BHAVCOPY_CACHE) >= _BHAVCOPY_CACHE_MAX:
        _BHAVCOPY_CACHE.pop(next(iter(_BHAVCOPY_CACHE)))
    _BHAVCOPY_CACHE[key] = value


def _cache_daily_map(cache_key, value):
    if len(_SYMBOL_DAILY_CACHE) >= _SYMBOL_DAILY_CACHE_MAX:
        _SYMBOL_DAILY_CACHE.pop(next(iter(_SYMBOL_DAILY_CACHE)))
    _SYMBOL_DAILY_CACHE[cache_key] = value

# NSE holidays used by the historical runner. Keep this list updated when
# NSE publishes the next annual trading calendar.
NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
    date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14),
    date(2026, 5, 1), date(2026, 5, 27), date(2026, 6, 26),
    date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 9), date(2026, 11, 24), date(2026, 12, 25),
}


def resolve_previous_working_date(requested_date: date) -> tuple[date, date, str | None]:
    """Return the requested date and the latest usable NSE trading date."""
    resolved_date = requested_date
    while resolved_date.weekday() >= 5 or resolved_date in NSE_HOLIDAYS:
        resolved_date -= timedelta(days=1)

    if resolved_date == requested_date:
        return requested_date, resolved_date, None
    reason = "weekend" if requested_date.weekday() >= 5 else "NSE holiday"
    return requested_date, resolved_date, reason


class ScanCancelled(BaseException):
    """Aborts a running scan. BaseException so per-symbol ``except Exception`` guards don't swallow it."""


_scan_cancel = threading.Event()


def request_scan_cancel() -> None:
    """Ask the in-flight ``run_strategies`` call to stop at the next symbol."""
    _scan_cancel.set()


def clear_scan_cancel() -> None:
    _scan_cancel.clear()


def _check_scan_cancel() -> None:
    if _scan_cancel.is_set():
        raise ScanCancelled()


class _CancellableSymbols(list):
    """Symbol list that checks for a cancel request before yielding each symbol."""

    def __iter__(self):
        for symbol in super().__iter__():
            _check_scan_cancel()
            yield symbol


@dataclass
class StrategyExecution:
    name: str
    results: pd.DataFrame
    bullish: pd.DataFrame
    bearish: pd.DataFrame


@dataclass
class StrategySpec:
    name: str
    runner: Callable[..., StrategyExecution]


def _find_column(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    lookup = {str(c).strip().upper(): c for c in columns}
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return None


def _get_md_service():
    """Lazily import the cache-aside OHLC service (DB first, provider for live).

    Symbols arrive here with their exchange prefix intact (e.g. ``NSE:INFY``,
    ``OANDA:XAUUSD``, ``MCX:CRUDEOIL``). The service routes each one to the right
    upstream (NSE bhavcopy vs TradingView) and prefers stored SQLite data, only
    fetching the missing/live window from the provider otherwise.
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from market_data import service as md_service

    return md_service


def _source_for_symbol(symbol: str) -> str:
    """Map a (possibly prefixed) symbol to its upstream data source name."""
    from market_data.config import SOURCE_NSE, SOURCE_TRADINGVIEW, normalize_source

    value = str(symbol).strip().upper()
    if ":" in value:
        prefix = value.split(":", 1)[0]
        return SOURCE_NSE if prefix == "NSE" else SOURCE_TRADINGVIEW
    return normalize_source(SOURCE_NSE)


_IST = ZoneInfo("Asia/Kolkata")


def _market_today(symbol: str) -> date:
    """Calendar date of the symbol's current daily bar, in that market's own zone.

    Mirrors the day boundaries of ``market_data.service.latest_final_session``:
    NSE / NSE IX use the IST date, crypto the UTC date, and other TradingView
    markets (forex/commodities) the New York date. Never the host-local date.
    """
    return _get_md_service().market_today(_source_for_symbol(symbol), symbol, now=datetime.now(timezone.utc))


# User-tunable strategy parameters (Settings page -> config/app_settings.json
# "strategy" block, owned by api/app_settings.py). The first choice is the default.
STRATEGY_SETTING_CHOICES: Dict[str, tuple[str, ...]] = {
    "ltf_timeframe": ("1h", "15m"),
    "propulsion_mean_threshold": ("range", "body"),
}


def strategy_setting(key: str) -> str:
    """Current value of a strategy parameter; re-read per call so a Settings
    change applies to the next scan. Missing/invalid values fall back to the default."""
    import json
    from pathlib import Path

    choices = STRATEGY_SETTING_CHOICES[key]
    path = Path(__file__).resolve().parent.parent / "config" / "app_settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("strategy", {}).get(key)
    except (OSError, ValueError, AttributeError):
        value = None
    return value if value in choices else choices[0]


_EXPECTED_SESSION_CACHE: Dict[tuple, Optional[date]] = {}


def _expected_last_session(symbol: str, as_of_date: date) -> Optional[date]:
    """Latest session whose *final* daily bar should exist for ``as_of_date``.

    Live (as_of today or later) uses each market's own cut-off (NSE bhavcopy
    17:00 IST, forex NY 17:00 rollover); historical dates use the calendar.
    """
    md = _get_md_service()
    source = _source_for_symbol(symbol)
    if as_of_date >= _market_today(symbol):
        return md.latest_final_session(source, symbol=symbol)
    prefix = str(symbol).upper().split(":", 1)[0] if ":" in str(symbol) else ""
    key = (source, prefix, as_of_date)
    if key not in _EXPECTED_SESSION_CACHE:
        if len(_EXPECTED_SESSION_CACHE) > 4096:
            _EXPECTED_SESSION_CACHE.clear()
        dates = md.expected_trading_dates(source, as_of_date - timedelta(days=12), as_of_date, symbol)
        _EXPECTED_SESSION_CACHE[key] = dates[-1] if dates else None
    return _EXPECTED_SESSION_CACHE[key]


def _is_stale(symbol: str, daily: pd.DataFrame, as_of_date: date) -> bool:
    """True when the latest bar is older than the session expected at ``as_of_date``
    (suspended symbol / missing data), so old bars are not reported as current.

    NSE uses its holiday calendar strictly; TradingView's calendar has no
    holiday list, so one missing session is tolerated there.
    """
    if daily is None or daily.empty:
        return False
    try:
        expected = _expected_last_session(symbol, as_of_date)
        if expected is None:
            return False
        last = pd.Timestamp(daily.index[-1]).date()
        if last >= expected:
            return False
        source = _source_for_symbol(symbol)
        if source == "NSE":
            return True
        missing = _get_md_service().expected_trading_dates(source, last + timedelta(days=1), expected, symbol)
        return len(missing) > 1
    except Exception as exc:  # calendar unavailable: never block a scan on it
        log.debug("Stale check skipped for %s: %s", symbol, exc)
        return False


def _rows_to_daily_df(rows: list[dict]) -> pd.DataFrame:
    """Convert stored/fetched OHLC rows into the daily frame the evaluators use."""
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    frame = pd.DataFrame(rows)
    dates = frame.get("date")
    if dates is not None and dates.astype(str).str.contains(r"[+-]\d{2}:\d{2}$|Z$", regex=True).any():
        # Intraday rows are UTC instants; daily rows are plain trading dates.
        frame["Date"] = pd.to_datetime(dates, errors="coerce", utc=True)
    else:
        frame["Date"] = pd.to_datetime(dates, errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date").set_index("Date")
    out = pd.DataFrame(index=frame.index)
    out["Open"] = frame.get("open")
    out["High"] = frame.get("high")
    out["Low"] = frame.get("low")
    out["Close"] = frame.get("close")
    return out


def _download_bhavcopy_for_date(trade_date: date) -> Optional[pd.DataFrame]:
    """Download one NSE full-market bhavcopy CSV.

    Kept because ``market_data.sources.nse_source`` (the live NSE provider used
    for backfill) reuses it. Strategies themselves no longer call this directly;
    they go through ``market_data.service.get_ohlc`` (DB-first, provider-fallback).
    """
    key = trade_date.strftime("%Y-%m-%d")
    if key in _BHAVCOPY_CACHE:
        return _BHAVCOPY_CACHE[key]

    # NSE bhavcopy URL: DDMMYYYY
    url = (
        "https://nsearchives.nseindia.com/products/content/"
        f"sec_bhavdata_full_{trade_date.strftime('%d%m%Y')}.csv"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }
    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=15) as response:
            content = response.read().decode("utf-8", errors="ignore")
        if not content or "<html>" in content.lower():
            _cache_bhavcopy(key, None)
            return None

        df = pd.read_csv(StringIO(content), on_bad_lines="skip")
        df.columns = [str(c).strip().upper() for c in df.columns]
        _cache_bhavcopy(key, df)
        return df
    except Exception:
        _cache_bhavcopy(key, None)
        return None


def _fetch_strategy_daily(
    symbol: str,
    as_of_date: date,
    max_lookback_days: int,
    hist_rows: Optional[list] = None,
) -> pd.DataFrame:
    """Daily OHLC for one strategy symbol under the read policy:

    * Historical (strictly before today) -> SQLite only. Backdate scans never
      hit a provider.
    * Today -> the final daily bar only: once the market's daily bar is ready
      (NSE bhavcopy after 17:00 IST) it is fetched/stored via get_ohlc; before
      that, SQLite is read, which never holds an in-progress bar (forex/crypto
      bars are stored only after their NY-17:00 / UTC-midnight close). Daily
      strategies therefore never see a forming candle; intraday timeframes use
      live TradingView bars instead (see ``_protected_swing_frame``).
    """
    md = _get_md_service()
    source = _source_for_symbol(symbol)
    today = _market_today(symbol)
    start = as_of_date - timedelta(days=max(int(max_lookback_days), 1))

    rows: list[dict] = []

    # Historical portion: strictly before today, SQLite only. When a batched
    # historical result is supplied (the strategy layer fetches the whole
    # universe in one query), use it directly instead of a per-symbol round-trip.
    hist_end = as_of_date if as_of_date < today else today - timedelta(days=1)
    if hist_end >= start:
        if hist_rows is not None:
            rows.extend(hist_rows)
        else:
            try:
                rows.extend(md.database.query_ohlc(source, symbol, start, hist_end))
            except Exception as exc:
                log.warning("DB read failed for %s history: %s", symbol, exc)

    # Live portion: only when today falls inside the requested window.
    if as_of_date >= today:
        session = None
        try:
            session = md.session_for_source(source)
        except Exception:
            session = None
        bar_ready = False
        if session is not None:
            try:
                import ict_scanner  # type: ignore

                bar_ready = bool(ict_scanner.is_daily_bar_ready(session))
            except Exception:
                bar_ready = False
        if bar_ready:
            try:
                result = md.get_ohlc(source, symbol, today, today, auto_fetch=True)
                rows.extend(result.rows)
            except Exception as exc:
                log.warning("Live provider fetch failed for %s: %s", symbol, exc)
        else:
            try:
                rows.extend(md.database.query_ohlc(source, symbol, today, today))
            except Exception as exc:
                log.warning("DB read failed for %s live: %s", symbol, exc)

    return _rows_to_daily_df(rows)


def _build_daily_map_for_symbols(
    symbols: Sequence[str],
    as_of_date: date,
    max_lookback_days: int,
) -> Dict[str, pd.DataFrame]:
    symbols_upper = [str(s).strip().upper() for s in symbols if str(s).strip()]
    sorted_key = tuple(sorted(set(symbols_upper)))
    cache_key = (as_of_date.strftime("%Y-%m-%d"), int(max_lookback_days), sorted_key)
    if cache_key in _SYMBOL_DAILY_CACHE:
        return _SYMBOL_DAILY_CACHE[cache_key]

    out: Dict[str, pd.DataFrame] = {}
    start = as_of_date - timedelta(days=max(int(max_lookback_days), 1))

    md = _get_md_service()
    md.database.init_db()

    # Group symbols by upstream source and market date (IST / NY / UTC) so the
    # historical window can be fetched in one batched query per group instead
    # of one round-trip per symbol, ending where _fetch_strategy_daily expects.
    by_source: Dict[tuple[str, date], list[str]] = {}
    for sym in symbols_upper:
        by_source.setdefault((_source_for_symbol(sym), _market_today(sym)), []).append(sym)

    hist_by_symbol: Dict[str, list[dict]] = {}
    for (source, today), syms in by_source.items():
        hist_end = as_of_date if as_of_date < today else today - timedelta(days=1)
        if hist_end < start:
            continue
        try:
            hist_by_symbol.update(
                md.database.query_ohlc_multi(source, syms, start, hist_end)
            )
        except Exception as exc:
            log.warning("Batch OHLC read failed for %s: %s", source, exc)

    for sym in symbols_upper:
        _check_scan_cancel()
        try:
            out[sym] = _fetch_strategy_daily(
                sym, as_of_date, max_lookback_days, hist_rows=hist_by_symbol.get(sym)
            )
        except Exception as exc:  # provider disabled / no upstream / network
            log.warning("No OHLC for %s: %s", sym, exc)
            out[sym] = pd.DataFrame(columns=["Open", "High", "Low", "Close"])

    _cache_daily_map(cache_key, out)
    return out


def _fetch_daily_from_bhavcopy(symbol: str, as_of_date: date, max_lookback_days: int) -> pd.DataFrame:
    prebuilt = _build_daily_map_for_symbols([symbol], as_of_date, max_lookback_days)
    return prebuilt.get(symbol.strip().upper(), pd.DataFrame(columns=["Open", "High", "Low", "Close"]))


PROTECTED_SWING_TIMEFRAMES = ("daily", "weekly", "15m", "1h", "4h")
_PROTECTED_SWING_TV_TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h"}


def _protected_swing_frame(
    symbol: str, timeframe: str, daily: pd.DataFrame, as_of_date: Optional[date] = None
) -> pd.DataFrame:
    """Build the protected-swing frame from historical or live data.

    Daily/weekly frames come from ``daily`` (SQLite, final bars only). Intraday
    frames (15m/1h/4h) are fetched from TradingView: live (through now) when
    ``as_of_date`` is today, and ending at ``as_of_date`` for a historical run so
    no bar after the tested session can leak in (look-ahead).
    """
    normalized = str(timeframe).strip().lower()
    if normalized not in PROTECTED_SWING_TIMEFRAMES:
        raise ValueError(f"Unsupported protected swing timeframe: {timeframe}")
    if normalized == "daily":
        return daily
    if normalized == "weekly":
        return (
            daily.resample("W-FRI")
            .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
            .dropna(subset=["Open", "High", "Low", "Close"])
        )

    from market_data.sources import tradingview_source

    # fetch_timeframe selects intraday bars by the IST date of their open for
    # every market, so cap the window in IST.
    today = _get_md_service().ist_today(now=datetime.now(timezone.utc))
    end = min(as_of_date or today, today)
    rows = tradingview_source.fetch_timeframe(
        symbol=symbol,
        start_date=end - timedelta(days=30),
        end_date=end,
        timeframe=_PROTECTED_SWING_TV_TIMEFRAMES[normalized],
        exchange=symbol.split(":", 1)[0] if ":" in symbol else "NSE",
    )
    frame = _rows_to_daily_df(rows)
    # Rows carry UTC instants (time contract). Strategy frames index intraday
    # bars by naive IST wall time, so session days and note dates are the same
    # on any host and unchanged from before the UTC switch.
    if not frame.empty and frame.index.tz is not None:
        frame.index = frame.index.tz_convert(_IST).tz_localize(None)
    return frame


def _daily_frame_stale(symbol: str, daily: pd.DataFrame, as_of_date: date, timeframe: str) -> bool:
    """Stale guard for the structure strategies (intraday frames are fetched live)."""
    return str(timeframe).strip().lower() in ("daily", "weekly") and _is_stale(symbol, daily, as_of_date)


def _liquidity_context(daily: pd.DataFrame, as_of_date: date) -> Dict[str, object]:
    """Prior-week and pre-week swing extremes: the pools ``_build_trade_plan`` targets."""
    if daily is None or daily.empty:
        return {"prior_weeks": [], "swing": {}}
    return {
        "prior_weeks": _prior_week_extremes(daily, as_of_date),
        "swing": _pre_week_swing_extremes(daily, as_of_date) or {},
    }


def _next_session(symbol: str, frame: pd.DataFrame, sessions: int = 1) -> Optional[date]:
    """The ``sessions``-th trading day after the frame's last bar (may be in the
    future, unlike ``expected_trading_dates`` which never returns future dates)."""
    if frame is None or frame.empty:
        return None
    day = pd.Timestamp(frame.index[-1]).date()
    nse = _source_for_symbol(symbol) == "NSE"
    found = 0
    while found < sessions:
        day += timedelta(days=1)
        if day.weekday() < 5 and not (nse and day in NSE_HOLIDAYS):
            found += 1
    return day


# Lower-timeframe confirmation contract (consumed by src/ltf_confirmation.py and
# the API's LTF confirmation watcher). A daily setup that should be confirmed
# intraday carries a zone, an invalidation level and the last valid session.
LTF_COLUMNS = ("ltf_zone_low", "ltf_zone_high", "ltf_invalidation", "ltf_signal_date", "ltf_valid_until")


def _set_ltf_setup(
    results: pd.DataFrame,
    idx: int,
    direction: int,
    high: float,
    low: float,
    signal_date: object,
    valid_until: Optional[date],
    timeframe: str = "daily",
    invalidation: Optional[float] = None,
    full_zone: bool = False,
) -> None:
    """Arm an intraday (1h/15m) CISD confirmation for a daily signal candle.

    Zone follows the fractal-model Candle 4 rule: a bullish setup is expected to
    wick into the upper half of the signal candle, a bearish one into the lower
    half. ``full_zone`` uses the whole ``low..high`` span instead (e.g. a
    propulsion block). Invalidation defaults to the far side of the candle.
    """
    if str(timeframe).strip().lower() != "daily" or direction == 0 or valid_until is None:
        return
    eq = (float(high) + float(low)) / 2.0
    if full_zone:
        zone_low, zone_high = float(low), float(high)
    else:
        zone_low, zone_high = (eq, float(high)) if direction > 0 else (float(low), eq)
    if invalidation is None:
        invalidation = float(low) if direction > 0 else float(high)
    for column in LTF_COLUMNS:
        if column not in results.columns:
            results[column] = pd.Series([None] * len(results), index=results.index, dtype=object)
    results.at[idx, "ltf_zone_low"] = round(zone_low, 4)
    results.at[idx, "ltf_zone_high"] = round(zone_high, 4)
    results.at[idx, "ltf_invalidation"] = round(float(invalidation), 4)
    results.at[idx, "ltf_signal_date"] = pd.Timestamp(signal_date).date().isoformat()
    results.at[idx, "ltf_valid_until"] = valid_until.isoformat()


def _inside_bar_points(daily: pd.DataFrame) -> Dict[str, float]:
    curr = daily.iloc[-1]
    d1 = daily.iloc[-2]
    d2 = daily.iloc[-3]

    return {
        "curr_high": float(curr["High"]),
        "curr_low": float(curr["Low"]),
        "curr_close": float(curr["Close"]),
        "d1_open": float(d1["Open"]),
        "d1_high": float(d1["High"]),
        "d1_low": float(d1["Low"]),
        "d1_close": float(d1["Close"]),
        "d2_open": float(d2["Open"]),
        "d2_high": float(d2["High"]),
        "d2_low": float(d2["Low"]),
        "d2_close": float(d2["Close"]),
    }


def _inside_bar_bullish(v: Dict[str, float]) -> bool:
    return (
        v["d2_open"] < v["d2_close"]
        and abs(v["d2_close"] - v["d2_open"]) > abs(v["d2_high"] - v["d2_low"]) * 0.6
        and v["d1_high"] <= v["d2_high"]
        and v["d1_low"] >= v["d2_low"]
        and v["curr_low"] < v["d1_low"]
        and v["curr_high"] < v["d1_high"]
        and v["curr_close"] > v["d1_low"]
    )


def _inside_bar_bearish(v: Dict[str, float]) -> bool:
    return (
        v["d2_open"] > v["d2_close"]
        and abs(v["d2_open"] - v["d2_close"]) > abs(v["d2_high"] - v["d2_low"]) * 0.6
        and v["d1_high"] < v["d2_high"]
        and v["d1_low"] > v["d2_low"]
        and v["curr_high"] > v["d1_high"]
        and v["curr_low"] > v["d1_low"]
        and v["curr_close"] < v["d1_high"]
    )


def _ema5_sweep_points(daily: pd.DataFrame) -> Dict[str, float]:
    work = daily.copy()
    work["ema5"] = work["Close"].ewm(span=5, adjust=False, min_periods=5).mean()
    work = work.dropna(subset=["ema5"])

    curr = work.iloc[-1]
    prev = work.iloc[-2]

    return {
        "curr_open": float(curr["Open"]),
        "curr_high": float(curr["High"]),
        "curr_low": float(curr["Low"]),
        "curr_close": float(curr["Close"]),
        "curr_ema5": float(curr["ema5"]),
        "prev_open": float(prev["Open"]),
        "prev_high": float(prev["High"]),
        "prev_low": float(prev["Low"]),
        "prev_close": float(prev["Close"]),
        "prev_ema5": float(prev["ema5"]),
    }


def _ema5_sweep_bullish(v: Dict[str, float]) -> bool:
    return (
        v["curr_low"] > v["curr_ema5"]
        and v["prev_low"] > v["prev_ema5"]
        and v["curr_high"] > v["prev_high"]
        and v["curr_close"] < v["prev_high"]
    )


def _ema5_sweep_bearish(v: Dict[str, float]) -> bool:
    return (
        v["curr_high"] < v["curr_ema5"]
        and v["prev_high"] < v["prev_ema5"]
        and v["curr_low"] < v["prev_low"]
        and v["curr_close"] > v["prev_low"]
    )


def _build_tradingview_link(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if ":" in value:
        return f"https://www.tradingview.com/chart/?symbol={quote(value)}"
    return f"https://www.tradingview.com/chart/?symbol=NSE%3A{quote(value)}"


def _extract_signal_frames(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    extra_cols = [c for c in ("daily_bias", "weekly_bias", "monthly_bias", "confluence", "note", "signal_date") if c in results.columns]
    bullish = (
        results.loc[results["bullish_match"] == True, ["symbol"] + extra_cols]
        .sort_values("symbol")
        .reset_index(drop=True)
    )
    bearish = (
        results.loc[results["bearish_match"] == True, ["symbol"] + extra_cols]
        .sort_values("symbol")
        .reset_index(drop=True)
    )

    for frame in (bullish, bearish):
        frame["tradingview_link"] = frame["symbol"].apply(_build_tradingview_link)

    return bullish, bearish


def run_inside_bar_daily_sweep(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
        }
    )

    for idx, symbol in enumerate(symbols):
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(symbol=symbol, as_of_date=as_of_date, max_lookback_days=160)
        else:
            daily = daily_map.get(str(symbol).upper(), pd.DataFrame(columns=["Open", "High", "Low", "Close"]))

        if len(daily) < 4:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol}: SKIPPED (no_data)")
            continue

        if _is_stale(str(symbol).upper(), daily, as_of_date):
            results.at[idx, "status"] = "stale"
            continue

        values = _inside_bar_points(daily)
        bullish = _inside_bar_bullish(values)
        bearish = _inside_bar_bearish(values)

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"

        if verbose:
            print(f"{symbol}: bullish={bullish}, bearish={bearish}")

    bullish, bearish = _extract_signal_frames(results)
    return StrategyExecution(
        name="inside_bar_pattern_daily_sweep",
        results=results,
        bullish=bullish,
        bearish=bearish,
    )


def run_ema5_sweep(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
        }
    )

    for idx, symbol in enumerate(symbols):
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(symbol=symbol, as_of_date=as_of_date, max_lookback_days=40)
        else:
            daily = daily_map.get(str(symbol).upper(), pd.DataFrame(columns=["Open", "High", "Low", "Close"]))

        if len(daily) < 6:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol}: SKIPPED (no_data)")
            continue

        if _is_stale(str(symbol).upper(), daily, as_of_date):
            results.at[idx, "status"] = "stale"
            continue

        values = _ema5_sweep_points(daily)
        bullish = _ema5_sweep_bullish(values)
        bearish = _ema5_sweep_bearish(values)

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"

        if verbose:
            print(f"{symbol}: bullish={bullish}, bearish={bearish}")

    bullish, bearish = _extract_signal_frames(results)
    return StrategyExecution(
        name="ema5_sweep",
        results=results,
        bullish=bullish,
        bearish=bearish,
    )


PROTECTED_SWINGS_LOOKBACK_DAYS = _PS_LOOKBACK


def run_protected_swings(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
    timeframe: str = "daily",
) -> StrategyExecution:
    """Protected Swings strategy.

    A protected swing is a swing high/low that has been swept (or entered via an
    FVG) and then *confirmed* by a candle closing beyond the body (the relevant
    open) of the same-direction candle series that created it. The most recent confirmed,
    non-invalidated swing sets the live bias (protected low -> bullish, protected
    high -> bearish) and becomes the active "stepping-stone" level.

    Output mirrors the weekly-profile conventions (``entry/sl/target/rr/state``
    plus ``bullish_match/bearish_match/final_signal``) so it flows through the
    same scan, backtest and UI plumbing unchanged. A signal (``final_signal``)
    is emitted only while a swing is *confirmed* — an anticipated (swept but
    unconfirmed) swing never fires a trade, matching the rule that protection is
    only real once the qualifying close lands.
    """
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
            "profile": "Protected Swings",
            "note": "",
            "state": STATE_NONE,
            "direction": 0,
            "entry": None,
            "sl": None,
            "target": None,
            "rr": None,
            "atr": None,
            "track_mode": "",
            "swing_level": None,
            "protected_level": None,
            "confirmation_price": None,
            "tag": "",
            "timeframe": timeframe,
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(
                symbol=symbol_upper, as_of_date=as_of_date, max_lookback_days=_PS_LOOKBACK
            )
        else:
            daily = daily_map.get(
                symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"])
            )

        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)
        if _daily_frame_stale(symbol_upper, daily, as_of_date, timeframe):
            results.at[idx, "status"] = "stale"
            results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
            continue

        try:
            frame = _protected_swing_frame(symbol_upper, timeframe, daily, as_of_date)
        except Exception as exc:
            log.warning("Protected swing %s frame failed for %s: %s", timeframe, symbol_upper, exc)
            frame = pd.DataFrame(columns=["Open", "High", "Low", "Close"])

        if frame.empty or len(frame) < 6:
            results.at[idx, "status"] = "no_data"
            results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
            if verbose:
                print(f"{symbol_upper}: SKIPPED (no_data)")
            continue

        analysis: ProtectedSwingAnalysis = evaluate_protected_swings(frame)
        active = analysis.active
        anticip = analysis.anticipated
        swing = active if active is not None else anticip
        track_mode = _track_mode_for(symbol_upper)

        direction = swing.direction if swing is not None else 0
        bullish = direction > 0
        bearish = direction < 0
        has_confirmed = active is not None
        # A protected swing is reported only on the confirmation session.
        # A swing confirmed earlier that merely persists is not re-listed.
        confirmed_window = (
            has_confirmed
            and active.confirm_idx is not None
            and active.confirm_idx == len(frame) - 1
        )

        results.at[idx, "direction"] = direction
        results.at[idx, "note"] = str(analysis.note)
        results.at[idx, "track_mode"] = track_mode
        results.at[idx, "status"] = "complete"

        if swing is not None:
            results.at[idx, "state"] = STATE_CONFIRMED if has_confirmed else STATE_ANTICIPATED
            results.at[idx, "protected_level"] = round(float(swing.protected_level), 4)
            results.at[idx, "swing_level"] = round(float(swing.swing_level), 4)
            results.at[idx, "confirmation_price"] = (
                round(float(swing.confirmation_price), 4)
                if swing.confirmation_price is not None
                else None
            )
            results.at[idx, "tag"] = swing.tag
            # The protected level is the reference for a confirmed bias; an
            # anticipated swing is reported for awareness but does not fire.
            results.at[idx, "final_signal"] = confirmed_window
            results.at[idx, "bullish_match"] = bool(bullish and confirmed_window)
            results.at[idx, "bearish_match"] = bool(bearish and confirmed_window)

            if confirmed_window:
                current_close = float(frame.iloc[-1]["Close"])
                protected_extreme = swing.sweep_extreme if swing.sweep_extreme is not None else swing.swing_level
                outcome = {
                    "bullish": bool(bullish and confirmed_window),
                    "bearish": bool(bearish and confirmed_window),
                    # Stop beyond the protected low/high itself (the sweep extreme).
                    "sl_level": float(protected_extreme),
                    "entry_ref": current_close,
                }
                plan = _build_trade_plan(outcome, _liquidity_context(daily, as_of_date), _daily_atr(frame))
                results.at[idx, "entry"] = plan["entry"]
                results.at[idx, "sl"] = plan["sl"]
                results.at[idx, "target"] = plan["target"]
                results.at[idx, "rr"] = plan["rr"]
                results.at[idx, "atr"] = plan["atr"]
                confirm_bar = frame.iloc[-1]
                _set_ltf_setup(
                    results, idx, direction, float(confirm_bar["High"]), float(confirm_bar["Low"]),
                    frame.index[-1], _next_session(symbol_upper, frame, 1), timeframe,
                    invalidation=float(protected_extreme),
                )

            results.at[idx, "note"] = analysis.note
        else:
            results.at[idx, "state"] = STATE_NONE
            results.at[idx, "note"] = str(analysis.note)

        if verbose:
            print(
                f"{symbol_upper}: protected_swings direction={direction} "
                f"state={results.at[idx, 'state']} {analysis.note}"
            )

    bullish_frame, bearish_frame = _extract_weekly_profile_signal_frames(results)
    return StrategyExecution(
        name="protected_swings",
        results=results,
        bullish=bullish_frame,
        bearish=bearish_frame,
    )


def _cisd_levels(daily: pd.DataFrame, direction: int, after_idx: int) -> List[float]:
    """CISD levels formed after ``after_idx``, defined as for the protected swing
    itself: the open of the first candle of an opposing series (down-close for
    bullish, up-close for bearish) that a later candle closed through."""
    o, _h, _l, c = _ohlc_arrays(daily)
    down = direction > 0
    levels: List[float] = []
    for end in range(after_idx + 1, len(c) - 1):
        if (c[end + 1] < o[end + 1]) if down else (c[end + 1] > o[end + 1]):
            continue  # series continues; evaluate it at its last candle
        run = _collect_prior_run(o, c, end, down)
        if not run or run[0] <= after_idx:
            continue
        level = float(o[run[0]])
        if _first_after(c, level, end, above=down) is not None:
            levels.append(level)
    return levels


# A POI gap narrower than this fraction of ATR(14) is noise and is skipped.
POI_MIN_FVG_ATR = 0.1


def _protected_extreme(active) -> float:
    """The protected low/high itself: sweep extreme, else swing level, else CISD level."""
    return next(
        float(value)
        for value in (
            getattr(active, "sweep_extreme", None),
            getattr(active, "swing_level", None),
            active.protected_level,
        )
        if value is not None
    )


def _select_point_of_interest(
    frame: pd.DataFrame,
    active,
) -> Optional[tuple[float, str]]:
    """Select the nearest POI from the protected swing toward current price.

    The search starts at the protected low/high itself (the sweep extreme and
    the bar that swept), not at the CISD confirmation."""
    if active is None or active.confirm_idx is None:
        return None
    sweep_idx = getattr(active, "sweep_idx", None)
    anchor_idx = sweep_idx if sweep_idx is not None else active.confirm_idx
    anchor = _protected_extreme(active)
    current = float(frame.iloc[-1]["Close"])
    direction = int(active.direction)
    if direction == 0 or (direction > 0 and current <= anchor) or (direction < 0 and current >= anchor):
        return None

    def in_path(level: float) -> bool:
        return anchor < level < current if direction > 0 else current < level < anchor

    closes = pd.to_numeric(frame["Close"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(frame["High"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(frame["Low"], errors="coerce").to_numpy(dtype=float)

    def gap_intact(gap) -> bool:
        # A gap a later candle has closed through (bullish: below its low,
        # bearish: above its high) is spent and no longer a POI.
        later = closes[gap.idx + 1:]
        return not (later < gap.gap_low).any() if direction > 0 else not (later > gap.gap_high).any()

    def swing_untaken(swing) -> bool:
        # A swing whose liquidity was already run by a later candle is no longer a POI.
        if swing.is_high:
            return not (highs[swing.idx + 1:] > swing.high).any()
        return not (lows[swing.idx + 1:] < swing.low).any()

    atr = _daily_atr(frame)
    min_gap = POI_MIN_FVG_ATR * atr if atr else 0.0
    gaps = [
        gap for gap in find_fvgs(frame)
        if gap.series_start >= anchor_idx
        and gap.gap_high - gap.gap_low >= min_gap
        and gap.fvg_type == ("bullish" if direction > 0 else "bearish")
        and in_path(gap.gap_low if direction > 0 else gap.gap_high)
        and gap_intact(gap)
    ]
    if gaps:
        # First gap met walking from the protected swing; price reaches it at
        # the edge nearest current price (bullish: top, bearish: bottom).
        gap = min(gaps, key=lambda item: abs((item.gap_low if direction > 0 else item.gap_high) - anchor))
        level = gap.gap_high if direction > 0 else gap.gap_low
        return float(level), "fvg"

    swings = [
        swing for swing in detect_swing_points(frame)
        if swing.idx > anchor_idx and in_path(swing.high if swing.is_high else swing.low)
        and swing_untaken(swing)
    ]
    if swings:
        swing = min(swings, key=lambda item: abs((item.high if item.is_high else item.low) - anchor))
        return float(swing.high if swing.is_high else swing.low), "sweep"

    cisds = [level for level in _cisd_levels(frame, direction, anchor_idx) if in_path(level)]
    if cisds:
        return min(cisds, key=lambda level: abs(level - anchor)), "CISD"
    return None


def run_points_of_interest(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
    timeframe: str = "daily",
) -> StrategyExecution:
    """Report the highest-priority protected-swing point of interest."""
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
            "profile": "Points of Interest",
            "note": "",
            "state": STATE_NONE,
            "direction": 0,
            "track_mode": "",
            "triggered_level": None,
            "type": "",
            "timeframe": timeframe,
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        daily = (
            _fetch_daily_from_bhavcopy(symbol_upper, as_of_date, _PS_LOOKBACK)
            if daily_map is None
            else daily_map.get(symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"]))
        )
        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)
        if _daily_frame_stale(symbol_upper, daily, as_of_date, timeframe):
            results.at[idx, "status"] = "stale"
            continue
        try:
            frame = _protected_swing_frame(symbol_upper, timeframe, daily, as_of_date)
        except Exception as exc:
            log.warning("Points of interest frame failed for %s: %s", symbol_upper, exc)
            frame = pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        if frame.empty or len(frame) < 5:
            results.at[idx, "status"] = "no_data"
            continue

        analysis: ProtectedSwingAnalysis = evaluate_protected_swings(frame)
        candidate = analysis.active
        results.at[idx, "status"] = "complete"
        results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
        results.at[idx, "note"] = analysis.note

        poi = _select_point_of_interest(frame, candidate)
        if poi is not None:
            level, poi_type = poi
            results.at[idx, "state"] = candidate.state
            results.at[idx, "direction"] = candidate.direction
            results.at[idx, "triggered_level"] = round(level, 4)
            results.at[idx, "type"] = poi_type
            results.at[idx, "bullish_match"] = candidate.direction > 0
            results.at[idx, "bearish_match"] = candidate.direction < 0
            extreme = _protected_extreme(candidate)
            window = frame["Low" if candidate.direction > 0 else "High"].iloc[
                candidate.series_start: candidate.confirm_idx + 1
            ]
            made_on = window.idxmin() if candidate.direction > 0 else window.idxmax()
            results.at[idx, "note"] = (
                f"{poi_type} POI at {level:.4f} | protected {'low' if candidate.direction > 0 else 'high'} "
                f"{extreme:.4f} ({pd.Timestamp(made_on):%Y-%m-%d}) | CISD {candidate.protected_level:.4f}"
            )
            continue

        if verbose:
            print(f"{symbol_upper}: poi={results.at[idx, 'type']} level={results.at[idx, 'triggered_level']}")

    bullish, bearish = _extract_points_of_interest_frames(results)
    return StrategyExecution(
        name="points_of_interest",
        results=results,
        bullish=bullish,
        bearish=bearish,
    )


def _closure_pois(frame: pd.DataFrame, active, reaction_bars: int) -> List[tuple[float, str]]:
    """POI levels in priority order, as known *before* the reaction candles:
    the FVG -> swing -> CISD POI worked from the protected swing toward price,
    then the protected (CISD) level of the swing itself."""
    levels: List[tuple[float, str]] = []
    history = frame.iloc[: len(frame) - reaction_bars]
    confirm_idx = getattr(active, "confirm_idx", None)
    if confirm_idx is not None and confirm_idx < len(history):
        poi = _select_point_of_interest(history, active)
        if poi is not None:
            levels.append(poi)
    levels.append((float(active.protected_level), "protected"))
    return levels


def _poi_reached(bar: pd.Series, direction: int, levels: List[tuple[float, str]]) -> Optional[tuple[float, str]]:
    for level, kind in levels:
        if (direction > 0 and float(bar["Low"]) <= level) or (direction < 0 and float(bar["High"]) >= level):
            return level, kind
    return None


def _candle_closure(frame: pd.DataFrame) -> Optional[Dict[str, object]]:
    """Candle 2 / Candle 3 closure on the latest bar at a protected-swing POI.

    TTrades fractal model (bullish; bearish mirrors):
    * Candle 2 closure: the candle reaches the POI, sweeps the previous
      candle's low and closes back above it (early reversal confirmation).
    * Candle 3 closure: candle 2 reached the POI but did NOT sweep candle 1's
      low and closed down; candle 3 then closes over candle 2's body.
    """
    if len(frame) < 5:
        return None
    analysis: ProtectedSwingAnalysis = evaluate_protected_swings(frame)
    active = analysis.active
    if active is None:
        return None
    direction = int(active.direction)

    c1, c2 = frame.iloc[-2], frame.iloc[-1]
    hit = _poi_reached(c2, direction, _closure_pois(frame, active, 1))
    if direction > 0:
        candle_2 = float(c2["Low"]) < float(c1["Low"]) and float(c2["Close"]) > float(c1["Low"])
    else:
        candle_2 = float(c2["High"]) > float(c1["High"]) and float(c2["Close"]) < float(c1["High"])
    if hit is not None and candle_2:
        return {"closure_type": "candle_2", "direction": direction, "level": hit[0], "poi_type": hit[1]}

    c1, c2, c3 = frame.iloc[-3], frame.iloc[-2], frame.iloc[-1]
    hit = _poi_reached(c2, direction, _closure_pois(frame, active, 2))
    c2_open, c2_close = float(c2["Open"]), float(c2["Close"])
    if direction > 0:
        no_sweep = float(c2["Low"]) >= float(c1["Low"])
        failed = c2_close <= c2_open
        body_closure = float(c3["Close"]) > max(c2_open, c2_close)
    else:
        no_sweep = float(c2["High"]) <= float(c1["High"])
        failed = c2_close >= c2_open
        body_closure = float(c3["Close"]) < min(c2_open, c2_close)
    if hit is not None and no_sweep and failed and body_closure:
        return {"closure_type": "candle_3", "direction": direction, "level": hit[0], "poi_type": hit[1]}
    return None


def run_candle_3_closure(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
    timeframe: str = "daily",
) -> StrategyExecution:
    """Report Candle 2 / Candle 3 closures at a protected-swing point of interest.

    ``candle_3_high/low/equilibrium`` describe the closure candle (the latest
    bar) whichever closure type fired; ``closure_type`` says which one.
    """
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
            "profile": "Candle 3 Closure",
            "note": "",
            "state": STATE_NONE,
            "direction": 0,
            "triggered_level": None,
            "closure_type": "",
            "candle_3_high": None,
            "candle_3_low": None,
            "equilibrium": None,
            "poi_type": "",
            "timeframe": timeframe,
        }
    )
    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        daily = (
            _fetch_daily_from_bhavcopy(symbol_upper, as_of_date, _PS_LOOKBACK)
            if daily_map is None
            else daily_map.get(symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"]))
        )
        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)
        if _daily_frame_stale(symbol_upper, daily, as_of_date, timeframe):
            results.at[idx, "status"] = "stale"
            continue
        try:
            frame = _protected_swing_frame(symbol_upper, timeframe, daily, as_of_date)
        except Exception as exc:
            log.warning("Candle 3 frame failed for %s: %s", symbol_upper, exc)
            frame = pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        results.at[idx, "status"] = "complete" if len(frame) >= 5 else "no_data"
        if len(frame) < 5:
            continue
        closure = _candle_closure(frame)
        if closure is None:
            continue
        direction = int(closure["direction"])
        closure_type = str(closure["closure_type"])
        candle = frame.iloc[-1]
        high = float(candle["High"])
        low = float(candle["Low"])
        results.at[idx, "direction"] = direction
        results.at[idx, "triggered_level"] = round(float(closure["level"]), 4)
        results.at[idx, "closure_type"] = closure_type
        results.at[idx, "candle_3_high"] = round(high, 4)
        results.at[idx, "candle_3_low"] = round(low, 4)
        results.at[idx, "equilibrium"] = round((high + low) / 2.0, 4)
        results.at[idx, "poi_type"] = str(closure["poi_type"])
        results.at[idx, "state"] = STATE_CONFIRMED
        results.at[idx, "bullish_match"] = direction > 0
        results.at[idx, "bearish_match"] = direction < 0
        results.at[idx, "note"] = (
            "Candle 2 closure (sweep + close back inside); Candle 3 continuation pending"
            if closure_type == "candle_2"
            else "Candle 3 body closure; Candle 4 expansion/retrace pending"
        )
        _set_ltf_setup(
            results, idx, direction, high, low, frame.index[-1], _next_session(symbol_upper, frame, 1), timeframe
        )
        if verbose:
            print(f"{symbol_upper}: {closure_type} direction={direction} eq={results.at[idx, 'equilibrium']}")

    bullish, bearish = _extract_candle_3_frames(results)
    return StrategyExecution(
        name="candle_3_closure",
        results=results,
        bullish=bullish,
        bearish=bearish,
    )


def run_propulsion_blocks(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
    timeframe: str = "daily",
) -> StrategyExecution:
    """Run the point-in-time propulsion-block lifecycle strategy."""
    _ = print_values
    mean_mode = strategy_setting("propulsion_mean_threshold")
    results = pd.DataFrame(
        {
            "symbol": list(symbols), "bullish_match": False, "bearish_match": False,
            "final_signal": False, "status": "pending", "profile": "Propulsion Blocks",
            "note": "", "state": STATE_NONE, "direction": 0, "entry": None,
            "sl": None, "target": None, "rr": None, "atr": None, "track_mode": "",
            "order_block_low": None, "order_block_high": None, "order_block_midpoint": None,
            "propulsion_open": None, "triggered_level": None, "mean_threshold": None,
            "confirmation_price": None,
            "timeframe": timeframe,
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        daily = (
            _fetch_daily_from_bhavcopy(symbol_upper, as_of_date, _PB_LOOKBACK)
            if daily_map is None
            else daily_map.get(symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"]))
        )
        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)
        if _daily_frame_stale(symbol_upper, daily, as_of_date, timeframe):
            results.at[idx, "status"] = "stale"
            results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
            continue
        try:
            frame = _protected_swing_frame(symbol_upper, timeframe, daily, as_of_date)
        except Exception as exc:
            log.warning("Propulsion block %s frame failed for %s: %s", timeframe, symbol_upper, exc)
            frame = pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        if frame.empty or len(frame) < 5:
            results.at[idx, "status"] = "no_data"
            results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
            continue

        analysis: PropulsionBlockAnalysis = evaluate_propulsion_blocks(frame, mean_mode=mean_mode)
        active = analysis.active
        anticipated = analysis.anticipated
        candidate = active if active is not None else anticipated
        confirmed_window = (
            active is not None
            and active.confirm_idx is not None
            and active.confirm_idx == len(frame) - 1
        )
        results.at[idx, "status"] = "complete"
        results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
        results.at[idx, "note"] = analysis.note
        if candidate is None:
            continue

        results.at[idx, "state"] = candidate.state
        results.at[idx, "direction"] = candidate.direction
        results.at[idx, "order_block_low"] = round(candidate.order_block_low, 4)
        results.at[idx, "order_block_high"] = round(candidate.order_block_high, 4)
        results.at[idx, "order_block_midpoint"] = round(candidate.order_block_midpoint, 4)
        results.at[idx, "propulsion_open"] = round(candidate.propulsion_open, 4)
        results.at[idx, "triggered_level"] = round(candidate.propulsion_open, 4)
        results.at[idx, "mean_threshold"] = round(candidate.mean_threshold, 4)
        results.at[idx, "confirmation_price"] = (
            round(candidate.confirmation_price, 4)
            if candidate.confirmation_price is not None else None
        )
        results.at[idx, "final_signal"] = confirmed_window
        results.at[idx, "bullish_match"] = bool(candidate.direction > 0 and confirmed_window)
        results.at[idx, "bearish_match"] = bool(candidate.direction < 0 and confirmed_window)
        if confirmed_window:
            outcome = {
                "bullish": candidate.direction > 0,
                "bearish": candidate.direction < 0,
                "sl_level": candidate.mean_threshold,
                # Entry reference is the propulsion candle's open (a resting
                # level). The backtest engine still fills at the next open.
                "entry_ref": float(candidate.propulsion_open),
            }
            plan = _build_trade_plan(outcome, _liquidity_context(daily, as_of_date), _daily_atr(frame))
            for key in ("entry", "sl", "target", "rr", "atr"):
                results.at[idx, key] = plan[key]
            # Intraday confirmation zone: the propulsion candle between its open
            # and the mean threshold; invalidation is a close through the mean.
            _set_ltf_setup(
                results, idx, candidate.direction,
                max(candidate.propulsion_open, candidate.mean_threshold),
                min(candidate.propulsion_open, candidate.mean_threshold),
                frame.index[-1], _next_session(symbol_upper, frame, 1), timeframe,
                invalidation=candidate.mean_threshold, full_zone=True,
            )
        if verbose:
            print(f"{symbol_upper}: propulsion_blocks state={candidate.state} {analysis.note}")

    bullish_frame, bearish_frame = _extract_weekly_profile_signal_frames(results)
    return StrategyExecution("propulsion_blocks", results, bullish_frame, bearish_frame)


def _daily_bias_from_history(history: pd.DataFrame) -> tuple[str, float | None, float | None, float | None]:
    """Resolve the historical daily bias and its candle range references.

    Bias comes from the reference candle vs the one before it: a close beyond
    the prior range, or a candle-2 closure (sweep one side of the prior range
    and close back inside it -> bias toward the other side). Conflicting reads
    are Neutral.
    """
    if history is None or len(history) < 2:
        return "Neutral", None, None, None
    reference = history.iloc[-1]
    prior = history.iloc[-2]
    reference_high = float(reference["High"])
    reference_low = float(reference["Low"])
    reference_eq = (reference_high + reference_low) / 2.0
    close = float(reference["Close"])
    prior_high = float(prior["High"])
    prior_low = float(prior["Low"])
    bull = close > prior_high or (reference_low < prior_low and close > prior_low)
    bear = close < prior_low or (reference_high > prior_high and close < prior_high)
    bias = "Bullish" if bull and not bear else "Bearish" if bear and not bull else "Neutral"
    return bias, reference_eq, reference_high, reference_low


def run_daily_bias_invalidation(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    """Trade only invalidation of a previously established daily bias.

    Per the TTrades invalidation framework only two things count:
    * opposing setup — the next candle sweeps the bias-side extreme of the
      reference candle and closes back inside it (an opposite candle 2);
    * EQ disrespect — the next candle closes through the reference candle's EQ.
    The following candle must then continue the other way without reclaiming
    EQ. Anything else (drift, consolidation, wicks) is noise.
    """
    _ = print_values
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
            "daily_bias": "Neutral",
            "invalidation_direction": 0,
            "invalidation_type": "",
            "eq": None,
            "entry": None,
            "sl": None,
            "target": None,
            "rr": None,
            "note": "",
            "signal_date": None,
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(symbol_upper, as_of_date, 600)
        else:
            daily = daily_map.get(symbol_upper, pd.DataFrame())
        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)
        if daily is None or len(daily) < 4:
            results.at[idx, "status"] = "no_data"
            continue
        if _is_stale(symbol_upper, daily, as_of_date):
            results.at[idx, "status"] = "stale"
            continue

        history = daily.iloc[:-2]
        bias, eq, reference_high, reference_low = _daily_bias_from_history(history)
        results.at[idx, "daily_bias"] = bias
        results.at[idx, "eq"] = eq
        results.at[idx, "status"] = "complete"
        if bias == "Neutral" or eq is None or reference_high is None or reference_low is None:
            results.at[idx, "status"] = "no_daily_bias"
            continue

        invalidation = daily.iloc[-2]
        continuation = daily.iloc[-1]
        invalidation_close = float(invalidation["Close"])
        continuation_close = float(continuation["Close"])
        eq_disrespected = (
            (bias == "Bullish" and invalidation_close < eq)
            or (bias == "Bearish" and invalidation_close > eq)
        )
        # Opposing setup = an opposite candle 2: sweep the bias-side extreme,
        # then close back inside the reference range. A bar that swept both
        # extremes and closed inside proves nothing, so it is ignored.
        swept_both_closed_inside = (
            float(invalidation["High"]) > reference_high
            and float(invalidation["Low"]) < reference_low
            and reference_low < invalidation_close < reference_high
        )
        opposing_setup = not swept_both_closed_inside and (
            (bias == "Bullish" and float(invalidation["High"]) > reference_high and invalidation_close < reference_high)
            or (bias == "Bearish" and float(invalidation["Low"]) < reference_low and invalidation_close > reference_low)
        )
        # Continuation the other way that does not reclaim EQ.
        continuation_ok = (
            (bias == "Bullish" and continuation_close < invalidation_close and continuation_close < eq)
            or (bias == "Bearish" and continuation_close > invalidation_close and continuation_close > eq)
        )
        if not (continuation_ok and (eq_disrespected or opposing_setup)):
            results.at[idx, "note"] = "daily bias intact or continuation unconfirmed"
            continue

        direction = -1 if bias == "Bullish" else 1
        invalidation_type = "eq_disrespect" if eq_disrespected else "opposing_setup"
        entry = continuation_close
        sl = float(invalidation["High"] if direction < 0 else invalidation["Low"])
        target = reference_low if direction < 0 else reference_high
        # A reference extreme price has already passed is behind the entry, not a target.
        if (direction < 0 and target >= entry) or (direction > 0 and target <= entry):
            target = None
        risk = abs(entry - sl)
        rr = abs(target - entry) / risk if (target is not None and risk > 0) else None
        results.at[idx, "invalidation_direction"] = direction
        results.at[idx, "invalidation_type"] = invalidation_type
        results.at[idx, "bullish_match"] = direction > 0
        results.at[idx, "bearish_match"] = direction < 0
        results.at[idx, "final_signal"] = True
        results.at[idx, "entry"] = entry
        results.at[idx, "sl"] = sl
        results.at[idx, "target"] = target
        results.at[idx, "rr"] = rr
        results.at[idx, "note"] = f"{bias} daily bias invalidated via {invalidation_type}; opposite continuation confirmed"
        # Date of the continuation (signal) candle, shown on the UI chip.
        results.at[idx, "signal_date"] = pd.Timestamp(daily.index[-1]).date().isoformat()
        _set_ltf_setup(
            results, idx, direction, float(continuation["High"]), float(continuation["Low"]),
            daily.index[-1], _next_session(symbol_upper, daily, 1), invalidation=sl,
        )
        if verbose:
            print(f"{symbol_upper}: {results.at[idx, 'note']}")

    bullish_frame, bearish_frame = _extract_signal_frames(results)
    return StrategyExecution(
        name="daily_bias_invalidation",
        results=results,
        bullish=bullish_frame,
        bearish=bearish_frame,
    )


# ---------------------------------------------------------------------------
# Weekly profile strategies (6-profile weekly series)
#
# 1. classic_expansion_sweep       Classic Expansion - Mon/Tue sets the weekly
#                                  extreme, price expands 2-3 days in that
#                                  direction, Friday slows/consolidates.
# 2. midweek_reversal_sweep        Midweek Reversal - early-week move reverses
#                                  at a Wednesday candle-two/three closure and
#                                  expands Thu-Fri in the new direction.
# 3. consolidation_reversal_sweep  Consolidation Reversal - Mon-Wed tight range,
#                                  Thursday fake breakout closing back inside,
#                                  Friday expands opposite the fake break.
# 4. intraweek_reversal_sweep      Intraweek Reversal - Monday expansion away
#                                  from the weekly open, Tuesday stall, Wed/Thu
#                                  candle-two reversal closure.
# 5. thursday_counter_sweep        Thursday Counter - established Mon-Wed bias
#                                  is countered on Thursday via a liquidity
#                                  grab that fails to continue.
# 6. tgif_setup_sweep              TGIF Setup - expansion week whose HTF
#                                  objective was reached; Friday retraces
#                                  20-30% back into the weekly range.
#
# Flags: WEEKLY_PROFILES_ENABLED is the master switch for the whole suite and
# WEEKLY_PROFILE_FLAGS toggles each profile individually. Disabled profiles are
# excluded from strategy_registry() so neither main.py nor run_strategies()
# will execute them.
#
# NOTE: NSE bhavcopy data is end-of-day only, so the hourly "change in the
# state of delivery" confirmation described in the source videos is approximated
# here with daily candle-two closures (a strong directional close through the
# prior day's range).
# ---------------------------------------------------------------------------

WEEKLY_PROFILES_ENABLED: bool = True

WEEKLY_PROFILE_FLAGS: Dict[str, bool] = {
    "classic_expansion_sweep": True,
    "midweek_reversal_sweep": True,
    "consolidation_reversal_sweep": True,
    "intraweek_reversal_sweep": True,
    "thursday_counter_sweep": True,
    "tgif_setup_sweep": True,
}

WEEKLY_PROFILE_LOOKBACK_DAYS = 60
CLASSIC_EXPANSION_MAX_EXPANSION_DAYS = 3
CONSOLIDATION_MAX_RANGE_PCT = 0.05
CONSOLIDATION_MAX_DRIFT_PCT = 0.02
INTRAWEEK_STALL_BODY_RATIO = 0.4
THURSDAY_COUNTER_BIAS_MIN_PCT = 0.01
TGIF_EXPANSION_MIN_PCT = 0.01
TGIF_RETRACE_TARGET_PCT = 0.25  # midpoint of the source's 20-30% weekly-range retrace
TGIF_OBJECTIVE_SESSIONS = 20    # pre-week sessions scanned for FVG / swing objectives

WEEKLY_PROFILE_LABELS: Dict[str, str] = {
    "classic_expansion_sweep": "Classic Expansion",
    "midweek_reversal_sweep": "Midweek Reversal",
    "consolidation_reversal_sweep": "Consolidation Reversal",
    "intraweek_reversal_sweep": "Intraweek Reversal",
    "thursday_counter_sweep": "Thursday Counter",
    "tgif_setup_sweep": "TGIF Setup",
}

def _week_start_end(anchor: date) -> tuple[date, date]:
    """Return the Monday and Friday calendar window containing the anchor date."""
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=4)


def _current_week_frame(daily: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
    monday, friday = _week_start_end(as_of_date)
    start = pd.Timestamp(monday)
    end = pd.Timestamp(friday)
    return daily[(daily.index >= start) & (daily.index <= end)].sort_index()


def _week_bars(daily: pd.DataFrame, as_of_date: date) -> List[Dict[str, object]]:
    """Current-week daily bars as weekday-tagged dicts (weekday: Mon=0..Fri=4)."""
    frame = _current_week_frame(daily, as_of_date)
    bars: List[Dict[str, object]] = []
    for timestamp, row in frame.iterrows():
        bars.append(
            {
                "weekday": int(timestamp.weekday()),
                "date": timestamp,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            }
        )
    return bars


def _bars_by_weekday(bars: List[Dict[str, object]]) -> Dict[int, Dict[str, object]]:
    return {int(bar["weekday"]): bar for bar in bars}


def _body_ratio(bar: Dict[str, object]) -> float:
    span = float(bar["high"]) - float(bar["low"])
    if span <= 0:
        return 0.0
    return abs(float(bar["close"]) - float(bar["open"])) / span


def _prior_week_extremes(daily: pd.DataFrame, as_of_date: date, count: int = 2) -> List[Dict[str, float]]:
    """High/low of the completed weeks before the current week (oldest first)."""
    monday = pd.Timestamp(_week_start_end(as_of_date)[0])
    history = daily[daily.index < monday]
    if history.empty:
        return []
    weekly = (
        history.resample("W-FRI")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    return [
        {"high": float(row["High"]), "low": float(row["Low"])}
        for _, row in weekly.tail(count).iterrows()
    ]


def _pre_week_swing_extremes(daily: pd.DataFrame, as_of_date: date, sessions: int = 10) -> Optional[Dict[str, float]]:
    """Highest high / lowest low over the sessions before the current week."""
    monday = pd.Timestamp(_week_start_end(as_of_date)[0])
    history = daily[daily.index < monday]
    if history.empty:
        return None
    tail = history.tail(sessions)
    return {"high": float(tail["High"].max()), "low": float(tail["Low"].min())}


def _pre_week_objectives(
    daily: pd.DataFrame, as_of_date: date, sessions: int = TGIF_OBJECTIVE_SESSIONS
) -> Dict[str, List[float]]:
    """Higher-timeframe objectives formed before the current week.

    Swing highs/lows (equal highs/lows are swing points at the same price) and
    the FVG edges an expansion would trade into (a rally reaches the bottom of a
    bearish gap above; a sell-off reaches the top of a bullish gap below).
    """
    monday = pd.Timestamp(_week_start_end(as_of_date)[0])
    history = daily[daily.index < monday].tail(sessions)
    if len(history) < 5:
        return {"highs": [], "lows": []}
    swings = detect_swing_points(history)
    highs = [s.high for s in swings if s.is_high]
    lows = [s.low for s in swings if not s.is_high]
    for gap in find_fvgs(history, lookback=sessions):
        if gap.fvg_type == "bearish":
            highs.append(gap.gap_low)
        else:
            lows.append(gap.gap_high)
    return {"highs": highs, "lows": lows}


def _extract_weekly_profile_signal_frames(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = [
        "symbol", "profile", "state", "direction", "entry", "sl", "target", "rr",
        "track_mode", "tag", "swing_level", "triggered_level", "order_block_low",
        "order_block_high", "order_block_midpoint", "mean_threshold", "note",
    ]
    bullish = (
        results.loc[results["bullish_match"] == True]
        .reindex(columns=columns)
        .sort_values("symbol")
        .reset_index(drop=True)
        .copy()
    )
    bearish = (
        results.loc[results["bearish_match"] == True]
        .reindex(columns=columns)
        .sort_values("symbol")
        .reset_index(drop=True)
        .copy()
    )
    for frame in (bullish, bearish):
        frame["tradingview_link"] = frame["symbol"].apply(_build_tradingview_link)
    return bullish, bearish


def _extract_points_of_interest_frames(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = ["symbol", "profile", "state", "direction", "triggered_level", "type", "note"]
    bullish = (
        results.loc[results["bullish_match"] == True]
        .reindex(columns=columns)
        .sort_values("symbol")
        .reset_index(drop=True)
    )
    bearish = (
        results.loc[results["bearish_match"] == True]
        .reindex(columns=columns)
        .sort_values("symbol")
        .reset_index(drop=True)
    )
    for frame in (bullish, bearish):
        frame["tradingview_link"] = frame["symbol"].apply(_build_tradingview_link)
    return bullish, bearish


def _extract_candle_3_frames(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = [
        "symbol", "profile", "state", "direction", "triggered_level", "closure_type", "poi_type",
        "candle_3_high", "candle_3_low", "equilibrium", "note",
    ]
    bullish = results.loc[results["bullish_match"] == True].reindex(columns=columns).sort_values("symbol").reset_index(drop=True)
    bearish = results.loc[results["bearish_match"] == True].reindex(columns=columns).sort_values("symbol").reset_index(drop=True)
    for frame in (bullish, bearish):
        frame["tradingview_link"] = frame["symbol"].apply(_build_tradingview_link)
    return bullish, bearish


def _evaluate_classic_expansion(context: Dict[str, object]) -> Dict[str, object]:
    """Mon/Tue weekly extreme followed by 2-3 expansion days with candle-two closure."""
    bars = context["bars"]
    if len(bars) < 3:
        return {"bullish": False, "bearish": False, "note": f"week_bars={len(bars)}"}

    lows = [float(bar["low"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    low_idx = lows.index(min(lows))
    high_idx = highs.index(max(highs))

    def side(direction: int) -> tuple[bool, str]:
        extreme_idx = low_idx if direction > 0 else high_idx
        # Judge "Mon/Tue" by weekday, not bar position, so a Monday holiday
        # cannot promote Wednesday to an "early" extreme.
        extreme_weekday = int(bars[extreme_idx]["weekday"])
        if extreme_weekday > 1:
            return False, f"weekly_extreme_late_d{extreme_weekday + 1}"
        post = bars[extreme_idx + 1:]
        if not 1 <= len(post) <= CLASSIC_EXPANSION_MAX_EXPANSION_DAYS:
            return False, f"expansion_days={len(post)}"
        if direction > 0 and any(float(bar["low"]) <= lows[extreme_idx] for bar in post):
            return False, "weekly_low_broken"
        if direction < 0 and any(float(bar["high"]) >= highs[extreme_idx] for bar in post):
            return False, "weekly_high_broken"

        latest = bars[-1]
        prev = bars[-2]
        first_post_open = float(post[0]["open"])
        friday_slowing = int(latest["weekday"]) == 4
        if direction > 0:
            displaced = float(latest["close"]) > first_post_open
            if friday_slowing:
                closure = float(latest["close"]) > float(prev["close"])
            else:
                closure = (
                    float(latest["close"]) > float(prev["high"])
                    and float(latest["close"]) > float(latest["open"])
                    and _body_ratio(latest) >= 0.5
                )
        else:
            displaced = float(latest["close"]) < first_post_open
            if friday_slowing:
                closure = float(latest["close"]) < float(prev["close"])
            else:
                closure = (
                    float(latest["close"]) < float(prev["low"])
                    and float(latest["close"]) < float(latest["open"])
                    and _body_ratio(latest) >= 0.5
                )
        if not displaced:
            return False, "no_displacement_off_extreme"
        if not closure:
            return False, "candle_two_pending"
        tag = "friday_slowing" if friday_slowing else "candle_two_closure"
        return True, f"extreme_d{extreme_weekday + 1}_expand{len(post)}_{tag}"

    bullish, bullish_note = side(1)
    bearish, bearish_note = side(-1)
    note = bullish_note if bullish else (bearish_note if bearish else bullish_note)
    return {"bullish": bullish, "bearish": bearish, "note": note}


def _evaluate_midweek_reversal(context: Dict[str, object]) -> Dict[str, object]:
    """Early-week move reverses at a Wednesday candle-two/three closure pivot."""
    weekday_bars = context["by_weekday"]
    if not all(day in weekday_bars for day in (0, 1, 2)):
        return {"bullish": False, "bearish": False, "note": "pivot_not_formed"}

    mon = weekday_bars[0]
    tue = weekday_bars[1]

    def pivot_closure(direction: int, day: int) -> bool:
        pivot = weekday_bars.get(day)
        prev = weekday_bars.get(day - 1)
        if pivot is None or prev is None:
            return False
        if direction > 0:
            return float(pivot["close"]) > float(prev["high"]) and float(pivot["close"]) > float(pivot["open"])
        return float(pivot["close"]) < float(prev["low"]) and float(pivot["close"]) < float(pivot["open"])

    def side(direction: int) -> tuple[bool, str, Optional[int]]:
        if direction > 0:
            early_move = float(tue["close"]) < float(mon["close"]) and float(tue["low"]) < float(mon["low"])
        else:
            early_move = float(tue["close"]) > float(mon["close"]) and float(tue["high"]) > float(mon["high"])
        if not early_move:
            return False, "no_early_week_move", None
        # Wednesday is THE pivot; when it lacks a clean closure the source says
        # wait for Thursday's confirmation instead.
        pivot_day = next((day for day in (2, 3) if pivot_closure(direction, day)), None)
        if pivot_day is None:
            if 3 not in weekday_bars:
                return False, "wednesday_unclear_await_thursday", None
            return False, "reversal_not_confirmed", None
        pivot_close = float(weekday_bars[pivot_day]["close"])
        later = [bar for bar in context["bars"] if int(bar["weekday"]) > pivot_day]
        for bar in later:
            if direction > 0 and float(bar["close"]) < pivot_close:
                return False, "continuation_failed", pivot_day
            if direction < 0 and float(bar["close"]) > pivot_close:
                return False, "continuation_failed", pivot_day
        label = "pivot_wed" if pivot_day == 2 else "pivot_thu_fallback"
        return True, f"{label}_continuing" if later else f"{label}_await_continuation", pivot_day

    bullish, bullish_note, bullish_pivot = side(1)
    bearish, bearish_note, bearish_pivot = side(-1)
    note = bullish_note if bullish else (bearish_note if bearish else bullish_note)
    pivot_weekday = bullish_pivot if bullish else (bearish_pivot if bearish else None)
    return {"bullish": bullish, "bearish": bearish, "note": note, "pivot_weekday": pivot_weekday}


def _evaluate_consolidation_reversal(context: Dict[str, object]) -> Dict[str, object]:
    """Mon-Wed tight range, Thursday fake breakout closing back inside, Friday opposite."""
    weekday_bars = context["by_weekday"]
    if not all(day in weekday_bars for day in (0, 1, 2)):
        return {"bullish": False, "bearish": False, "note": "range_not_formed"}
    if 3 not in weekday_bars:
        return {"bullish": False, "bearish": False, "note": "thursday_pending"}

    base = [weekday_bars[day] for day in (0, 1, 2)]
    cons_high = max(float(bar["high"]) for bar in base)
    cons_low = min(float(bar["low"]) for bar in base)
    ref_close = float(weekday_bars[2]["close"])
    if ref_close <= 0 or cons_high <= cons_low:
        return {"bullish": False, "bearish": False, "note": "bad_data"}

    range_pct = (cons_high - cons_low) / ref_close
    drift_pct = abs(float(weekday_bars[2]["close"]) - float(weekday_bars[0]["open"])) / ref_close
    if range_pct > CONSOLIDATION_MAX_RANGE_PCT:
        return {"bullish": False, "bearish": False, "note": f"range_too_wide_{range_pct:.1%}"}
    if drift_pct > CONSOLIDATION_MAX_DRIFT_PCT:
        return {"bullish": False, "bearish": False, "note": f"drifting_not_consolidation_{drift_pct:.1%}"}

    thu = weekday_bars[3]
    fri = weekday_bars.get(4)
    fake_up = float(thu["high"]) > cons_high and float(thu["close"]) < cons_high
    fake_down = float(thu["low"]) < cons_low and float(thu["close"]) > cons_low
    if not fake_up and not fake_down:
        return {"bullish": False, "bearish": False, "note": "no_thursday_failure"}
    if fake_up and fake_down:
        # Both sides swept and closed back inside: no directional conclusion.
        return {"bullish": False, "bearish": False, "note": "thursday_swept_both_sides"}

    # Fake upside break expects downside expansion on Friday; mirror otherwise.
    direction = -1 if fake_up else 1
    if fri is None:
        matched = True
        note = "thursday_fake_break_aggressive_entry"
    elif direction < 0:
        matched = float(fri["close"]) < float(thu["close"])
        invalidated = float(fri["close"]) > cons_high
        if matched:
            note = "friday_expansion_confirmed"
        elif invalidated:
            note = "friday_invalidated"
        else:
            note = "friday_confirmation_pending"
    else:
        matched = float(fri["close"]) > float(thu["close"])
        invalidated = float(fri["close"]) < cons_low
        if matched:
            note = "friday_expansion_confirmed"
        elif invalidated:
            note = "friday_invalidated"
        else:
            note = "friday_confirmation_pending"

    return {
        "bullish": bool(direction > 0 and matched),
        "bearish": bool(direction < 0 and matched),
        "note": note,
        # Source target: the opposite side of the consolidation range.
        "target_level": cons_low if direction < 0 else cons_high,
    }


def _evaluate_intraweek_reversal(context: Dict[str, object]) -> Dict[str, object]:
    """Monday expansion away from the open, Tuesday stall, Wed/Thu reversal closure."""
    weekday_bars = context["by_weekday"]
    bars = context["bars"]
    if 0 not in weekday_bars or 1 not in weekday_bars:
        return {"bullish": False, "bearish": False, "note": "monday_pending"}

    mon = weekday_bars[0]
    tue = weekday_bars[1]

    def side(direction: int) -> tuple[bool, str]:
        if direction > 0:
            monday_expansion = float(mon["close"]) < float(mon["open"]) and _body_ratio(mon) >= 0.5
        else:
            monday_expansion = float(mon["close"]) > float(mon["open"]) and _body_ratio(mon) >= 0.5
        if not monday_expansion:
            return False, "no_monday_expansion"

        inside_bar = float(tue["high"]) <= float(mon["high"]) and float(tue["low"]) >= float(mon["low"])
        stalled = _body_ratio(tue) <= INTRAWEEK_STALL_BODY_RATIO
        if not (inside_bar or stalled):
            return False, "no_tuesday_stall"

        reversal_day = None
        closure = ""
        for candidate in (2, 3):
            pivot = weekday_bars.get(candidate)
            prev = weekday_bars.get(candidate - 1)
            if pivot is None or prev is None:
                continue
            if direction > 0 and float(pivot["close"]) > float(prev["high"]) and float(pivot["close"]) > float(pivot["open"]) and _body_ratio(pivot) >= 0.5:
                reversal_day, closure = candidate, "candle_two_closure"
                break
            if direction < 0 and float(pivot["close"]) < float(prev["low"]) and float(pivot["close"]) < float(pivot["open"]) and _body_ratio(pivot) >= 0.5:
                reversal_day, closure = candidate, "candle_two_closure"
                break
            # Candle 3 closure: the prior candle (candle 2) failed without
            # sweeping the one before it, and this candle closes over its body.
            before = weekday_bars.get(candidate - 2)
            if before is not None:
                p_open, p_close = float(prev["open"]), float(prev["close"])
                if (
                    direction > 0 and p_close <= p_open and float(prev["low"]) >= float(before["low"])
                    and float(pivot["close"]) > max(p_open, p_close) and float(pivot["close"]) > float(pivot["open"])
                ):
                    reversal_day, closure = candidate, "candle_three_closure"
                    break
                if (
                    direction < 0 and p_close >= p_open and float(prev["high"]) <= float(before["high"])
                    and float(pivot["close"]) < min(p_open, p_close) and float(pivot["close"]) < float(pivot["open"])
                ):
                    reversal_day, closure = candidate, "candle_three_closure"
                    break
        if reversal_day is None:
            return False, "reversal_not_confirmed"

        pivot = weekday_bars[reversal_day]
        later = [bar for bar in bars if int(bar["weekday"]) > reversal_day]
        for bar in later:
            if direction > 0 and float(bar["close"]) < float(pivot["low"]):
                return False, "continuation_violated"
            if direction < 0 and float(bar["close"]) > float(pivot["high"]):
                return False, "continuation_violated"
        return True, f"{closure}_d{reversal_day + 1}"

    bullish, bullish_note = side(1)
    bearish, bearish_note = side(-1)
    note = bullish_note if bullish else (bearish_note if bearish else bullish_note)
    return {"bullish": bullish, "bearish": bearish, "note": note}


def _evaluate_thursday_counter(context: Dict[str, object]) -> Dict[str, object]:
    """Established Mon-Wed bias countered Thursday via a failed liquidity grab."""
    weekday_bars = context["by_weekday"]
    if not all(day in weekday_bars for day in (0, 1, 2)):
        return {"bullish": False, "bearish": False, "note": "bias_pending"}
    if 3 not in weekday_bars:
        return {"bullish": False, "bearish": False, "note": "thursday_pending"}

    mon = weekday_bars[0]
    tue = weekday_bars[1]
    wed = weekday_bars[2]
    thu = weekday_bars[3]
    fri = weekday_bars.get(4)

    ref_close = float(wed["close"])
    if ref_close <= 0:
        return {"bullish": False, "bearish": False, "note": "bad_data"}
    net_pct = (float(wed["close"]) - float(mon["open"])) / ref_close
    bias_up = net_pct >= THURSDAY_COUNTER_BIAS_MIN_PCT and float(wed["high"]) >= max(float(mon["high"]), float(tue["high"]))
    bias_down = net_pct <= -THURSDAY_COUNTER_BIAS_MIN_PCT and float(wed["low"]) <= min(float(mon["low"]), float(tue["low"]))
    if not bias_up and not bias_down:
        return {"bullish": False, "bearish": False, "note": f"no_established_bias_net_{net_pct:.1%}"}

    if bias_up:
        grab_failure = float(thu["high"]) > float(wed["high"]) and float(thu["close"]) < float(wed["close"])
        direction = -1  # counter-move against an up week is bearish
    else:
        grab_failure = float(thu["low"]) < float(wed["low"]) and float(thu["close"]) > float(wed["close"])
        direction = 1  # counter-move against a down week is bullish
    if not grab_failure:
        return {"bullish": False, "bearish": False, "note": "no_thursday_grab_failure"}

    # Source: the weekly open is the most common target of the counter move.
    weekly_open = float(mon["open"])
    if fri is None:
        return {
            "bullish": bool(direction > 0),
            "bearish": bool(direction < 0),
            "note": "thursday_counter_set_friday_pending",
            "target_level": weekly_open,
        }

    if direction < 0:
        confirmed = float(fri["close"]) < float(thu["close"])
    else:
        confirmed = float(fri["close"]) > float(thu["close"])
    note = "friday_continuation_confirmed" if confirmed else "friday_continuation_failed"
    return {
        "bullish": bool(direction > 0 and confirmed),
        "bearish": bool(direction < 0 and confirmed),
        "note": note,
        "target_level": weekly_open,
    }


def _evaluate_tgif(context: Dict[str, object]) -> Dict[str, object]:
    """Completed expansion week with HTF objective reached; Friday retraces into range."""
    bars = context["bars"]
    weekday_bars = context["by_weekday"]
    if not (0 in weekday_bars and 1 in weekday_bars):
        return {"bullish": False, "bearish": False, "note": "week_forming"}
    if 3 not in weekday_bars:
        return {"bullish": False, "bearish": False, "note": "expansion_leg_pending"}

    thru_thu = [bar for bar in bars if int(bar["weekday"]) <= 3]
    week_low = min(float(bar["low"]) for bar in thru_thu)
    week_high = max(float(bar["high"]) for bar in thru_thu)
    low_idx = [float(bar["low"]) for bar in thru_thu].index(week_low)
    high_idx = [float(bar["high"]) for bar in thru_thu].index(week_high)
    thu = weekday_bars[3]
    fri = weekday_bars.get(4)
    wed = weekday_bars.get(2)

    prior_weeks = context["prior_weeks"]
    swing = context["swing"]
    objectives = context.get("objectives") or {}
    refs_high: List[float] = list(objectives.get("highs", []))
    refs_low: List[float] = list(objectives.get("lows", []))
    if prior_weeks:
        refs_high.append(float(prior_weeks[-1]["high"]))
        refs_low.append(float(prior_weeks[-1]["low"]))
    if swing:
        refs_high.append(float(swing["high"]))
        refs_low.append(float(swing["low"]))
    # An objective must sit beyond where the week started; a level already
    # below (above) the weekly open is not something the expansion reached for.
    week_open = float(weekday_bars[0]["open"])
    refs_high = [level for level in refs_high if level > week_open]
    refs_low = [level for level in refs_low if level < week_open]

    def fade_of(direction: int) -> tuple[bool, str]:
        """direction=+1 fades a completed bullish expansion week (bearish signal)."""
        if week_low <= 0 or week_high <= 0:
            return False, "bad_data"
        leg = thu
        stage = "friday_retrace_into_range"
        faded = False
        leg_bars = thru_thu
        if fri is None:
            if wed is None:
                return False, "expansion_leg_pending"
            leg = wed
            stage = "thursday_reversed_friday_continuation"
            leg_bars = [bar for bar in thru_thu if int(bar["weekday"]) <= 2]
            if direction > 0:
                faded = float(thu["close"]) < float(leg["close"]) and float(thu["high"]) <= float(leg["high"])
            else:
                faded = float(thu["close"]) > float(leg["close"]) and float(thu["low"]) >= float(leg["low"])
        elif direction > 0:
            faded = float(fri["high"]) <= float(thu["high"]) and float(fri["close"]) < float(thu["close"])
        else:
            faded = float(fri["low"]) >= float(thu["low"]) and float(fri["close"]) > float(thu["close"])

        # Weekday-based (not bar position) so a holiday cannot shift the days.
        if direction > 0:
            extreme_early = int(thru_thu[low_idx]["weekday"]) <= 1
            leg_closes = [float(bar["close"]) for bar in leg_bars]
            extreme_close_idx = leg_closes.index(max(leg_closes))
            leg_completed_late = int(leg_bars[extreme_close_idx]["weekday"]) >= 2
            expansion = week_low > 0 and (max(leg_closes) - week_low) / week_low >= TGIF_EXPANSION_MIN_PCT
            leg_objective = max(float(bar["high"]) for bar in leg_bars)
            objective = any(level <= leg_objective for level in refs_high)
        else:
            extreme_early = int(thru_thu[high_idx]["weekday"]) <= 1
            leg_closes = [float(bar["close"]) for bar in leg_bars]
            extreme_close_idx = leg_closes.index(min(leg_closes))
            leg_completed_late = int(leg_bars[extreme_close_idx]["weekday"]) >= 2
            expansion = week_high > 0 and (week_high - min(leg_closes)) / week_high >= TGIF_EXPANSION_MIN_PCT
            leg_objective = min(float(bar["low"]) for bar in leg_bars)
            objective = any(level >= leg_objective for level in refs_low)

        if not extreme_early:
            return False, "weekly_extreme_made_late"
        if not (leg_completed_late and expansion):
            return False, "no_completed_expansion_to_objective"
        if not objective:
            # Critical rule: no expansion + no objective reached = no valid TGIF setup.
            return False, "objective_not_reached_no_tgif"
        if not faded:
            return False, "retrace_not_started"
        return True, stage

    fade_up_matched, fade_up_note = fade_of(1)       # bullish week -> bearish signal
    fade_down_matched, fade_down_note = fade_of(-1)  # bearish week -> bullish signal
    bullish = fade_down_matched
    bearish = fade_up_matched
    note = fade_down_note if bullish else (fade_up_note if bearish else fade_up_note)
    # Source target: a 20-30% retracement of the weekly range; use its midpoint.
    week_range = week_high - week_low
    target_level = None
    if bearish:
        target_level = week_high - TGIF_RETRACE_TARGET_PCT * week_range
    elif bullish:
        target_level = week_low + TGIF_RETRACE_TARGET_PCT * week_range
    return {"bullish": bullish, "bearish": bearish, "note": note, "target_level": target_level}


def _daily_atr(daily: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Average True Range of the daily series (volatility context for SL sizing)."""
    if daily is None or len(daily) < 2:
        return None
    high = daily["High"]
    low = daily["Low"]
    close = daily["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else None


def _track_mode_for(symbol: str) -> str:
    """'live' if the exchange can be tracked intraday (TradingView near-24/5),
    otherwise 'eod_confirm' (NSE daily bars are only published after the close)."""
    try:
        source = _source_for_symbol(symbol)
        if source != "NSE":
            return "live"
    except Exception:
        pass
    return "eod_confirm"


def _signal_state(outcome: Dict[str, object], context: Optional[Dict[str, object]] = None) -> str:
    """Lifecycle state derived from the evaluator outcome (see setup-lifecycle
    trackers: forming -> armed -> triggered -> invalidated -> expired).

    A setup that is still only pending/forming once the Friday bar is in the
    data (i.e. the trigger window has closed without a fill) is marked
    EXPIRED rather than left as ARMED, so it no longer looks live.
    """
    if outcome.get("bullish") or outcome.get("bearish"):
        return "triggered"
    note = str(outcome.get("note") or "").lower()
    invalid_tokens = (
        "broken", "failed", "invalidated", "violated", "no_", "not_",
        "too_wide", "drifting", "bad_data", "late", "objective_not_reached",
        "retrace_not_started", "range_",
    )
    if any(token in note for token in invalid_tokens):
        return "invalidated"
    latest = (context or {}).get("latest")
    if latest is not None and int(latest.get("weekday", -1)) == 4:
        return "expired"
    return "armed"


def _profile_levels(profile_key: str, context: Dict[str, object]) -> Dict[str, Optional[float]]:
    """Structure-based invalidation extremes + entry reference per profile.

    Returns the bullish-invalidation low (`sl_bull`), the bearish-invalidation
    high (`sl_bear`), and the trigger/entry reference close. These are the
    exact extremes the evaluator already uses to invalidate, so the SL sits
    just beyond the liquidity sweep / extreme (ICT convention), not at a fixed
    pip distance.
    """
    bars = context.get("bars") or []
    by = context.get("by_weekday") or {}
    if not bars:
        return {"sl_bull": None, "sl_bear": None, "entry_ref": None}
    latest = bars[-1]
    entry_ref = float(latest["close"])
    lows = [float(b["low"]) for b in bars]
    highs = [float(b["high"]) for b in bars]

    if profile_key == "classic_expansion_sweep":
        sl_bull, sl_bear = min(lows), max(highs)
    elif profile_key == "midweek_reversal_sweep":
        # Pivot is Wednesday, or Thursday under the source's fallback.
        pivot_day = int(context.get("pivot_weekday") or 2)
        pivot = by.get(pivot_day)
        if pivot is None:
            return {"sl_bull": None, "sl_bear": None, "entry_ref": entry_ref}
        span = [by[day] for day in range(2, pivot_day + 1) if day in by]
        sl_bull = min(float(b["low"]) for b in span)
        sl_bear = max(float(b["high"]) for b in span)
        entry_ref = float(pivot["close"])
    elif profile_key == "consolidation_reversal_sweep":
        thu = by.get(3)
        if thu is None:
            return {"sl_bull": None, "sl_bear": None, "entry_ref": entry_ref}
        sl_bull, sl_bear = float(thu["low"]), float(thu["high"])
        entry_ref = float(thu["close"])
    elif profile_key == "intraweek_reversal_sweep":
        mon = by.get(0)
        if mon is None:
            return {"sl_bull": None, "sl_bear": None, "entry_ref": entry_ref}
        sl_bull, sl_bear = float(mon["low"]), float(mon["high"])
    elif profile_key in ("thursday_counter_sweep", "tgif_setup_sweep"):
        thu = by.get(3)
        if thu is None:
            return {"sl_bull": None, "sl_bear": None, "entry_ref": entry_ref}
        sl_bull, sl_bear = float(thu["low"]), float(thu["high"])
        entry_ref = float(thu["close"])
    else:
        sl_bull, sl_bear = min(lows), max(highs)
    return {"sl_bull": sl_bull, "sl_bear": sl_bear, "entry_ref": entry_ref}


def _build_trade_plan(outcome: Dict[str, object], context: Dict[str, object], atr: Optional[float]) -> Dict[str, object]:
    """Turn the evaluator outcome + structural levels into an actionable plan.

    SL = invalidation extreme +/- a >=1xATR buffer; target = the profile's own
    source target (``outcome["target_level"]``, e.g. weekly open, range side,
    retrace level) when it lies beyond entry, else the opposite prior-week or
    swing extreme (the liquidity pool the profile expands toward). R:R is
    reported so sub-minimum setups can be filtered by the caller/UI.
    """
    direction = 1 if outcome.get("bullish") else (-1 if outcome.get("bearish") else 0)
    sl_level = outcome.get("sl_level")
    entry_ref = outcome.get("entry_ref")
    if direction == 0 or sl_level is None or entry_ref is None:
        return {"direction": 0, "entry": None, "sl": None, "target": None, "rr": None, "atr": atr}
    buffer = (atr if (atr and atr > 0) else abs(float(entry_ref)) * 0.005)
    if direction > 0:
        sl = float(sl_level) - buffer
        entry = float(entry_ref)
    else:
        sl = float(sl_level) + buffer
        entry = float(entry_ref)

    pw = context.get("prior_weeks") or []
    swing = context.get("swing") or {}
    # Measured-move fallback = entry +/- 2x risk, i.e. a clean 1:2 R:R when no
    # valid liquidity pool sits beyond the entry.
    measured = (entry + 2.0 * (entry - sl)) if direction > 0 else (entry - 2.0 * (sl - entry))
    override = outcome.get("target_level")
    if override is not None and (
        (direction > 0 and float(override) > entry) or (direction < 0 and float(override) < entry)
    ):
        target = float(override)
    elif direction > 0:
        cands = [float(p["high"]) for p in pw] + ([float(swing["high"])] if swing else [])
        # Only count pools that are genuinely above entry; a pool below entry is
        # behind price and would put the target behind the entry (broken R:R).
        valid = [c for c in cands if c > entry]
        pool = max(valid) if valid else measured
        target = max(pool, measured)
    else:
        cands = [float(p["low"]) for p in pw] + ([float(swing["low"])] if swing else [])
        valid = [c for c in cands if c < entry]
        pool = min(valid) if valid else measured
        target = min(pool, measured)

    denom = abs(entry - sl)
    rr = (abs(target - entry) / denom) if denom > 0 else None
    return {
        "direction": direction,
        "entry": round(entry, 4),
        "sl": round(sl, 4),
        "target": round(target, 4),
        "rr": (round(rr, 2) if rr is not None else None),
        "atr": (round(atr, 4) if atr is not None else None),
    }


WEEKLY_PROFILE_EVALUATORS: Dict[str, Callable[[Dict[str, object]], Dict[str, object]]] = {
    "classic_expansion_sweep": _evaluate_classic_expansion,
    "midweek_reversal_sweep": _evaluate_midweek_reversal,
    "consolidation_reversal_sweep": _evaluate_consolidation_reversal,
    "intraweek_reversal_sweep": _evaluate_intraweek_reversal,
    "thursday_counter_sweep": _evaluate_thursday_counter,
    "tgif_setup_sweep": _evaluate_tgif,
}


def _trim_in_progress_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Drop the final daily bar when it is today's bar and not yet final.

    NSE bars are only final after the bhavcopy publishes (~17:00 IST); forex/
    commodity bars are deferred to the next day. This stops weekly-profile
    signals from repainting as an in-progress bar develops during the session,
    while leaving already-completed bars (and live-tracked symbols) untouched.
    """
    if daily is None or len(daily) < 2:
        return daily
    try:
        import ict_scanner  # type: ignore

        last_date = daily.index[-1]
        last_day = getattr(last_date, "date", lambda: last_date)()
        if (
            isinstance(last_day, date)
            and last_day == datetime.now(_IST).date()
            and not ict_scanner.is_daily_bar_ready(ict_scanner.Session.NSE)
        ):
            return daily.iloc[:-1]
    except Exception:
        pass
    return daily


def _weekly_context(daily: pd.DataFrame, as_of_date: date) -> Optional[Dict[str, object]]:
    """Point-in-time evaluator context for the week containing ``as_of_date``."""
    bars = _week_bars(daily, as_of_date)
    if not bars:
        return None
    return {
        "bars": bars,
        "by_weekday": _bars_by_weekday(bars),
        "latest": bars[-1],
        "prior_weeks": _prior_week_extremes(daily, as_of_date),
        "swing": _pre_week_swing_extremes(daily, as_of_date),
        "objectives": _pre_week_objectives(daily, as_of_date),
    }


def _first_weekly_trigger(
    evaluator: Callable[[Dict[str, object]], Dict[str, object]],
    daily: pd.DataFrame,
    bars: List[Dict[str, object]],
    direction: int,
) -> Optional[date]:
    """Earliest earlier session this week on which the profile already matched
    in ``direction`` (evaluated on data truncated to that session), else None."""
    key = "bullish" if direction > 0 else "bearish"
    for bar in bars[:-1]:
        stamp = pd.Timestamp(bar["date"])
        context = _weekly_context(daily[daily.index <= stamp], stamp.date())
        if context is not None and evaluator(context).get(key):
            return stamp.date()
    return None


def run_weekly_profile(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
    profile_key: str = "classic_expansion_sweep",
) -> StrategyExecution:
    """Evaluate one weekly profile across all symbols for the week of as_of_date."""
    _ = print_values
    label = WEEKLY_PROFILE_LABELS[profile_key]
    evaluator = WEEKLY_PROFILE_EVALUATORS[profile_key]
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bullish_match": False,
            "bearish_match": False,
            "final_signal": False,
            "status": "pending",
            "profile": label,
            "note": "",
            "state": "armed",
            "direction": 0,
            "entry": None,
            "sl": None,
            "target": None,
            "rr": None,
            "atr": None,
            "track_mode": "",
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(
                symbol=symbol_upper, as_of_date=as_of_date, max_lookback_days=WEEKLY_PROFILE_LOOKBACK_DAYS
            )
        else:
            daily = daily_map.get(symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"]))

        # Repainting guard: for EOD-confirmed (NSE) symbols, evaluate only on the
        # last *completed* session (see _trim_in_progress_daily). Live-tracked
        # (forex/commodity) symbols keep their in-progress bar by design.
        if _track_mode_for(symbol_upper) == "eod_confirm":
            daily = _trim_in_progress_daily(daily)

        if daily is None or daily.empty or len(daily) < 3:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol_upper}: SKIPPED (no_data)")
            continue
        if _is_stale(symbol_upper, daily, as_of_date):
            results.at[idx, "status"] = "stale"
            continue

        context = _weekly_context(daily, as_of_date)
        if context is None:
            results.at[idx, "status"] = "no_week_data"
            if verbose:
                print(f"{symbol_upper}: SKIPPED (no_week_data)")
            continue
        bars = context["bars"]

        outcome = evaluator(context)
        bullish = bool(outcome["bullish"])
        bearish = bool(outcome["bearish"])
        context["pivot_weekday"] = outcome.get("pivot_weekday")

        levels = _profile_levels(profile_key, context)
        direction = 1 if bullish else (-1 if bearish else 0)
        outcome["sl_level"] = levels["sl_bull"] if direction > 0 else (levels["sl_bear"] if direction < 0 else None)
        outcome["entry_ref"] = levels["entry_ref"]
        plan = _build_trade_plan(outcome, context, _daily_atr(daily))
        state = _signal_state(outcome, context)
        note = str(outcome["note"])

        # A profile fires once per week: only the first session it matches
        # (re-evaluated point-in-time on the earlier sessions of this week).
        # A first match at Friday's close is a label only — the weekly profile
        # has played out and the next entry would fall in a different week.
        fires = direction != 0
        if fires:
            first_date = _first_weekly_trigger(evaluator, daily, bars, direction)
            if first_date is not None:
                fires = False
                note = f"{note} | first signalled {first_date.isoformat()}"
            elif int(bars[-1]["weekday"]) == 4:
                fires = False
                state = "expired"
                note = f"{note} | friday_close_label_only"

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = fires
        results.at[idx, "status"] = "complete"
        results.at[idx, "note"] = note
        results.at[idx, "state"] = state
        results.at[idx, "direction"] = direction
        results.at[idx, "entry"] = plan["entry"]
        results.at[idx, "sl"] = plan["sl"]
        results.at[idx, "target"] = plan["target"]
        results.at[idx, "rr"] = plan["rr"]
        results.at[idx, "atr"] = plan["atr"]
        results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)
        if fires:
            # Intraday confirmation window: the remaining sessions of this week.
            latest = bars[-1]
            _set_ltf_setup(
                results, idx, direction, float(latest["high"]), float(latest["low"]),
                latest["date"], _week_start_end(as_of_date)[1],
                invalidation=outcome["sl_level"],
            )

        if verbose:
            print(f"{symbol_upper}: {label} bullish={bullish}, bearish={bearish}, note={note}")

    bullish_frame, bearish_frame = _extract_weekly_profile_signal_frames(results)
    return StrategyExecution(name=profile_key, results=results, bullish=bullish_frame, bearish=bearish_frame)


def run_classic_expansion(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="classic_expansion_sweep")


def run_midweek_reversal(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="midweek_reversal_sweep")


def run_consolidation_reversal(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="consolidation_reversal_sweep")


def run_intraweek_reversal(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="intraweek_reversal_sweep")


def run_thursday_counter(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="thursday_counter_sweep")


def run_tgif_setup(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    return run_weekly_profile(symbols, as_of_date, verbose, print_values, daily_map, profile_key="tgif_setup_sweep")


_WEEKLY_PROFILE_RUNNERS: Dict[str, Callable[..., StrategyExecution]] = {
    "classic_expansion_sweep": run_classic_expansion,
    "midweek_reversal_sweep": run_midweek_reversal,
    "consolidation_reversal_sweep": run_consolidation_reversal,
    "intraweek_reversal_sweep": run_intraweek_reversal,
    "thursday_counter_sweep": run_thursday_counter,
    "tgif_setup_sweep": run_tgif_setup,
}


def strategy_registry() -> Dict[str, StrategySpec]:
    registry = {
        "inside_bar_pattern_daily_sweep": StrategySpec(
            name="inside_bar_pattern_daily_sweep",
            runner=run_inside_bar_daily_sweep,
        ),
        "ema5_sweep": StrategySpec(
            name="ema5_sweep",
            runner=run_ema5_sweep,
        ),
        "daily_bias_invalidation": StrategySpec(
            name="daily_bias_invalidation",
            runner=run_daily_bias_invalidation,
        ),
        "protected_swings": StrategySpec(
            name="protected_swings",
            runner=run_protected_swings,
        ),
        "points_of_interest": StrategySpec(
            name="points_of_interest",
            runner=run_points_of_interest,
        ),
        "candle_3_closure": StrategySpec(
            name="candle_3_closure",
            runner=run_candle_3_closure,
        ),
        "propulsion_blocks": StrategySpec(
            name="propulsion_blocks",
            runner=run_propulsion_blocks,
        ),
    }

    if WEEKLY_PROFILES_ENABLED:
        for profile_name, profile_runner in _WEEKLY_PROFILE_RUNNERS.items():
            if WEEKLY_PROFILE_FLAGS.get(profile_name, False):
                registry[profile_name] = StrategySpec(name=profile_name, runner=profile_runner)

    return registry


def load_default_symbols() -> List[str]:
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/csv,text/plain,*/*",
    }
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        content = response.read().decode("utf-8", errors="ignore")

    raw = pd.read_csv(
        StringIO(content),
        skipinitialspace=True,
        engine="python",
        on_bad_lines="skip",
    )
    raw.columns = [str(c).strip().upper() for c in raw.columns]

    if "SYMBOL" not in raw.columns:
        raise RuntimeError("Unable to find SYMBOL column in NSE F&O market lot CSV.")

    symbols = raw["SYMBOL"].astype(str).str.strip().str.upper()
    symbols = symbols[symbols.str.match(r"^[A-Z0-9&\-]+$")]
    symbols = symbols[symbols != "SYMBOL"]

    index_symbols = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    symbols = symbols[~symbols.isin(index_symbols)]

    unique_sorted = sorted(set(symbols.tolist()))
    if not unique_sorted:
        raise RuntimeError("Online NSE futures stock list returned no valid symbols.")

    return unique_sorted


def run_strategies(
    strategy_names: Sequence[str],
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    parallel: bool = True,
    timeframe: str = "daily",
    include_context: bool = True,
    include_bias: bool = True,
) -> List[StrategyExecution]:
    """``include_context=False`` skips the display-only ``ctx_*`` columns;
    ``include_bias=False`` skips only the M/W/D bias (and its longer history)."""
    normalized_timeframe = str(timeframe).strip().lower()
    if any(name in strategy_names for name in ("protected_swings", "points_of_interest", "candle_3_closure", "propulsion_blocks")) and normalized_timeframe not in PROTECTED_SWING_TIMEFRAMES:
        supported = ", ".join(PROTECTED_SWING_TIMEFRAMES)
        raise ValueError(f"Unsupported protected swing timeframe '{timeframe}'. Use: {supported}")

    registry = strategy_registry()
    for name in strategy_names:
        if name not in registry:
            valid = ", ".join(sorted(registry.keys()))
            raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")

    lookback_by_strategy = {
        "inside_bar_pattern_daily_sweep": 160,
        "ema5_sweep": 40,
        "daily_bias_invalidation": 600,
        "classic_expansion_sweep": 60,
        "midweek_reversal_sweep": 60,
        "consolidation_reversal_sweep": 60,
        "intraweek_reversal_sweep": 60,
        "thursday_counter_sweep": 60,
         "tgif_setup_sweep": 60,
         "protected_swings": PROTECTED_SWINGS_LOOKBACK_DAYS,
        "points_of_interest": PROTECTED_SWINGS_LOOKBACK_DAYS,
        "candle_3_closure": PROTECTED_SWINGS_LOOKBACK_DAYS,
        "propulsion_blocks": _PB_LOOKBACK,
     }
    symbols = _CancellableSymbols(symbols)
    max_lookback = max(lookback_by_strategy.get(name, 60) for name in strategy_names) if strategy_names else 60
    if include_context and include_bias:
        max_lookback = max(max_lookback, CONTEXT_LOOKBACK_DAYS)
    daily_map = _build_daily_map_for_symbols(symbols=symbols, as_of_date=as_of_date, max_lookback_days=max_lookback)

    if parallel and len(strategy_names) > 1:
        results_by_name: Dict[str, StrategyExecution] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(strategy_names))) as executor:
            future_to_name = {}
            for name in strategy_names:
                runner_kwargs = {
                    "symbols": symbols,
                    "as_of_date": as_of_date,
                    "verbose": verbose,
                    "print_values": print_values,
                    "daily_map": daily_map,
                }
                if name in {"protected_swings", "points_of_interest", "candle_3_closure", "propulsion_blocks"}:
                    runner_kwargs["timeframe"] = normalized_timeframe
                future_to_name[executor.submit(registry[name].runner, **runner_kwargs)] = name

            for future in as_completed(future_to_name):
                name = future_to_name[future]
                results_by_name[name] = future.result()

        ordered = [results_by_name[name] for name in strategy_names]
        return _with_daily_context(ordered, daily_map, symbols, as_of_date, include_bias) if include_context else ordered

    executions: List[StrategyExecution] = []
    for name in strategy_names:
        runner_kwargs = {
            "symbols": symbols,
            "as_of_date": as_of_date,
            "verbose": verbose,
            "print_values": print_values,
            "daily_map": daily_map,
        }
        if name in {"protected_swings", "points_of_interest", "candle_3_closure", "propulsion_blocks"}:
            runner_kwargs["timeframe"] = normalized_timeframe
        execution = registry[name].runner(**runner_kwargs)
        executions.append(execution)

    return _with_daily_context(executions, daily_map, symbols, as_of_date, include_bias) if include_context else executions


# Monthly bias needs M-1 vs M-2 with a full bucket before M-2 (up to ~4 months).
CONTEXT_LOOKBACK_DAYS = 130


def _with_daily_context(
    executions: List[StrategyExecution],
    daily_map: Dict[str, pd.DataFrame],
    symbols: Sequence[str],
    as_of_date: date,
    include_bias: bool = True,
) -> List[StrategyExecution]:
    """Append display-only ``ctx_*`` columns (see daily_context.py); never gates a signal."""
    contexts: Dict[str, Dict[str, object]] = {}
    for symbol in symbols:
        symbol_upper = str(symbol).strip().upper()
        if symbol_upper in contexts:
            continue  # once per symbol; every row/strategy for it shares this
        try:
            daily = daily_map.get(symbol_upper)
            if daily is None or daily.empty:
                continue
            if _track_mode_for(symbol_upper) == "eod_confirm":
                daily = _trim_in_progress_daily(daily)
            if _daily_frame_stale(symbol_upper, daily, as_of_date, "daily"):
                continue
            contexts[symbol_upper] = compute_daily_context(daily, include_bias)
        except Exception as exc:
            log.warning("Daily context failed for %s: %s", symbol_upper, exc)
    if not contexts:
        return executions
    return [
        StrategyExecution(
            name=execution.name,
            results=annotate_frame(execution.results, contexts, None),
            bullish=annotate_frame(execution.bullish, contexts, 1),
            bearish=annotate_frame(execution.bearish, contexts, -1),
        )
        for execution in executions
    ]
