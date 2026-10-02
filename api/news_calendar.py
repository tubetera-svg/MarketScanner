"""Economic calendar: high-impact news events plus EIA Crude / NatGas release times.

Primary source is TradingView's economic calendar (the JSON endpoint its own
calendar page uses; it needs a tradingview.com Origin header). High-impact
events are ``importance == 1``. The EIA crude and natgas inventory reports are
picked by title, whatever their importance, because only their release time is
needed. If TradingView fails, ForexFactory's weekly JSON export is used instead
(``impact == "High"`` events, and the inventory reports by title).

The calendar is downloaded at most once per IST calendar day and cached in
data/state/economic_calendar.json; ``get_events(refresh=True)`` (the UI "fetch
live" button) bypasses that. If both sources fail, the last cache (if any) is
served with ``stale: True`` and no download is attempted again, not even by
refresh, until ``retry_at``. That is RETRY_BACKOFF_SECONDS, or longer if a 429
response sends Retry-After. Currency filtering (app_settings ``news.currencies``)
is applied on read, so changing it never needs a re-download.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "state" / "economic_calendar.json"
TV_URL = "https://economic-calendar.tradingview.com/events"
TV_HEADERS = {"User-Agent": "Mozilla/5.0 MarketScanner", "Origin": "https://www.tradingview.com"}
# Countries behind app_settings.NEWS_CURRENCIES; EUR members report separately.
TV_COUNTRIES = ("US", "EU", "DE", "FR", "IT", "ES", "GB", "JP", "AU", "NZ", "CA", "CH", "CN")
TV_EUR_MEMBERS = {"DE", "FR", "IT", "ES"}
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_HEADERS = {"User-Agent": "Mozilla/5.0 MarketScanner"}
# Inventory report key -> calendar title, per source.
INVENTORY_TITLES = {
    "tradingview": {"crude": "EIA Crude Oil Stocks Change", "natgas": "EIA Natural Gas Stocks Change"},
    "forexfactory": {"crude": "Crude Oil Inventories", "natgas": "Natural Gas Storage"},
}
IST = ZoneInfo("Asia/Kolkata")
RETRY_BACKOFF_SECONDS = 30 * 60  # faireconomy rate-limits (HTTP 429) aggressively

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    return _now().astimezone(IST).date().isoformat()


def _iso_utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _backoff_seconds(exc: Exception) -> int:
    if isinstance(exc, urllib.error.HTTPError) and exc.headers is not None:
        try:
            return max(RETRY_BACKOFF_SECONDS, int(exc.headers.get("Retry-After", "")))
        except ValueError:
            pass
    return RETRY_BACKOFF_SECONDS


def _read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("events"), list) else None


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _tv_value(value: Any, item: dict[str, Any]) -> str:
    if value is None or value == "":
        return ""
    text = f"{value:g}" if isinstance(value, (int, float)) else str(value)
    unit = item.get("unit") or ""
    text += item.get("scale") or ""
    return text + unit if unit == "%" else unit + text  # 3.2%, A$2B


def parse_tradingview(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(raw, dict) or raw.get("status") != "ok" or not isinstance(raw.get("result"), list):
        raise ValueError("unexpected TradingView calendar response")
    titles = {title: key for key, title in INVENTORY_TITLES["tradingview"].items()}
    events, inventory = [], []
    for item in raw["result"]:
        try:
            when = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        title = str(item.get("title", "")).strip()
        country = str(item.get("country", "")).strip().upper()
        if title in titles and country == "US":
            inventory.append({"report": titles[title], "time_utc": _iso_utc(when)})
        if item.get("importance") != 1:
            continue
        events.append({
            "title": f"{country} {title}" if country in TV_EUR_MEMBERS else title,
            "currency": str(item.get("currency") or "").strip().upper(),
            "time_utc": _iso_utc(when),
            "forecast": _tv_value(item.get("forecast"), item),
            "previous": _tv_value(item.get("previous"), item),
        })
    return events, inventory


def parse_forexfactory(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(raw, list):
        raise ValueError("unexpected ForexFactory calendar response")
    titles = {title: key for key, title in INVENTORY_TITLES["forexfactory"].items()}
    events, inventory = [], []
    for item in raw:
        try:
            when = datetime.fromisoformat(str(item["date"]))
        except (KeyError, ValueError):
            continue
        title = str(item.get("title", "")).strip()
        currency = str(item.get("country", "")).strip().upper()
        if title in titles and currency == "USD":
            inventory.append({"report": titles[title], "time_utc": _iso_utc(when)})
        if str(item.get("impact", "")).strip().lower() != "high":
            continue
        events.append({
            "title": title,
            "currency": currency,
            "time_utc": _iso_utc(when),
            "forecast": str(item.get("forecast", "") or ""),
            "previous": str(item.get("previous", "") or ""),
        })
    return events, inventory


def _download_tradingview() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    # From yesterday (IST) for 9 days: covers today plus the next weekly releases.
    start = datetime.combine(_now().astimezone(IST).date() - timedelta(days=1), time(), IST)
    query = urllib.parse.urlencode({
        "from": _iso_utc(start),
        "to": _iso_utc(start + timedelta(days=9)),
        "countries": ",".join(TV_COUNTRIES),
    })
    return parse_tradingview(_get_json(f"{TV_URL}?{query}", TV_HEADERS))


def _download_forexfactory() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return parse_forexfactory(_get_json(FF_URL, FF_HEADERS))


def _load(refresh: bool) -> dict[str, Any]:
    """Cached document for today, downloading once per IST day (or on refresh)."""
    with _lock:
        cache = _read_cache()
        today = _today_ist()
        if cache and not refresh and cache.get("fetched_day_ist") == today:
            return cache
        retry_at = (cache or {}).get("retry_at")
        if cache and retry_at and _now() < datetime.fromisoformat(retry_at):
            return cache
        errors, backoff = [], RETRY_BACKOFF_SECONDS
        doc = None
        for source, download in (("tradingview", _download_tradingview), ("forexfactory", _download_forexfactory)):
            try:
                events, inventory = download()
            except Exception as exc:  # network / parse failure: try the next source
                logger.warning("%s calendar fetch failed: %s", source, exc)
                errors.append(f"{source}: {exc}")
                backoff = max(backoff, _backoff_seconds(exc))
                continue
            doc = {
                "fetched_day_ist": today,
                "fetched_at": _now().isoformat(),
                "source": source,
                "events": sorted(events, key=lambda event: event["time_utc"]),
                "inventory": sorted(inventory, key=lambda row: row["time_utc"]),
                "stale": False,
                "error": "; ".join(errors) or None,
                "retry_at": None,
            }
            break
        if doc is None:  # both failed: keep the old lists
            # fetched_day_ist stays at the last successful day so the next try happens after retry_at
            doc = {**(cache or {"fetched_day_ist": None, "fetched_at": None, "source": None, "events": [], "inventory": []})}
            retry = _now() + timedelta(seconds=backoff)
            doc.update({"stale": True, "error": "; ".join(errors), "retry_at": retry.isoformat()})
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return doc


def get_events(currencies: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
    """High-impact events (``currencies`` empty/None = all) plus inventory release times."""
    doc = _load(refresh)
    wanted = {c.upper() for c in currencies or []}
    events = [e for e in doc["events"] if not wanted or e["currency"] in wanted]
    return {
        "events": events,
        "inventory": doc.get("inventory", []),
        "available_currencies": sorted({e["currency"] for e in doc["events"]}),
        "currencies": sorted(wanted),
        "fetched_at": doc.get("fetched_at"),
        "source": doc.get("source"),
        "stale": bool(doc.get("stale")),
        "error": doc.get("error"),
    }
