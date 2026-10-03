"""Upcoming NSE stock events shown next to scanner signals (display only).

- **Results / board meetings**: NSE's event calendar (``/api/event-calendar``),
  the data behind https://www.nseindia.com/companies-listing/corporate-filings-event-calendar.
  A meeting whose purpose mentions "Financial Results" is ``results``; any other
  purpose is ``board``.
- **Ex-dates**: NSE corporate actions (``/api/corporates-corporateActions``):
  dividends, splits, bonuses, rights - kind ``ex_date``.
- **F&O expiries**: the expiry dates NSE lists for live contracts
  (``/api/option-chain-contract-info``) - stock F&O (all stocks share one
  monthly expiry, read from ``EXPIRY_SYMBOLS["stock"]``) and the Nifty index
  (weekly + monthly). NSE already moves them for holidays; nothing is calculated.

Nothing here gates a signal; it only labels rows. Downloads follow
``nse_holidays``: at most once per IST day, in a background thread so no request
waits on NSE, saved to data/state/nse_events.json via ``state_store``. A failed
download keeps the last saved list and is retried after RETRY_BACKOFF.
Events and expiries are separate feeds: one failing keeps its last saved copy
and does not drop the other. ``FETCH_NSE_DATA=false`` disables downloading.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import state_store
from .config import FLAG_FETCH_NSE, env_flag

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "state" / "nse_events.json"
HOME_URL = "https://www.nseindia.com/"
EVENTS_URL = "https://www.nseindia.com/api/event-calendar?index=equities&from_date={start}&to_date={end}"
ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={start}&to_date={end}"
CONTRACT_URL = "https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}"
# Contracts whose expiry list is shown: any liquid F&O stock carries the common
# stock-F&O expiries; NIFTY carries the index weeklies and monthlies.
EXPIRY_SYMBOLS = {"stock": "RELIANCE", "index": "NIFTY"}
PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": PAGE_URL,
}
TIMEOUT_SECONDS = 15
RETRY_BACKOFF = timedelta(hours=3)
# Download window around the IST date: recent ex-dates still explain a gap on the chart.
LOOKBACK_DAYS = 7
LOOKAHEAD_DAYS = 45
IST = ZoneInfo("Asia/Kolkata")

_lock = threading.Lock()
_memo: dict[str, Any] = {"day": None, "doc": None}
_refreshing = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> date:
    return _now().astimezone(IST).date()


# --- NSE downloads ------------------------------------------------------------

def _nse_date(value: Any) -> Optional[str]:
    try:
        return datetime.strptime(str(value).strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def parse_board_meetings(raw: Any) -> list[dict[str, str]]:
    """NSE event-calendar JSON -> [{symbol, date, kind, title}]."""
    out = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        day = _nse_date(row.get("date"))
        purpose = " ".join(str(row.get("purpose") or "").split())
        if not symbol or not day:
            continue
        kind = "results" if "financial result" in purpose.lower() else "board"
        out.append({"symbol": f"NSE:{symbol}", "date": day, "kind": kind, "title": purpose or "Board meeting"})
    return out


def parse_corporate_actions(raw: Any) -> list[dict[str, str]]:
    """NSE corporate-actions JSON -> [{symbol, date (ex-date), kind, title}]."""
    out = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        day = _nse_date(row.get("exDate"))
        if not symbol or not day:
            continue
        subject = " ".join(str(row.get("subject") or "").split())
        out.append({"symbol": f"NSE:{symbol}", "date": day, "kind": "ex_date", "title": subject or "Corporate action"})
    return out


def parse_expiries(raw: Any) -> list[str]:
    """NSE option-chain contract-info JSON -> sorted ISO expiry dates."""
    values = raw.get("expiryDates") if isinstance(raw, dict) else None
    return sorted({day for day in (_nse_date(v) for v in values or []) if day})


def _get_json(opener: urllib.request.OpenerDirector, url: str) -> Any:
    try:
        with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
    # NSE sometimes wants its session cookies first: visit the home page once, retry.
    opener.open(urllib.request.Request(HOME_URL, headers={**HEADERS, "Accept": "text/html"}),
                timeout=TIMEOUT_SECONDS).read()
    with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_events(opener: urllib.request.OpenerDirector, today: date) -> list[dict[str, str]]:
    fmt = "%d-%m-%Y"
    window = {"start": (today - timedelta(days=LOOKBACK_DAYS)).strftime(fmt),
              "end": (today + timedelta(days=LOOKAHEAD_DAYS)).strftime(fmt)}
    events: list[dict[str, str]] = []
    for url, parse in ((EVENTS_URL, parse_board_meetings), (ACTIONS_URL, parse_corporate_actions)):
        raw = _get_json(opener, url.format(**window))
        if not isinstance(raw, list):
            raise ValueError(f"unexpected NSE response from {url.split('?')[0]}")
        events.extend(parse(raw))
    return events


def _download_expiries(opener: urllib.request.OpenerDirector, today: date) -> dict[str, list[str]]:
    out = {}
    for key, symbol in EXPIRY_SYMBOLS.items():
        days = parse_expiries(_get_json(opener, CONTRACT_URL.format(symbol=symbol)))
        if not days:
            raise ValueError(f"no expiry dates in NSE contract info for {symbol}")
        out[key] = days
    return out


# Cache key -> downloader; each feed is fetched and kept independently.
FEEDS: dict[str, Callable[[urllib.request.OpenerDirector, date], Any]] = {
    "events": _download_events,
    "expiries": _download_expiries,
}
_EMPTY = {"events": [], "expiries": {}}


def _read_cache() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(state_store.read_text(CACHE_PATH))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("events"), list) else None


def _write_cache(doc: dict[str, Any]) -> None:
    try:
        if not state_store.handles(CACHE_PATH):
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state_store.write_text(CACHE_PATH, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("Could not save NSE events (%s); using them from memory.", exc)


def refresh(force: bool = False) -> dict[str, Any]:
    """Download today's feeds if due (once per IST day, honouring the backoff)."""
    with _lock:
        cache = _read_cache()
        today = _today_ist()
        retry_at = (cache or {}).get("retry_at")
        due = force or (cache or {}).get("fetched_day_ist") != today.isoformat()
        if retry_at and not force:
            try:
                due = due and _now() >= datetime.fromisoformat(retry_at)
            except ValueError:
                pass
        doc = {**_EMPTY, "fetched_day_ist": None, "fetched_at": None, "error": None, "retry_at": None, **(cache or {})}
        if due and env_flag(FLAG_FETCH_NSE, default=True):
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            errors = []
            for key, download in FEEDS.items():
                try:
                    doc[key] = download(opener, today)
                except Exception as exc:  # network / NSE block / bad payload: keep this feed's last copy
                    log.warning("NSE %s fetch failed: %s", key, exc)
                    errors.append(f"{key}: {exc}")
            if errors:
                doc.update(error="; ".join(errors), retry_at=(_now() + RETRY_BACKOFF).isoformat(timespec="seconds"))
            else:
                doc.update(fetched_day_ist=today.isoformat(), fetched_at=_now().isoformat(timespec="seconds"),
                           error=None, retry_at=None)
            _write_cache(doc)
        _memo["doc"] = doc
        return doc


