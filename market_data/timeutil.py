"""Time contract helpers — see AGENTS.md "Time & Timezone Contract".

* Instants (bar open times, alerts, news, last-run stamps) are timezone-aware
  and serialized as UTC ISO-8601 (``2026-10-03T04:15:00+00:00``).
* Trading-day dates (daily bars, signal dates) are plain ``YYYY-MM-DD`` in the
  market's own calendar and are never timezone-converted.
* Market logic uses fixed market zones (NSE = IST, forex/commodities = New
  York, crypto = UTC). The user-selectable *display* zone only affects how an
  instant is formatted for people (UI, push messages).
* Legacy naive datetimes are IST wall time (the app's former convention).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

DEFAULT_DISPLAY_TIMEZONE = "Asia/Kolkata"
# Value -> label for the Settings dropdown. "browser" = the viewer's own zone
# (UI only; server-side formatting such as push messages falls back to IST).
DISPLAY_TIMEZONES: dict[str, str] = {
    "Asia/Kolkata": "IST (India)",
    "America/New_York": "New York",
    "Europe/London": "London",
    "UTC": "UTC",
    "Asia/Singapore": "Singapore",
    "Asia/Dubai": "Dubai",
    "browser": "Browser local",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_instant(value: object, naive_tz: ZoneInfo = IST) -> Optional[datetime]:
    """str/datetime -> aware UTC datetime. Naive input is ``naive_tz`` wall time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if hasattr(parsed, "to_pydatetime"):  # pandas Timestamp
        parsed = parsed.to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed.astimezone(UTC)


def to_utc_iso(value: object, naive_tz: ZoneInfo = IST) -> Optional[str]:
    """Serialize an instant as UTC ISO-8601 with offset (seconds precision)."""
    parsed = parse_instant(value, naive_tz)
    return parsed.isoformat(timespec="seconds") if parsed else None


def machine_local_to_utc(value: datetime) -> datetime:
    """A naive *host-local* timestamp (what tvDatafeed returns) -> aware UTC."""
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    return value.astimezone(UTC)  # naive -> interpreted as host local time


def resolve_display_zone(name: Optional[str]) -> ZoneInfo:
    """Valid IANA zone for server-side formatting ("browser"/unknown -> IST)."""
    if not name or name == "browser":
        return IST
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return IST


def fmt_display(value: object, zone_name: Optional[str] = None, fmt: str = "%d %b %H:%M") -> str:
    """Format an instant for people in the display zone, with its abbreviation.

    Text that is not an ISO instant is returned unchanged (never dropped).
    """
    parsed = parse_instant(value)
    if parsed is None:
        return "" if value is None else str(value)
    local = parsed.astimezone(resolve_display_zone(zone_name))
    return f"{local.strftime(fmt)} {local.tzname()}"
