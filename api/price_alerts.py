"""User price alerts set from the chart popup (in-app toasts + optional push).

Alerts live in data/state/price_alerts.json; fired events (with their outcome
prices) in data/state/price_alert_history.json, so history survives restarts.

Conditions
- Line, wick-based on completed 5m bars: ``crosses_above`` / ``crosses_below``
  / ``crosses``. Bar highs/lows count, so a wick between two polls is caught.
- Zone (``level``..``level2``), wick-based on 5m bars: ``enters_zone`` /
  ``exits_zone``.
- Bar close on ``timeframe`` (15m/1h/4h = TradingView's completed bars;
  1D = the daily bar ending at the market's Settings > Daily bar cut-off, built
  from 5m bars): ``closes_above`` / ``closes_below`` (line) and
  ``closes_inside`` (zone). Only completed bars are used, so nothing repaints.

Every condition needs a fresh transition (``last_side``: above / below /
inside), so an alert created with price already beyond its level waits for a
new cross. Trigger modes: ``once``, ``once_per_day`` (market day per the
cut-offs), ``every_check`` (each fresh transition, ``cooldown_min`` apart).
``window`` limits when an alert may fire (NSE hours / ICT kill zones / AM
Silver Bullet); ``snoozed_until`` mutes it; ``expires_at`` may be the market
day's cut-off (``"session_end"``).

``PriceAlertWatcher`` ticks every 5 minutes. A symbol is fetched when its
``interval_minutes`` (default 15) has elapsed, or on every tick while price is
within ``near_pct`` % of one of its levels (adaptive polling); closed markets
are skipped. Fired events are appended to the history and, when enabled, pushed
to Telegram / ntfy (credentials only from environment variables).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import push as push_notify
from market_data import state_store

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "state" / "price_alerts.json"
HISTORY_PATH = ROOT / "data" / "state" / "price_alert_history.json"
HISTORY_LIMIT = 500
IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
BAR_INTERVAL = "5m"
BAR_MINUTES = 5
TICK_MINUTES = 5  # watcher tick = minimum check interval

LINE_WICK = ("crosses_above", "crosses_below", "crosses")
ZONE_WICK = ("enters_zone", "exits_zone")
CLOSE_LINE = ("closes_above", "closes_below")
CLOSE_ZONE = ("closes_inside",)
CONDITIONS = LINE_WICK + ZONE_WICK + CLOSE_LINE + CLOSE_ZONE
ZONE_CONDITIONS = ZONE_WICK + CLOSE_ZONE
CLOSE_CONDITIONS = CLOSE_LINE + CLOSE_ZONE
TRIGGERS = ("once", "once_per_day", "every_check")
TIMEFRAMES = ("15m", "1h", "4h", "1D")  # bar-close conditions; wick conditions use 5m
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
# Firing windows. ICT kill zones in New York time (the repo defines none; these
# are the common ICT conventions); Silver Bullet matches the app's AM scan.
WINDOWS: dict[str, tuple[ZoneInfo, list[tuple[dtime, dtime]]]] = {
    "nse_hours": (IST, [(dtime(9, 15), dtime(15, 30))]),
    "ny_killzones": (NY, [(dtime(2, 0), dtime(5, 0)), (dtime(7, 0), dtime(10, 0)), (dtime(13, 30), dtime(16, 0))]),
    "silver_bullet": (NY, [(dtime(10, 0), dtime(11, 0))]),
}
OUTCOMES = (("15m", timedelta(minutes=15)), ("1h", timedelta(hours=1)), ("eod", None))
OUTCOME_GIVE_UP = timedelta(days=4)

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def normalize_symbol(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    return sym if ":" in sym else f"NSE:{sym}"


# ---------------------------------------------------------------- market day

# Daily-bar cut-off market per exchange prefix: (cut-off key, timezone, final
# on the same day?). Mirrors market_data.service.bar_final_at.
_CUTOFF_MARKETS = {
    "NSE": ("nse", IST, True),
    "BSE": ("nse", IST, True),
    "CRYPTO": ("crypto", timezone.utc, False),
    "NSEIX": ("gift_nifty", IST, False),
}


def _cutoff(market: str):
    import ict_scanner  # type: ignore  (src/ is on sys.path via strategy_bridge)

    return ict_scanner.daily_bar_cutoff(market)


def _cutoff_market(symbol: str):
    return _CUTOFF_MARKETS.get(symbol.split(":", 1)[0], ("commodities", NY, True))


def market_day(ts: datetime, symbol: str) -> str:
    """Daily bar date that ``ts`` belongs to, per Settings > Daily bar cut-offs.

    A same-day market's bar D runs from the cut-off on D-1 to the cut-off on D
    (NSE 17:00 IST, commodities/forex 17:00 New York); a next-day market's bar
    D runs from the cut-off on D to the cut-off on D+1 (crypto 00:00 UTC, GIFT
    Nifty 03:00 IST). Governs "once per day", 1D closes and the day counts.
    """
    market, tz, same_day = _cutoff_market(symbol)
    cut = _cutoff(market)
    local = ts.astimezone(tz) - timedelta(hours=cut.hour, minutes=cut.minute)
    return (local + timedelta(days=1) if same_day else local).date().isoformat()


def market_day_end(ts: datetime, symbol: str) -> datetime:
    """Instant (UTC) at which the market day containing ``ts`` becomes final."""
    market, tz, same_day = _cutoff_market(symbol)
    day = datetime.fromisoformat(market_day(ts, symbol)).date()
    end_day = day if same_day else day + timedelta(days=1)
    # Built from the wall-clock cut-off in the market's zone (DST-safe).
    return datetime.combine(end_day, _cutoff(market), tz).astimezone(timezone.utc)


def triggered_this_session(alert: dict[str, Any], now: datetime) -> bool:
    last = alert.get("last_triggered_at")
    return bool(last) and market_day(_ts(last), alert["symbol"]) == market_day(now, alert["symbol"])


def in_window(ts: datetime, window: str | None) -> bool:
    if not window or window == "always":
        return True
    tz, spans = WINDOWS[window]
    local = ts.astimezone(tz)
    if local.weekday() >= 5:
        return False
    return any(start <= local.time() <= end for start, end in spans)


# ---------------------------------------------------------------- sides

def _zone(alert: dict[str, Any]) -> tuple[float, float]:
    a, b = alert["level"], alert.get("level2") or alert["level"]
    return (a, b) if a <= b else (b, a)


def side_of(alert: dict[str, Any], price: float) -> str:
    if alert["condition"] in ZONE_CONDITIONS:
        low, high = _zone(alert)
        return "inside" if low <= price <= high else ("above" if price > high else "below")
    return "above" if price >= alert["level"] else "below"


# ---------------------------------------------------------------- store

def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(state_store.read_text(path))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    if state_store.handles(path):
        state_store.write_text(path, json.dumps(rows, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(path)


def load() -> list[dict[str, Any]]:
    return _read(STORE_PATH)


def save(alerts: list[dict[str, Any]]) -> None:
    _write(STORE_PATH, alerts)


def load_history() -> list[dict[str, Any]]:
    return _read(HISTORY_PATH)


def _validate(fields: dict[str, Any], current: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Validate a create/update payload; ``current`` is the stored alert on update."""
    now = now or _now()
    out: dict[str, Any] = {}
    for key in ("level", "level2"):
        if key in fields:
            raw = fields[key]
            if raw in (None, "") and key == "level2":
                out[key] = None
                continue
            value = float(raw)
            if not value > 0:
                raise ValueError(f"{key} must be > 0")
            out[key] = value
    if "condition" in fields:
        if fields["condition"] not in CONDITIONS:
            raise ValueError(f"condition must be one of {CONDITIONS}")
        out["condition"] = fields["condition"]
    if "trigger" in fields:
        if fields["trigger"] not in TRIGGERS:
            raise ValueError(f"trigger must be one of {TRIGGERS}")
        out["trigger"] = fields["trigger"]
    if "timeframe" in fields and fields["timeframe"]:
        if fields["timeframe"] not in TIMEFRAMES + ("5m",):
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        out["timeframe"] = fields["timeframe"]
    if "window" in fields:
        window = fields["window"] or "always"
        if window != "always" and window not in WINDOWS:
            raise ValueError(f"window must be always or one of {tuple(WINDOWS)}")
        out["window"] = window
    if "cooldown_min" in fields:
        out["cooldown_min"] = max(0, min(1440, int(fields["cooldown_min"] or 0)))
    symbol = normalize_symbol((current or {}).get("symbol") or fields.get("symbol", ""))
    if "expires_at" in fields:
        raw = fields["expires_at"]
        if raw == "session_end":
            out["expires_at"] = _iso(market_day_end(now, symbol))
        else:
            out["expires_at"] = _iso(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))) if raw else None
    if "note" in fields:
        out["note"] = str(fields["note"] or "").strip()[:200]
    if "status" in fields:
        if fields["status"] not in ("active", "paused"):
            raise ValueError("status can only be set to active or paused")
        out["status"] = fields["status"]
    if "snooze_minutes" in fields:
        minutes = max(0, min(7 * 1440, int(fields["snooze_minutes"] or 0)))
        out["snoozed_until"] = _iso(now + timedelta(minutes=minutes)) if minutes else None

    # Cross-field rules, only when the alert's shape is being set or changed
    # (a note/status edit must not look like a shape change).
    if current is None or any(key in fields for key in ("condition", "level2", "timeframe")):
        merged = {**(current or {}), **out}
        condition = merged.get("condition", "crosses")
        if condition in ZONE_CONDITIONS:
            if not merged.get("level2"):
                raise ValueError("zone conditions need level2 (the other edge of the zone)")
        else:
            out["level2"] = None
        if condition in CLOSE_CONDITIONS:
            if merged.get("timeframe") not in TIMEFRAMES:
                out["timeframe"] = "15m"
        else:
            out["timeframe"] = "5m"
    return out


