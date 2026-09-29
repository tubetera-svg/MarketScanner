"""High-impact ("red") economic news from the ForexFactory calendar.

forexfactory.com/calendar itself is Cloudflare-protected (403 to scripts), so
this reads ForexFactory's own weekly JSON export. Only ``impact == "High"``
events are kept. The feed is downloaded at most once per IST calendar day and
cached in data/state/ff_high_impact.json; ``get_events(refresh=True)`` (the UI
"fetch live" button) bypasses that. On a failed download the last cache is
served with ``stale: True`` and no retry happens until the next IST day.
Currency filtering (app_settings ``news.currencies``) is applied on read, so
changing it never needs a re-download.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "state" / "ff_high_impact.json"
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("events"), list) else None


def _download() -> list[dict[str, Any]]:
    request = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 MarketScanner"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = json.loads(response.read().decode("utf-8"))
    events = []
    for item in raw if isinstance(raw, list) else []:
        if str(item.get("impact", "")).strip().lower() != "high":
            continue
        try:
            when = datetime.fromisoformat(str(item["date"])).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append({
            "title": str(item.get("title", "")).strip(),
            "currency": str(item.get("country", "")).strip().upper(),
            "time_utc": when.isoformat().replace("+00:00", "Z"),
            "forecast": str(item.get("forecast", "") or ""),
            "previous": str(item.get("previous", "") or ""),
        })
    events.sort(key=lambda event: event["time_utc"])
    return events


def _load(refresh: bool) -> dict[str, Any]:
    """Cached document for today, downloading once per IST day (or on refresh)."""
    with _lock:
        cache = _read_cache()
        today = _today_ist()
        if cache and not refresh and cache.get("fetched_day_ist") == today:
            return cache
        try:
            events = _download()
            doc = {
                "fetched_day_ist": today,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": FEED_URL,
                "events": events,
                "stale": False,
                "error": None,
            }
        except Exception as exc:  # network / parse failure: keep the old list
            logger.warning("ForexFactory calendar fetch failed: %s", exc)
            doc = {**(cache or {"fetched_at": None, "source": FEED_URL, "events": []})}
            doc.update({"fetched_day_ist": today, "stale": True, "error": str(exc)})
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return doc


def get_events(currencies: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
    """High-impact events for this week; ``currencies`` empty/None = all."""
    doc = _load(refresh)
    wanted = {c.upper() for c in currencies or []}
    events = [e for e in doc["events"] if not wanted or e["currency"] in wanted]
    return {
        "events": events,
        "available_currencies": sorted({e["currency"] for e in doc["events"]}),
        "currencies": sorted(wanted),
        "fetched_at": doc.get("fetched_at"),
        "stale": bool(doc.get("stale")),
        "error": doc.get("error"),
    }
