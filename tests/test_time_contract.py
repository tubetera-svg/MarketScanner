"""Time & Timezone Contract (AGENTS.md): helpers behave, and banned patterns stay out.

Backend: no host-local clock reads (date.today(), naive datetime.now(), ...).
Frontend: dates are formatted/derived only in frontend/components/time.ts.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from market_data import timeutil  # noqa: E402

BACKEND_BANNED = re.compile(
    r"date\.today\(\)|datetime\.now\(\)|datetime\.utcnow\(|datetime\.today\(\)|\.astimezone\(\)"
)
FRONTEND_BANNED = re.compile(
    r"toLocaleTimeString|toLocaleDateString|new Date\([^)]*\)\.toLocaleString|toISOString\(\)\.slice"
    r"|\.get(Hours|Minutes|Date|Day|Month|FullYear)\(\)|new Intl\.DateTimeFormat|IST_OFFSET"
)
FRONTEND_TIME_MODULE = ROOT / "frontend" / "components" / "time.ts"


def _offenders(paths, pattern):
    found = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    return found


def test_backend_never_reads_the_host_local_clock():
    files = [p for d in ("src", "api", "market_data") for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts]
    files.append(ROOT / "main.py")
    offenders = _offenders(files, BACKEND_BANNED)
    assert not offenders, (
        "Use market_data.service.market_today()/ist_today() or timeutil.utc_now() "
        "(AGENTS.md Time & Timezone Contract):\n" + "\n".join(offenders)
    )


def test_frontend_formats_dates_only_in_time_module():
    files = [
        p for d in ("app", "components") for p in (ROOT / "frontend" / d).rglob("*.ts*")
        if p.resolve() != FRONTEND_TIME_MODULE.resolve()
    ]
    offenders = _offenders(files, FRONTEND_BANNED)
    assert not offenders, (
        "Format/derive dates via frontend/components/time.ts (AGENTS.md Time & Timezone Contract):\n"
        + "\n".join(offenders)
    )


# ------------------------------------------------------------------ helpers

def test_parse_instant_treats_naive_as_ist_and_returns_utc():
    assert timeutil.parse_instant("2026-10-03T09:45") == datetime(2026, 10, 3, 4, 15, tzinfo=timezone.utc)
    assert timeutil.parse_instant("2026-10-03T04:15:00Z") == datetime(2026, 10, 3, 4, 15, tzinfo=timezone.utc)
    assert timeutil.parse_instant("2026-10-03T00:15:00-04:00") == datetime(2026, 10, 3, 4, 15, tzinfo=timezone.utc)
    assert timeutil.parse_instant("not a time") is None
    assert timeutil.to_utc_iso("2026-10-03T09:45") == "2026-10-03T04:15:00+00:00"


def test_machine_local_to_utc_is_host_independent_for_aware_input():
    aware = datetime(2026, 10, 3, 18, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert timeutil.machine_local_to_utc(aware) == datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)


def test_fmt_display_uses_zone_and_label():
    instant = "2026-10-03T04:15:00+00:00"
    assert timeutil.fmt_display(instant, "Asia/Kolkata") == "03 Oct 09:45 IST"
    assert timeutil.fmt_display(instant, "America/New_York") == "03 Oct 00:15 EDT"
    assert timeutil.fmt_display(instant, "browser") == "03 Oct 09:45 IST"  # server falls back to IST
    assert timeutil.fmt_display(instant, "Not/AZone") == "03 Oct 09:45 IST"
    assert timeutil.fmt_display("10:25") == "10:25"  # non-instant text passes through


def test_display_timezone_setting_defaults_and_validates(monkeypatch):
    import app_settings

    stored = {}
    monkeypatch.setattr(app_settings.state_store, "read_text", lambda path: stored.get("text", "{}"))
    monkeypatch.setattr(app_settings.state_store, "write_text", lambda path, text: stored.update(text=text))

    assert app_settings.load_settings()["ui"]["display_timezone"] == "Asia/Kolkata"
    assert app_settings.save_settings({"ui": {"display_timezone": "America/New_York"}})["ui"]["display_timezone"] == "America/New_York"
    assert app_settings.display_timezone() == "America/New_York"
    assert app_settings.save_settings({"ui": {"display_timezone": "Mars/Base"}})["ui"]["display_timezone"] == "Asia/Kolkata"
    assert set(timeutil.DISPLAY_TIMEZONES) >= {"Asia/Kolkata", "browser"}


def test_market_dates_are_never_shifted_by_display_zone():
    """A trading-day date is a calendar label, not an instant."""
    from market_data import service

    instant = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)
    assert service.market_today("NSE", "NSE:INFY", now=instant) == date(2026, 10, 4)
    assert service.market_today("TRADINGVIEW", "OANDA:XAUUSD", now=instant) == date(2026, 10, 3)
