from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class CandleSet:
    weekly: pd.DataFrame
    daily: pd.DataFrame


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


def _rows_to_daily_df(rows: list[dict]) -> pd.DataFrame:
    """Convert stored/fetched OHLC rows into the daily frame the evaluators use."""
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    frame = pd.DataFrame(rows)
    frame["Date"] = pd.to_datetime(frame.get("date"), errors="coerce")
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
    * Live (today) -> the provider, for the in-progress session, unless the
      exchange has already closed for the day. Once closed, the final bar is read
      from SQLite (it should have been synced after the close). Close times are
      exchange-aware: NSE 15:30 IST vs the near-24/5 commodity/forex session.
    """
    md = _get_md_service()
    source = _source_for_symbol(symbol)
    today = date.today()
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
        market_open = False
        if session is not None:
            try:
                import ict_scanner  # type: ignore

                market_open = bool(ict_scanner.is_market_open(session))
            except Exception:
                market_open = False
        if market_open:
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
    today = date.today()
    start = as_of_date - timedelta(days=max(int(max_lookback_days), 1))
    hist_end = as_of_date if as_of_date < today else today - timedelta(days=1)

    # Group symbols by upstream source so the historical window can be fetched
    # in one batched query per source instead of one round-trip per symbol.
    by_source: Dict[str, list[str]] = {}
    for sym in symbols_upper:
        by_source.setdefault(_source_for_symbol(sym), []).append(sym)

    hist_by_symbol: Dict[str, list[dict]] = {}
    if hist_end >= start:
        for source, syms in by_source.items():
            try:
                hist_by_symbol.update(
                    md.database.query_ohlc_multi(source, syms, start, hist_end)
                )
            except Exception as exc:
                log.warning("Batch OHLC read failed for %s: %s", source, exc)

    for sym in symbols_upper:
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


def _build_candles_from_daily(daily: pd.DataFrame) -> Optional[CandleSet]:
    if daily.empty:
        return None

    weekly = (
        daily.resample("W-FRI")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
        .dropna(subset=["Open", "High", "Low", "Close"])
    )

    if len(weekly) < 3 or len(daily) < 3:
        return None

    return CandleSet(weekly=weekly, daily=daily)


def _fetch_candles_weekly_daily(symbol: str, as_of_date: date, max_lookback_days: int = 420) -> Optional[CandleSet]:
    daily = _fetch_daily_from_bhavcopy(symbol=symbol, as_of_date=as_of_date, max_lookback_days=max_lookback_days)
    return _build_candles_from_daily(daily)


def _extract_weekly_daily_points(candles: CandleSet) -> Dict[str, float]:
    w = candles.weekly
    d = candles.daily
    w_curr = w.iloc[-1]
    w1 = w.iloc[-2]
    w2 = w.iloc[-3]
    d1 = d.iloc[-1]
    d2 = d.iloc[-2]

    return {
        "w1_high": float(w1["High"]),
        "w1_low": float(w1["Low"]),
        "w1_close": float(w1["Close"]),
        "w2_high": float(w2["High"]),
        "w2_low": float(w2["Low"]),
        "weekly_high": float(w_curr["High"]),
        "weekly_low": float(w_curr["Low"]),
        "d1_high": float(d1["High"]),
        "d1_low": float(d1["Low"]),
        "d1_close": float(d1["Close"]),
        "d2_high": float(d2["High"]),
        "d2_low": float(d2["Low"]),
        "d2_close": float(d2["Close"]),
    }


def _weekly_pattern_1(values: Dict[str, float]) -> bool:
    return (
        values["w1_high"] > values["w2_high"]
        and values["w1_close"] < values["w2_high"]
        and values["w1_low"] > values["w2_low"]
        and values["weekly_low"] > values["w1_low"]
        and values["d1_low"] < values["d2_low"]
        and values["d1_close"] > values["d2_low"]
        and values["d1_high"] < values["d2_high"]
    )


def _weekly_pattern_2(values: Dict[str, float]) -> bool:
    return (
        values["w1_low"] < values["w2_low"]
        and values["w1_close"] > values["w2_low"]
        and values["w1_high"] < values["w2_high"]
        and values["weekly_high"] < values["w1_high"]
        and values["d1_high"] > values["d2_high"]
        and values["d1_close"] < values["d2_close"]
        and values["d1_low"] > values["d2_low"]
    )


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


def _daily_fvg_sweep_points(daily: pd.DataFrame) -> Dict[str, float]:
    curr = daily.iloc[-1]
    d1 = daily.iloc[-2]
    d2 = daily.iloc[-3]
    d3 = daily.iloc[-4]

    return {
        "curr_open": float(curr["Open"]),
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
        "d3_open": float(d3["Open"]),
        "d3_high": float(d3["High"]),
        "d3_low": float(d3["Low"]),
        "d3_close": float(d3["Close"]),
    }


def _daily_fvg_sweep_bullish(v: Dict[str, float]) -> bool:
    return (
        (v["d3_low"] - v["d1_high"]) > (v["curr_close"] * 0.01)
        and v["d3_low"] > v["d2_low"]
        and v["d2_low"] > v["d1_low"]
        and v["d2_high"] < v["d3_high"]
        and v["d1_high"] < v["d2_high"]
        and v["curr_high"] > v["d1_high"]
        and v["curr_close"] < v["d1_high"]
        and v["curr_low"] > v["d1_low"]
    )


def _daily_fvg_sweep_bearish(v: Dict[str, float]) -> bool:
    return (
        (v["d3_high"] - v["d1_low"]) < (v["curr_close"] * 0.01)
        and v["d3_high"] < v["d2_high"]
        and v["d3_high"] < v["d1_low"]
        and v["d2_high"] < v["d1_high"]
        and v["d2_low"] > v["d3_low"]
        and v["d1_low"] > v["d2_low"]
        and v["curr_low"] < v["d1_low"]
        and v["curr_close"] > v["d1_low"]
        and v["curr_high"] < v["d1_high"]
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
    bullish = (
        results.loc[results["bullish_match"] == True, ["symbol"]]
        .sort_values("symbol")
        .reset_index(drop=True)
    )
    bearish = (
        results.loc[results["bearish_match"] == True, ["symbol"]]
        .sort_values("symbol")
        .reset_index(drop=True)
    )

    for frame in (bullish, bearish):
        frame["tradingview_link"] = frame["symbol"].apply(_build_tradingview_link)

    return bullish, bearish


def run_weekly_vs_daily(
    symbols: Sequence[str],
    as_of_date: date,
    verbose: bool = False,
    print_values: bool = False,
    daily_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> StrategyExecution:
    results = pd.DataFrame(
        {
            "symbol": list(symbols),
            "bearish_match": False,
            "bullish_match": False,
            "final_signal": False,
            "status": "pending",
        }
    )

    for idx, symbol in enumerate(symbols):
        symbol_upper = str(symbol).upper()
        if daily_map is None:
            daily = _fetch_daily_from_bhavcopy(symbol=symbol_upper, as_of_date=as_of_date, max_lookback_days=420)
        else:
            daily = daily_map.get(symbol_upper, pd.DataFrame(columns=["Open", "High", "Low", "Close"]))

        candles = _build_candles_from_daily(daily)
        if candles is None:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol_upper}: SKIPPED (no_data)")
            continue

        values = _extract_weekly_daily_points(candles)

        if print_values:
            print(f"\n{symbol_upper} extracted values:")
            for key in sorted(values.keys()):
                print(f"  {key}: {values[key]:.2f}")

        bullish = _weekly_pattern_1(values)
        bearish = _weekly_pattern_2(values)
        
        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"

        if verbose:
            print(f"{symbol_upper}: bullish={bullish}, bearish={bearish}, final_signal={bullish or bearish}")

    bullish_frame, bearish_frame = _extract_signal_frames(results)
    return StrategyExecution(
        name="weekly_vs_daily_sweep",
        results=results,
        bullish=bullish_frame,
        bearish=bearish_frame,
    )


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


def run_daily_fvg_sweep(
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
            daily = _fetch_daily_from_bhavcopy(symbol=symbol, as_of_date=as_of_date, max_lookback_days=80)
        else:
            daily = daily_map.get(str(symbol).upper(), pd.DataFrame(columns=["Open", "High", "Low", "Close"]))

        if len(daily) < 4:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol}: SKIPPED (no_data)")
            continue

        values = _daily_fvg_sweep_points(daily)
        bullish = _daily_fvg_sweep_bullish(values)
        bearish = _daily_fvg_sweep_bearish(values)

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"

        if verbose:
            print(f"{symbol}: bullish={bullish}, bearish={bearish}")

    bullish, bearish = _extract_signal_frames(results)
    return StrategyExecution(
        name="daily_fvg_sweep",
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


def _ict_daily_bias_points(daily: pd.DataFrame) -> dict[str, float]:
    latest = daily.iloc[-1]
    prior = daily.iloc[-2]
    sma5 = daily["Close"].tail(5).mean() if len(daily) >= 5 else daily["Close"].mean()
    return {
        "curr_open": latest["Open"],
        "curr_high": latest["High"],
        "curr_low": latest["Low"],
        "curr_close": latest["Close"],
        "prev_close": prior["Close"],
        "prev_high": prior["High"],
        "prev_low": prior["Low"],
        "sma5": float(sma5),
    }


def _ict_daily_bias_bullish(values: dict[str, float]) -> bool:
    return (
        values["curr_close"] > values["sma5"]
        and values["curr_close"] > values["prev_close"]
        and values["curr_low"] > values["prev_low"]
    )


def _ict_daily_bias_bearish(values: dict[str, float]) -> bool:
    return (
        values["curr_close"] < values["sma5"]
        and values["curr_close"] < values["prev_close"]
        and values["curr_high"] < values["prev_high"]
    )


def run_ict_daily_bias(
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

        if len(daily) < 5:
            results.at[idx, "status"] = "no_data"
            if verbose:
                print(f"{symbol}: SKIPPED (no_data)")
            continue

        values = _ict_daily_bias_points(daily)
        bullish = _ict_daily_bias_bullish(values)
        bearish = _ict_daily_bias_bearish(values)

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"

        if verbose:
            print(f"{symbol}: bullish={bullish}, bearish={bearish}")

    bullish, bearish = _extract_signal_frames(results)
    return StrategyExecution(
        name="ict_daily_bias_sweep",
        results=results,
        bullish=bullish,
        bearish=bearish,
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


def _extract_weekly_profile_signal_frames(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = ["symbol", "profile", "state", "direction", "entry", "sl", "target", "rr", "track_mode", "note"]
    bullish = (
        results.loc[results["bullish_match"] == True, columns]
        .sort_values("symbol")
        .reset_index(drop=True)
        .copy()
    )
    bearish = (
        results.loc[results["bearish_match"] == True, columns]
        .sort_values("symbol")
        .reset_index(drop=True)
        .copy()
    )
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
        if extreme_idx > 1:
            return False, f"weekly_extreme_late_d{extreme_idx + 1}"
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
        return True, f"extreme_d{extreme_idx + 1}_expand{len(post)}_{tag}"

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
    wed = weekday_bars[2]
    later = [bar for bar in context["bars"] if int(bar["weekday"]) > 2]

    def side(direction: int) -> tuple[bool, str]:
        if direction > 0:
            early_move = float(tue["close"]) < float(mon["close"]) and float(tue["low"]) < float(mon["low"])
            pivot = float(wed["close"]) > float(tue["high"]) and float(wed["close"]) > float(wed["open"])
        else:
            early_move = float(tue["close"]) > float(mon["close"]) and float(tue["high"]) > float(mon["high"])
            pivot = float(wed["close"]) < float(tue["low"]) and float(wed["close"]) < float(wed["open"])
        if not early_move:
            return False, "no_early_week_move"
        if not pivot:
            return False, "wednesday_closure_pending"
        for bar in later:
            if direction > 0 and float(bar["close"]) < float(wed["close"]):
                return False, "continuation_failed"
            if direction < 0 and float(bar["close"]) > float(wed["close"]):
                return False, "continuation_failed"
        if later:
            return True, "pivot_wed_continuing"
        return True, "pivot_wed_await_thu_continuation"

    bullish, bullish_note = side(1)
    bearish, bearish_note = side(-1)
    note = bullish_note if bullish else (bearish_note if bearish else bullish_note)
    return {"bullish": bullish, "bearish": bearish, "note": note}


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
        for candidate in (2, 3):
            pivot = weekday_bars.get(candidate)
            prev = weekday_bars.get(candidate - 1)
            if pivot is None or prev is None:
                continue
            if direction > 0 and float(pivot["close"]) > float(prev["high"]) and float(pivot["close"]) > float(pivot["open"]) and _body_ratio(pivot) >= 0.5:
                reversal_day = candidate
                break
            if direction < 0 and float(pivot["close"]) < float(prev["low"]) and float(pivot["close"]) < float(pivot["open"]) and _body_ratio(pivot) >= 0.5:
                reversal_day = candidate
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
        return True, f"candle_two_closure_d{reversal_day + 1}"

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

    if fri is None:
        return {
            "bullish": bool(direction > 0),
            "bearish": bool(direction < 0),
            "note": "thursday_counter_set_friday_pending",
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
    refs_high: List[float] = []
    refs_low: List[float] = []
    if prior_weeks:
        refs_high.append(float(prior_weeks[-1]["high"]))
        refs_low.append(float(prior_weeks[-1]["low"]))
    if swing:
        refs_high.append(float(swing["high"]))
        refs_low.append(float(swing["low"]))

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

        if direction > 0:
            extreme_early = low_idx <= 1
            leg_closes = [float(bar["close"]) for bar in leg_bars]
            extreme_close_idx = leg_closes.index(max(leg_closes))
            leg_completed_late = extreme_close_idx >= 2
            expansion = week_low > 0 and (max(leg_closes) - week_low) / week_low >= TGIF_EXPANSION_MIN_PCT
            leg_objective = max(float(bar["high"]) for bar in leg_bars)
            objective = bool(refs_high) and min(refs_high) <= leg_objective
        else:
            extreme_early = high_idx <= 1
            leg_closes = [float(bar["close"]) for bar in leg_bars]
            extreme_close_idx = leg_closes.index(min(leg_closes))
            leg_completed_late = extreme_close_idx >= 2
            expansion = week_high > 0 and (week_high - min(leg_closes)) / week_high >= TGIF_EXPANSION_MIN_PCT
            leg_objective = min(float(bar["low"]) for bar in leg_bars)
            objective = bool(refs_low) and max(refs_low) >= leg_objective

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
    return {"bullish": bullish, "bearish": bearish, "note": note}


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
        wed = by.get(2)
        if wed is None:
            return {"sl_bull": None, "sl_bear": None, "entry_ref": entry_ref}
        sl_bull, sl_bear = float(wed["low"]), float(wed["high"])
        entry_ref = float(wed["close"])
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

    SL = invalidation extreme +/- a >=1xATR buffer; target = opposite prior-week
    or swing extreme (the liquidity pool the profile expands toward). R:R is
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
    if direction > 0:
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
            and last_day == date.today()
            and not ict_scanner.is_daily_bar_ready(ict_scanner.Session.NSE)
        ):
            return daily.iloc[:-1]
    except Exception:
        pass
    return daily


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

        bars = _week_bars(daily, as_of_date)
        if not bars:
            results.at[idx, "status"] = "no_week_data"
            if verbose:
                print(f"{symbol_upper}: SKIPPED (no_week_data)")
            continue

        context = {
            "bars": bars,
            "by_weekday": _bars_by_weekday(bars),
            "latest": bars[-1],
            "prior_weeks": _prior_week_extremes(daily, as_of_date),
            "swing": _pre_week_swing_extremes(daily, as_of_date),
        }
        outcome = evaluator(context)
        bullish = bool(outcome["bullish"])
        bearish = bool(outcome["bearish"])

        levels = _profile_levels(profile_key, context)
        direction = 1 if bullish else (-1 if bearish else 0)
        outcome["sl_level"] = levels["sl_bull"] if direction > 0 else (levels["sl_bear"] if direction < 0 else None)
        outcome["entry_ref"] = levels["entry_ref"]
        plan = _build_trade_plan(outcome, context, _daily_atr(daily))
        state = _signal_state(outcome, context)

        results.at[idx, "bullish_match"] = bullish
        results.at[idx, "bearish_match"] = bearish
        results.at[idx, "final_signal"] = bullish or bearish
        results.at[idx, "status"] = "complete"
        results.at[idx, "note"] = str(outcome["note"])
        results.at[idx, "state"] = state
        results.at[idx, "direction"] = direction
        results.at[idx, "entry"] = plan["entry"]
        results.at[idx, "sl"] = plan["sl"]
        results.at[idx, "target"] = plan["target"]
        results.at[idx, "rr"] = plan["rr"]
        results.at[idx, "atr"] = plan["atr"]
        results.at[idx, "track_mode"] = _track_mode_for(symbol_upper)

        if verbose:
            print(f"{symbol_upper}: {label} bullish={bullish}, bearish={bearish}, note={outcome['note']}")

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
        "weekly_vs_daily_sweep": StrategySpec(
            name="weekly_vs_daily_sweep",
            runner=run_weekly_vs_daily,
        ),
        "inside_bar_pattern_daily_sweep": StrategySpec(
            name="inside_bar_pattern_daily_sweep",
            runner=run_inside_bar_daily_sweep,
        ),
        "daily_fvg_sweep": StrategySpec(
            name="daily_fvg_sweep",
            runner=run_daily_fvg_sweep,
        ),
        "ema5_sweep": StrategySpec(
            name="ema5_sweep",
            runner=run_ema5_sweep,
        ),
        "ict_daily_bias_sweep": StrategySpec(
            name="ict_daily_bias_sweep",
            runner=run_ict_daily_bias,
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
) -> List[StrategyExecution]:
    registry = strategy_registry()
    for name in strategy_names:
        if name not in registry:
            valid = ", ".join(sorted(registry.keys()))
            raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")

    lookback_by_strategy = {
        "weekly_vs_daily_sweep": 420,
        "inside_bar_pattern_daily_sweep": 160,
        "daily_fvg_sweep": 80,
        "ema5_sweep": 40,
        "ict_daily_bias_sweep": 40,
        "classic_expansion_sweep": 60,
        "midweek_reversal_sweep": 60,
        "consolidation_reversal_sweep": 60,
        "intraweek_reversal_sweep": 60,
        "thursday_counter_sweep": 60,
        "tgif_setup_sweep": 60,
    }
    max_lookback = max(lookback_by_strategy.get(name, 60) for name in strategy_names) if strategy_names else 60
    daily_map = _build_daily_map_for_symbols(symbols=symbols, as_of_date=as_of_date, max_lookback_days=max_lookback)

    if parallel and len(strategy_names) > 1:
        results_by_name: Dict[str, StrategyExecution] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(strategy_names))) as executor:
            future_to_name = {
                executor.submit(
                    registry[name].runner,
                    symbols=symbols,
                    as_of_date=as_of_date,
                    verbose=verbose,
                    print_values=print_values,
                    daily_map=daily_map,
                ): name
                for name in strategy_names
            }

            for future in as_completed(future_to_name):
                name = future_to_name[future]
                results_by_name[name] = future.result()

        return [results_by_name[name] for name in strategy_names]

    executions: List[StrategyExecution] = []
    for name in strategy_names:
        execution = registry[name].runner(
            symbols=symbols,
            as_of_date=as_of_date,
            verbose=verbose,
            print_values=print_values,
            daily_map=daily_map,
        )
        executions.append(execution)

    return executions