def _refresh_in_background() -> None:
    try:
        refresh()
    finally:
        _refreshing.clear()


def _current() -> dict[str, Any]:
    """Saved events without blocking: a new IST day kicks off one background refresh."""
    if _memo["doc"] is None:
        with _lock:
            if _memo["doc"] is None:
                cache = _read_cache()
                _memo["doc"] = {**_EMPTY, "fetched_day_ist": None, "fetched_at": None, "error": None, **(cache or {})}
                if (cache or {}).get("fetched_day_ist") == _today_ist().isoformat():
                    _memo["day"] = _today_ist().isoformat()  # already downloaded today (before a restart)
    today = _today_ist().isoformat()
    if _memo["day"] != today and env_flag(FLAG_FETCH_NSE, default=True):
        _memo["day"] = today
        if not _refreshing.is_set():
            _refreshing.set()
            threading.Thread(target=_refresh_in_background, name="nse-events", daemon=True).start()
    return _memo["doc"]


def upcoming(days: int = 14, refresh_now: bool = False, today: Optional[date] = None) -> dict[str, Any]:
    """Events from LOOKBACK_DAYS ago to ``days`` ahead (IST dates), grouped by ``NSE:SYMBOL``."""
    doc = refresh(force=True) if refresh_now else _current()
    today = today or _today_ist()
    start, end = today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=max(0, days))
    by_symbol: dict[str, list[dict[str, str]]] = {}
    seen = set()
    for event in sorted(doc.get("events") or [], key=lambda e: (e.get("date", ""), e.get("kind", ""))):
        key = (event.get("symbol"), event.get("date"), event.get("kind"), event.get("title"))
        if key in seen or not (start.isoformat() <= str(event.get("date")) <= end.isoformat()):
            continue
        seen.add(key)
        by_symbol.setdefault(str(event["symbol"]), []).append(
            {"date": event["date"], "kind": event["kind"], "title": event.get("title", "")})
    expiries = doc.get("expiries") or {}
    upcoming_days = lambda key: [d for d in expiries.get(key) or [] if d >= today.isoformat()]  # noqa: E731
    return {
        "today": today.isoformat(),
        "days": days,
        "events": by_symbol,
        "fno_expiries": upcoming_days("stock"),
        "index_expiries": upcoming_days("index"),
        "fetched_at": doc.get("fetched_at"),
        "error": doc.get("error"),
        "url": PAGE_URL,
    }


def info() -> dict[str, Any]:
    """Cache status for the health page (no download)."""
    doc = _memo["doc"] or _read_cache() or {}
    return {"fetched_at": doc.get("fetched_at"), "error": doc.get("error"),
            "retry_at": doc.get("retry_at"), "events": len(doc.get("events") or []),
            "expiries": len((doc.get("expiries") or {}).get("stock") or [])}
