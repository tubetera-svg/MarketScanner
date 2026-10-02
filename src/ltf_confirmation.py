"""Lower-timeframe (1h / 15m) confirmation of daily setups.

The TTrades frameworks the daily strategies follow (weekly profiles, daily-bias
invalidation, candle 2/3 closures, protected swings, propulsion blocks) take
their entries only after an intraday *change in the state of delivery* (CISD)
on the trade day. Daily bars can only show that after the move is over, so the
work is split in two:

1. **Arm (EOD).** A daily runner that fires writes the ``ltf_*`` columns
   (``all_strategy.LTF_COLUMNS``): a zone the next session should trade into,
   an invalidation level, and the last session the setup stays valid.
2. **Trigger (intraday).** :func:`evaluate_ltf` replays *completed* intraday
   bars from the session after the signal. Bullish (bearish mirrors): price must
   trade into the zone; the setup triggers on the first close above the open of
   the first candle of the most recent down-close series (the CISD level, per
   the TTrades CISD definition). A close through the invalidation level first
   invalidates it; running out of sessions expires it.

:func:`evaluate_ltf` is pure and point-in-time (bars whose close time is after
``now`` are ignored), so the live watcher and any future intraday backtest
share one code path. :class:`LtfSetupStore` persists setups across restarts
(atomic JSON).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "data", "state", "ltf_setups.json")

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

TIMEFRAME_MINUTES = {"15m": 15, "1h": 60}

STATE_ARMED = "armed"
STATE_TRIGGERED = "triggered"
STATE_INVALIDATED = "invalidated"
STATE_EXPIRED = "expired"
TERMINAL_STATES = {STATE_TRIGGERED, STATE_INVALIDATED, STATE_EXPIRED}

RETENTION_DAYS = 21


@dataclass
class LtfSetup:
    """One daily setup waiting for (or resolved by) intraday confirmation."""

    key: str
    symbol: str
    strategy: str
    direction: int
    zone_low: float
    zone_high: float
    invalidation: float
    signal_date: str          # ISO date of the daily bar that armed it
    valid_until: str          # ISO date of the last session it may confirm on
    market: str = "NSE"       # "NSE" (IST sessions) or "FOREX" (NY 17:00 rollover)
    target: Optional[float] = None
    state: str = STATE_ARMED
    reached_at: Optional[str] = None
    triggered_at: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    note: str = ""
    armed_at: str = ""
    updated_at: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)


def setup_key(symbol: str, strategy: str, signal_date: str, direction: int) -> str:
    return f"{symbol}|{strategy}|{signal_date}|{direction}"


def market_for_symbol(symbol: str) -> str:
    """NSE sessions run on IST dates; everything else rolls at NY 17:00."""
    value = str(symbol).strip().upper()
    if ":" not in value or value.split(":", 1)[0] == "NSE":
        return "NSE"
    return "FOREX"


def session_date(ts: pd.Timestamp, market: str) -> date:
    """Trading-session date of a tz-aware bar timestamp.

    NSE: the IST calendar date. Forex/commodities: the NY trading day, which
    rolls at 17:00 New York (a bar at 18:00 NY belongs to the next day).
    """
    if market == "NSE":
        return ts.tz_convert(IST).date()
    return (ts.tz_convert(NEW_YORK) + timedelta(hours=7)).date()


def bars_from_rows(rows: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    """TradingView rows -> tz-aware (IST) OHLC frame sorted by bar open time.

    The market-data layer returns TradingView bars as naive IST wall time.
    """
    frame = pd.DataFrame(list(rows))
    if frame.empty or "date" not in frame:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    stamps = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.assign(ts=stamps).dropna(subset=["ts"])
    if frame["ts"].dt.tz is None:
        frame["ts"] = frame["ts"].dt.tz_localize(IST)
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(frame["open"], errors="coerce").to_numpy(),
            "High": pd.to_numeric(frame["high"], errors="coerce").to_numpy(),
            "Low": pd.to_numeric(frame["low"], errors="coerce").to_numpy(),
            "Close": pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
        },
        index=pd.DatetimeIndex(frame["ts"]),
    )
    return out.dropna().sort_index()


def evaluate_ltf(
    setup: LtfSetup,
    bars: pd.DataFrame,
    now: datetime,
    timeframe: str = "1h",
) -> Dict[str, Any]:
    """Replay completed intraday bars for one armed setup.

    ``bars`` is a tz-aware OHLC frame indexed by bar *open* time. Returns a
    dict with ``state`` (armed / triggered / invalidated / expired) and, when
    resolved, ``at`` (ISO bar time), ``entry``, ``sl`` and ``note``.
    """
    minutes = TIMEFRAME_MINUTES.get(timeframe, 60)
    signal_day = date.fromisoformat(setup.signal_date)
    last_day = date.fromisoformat(setup.valid_until)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(IST)
    bullish = setup.direction > 0

    reached_at: Optional[str] = None
    extreme: Optional[float] = None
    run_open: Optional[float] = None
    prev_down: Optional[bool] = None

    for ts, bar in bars.sort_index().iterrows():
        ts = pd.Timestamp(ts)
        if ts + pd.Timedelta(minutes=minutes) > now_ts:
            break  # forming bar: its close is not known yet
        day = session_date(ts, setup.market)
        if day <= signal_day:
            continue
        if day > last_day:
            break
        o, h, l, c = (float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"]))

        if (bullish and c < setup.invalidation) or (not bullish and c > setup.invalidation):
            return {"state": STATE_INVALIDATED, "at": ts.isoformat(), "reached_at": reached_at,
                    "note": f"closed through invalidation {setup.invalidation:g} before a CISD"}

        if reached_at is None and ((bullish and l <= setup.zone_high) or (not bullish and h >= setup.zone_low)):
            reached_at = ts.isoformat()
        if reached_at is not None:
            # The intraday swing made in the zone: the stop sits beyond it.
            point = l if bullish else h
            extreme = point if extreme is None else (min(extreme, point) if bullish else max(extreme, point))

        # Track the opposite-colour series delivering price into the zone; its
        # first candle's open is the CISD level.
        against = (c < o) if bullish else (c > o)
        if against:
            if not prev_down:
                run_open = o
            prev_down = True
            continue
        prev_down = False

        if reached_at is not None and run_open is not None and (
            (bullish and c > run_open) or (not bullish and c < run_open)
        ):
            return {
                "state": STATE_TRIGGERED,
                "at": ts.isoformat(),
                "reached_at": reached_at,
                "entry": c,
                "sl": extreme,
                "note": f"{timeframe} CISD: close {c:g} through series open {run_open:g}",
            }

    today = session_date(now_ts, setup.market)
    if today > last_day:
        return {"state": STATE_EXPIRED, "reached_at": reached_at,
                "note": "valid window ended without an intraday CISD"}
    return {"state": STATE_ARMED, "reached_at": reached_at,
            "note": "zone reached; awaiting CISD" if reached_at else "awaiting a trade into the zone"}


def _state_store_for(path: str):
    """market_data.state_store when APP_STATE_STORE=db and it stores ``path``, else None (plain file)."""
    if os.environ.get("APP_STATE_STORE", "").strip().lower() != "db":
        return None
    if ROOT not in sys.path:
        sys.path.append(ROOT)
    from market_data import state_store

    return state_store if state_store.handles(path) else None


class LtfSetupStore:
    """Persisted armed/resolved LTF setups keyed by :func:`setup_key`."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_PATH

    def load(self) -> Dict[str, LtfSetup]:
        store = _state_store_for(self.path)
        try:
            if store is not None:
                raw = json.loads(store.read_text(self.path))
            else:
                with open(self.path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        out: Dict[str, LtfSetup] = {}
        for key, value in (raw or {}).items():
            try:
                out[key] = LtfSetup(**value)
            except TypeError:
                continue
        return out

    def save(self, data: Dict[str, LtfSetup]) -> None:
        store = _state_store_for(self.path)
        if store is not None:
            try:
                store.write_text(self.path, json.dumps({key: asdict(value) for key, value in data.items()}))
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({key: asdict(value) for key, value in data.items()}, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def arm(self, setups: Iterable[LtfSetup], now: Optional[datetime] = None) -> List[LtfSetup]:
        """Add new setups (existing keys are left as they are); return the new ones."""
        data = self.load()
        stamp = (now or datetime.now(IST)).isoformat(timespec="seconds")
        added: List[LtfSetup] = []
        for setup in setups:
            if setup.key in data:
                continue
            setup.armed_at = setup.updated_at = stamp
            setup.events.append({"ts": stamp, "state": STATE_ARMED, "note": setup.note})
            data[setup.key] = setup
            added.append(setup)
        self._prune(data, now)
        self.save(data)
        return added

    def apply(self, key: str, result: Dict[str, Any], now: Optional[datetime] = None) -> Optional[LtfSetup]:
        """Fold an :func:`evaluate_ltf` result in; return the setup if its state changed."""
        data = self.load()
        setup = data.get(key)
        if setup is None or setup.state in TERMINAL_STATES:
            return None
        stamp = (now or datetime.now(IST)).isoformat(timespec="seconds")
        setup.reached_at = result.get("reached_at") or setup.reached_at
        setup.note = str(result.get("note") or setup.note)
        setup.updated_at = stamp
        changed = result["state"] != setup.state
        if changed:
            setup.state = result["state"]
            if setup.state == STATE_TRIGGERED:
                setup.triggered_at = result.get("at")
                setup.entry = result.get("entry")
                setup.sl = result.get("sl")
            setup.events.append({"ts": stamp, "state": setup.state, "at": result.get("at"), "note": setup.note})
        data[key] = setup
        self.save(data)
        return setup if changed else None

    def active(self) -> List[LtfSetup]:
        return [setup for setup in self.load().values() if setup.state == STATE_ARMED]

    @staticmethod
    def _prune(data: Dict[str, LtfSetup], now: Optional[datetime]) -> None:
        cutoff = ((now or datetime.now(IST)).date() - timedelta(days=RETENTION_DAYS)).isoformat()
        for key in [k for k, v in data.items() if v.valid_until < cutoff]:
            data.pop(key, None)
