"""NSE trading holidays: the one calendar every NSE-session consumer shares.

Scope: NSE Capital Market segment only (equities, ETFs, IPOs and NSE indices).
GIFT Nifty (NSE IX), forex, commodities and crypto have their own calendars and
must not consult this list.

Source: NSE's holiday master (``CM`` segment), the data behind
https://www.nseindia.com/resources/exchange-communication-holidays. It is
downloaded at most once per IST calendar day, in a background thread, so no
caller ever waits on the network; until a download succeeds the last saved copy
(data/state/nse_holidays.json via ``state_store``) or the built-in list below is
used. Years already saved are kept, so historical anchors keep their holidays
after NSE starts publishing the next year. A failed download is retried after
RETRY_BACKOFF, not on every call. Set ``FETCH_NSE_DATA=false`` to never download.

``NSE_HOLIDAYS`` is a live set-like view (``day in NSE_HOLIDAYS``) for the code
that used to hold its own copy of the list.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Set
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from . import state_store
from .config import FLAG_FETCH_NSE, env_flag

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "state" / "nse_holidays.json"
API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
PAGE_URL = "https://www.nseindia.com/resources/exchange-communication-holidays"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": PAGE_URL,
}
SEGMENT = "CM"  # Capital Market: equities, ETFs, IPOs, indices
TIMEOUT_SECONDS = 15
RETRY_BACKOFF = timedelta(hours=3)
IST = ZoneInfo("Asia/Kolkata")

# Used until NSE has been reached once (and for years NSE no longer lists).
# Matches NSE's 2026 CM circular; weekend entries are informational only.
BUILTIN: dict[str, str] = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan*",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

_lock = threading.Lock()
# day: IST date this process last checked for a download; doc/dates: the list in use.
_memo: dict[str, Any] = {"day": None, "doc": None, "dates": frozenset()}
_refreshing = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    return _now().astimezone(IST).date().isoformat()


def parse_nse(raw: Any) -> dict[str, str]:
    """NSE holiday-master JSON -> {ISO date: name} for the CM segment."""
    out: dict[str, str] = {}
    for row in (raw or {}).get(SEGMENT, []) if isinstance(raw, dict) else []:
        try:
            day = datetime.strptime(str(row["tradingDate"]).strip(), "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        out[day.isoformat()] = str(row.get("description") or "").strip()
    return out


def _download() -> dict[str, str]:
    request = urllib.request.Request(API_URL, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        parsed = parse_nse(json.loads(response.read().decode("utf-8")))
    if not parsed:
        raise ValueError(f"no {SEGMENT} holidays in NSE response")
    return parsed


def _read_cache() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(state_store.read_text(CACHE_PATH))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("holidays"), dict) else None


def _write_cache(doc: dict[str, Any]) -> None:
    try:
        if not state_store.handles(CACHE_PATH):
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state_store.write_text(CACHE_PATH, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("Could not save NSE holidays (%s); using them from memory.", exc)


def _merged(cache: Optional[dict[str, Any]]) -> dict[str, str]:
    """Saved NSE years win over the built-in list year by year."""
    saved = dict((cache or {}).get("holidays") or {})
    saved_years = {day[:4] for day in saved}
    return {**{d: n for d, n in BUILTIN.items() if d[:4] not in saved_years}, **saved}


def _set_memo(doc: dict[str, Any]) -> None:
    dates = set()
    for day in doc["holidays"]:
        try:
            dates.add(date.fromisoformat(day))
        except ValueError:
            continue
    _memo.update(doc=doc, dates=frozenset(dates))


def refresh(force: bool = False) -> dict[str, Any]:
    """Download today's list if due (once per IST day, honouring the backoff)."""
    with _lock:
        cache = _read_cache()
        today = _today_ist()
        retry_at = (cache or {}).get("retry_at")
        due = force or (cache or {}).get("fetched_day_ist") != today
        if retry_at and not force:
            try:
                due = due and _now() >= datetime.fromisoformat(retry_at)
            except ValueError:
                pass
        if due and env_flag(FLAG_FETCH_NSE, default=True):
            try:
                fetched = _download()
                years = {day[:4] for day in fetched}
                kept = {d: n for d, n in ((cache or {}).get("holidays") or {}).items() if d[:4] not in years}
                cache = {"holidays": {**kept, **fetched}, "fetched_day_ist": today,
                         "fetched_at": _now().isoformat(timespec="seconds"), "error": None, "retry_at": None}
            except Exception as exc:  # network / NSE block / bad payload: keep the last list
                log.warning("NSE holiday list fetch failed: %s", exc)
                cache = {**(cache or {"holidays": {}, "fetched_day_ist": None, "fetched_at": None}),
                         "error": str(exc), "retry_at": (_now() + RETRY_BACKOFF).isoformat(timespec="seconds")}
            _write_cache(cache)
        doc = _doc(cache)
        _set_memo(doc)
        return doc


def _doc(cache: Optional[dict[str, Any]]) -> dict[str, Any]:
    cache = cache or {}
    return {
        "holidays": _merged(cache),
        "source": "nse" if cache.get("fetched_at") else "builtin",
        "fetched_at": cache.get("fetched_at"),
        "error": cache.get("error"),
        "segment": SEGMENT,
        "url": PAGE_URL,
    }


def _refresh_in_background() -> None:
    try:
        refresh()
    finally:
        _refreshing.clear()


def _start_refresh() -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()
    threading.Thread(target=_refresh_in_background, name="nse-holidays", daemon=True).start()


def _current() -> dict[str, Any]:
    """Today's list without blocking: a stale day kicks off one background refresh."""
    if _memo["doc"] is None:
        with _lock:
            if _memo["doc"] is None:
                cache = _read_cache()
                _set_memo(_doc(cache))
                if (cache or {}).get("fetched_day_ist") == _today_ist():
                    _memo["day"] = _today_ist()  # already downloaded today (before a restart)
    today = _today_ist()
    if _memo["day"] != today:
        _memo["day"] = today  # one refresh per IST day from this process
        _start_refresh()
    return _memo["doc"]


def holiday_dates() -> frozenset[date]:
    _current()
    return _memo["dates"]


def is_holiday(day: date) -> bool:
    if isinstance(day, datetime):
        day = day.date()
    return day in holiday_dates()


def info() -> dict[str, Any]:
    """For the Settings page: [{date, name}] plus where and when it came from."""
    doc = _current()
    return {
        "items": [{"date": day, "name": name} for day, name in sorted(doc["holidays"].items())],
        "source": doc["source"], "fetched_at": doc["fetched_at"], "error": doc["error"],
        "segment": doc["segment"], "url": doc["url"],
    }


class _HolidayView(Set):
    """``day in NSE_HOLIDAYS`` against the live list (dates or datetimes)."""

    def __contains__(self, day: object) -> bool:
        return isinstance(day, date) and is_holiday(day)

    def __iter__(self) -> Iterator[date]:
        return iter(sorted(holiday_dates()))

    def __len__(self) -> int:
        return len(holiday_dates())


NSE_HOLIDAYS: Set = _HolidayView()
