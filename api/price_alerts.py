"""User price alerts set from the chart popup (in-app delivery only).

Alerts live in data/state/price_alerts.json. ``PriceAlertWatcher`` polls every
``automation.price_alerts.interval_minutes`` (default 15, min 5): it groups
active alerts by symbol, fetches recent 5m bars once per symbol from
TradingView, and replays the bars that closed or formed since the alert's last
check through :func:`evaluate`. Only completed bars are used, so a cross is
reported up to one 5m bar after it happens; using bar highs/lows (not only the
last price) catches a wick that crossed the level between two polls.

A cross needs the price to have been on the other side first (``last_side``),
so an alert created with price already beyond its level waits for a fresh
cross, like TradingView's "Crossing" alerts. Trigger modes: ``once`` (then the
alert is done), ``once_per_day`` (daily bar day per Settings > Daily bar cut-offs), ``every_check`` (each
fresh cross, subject to ``cooldown_min``). Closed markets are skipped, so they
cost no requests. Fired events are kept in memory for the UI feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "state" / "price_alerts.json"
IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
BAR_INTERVAL = "5m"
BAR_MINUTES = 5

CONDITIONS = ("crosses_above", "crosses_below", "crosses")
TRIGGERS = ("once", "once_per_day", "every_check")

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_symbol(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    return sym if ":" in sym else f"NSE:{sym}"


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


def market_day(ts: datetime, symbol: str) -> str:
    """Daily bar date that ``ts`` belongs to, per Settings > Daily bar cut-offs.

    A same-day market's bar D runs from the cut-off on D-1 to the cut-off on D
    (NSE 17:00 IST, commodities/forex 17:00 New York); a next-day market's bar
    D runs from the cut-off on D to the cut-off on D+1 (crypto 00:00 UTC, GIFT
    Nifty 03:00 IST). Governs "once per day" and the triggered-today count.
    """
    market, tz, same_day = _CUTOFF_MARKETS.get(symbol.split(":", 1)[0], ("commodities", NY, True))
    cut = _cutoff(market)
    local = ts.astimezone(tz) - timedelta(hours=cut.hour, minutes=cut.minute)
    return (local + timedelta(days=1) if same_day else local).date().isoformat()


def triggered_this_session(alert: dict[str, Any], now: datetime) -> bool:
    last = alert.get("last_triggered_at")
    return bool(last) and market_day(datetime.fromisoformat(last), alert["symbol"]) == market_day(now, alert["symbol"])


def _side(price: float, level: float) -> str:
    return "above" if price >= level else "below"


# ---------------------------------------------------------------- store

def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save(alerts: list[dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _validate(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "level" in fields:
        level = float(fields["level"])
        if not level > 0:
            raise ValueError("level must be > 0")
        out["level"] = level
    if "condition" in fields:
        if fields["condition"] not in CONDITIONS:
            raise ValueError(f"condition must be one of {CONDITIONS}")
        out["condition"] = fields["condition"]
    if "trigger" in fields:
        if fields["trigger"] not in TRIGGERS:
            raise ValueError(f"trigger must be one of {TRIGGERS}")
        out["trigger"] = fields["trigger"]
    if "cooldown_min" in fields:
        out["cooldown_min"] = max(0, min(1440, int(fields["cooldown_min"] or 0)))
    if "expires_at" in fields:
        raw = fields["expires_at"]
        out["expires_at"] = _iso(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))) if raw else None
    if "note" in fields:
        out["note"] = str(fields["note"] or "").strip()[:200]
    if "status" in fields:
        if fields["status"] not in ("active", "paused"):
            raise ValueError("status can only be set to active or paused")
        out["status"] = fields["status"]
    return out


def create(fields: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """New active alert. ``reference_price`` (the chart's last close) seeds the side."""
    now = now or _now()
    data = _validate({"condition": "crosses", "trigger": "once", "cooldown_min": 0,
                      "expires_at": None, "note": "", **fields})
    if "level" not in data:
        raise ValueError("level is required")
    reference = fields.get("reference_price")
    alert = {
        "id": uuid.uuid4().hex[:12],
        "symbol": normalize_symbol(fields.get("symbol", "")),
        **data,
        "status": "active",
        "last_side": _side(float(reference), data["level"]) if reference not in (None, "") else None,
        "last_checked_at": _iso(now),
        "last_price": float(reference) if reference not in (None, "") else None,
        "last_triggered_at": None,
        "trigger_count": 0,
        "created_at": _iso(now),
    }
    if alert["symbol"] == "NSE:":
        raise ValueError("symbol is required")
    with _lock:
        alerts = load()
        alerts.append(alert)
        save(alerts)
    return alert


def update(alert_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    data = _validate(fields)
    with _lock:
        alerts = load()
        for alert in alerts:
            if alert["id"] != alert_id:
                continue
            level_changed = "level" in data and data["level"] != alert["level"]
            reactivated = data.get("status") == "active" and alert["status"] != "active"
            alert.update(data)
            if level_changed or reactivated:
                # Re-seed the side so an edit never fires on stale history.
                alert["last_side"] = _side(alert["last_price"], alert["level"]) if alert["last_price"] else None
                alert["last_checked_at"] = _iso(_now())
            save(alerts)
            return alert
    return None


def delete(alert_id: str) -> bool:
    with _lock:
        alerts = load()
        kept = [a for a in alerts if a["id"] != alert_id]
        if len(kept) == len(alerts):
            return False
        save(kept)
        return True


# ---------------------------------------------------------------- evaluation

def bar_close_utc(label: str) -> datetime:
    """``YYYY-MM-DDTHH:MM`` IST bar-open label -> bar close time (UTC)."""
    opened = datetime.fromisoformat(label).replace(tzinfo=IST)
    return (opened + timedelta(minutes=BAR_MINUTES)).astimezone(timezone.utc)


def evaluate(alert: dict[str, Any], bars: list[dict[str, Any]], now: datetime) -> bool:
    """Replay bars since ``last_checked_at``; mutate ``alert``; True when it fired.

    ``bars`` are 5m rows (IST open labels, oldest first). A bar is included when
    it closes after the last check, so the forming bar counts: a traded price
    reaching the level is a real cross, not a repaint.
    """
    if alert["status"] != "active":
        return False
    if alert.get("expires_at") and now >= datetime.fromisoformat(alert["expires_at"]):
        alert["status"] = "expired"
        return False
    since = datetime.fromisoformat(alert["last_checked_at"])
    created = datetime.fromisoformat(alert["created_at"])
    fresh = [
        b for b in bars
        if since < bar_close_utc(b["date"]) <= now
        and bar_close_utc(b["date"]) - timedelta(minutes=BAR_MINUTES) >= created
    ]
    if not fresh:
        return False
    # Advance only past the bars actually used, so none is replayed or skipped.
    alert["last_checked_at"] = _iso(bar_close_utc(fresh[-1]["date"]))
    level = alert["level"]
    alert["last_price"] = float(fresh[-1]["close"])
    if alert.get("last_side") is None:
        # No reference price: the first data seeds the side without firing.
        alert["last_side"] = _side(alert["last_price"], level)
        return False

    fired = False
    side = alert["last_side"]
    for bar in fresh:
        up = side == "below" and float(bar["high"]) >= level
        down = side == "above" and float(bar["low"]) <= level
        if (up and alert["condition"] in ("crosses_above", "crosses")) or (
            down and alert["condition"] in ("crosses_below", "crosses")
        ):
            fired = True
        side = _side(float(bar["close"]), level)
    alert["last_side"] = side
    if not fired:
        return False

    last = alert.get("last_triggered_at")
    if last:
        last_ts = datetime.fromisoformat(last)
        if alert["trigger"] == "once_per_day" and market_day(last_ts, alert["symbol"]) == market_day(now, alert["symbol"]):
            return False
        if alert["trigger"] == "every_check" and now - last_ts < timedelta(minutes=alert.get("cooldown_min", 0)):
            return False
    alert["last_triggered_at"] = _iso(now)
    alert["trigger_count"] = int(alert.get("trigger_count", 0)) + 1
    if alert["trigger"] == "once":
        alert["status"] = "triggered"
    return True


# ---------------------------------------------------------------- watcher

def _default_fetch(symbol: str, n_bars: int) -> list[dict[str, Any]]:
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
    return tradingview_source.fetch_recent_bars(tv_symbol, BAR_INTERVAL, n_bars)


def _default_is_open(symbol: str) -> bool:
    import ict_scanner  # type: ignore  (src/ is on sys.path via strategy_bridge)

    return bool(ict_scanner.is_market_open(ict_scanner.detect_session(symbol)))


class PriceAlertWatcher:
    def __init__(
        self,
        fetch: Callable[[str, int], list[dict[str, Any]]] = _default_fetch,
        is_open: Callable[[str], bool] = _default_is_open,
    ) -> None:
        self.task: asyncio.Task[None] | None = None
        self.interval_minutes = 15
        self.last_check_at: str | None = None
        self.last_error: str | None = None
        self.events: list[dict[str, Any]] = []
        self._fetch = fetch
        self._is_open = is_open

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
        return {
            "running": self.running,
            "interval_minutes": self.interval_minutes,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "alerts": alerts,
            "triggered_session_count": sum(1 for alert in alerts if alert["triggered_this_session"]),
            "events": self.events[-50:],
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # keep the watcher alive across one bad poll
                logger.exception("Price alert poll failed")
                self.last_error = str(exc)
            await asyncio.sleep(self.interval_minutes * 60)

    async def check(self) -> dict[str, Any]:
        now = _now()
        self.last_check_at = _iso(now)
        by_symbol: dict[str, list[str]] = {}
        for alert in load():
            if alert["status"] == "active":
                by_symbol.setdefault(alert["symbol"], []).append(alert["id"])

        errors: list[str] = []
        results: dict[str, dict[str, Any]] = {}
        for symbol, ids in by_symbol.items():
            with _lock:
                pending = [a for a in load() if a["id"] in ids]
            bars: list[dict[str, Any]] = []  # closed market: no request, expiry still applies
            if self._is_open(symbol):
                oldest = min(datetime.fromisoformat(a["last_checked_at"]) for a in pending)
                n_bars = min(300, int((now - oldest).total_seconds() // (BAR_MINUTES * 60)) + 3)
                try:
                    bars = await asyncio.to_thread(self._fetch, symbol, max(10, n_bars))
                except Exception as exc:
                    errors.append(f"{symbol}: {exc}")
            for alert in pending:
                if evaluate(alert, bars, now):
                    self.events.append({
                        "id": f"{alert['id']}-{alert['trigger_count']}",
                        "alert_id": alert["id"], "ts": alert["last_triggered_at"],
                        "symbol": symbol, "condition": alert["condition"],
                        "level": alert["level"], "price": alert["last_price"], "note": alert["note"],
                    })
                results[alert["id"]] = alert

        # Write back only the evaluated fields so concurrent UI edits survive.
        with _lock:
            alerts = load()
            for alert in alerts:
                done = results.get(alert["id"])
                if done is not None and alert["status"] == "active" and alert["level"] == done["level"]:
                    for key in ("status", "last_side", "last_checked_at", "last_price", "last_triggered_at", "trigger_count"):
                        alert[key] = done[key]
            save(alerts)
        self.events = self.events[-100:]
        self.last_error = "; ".join(errors) if errors else None
        return self.status()