def _new_alert(fields: dict[str, Any], now: datetime) -> dict[str, Any]:
    data = _validate({"condition": "crosses", "trigger": "once", "cooldown_min": 0, "window": "always",
                      "expires_at": None, "note": "", **fields}, now=now)
    if "level" not in data:
        raise ValueError("level is required")
    symbol = normalize_symbol(fields.get("symbol", ""))
    if symbol == "NSE:":
        raise ValueError("symbol is required")
    reference = fields.get("reference_price")
    alert = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol,
        "level2": None,
        "snoozed_until": None,
        **data,
        "status": "active",
        "last_side": None,
        "last_checked_at": _iso(now),
        "last_price": float(reference) if reference not in (None, "") else None,
        "last_triggered_at": None,
        "trigger_count": 0,
        "created_at": _iso(now),
    }
    if alert["last_price"] is not None:
        alert["last_side"] = side_of(alert, alert["last_price"])
    return alert


def create(fields: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """New active alert. ``reference_price`` (the chart's last close) seeds the side."""
    return create_many([fields], now)[0]


def create_many(items: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Validate all first, then store all (a bad item stores nothing)."""
    now = now or _now()
    created = [_new_alert(item, now) for item in items]
    with _lock:
        alerts = load()
        alerts.extend(created)
        save(alerts)
    return created


def _reseed(alert: dict[str, Any]) -> None:
    # Re-seed the side so an edit never fires on stale history.
    alert["last_side"] = side_of(alert, alert["last_price"]) if alert.get("last_price") else None
    alert["last_checked_at"] = _iso(_now())


def update(alert_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        alerts = load()
        for alert in alerts:
            if alert["id"] != alert_id:
                continue
            data = _validate(fields, current=alert)
            shape = any(key in data and data[key] != alert.get(key) for key in ("level", "level2", "condition", "timeframe"))
            reactivated = data.get("status") == "active" and alert["status"] != "active"
            alert.update(data)
            if shape or reactivated:
                _reseed(alert)
            save(alerts)
            return alert
    return None


def bulk(ids: list[str], action: str) -> int:
    """pause / resume (also re-arms triggered or expired) / delete; returns count."""
    if action not in ("pause", "resume", "delete"):
        raise ValueError("action must be pause, resume or delete")
    wanted = set(ids)
    with _lock:
        alerts = load()
        hit = [a for a in alerts if a["id"] in wanted]
        if action == "delete":
            alerts = [a for a in alerts if a["id"] not in wanted]
        for alert in hit:
            if action == "pause" and alert["status"] == "active":
                alert["status"] = "paused"
            elif action == "resume" and alert["status"] != "active":
                if alert["status"] == "expired":
                    alert["expires_at"] = None
                alert["status"] = "active"
                _reseed(alert)
        save(alerts)
    return len(hit)


def delete(alert_id: str) -> bool:
    return bulk([alert_id], "delete") > 0


# ---------------------------------------------------------------- evaluation

def bar_close_utc(label: str, minutes: int = BAR_MINUTES) -> datetime:
    """``YYYY-MM-DDTHH:MM`` IST bar-open label -> bar close time (UTC)."""
    opened = datetime.fromisoformat(label).replace(tzinfo=IST)
    return (opened + timedelta(minutes=minutes)).astimezone(timezone.utc)


def _units(alert: dict[str, Any], bars_by_tf: dict[str, list[dict[str, Any]]], since: datetime, now: datetime) -> list[dict[str, Any]]:
    """Completed evaluation units after ``since``: {end, high, low, close}, oldest first."""
    timeframe = alert.get("timeframe") or "5m"
    if alert["condition"] not in CLOSE_CONDITIONS:
        timeframe = "5m"
    if timeframe == "1D":
        days: dict[str, list[dict[str, Any]]] = {}
        for bar in bars_by_tf.get("5m", []):
            end = bar_close_utc(bar["date"])
            if since < end <= now:
                days.setdefault(market_day(end - timedelta(seconds=1), alert["symbol"]), []).append(bar)
        units = []
        for day, rows in sorted(days.items()):
            end = market_day_end(bar_close_utc(rows[-1]["date"]) - timedelta(seconds=1), alert["symbol"])
            if end > now:
                continue  # the day is still forming
            units.append({"end": end, "high": max(float(r["high"]) for r in rows),
                          "low": min(float(r["low"]) for r in rows), "close": float(rows[-1]["close"])})
        return units
    minutes = TF_MINUTES[timeframe]
    created = _ts(alert["created_at"])
    units = []
    for bar in bars_by_tf.get(timeframe, []):
        end = bar_close_utc(bar["date"], minutes)
        if not since < end <= now:
            continue
        # Wick conditions ignore bars that opened before the alert existed
        # (their extremes may predate it); a later close is always fair.
        if timeframe == "5m" and alert["condition"] not in CLOSE_CONDITIONS and end - timedelta(minutes=minutes) < created:
            continue
        units.append({"end": end, "high": float(bar["high"]), "low": float(bar["low"]), "close": float(bar["close"])})
    return units


def _fires(alert: dict[str, Any], side: str, unit: dict[str, Any]) -> bool:
    condition = alert["condition"]
    if condition in LINE_WICK:
        level = alert["level"]
        up = side == "below" and unit["high"] >= level
        down = side == "above" and unit["low"] <= level
        return (up and condition in ("crosses_above", "crosses")) or (down and condition in ("crosses_below", "crosses"))
    if condition in ZONE_WICK:
        low, high = _zone(alert)
        touched = unit["high"] >= low and unit["low"] <= high
        if condition == "enters_zone":
            return side != "inside" and touched
        return side == "inside" and (unit["high"] > high or unit["low"] < low)
    new_side = side_of(alert, unit["close"])
    if condition == "closes_above":
        return side == "below" and new_side == "above"
    if condition == "closes_below":
        return side == "above" and new_side == "below"
    return side != "inside" and new_side == "inside"  # closes_inside


def evaluate(alert: dict[str, Any], bars: list[dict[str, Any]] | dict[str, list[dict[str, Any]]], now: datetime) -> bool:
    """Replay completed units since ``last_checked_at``; mutate ``alert``; True when it fired.

    ``bars`` maps timeframe -> TradingView rows (IST open labels, oldest
    first); a plain list means 5m bars. ``last_checked_at`` only advances past
    units actually used, so none is replayed or skipped.
    """
    if alert["status"] != "active":
        return False
    if alert.get("expires_at") and now >= _ts(alert["expires_at"]):
        alert["status"] = "expired"
        return False
    bars_by_tf = {"5m": bars} if isinstance(bars, list) else bars
    units = _units(alert, bars_by_tf, _ts(alert["last_checked_at"]), now)
    if not units:
        return False
    alert["last_checked_at"] = _iso(units[-1]["end"])
    alert["last_price"] = units[-1]["close"]
    if alert.get("last_side") is None:
        # No reference price: the first data seeds the side without firing.
        alert["last_side"] = side_of(alert, alert["last_price"])
        return False

    snoozed = _ts(alert["snoozed_until"]) if alert.get("snoozed_until") else None
    daily = alert.get("timeframe") == "1D"  # a daily close always falls outside intraday windows
    fired_unit: dict[str, Any] | None = None
    side = alert["last_side"]
    for unit in units:
        if (
            fired_unit is None
            and _fires(alert, side, unit)
            and (daily or in_window(unit["end"], alert.get("window")))
            and not (snoozed and unit["end"] < snoozed)
            and _allowed(alert, unit["end"])
        ):
            fired_unit = unit
        side = side_of(alert, unit["close"])
    alert["last_side"] = side
    if fired_unit is None:
        return False
    # Stamp the bar where it happened (not the poll time): cooldowns, the day
    # count and outcome prices are measured from there.
    alert["last_triggered_at"] = _iso(fired_unit["end"])
    alert["last_trigger_price"] = fired_unit["close"]
    alert["trigger_count"] = int(alert.get("trigger_count", 0)) + 1
    if alert["trigger"] == "once":
        alert["status"] = "triggered"
    return True


def _allowed(alert: dict[str, Any], at: datetime) -> bool:
    """Trigger-mode gate for a candidate firing at ``at``."""
    last = alert.get("last_triggered_at")
    if not last:
        return True
    last_ts = _ts(last)
    if alert["trigger"] == "once_per_day":
        return market_day(last_ts, alert["symbol"]) != market_day(at, alert["symbol"])
    if alert["trigger"] == "every_check":
        return at - last_ts >= timedelta(minutes=alert.get("cooldown_min", 0))
    return True


def distance_pct(alert: dict[str, Any]) -> float | None:
    """% from the last price to the nearest level (0 inside a zone)."""
    price = alert.get("last_price")
    if not price:
        return None
    if alert["condition"] in ZONE_CONDITIONS:
        low, high = _zone(alert)
        if low <= price <= high:
            return 0.0
        target = low if price < low else high
    else:
        target = alert["level"]
    return abs(target - price) / price * 100


# ---------------------------------------------------------------- outcomes

def resolve_outcomes(event: dict[str, Any], bars_5m: list[dict[str, Any]], now: datetime) -> bool:
    """Fill due outcome prices (+15m, +1h, end of market day) from 5m bars; True if changed."""
    outcomes = event.setdefault("outcomes", {})
    fired_at = _ts(event["ts"])
    trigger_price = event.get("price")
    closes = [(bar_close_utc(b["date"]), float(b["close"])) for b in bars_5m]
    changed = False
    for key, delta in OUTCOMES:
        if key in outcomes:
            continue
        target = fired_at + delta if delta else _ts(event["eod_at"])
        if now < target:
            continue
        if now - target > OUTCOME_GIVE_UP:
            outcomes[key] = None  # data no longer in the fetch window
            changed = True
            continue
        # Need data spanning the target: a bar at/after the trigger and none missing before it.
        before = [(end, close) for end, close in closes if fired_at < end <= target]
        if not before or not closes or closes[0][0] > fired_at + timedelta(minutes=BAR_MINUTES):
            continue
        price = before[-1][1]
        outcomes[key] = {"price": price, "pct": round((price - trigger_price) / trigger_price * 100, 3) if trigger_price else None}
        changed = True
    return changed


def _pending_outcomes(event: dict[str, Any]) -> bool:
    return len(event.get("outcomes") or {}) < len(OUTCOMES)


# ---------------------------------------------------------------- push text

CONDITION_TEXT = {
    "crosses_above": "crossed above", "crosses_below": "crossed below", "crosses": "crossed",
    "enters_zone": "entered zone", "exits_zone": "left zone",
    "closes_above": "closed above", "closes_below": "closed below", "closes_inside": "closed inside zone",
}


def event_text(event: dict[str, Any]) -> str:
    level = f"{event['level']:g}" + (f"-{event['level2']:g}" if event.get("level2") else "")
    tf = f" ({event['timeframe']} close)" if event.get("condition") in CLOSE_CONDITIONS else ""
    note = f" - {event['note']}" if event.get("note") else ""
    return f"{event['symbol']} {CONDITION_TEXT.get(event['condition'], event['condition'])} {level}{tf}; last {event.get('price')}{note}"


# ---------------------------------------------------------------- watcher

def _default_fetch(symbol: str, interval: str, n_bars: int) -> list[dict[str, Any]]:
    from market_data.config import source_enabled
    from market_data.sources import tradingview_source

    if not source_enabled("TRADINGVIEW"):
        raise RuntimeError("TradingView fetching is disabled (FETCH_TRADINGVIEW_DATA)")
    tv_symbol: str | None = symbol
    if symbol.split(":", 1)[0] in {"NSE", "BSE"}:
        from market_data import tv_symbol as tv_service

        tv_symbol = tv_service.resolve_tv_symbol(symbol).get("tv_symbol")
        if not tv_symbol:
            raise RuntimeError("not found on TradingView")
    return tradingview_source.fetch_recent_bars(tv_symbol, interval, n_bars)


def _default_is_open(symbol: str) -> bool:
    import ict_scanner  # type: ignore  (src/ is on sys.path via strategy_bridge)

    return bool(ict_scanner.is_market_open(ict_scanner.detect_session(symbol)))


class PriceAlertWatcher:
    def __init__(
        self,
        fetch: Callable[[str, str, int], list[dict[str, Any]]] = _default_fetch,
        is_open: Callable[[str], bool] = _default_is_open,
        push: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = 15
        self.near_pct = 0.5
        self.last_check_at: str | None = None
        self.last_error: str | None = None
        self._polled: dict[str, datetime] = {}
        self._fetch = fetch
        self._is_open = is_open
        self.pusher = push_notify.Pusher("Price alert", push)

    def start(self, interval_minutes: int | None = None) -> None:
        self.stop()
        if interval_minutes is not None:
            self.interval_minutes = interval_minutes
        self.task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict[str, Any]:
        now = _now()
        alerts = load()
        for alert in alerts:
            alert["triggered_this_session"] = triggered_this_session(alert, now)
            alert["distance_pct"] = distance_pct(alert)
        history = load_history()
        return {
            "running": self.running,
            "interval_minutes": self.interval_minutes,
            "near_pct": self.near_pct,
            **self.pusher.status(),
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "alerts": alerts,
            "triggered_session_count": sum(1 for alert in alerts if alert["triggered_this_session"]),
            "events": history[-100:],
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # keep the watcher alive across one bad poll
                logger.exception("Price alert poll failed")
                self.last_error = str(exc)
            await asyncio.sleep(TICK_MINUTES * 60)

    def _due(self, symbol: str, alerts: list[dict[str, Any]], now: datetime, force: bool) -> bool:
        last = self._polled.get(symbol)
        if force or last is None or now - last >= timedelta(minutes=self.interval_minutes) - timedelta(seconds=30):
            return True
        if self.near_pct > 0:
            return any((d := distance_pct(a)) is not None and d <= self.near_pct for a in alerts)
        return False

    async def check(self, force: bool = False) -> dict[str, Any]:
        now = _now()
        self.last_check_at = _iso(now)
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for alert in load():
            if alert["status"] == "active":
                by_symbol.setdefault(alert["symbol"], []).append(alert)
        history = load_history()
        outcome_symbols = {e["symbol"] for e in history if _pending_outcomes(e)}

        errors: list[str] = []
        results: dict[str, dict[str, Any]] = {}
        fired: list[dict[str, Any]] = []
        history_changed = False
        for symbol in sorted(set(by_symbol) | outcome_symbols):
            pending = by_symbol.get(symbol, [])
            if not self._due(symbol, pending, now, force):
                continue
            bars_by_tf: dict[str, list[dict[str, Any]]] = {}
            if self._is_open(symbol):
                self._polled[symbol] = now
                starts = [_ts(a["last_checked_at"]) for a in pending]
                starts += [_ts(e["ts"]) for e in history if e["symbol"] == symbol and _pending_outcomes(e)]
                oldest = min(starts) if starts else now
                timeframes = {"5m"} | {a.get("timeframe") for a in pending if a["condition"] in CLOSE_CONDITIONS and a.get("timeframe") in TF_MINUTES}
                for tf in sorted(timeframes):
                    n_bars = min(300, int((now - oldest).total_seconds() // (TF_MINUTES[tf] * 60)) + 3)
                    try:
                        bars_by_tf[tf] = await asyncio.to_thread(self._fetch, symbol, tf, max(10, n_bars))
                    except Exception as exc:
                        errors.append(f"{symbol} {tf}: {exc}")
            # Closed market: no request; expiry still applies.
            for alert in pending:
                if evaluate(alert, bars_by_tf, now):
                    event = {
                        "id": f"{alert['id']}-{alert['trigger_count']}",
                        "alert_id": alert["id"], "ts": alert["last_triggered_at"],
                        "symbol": symbol, "condition": alert["condition"],
                        "level": alert["level"], "level2": alert.get("level2"),
                        "timeframe": alert.get("timeframe"), "trigger": alert["trigger"],
                        "price": alert["last_trigger_price"], "note": alert.get("note", ""),
                        "eod_at": _iso(market_day_end(_ts(alert["last_triggered_at"]), symbol)), "outcomes": {},
                    }
                    fired.append(event)
                results[alert["id"]] = alert
            if "5m" in bars_by_tf:
                for event in history + fired:
                    if event["symbol"] == symbol and _pending_outcomes(event):
                        history_changed |= resolve_outcomes(event, bars_by_tf["5m"], now)

        with _lock:
            # Write back only the evaluated fields so concurrent UI edits survive.
            alerts = load()
            for alert in alerts:
                done = results.get(alert["id"])
                if done is not None and alert["status"] == "active" and (alert["level"], alert.get("level2")) == (done["level"], done.get("level2")):
                    for key in ("status", "last_side", "last_checked_at", "last_price", "last_triggered_at", "last_trigger_price", "trigger_count"):
                        alert[key] = done.get(key)
            save(alerts)
            if fired or history_changed:
                _write(HISTORY_PATH, (history + fired)[-HISTORY_LIMIT:])

        await self.pusher.push(event_text(event) for event in fired)
        self.last_error = "; ".join(errors) if errors else None
        return self.status()
