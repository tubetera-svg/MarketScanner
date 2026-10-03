"""Shared NSE (CM segment) holiday calendar: parse, once-per-IST-day refresh, fallback."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from market_data import nse_holidays

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

NSE_PAYLOAD = {
    "CM": [
        {"tradingDate": "02-Oct-2026", "weekDay": "Friday", "description": "Mahatma Gandhi Jayanti"},
        {"tradingDate": "10-Nov-2026", "weekDay": "Tuesday", "description": "Diwali-Balipratipada"},
        {"tradingDate": "bad", "description": "skipped"},
    ],
    "FO": [{"tradingDate": "01-Jan-2026", "description": "other segment"}],
}


def _clock(monkeypatch, iso):
    monkeypatch.setenv("FETCH_NSE_DATA", "true")
    monkeypatch.setattr(nse_holidays, "_now", lambda: datetime.fromisoformat(iso).astimezone(timezone.utc))


def test_parse_keeps_only_cm_segment():
    assert nse_holidays.parse_nse(NSE_PAYLOAD) == {
        "2026-10-02": "Mahatma Gandhi Jayanti", "2026-11-10": "Diwali-Balipratipada",
    }


def test_builtin_list_until_downloaded_and_shared_view():
    import all_strategy
    import ict_scanner

    assert nse_holidays.info()["source"] == "builtin"
    assert date(2026, 10, 2) in nse_holidays.NSE_HOLIDAYS
    assert datetime(2026, 11, 10, 9, 30) in ict_scanner.NSE_HOLIDAYS
    assert date(2026, 11, 9) not in all_strategy.NSE_HOLIDAYS
    assert ict_scanner.resolve_previous_working_date(date(2026, 10, 3))[1] == date(2026, 10, 1)


def test_nse_market_closed_on_holiday():
    import ict_scanner
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    assert not ict_scanner.is_nse_market_open(datetime(2026, 10, 2, 11, 0, tzinfo=ist))  # Gandhi Jayanti
    assert not ict_scanner.is_market_open(ict_scanner.Session.NSE, datetime(2026, 10, 2, 11, 0))
    assert ict_scanner.is_nse_market_open(datetime(2026, 10, 1, 11, 0, tzinfo=ist))
    # 05:30 UTC on 1 Oct is 11:00 IST on a trading day, whatever the host zone.
    assert ict_scanner.is_nse_market_open(datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc))
    assert not ict_scanner.is_nse_market_open(datetime(2026, 10, 2, 5, 30, tzinfo=timezone.utc))


def test_downloads_once_per_ist_day_and_keeps_older_years(monkeypatch):
    calls = []
    monkeypatch.setattr(nse_holidays, "_download", lambda: calls.append(1) or nse_holidays.parse_nse(NSE_PAYLOAD))
    # 20:00 UTC on 3 Oct = 4 Oct 01:30 IST: the IST day is what counts.
    _clock(monkeypatch, "2026-10-03T20:00:00+00:00")
    nse_holidays.CACHE_PATH.write_text('{"holidays": {"2025-12-25": "Christmas"}, "fetched_day_ist": "2026-10-03"}')

    doc = nse_holidays.refresh()
    assert len(calls) == 1 and doc["source"] == "nse"
    assert doc["holidays"]["2025-12-25"] == "Christmas"  # previous year kept
    assert "2026-01-26" not in doc["holidays"]  # NSE's year replaces the built-in one
    nse_holidays.refresh()
    assert len(calls) == 1  # same IST day: no second download


def test_failed_download_falls_back_and_backs_off(monkeypatch):
    calls = []

    def fail():
        calls.append(1)
        raise OSError("403 Forbidden")

    monkeypatch.setattr(nse_holidays, "_download", fail)
    _clock(monkeypatch, "2026-10-04T03:00:00+00:00")
    doc = nse_holidays.refresh()
    assert doc["source"] == "builtin" and "403" in doc["error"]
    assert "2026-10-02" in doc["holidays"]
    _clock(monkeypatch, "2026-10-05T03:00:00+00:00")  # next day, past the 3h backoff
    nse_holidays.refresh()
    assert len(calls) == 2
    _clock(monkeypatch, "2026-10-05T04:00:00+00:00")  # 1h after that failure: still backing off
    nse_holidays.refresh(force=False)
    assert len(calls) == 2


def test_fetch_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FETCH_NSE_DATA", "false")
    monkeypatch.setattr(nse_holidays, "_download", lambda: (_ for _ in ()).throw(AssertionError("no network")))
    assert nse_holidays.refresh()["source"] == "builtin"


def test_hot_path_starts_at_most_one_refresh_per_ist_day(monkeypatch):
    started = []
    monkeypatch.setattr(nse_holidays, "_start_refresh", lambda: started.append(1))
    _clock(monkeypatch, "2026-10-03T10:00:00+00:00")
    for _ in range(50):
        nse_holidays.is_holiday(date(2026, 10, 2))
    assert started == [1]
    _clock(monkeypatch, "2026-10-03T19:00:00+00:00")  # 00:30 IST on 4 Oct: new IST day
    nse_holidays.is_holiday(date(2026, 10, 2))
    assert started == [1, 1]
