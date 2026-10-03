"""NSE events (results / board meetings / ex-dates) and the calculated F&O expiry."""
from __future__ import annotations

from datetime import date

import pytest

from market_data import nse_events


@pytest.fixture(autouse=True)
def offline_events(tmp_path, monkeypatch):
    monkeypatch.setattr(nse_events, "CACHE_PATH", tmp_path / "nse_events.json")
    monkeypatch.setattr(nse_events, "_memo", {"day": None, "doc": None})
    monkeypatch.setenv("FETCH_NSE_DATA", "false")


def test_parse_expiries_from_contract_info():
    raw = {"expiryDates": ["27-Oct-2026", "06-Oct-2026", "19-Oct-2026", "bad"], "strikePrice": ["100"]}
    assert nse_events.parse_expiries(raw) == ["2026-10-06", "2026-10-19", "2026-10-27"]
    assert nse_events.parse_expiries({"error": "blocked"}) == []


def test_parsers_classify_and_prefix_symbols():
    meetings = nse_events.parse_board_meetings([
        {"symbol": "lccproject", "purpose": "Financial  Results", "date": "05-Oct-2026"},
        {"symbol": "OLAELEC", "purpose": "Fund Raising", "date": "05-Oct-2026"},
        {"symbol": "BAD", "purpose": "Financial Results", "date": "n/a"},
    ])
    assert meetings == [
        {"symbol": "NSE:LCCPROJECT", "date": "2026-10-05", "kind": "results", "title": "Financial Results"},
        {"symbol": "NSE:OLAELEC", "date": "2026-10-05", "kind": "board", "title": "Fund Raising"},
    ]
    actions = nse_events.parse_corporate_actions([
        {"symbol": "NMDC", "subject": "Dividend - Re 1 Per Share", "exDate": "05-Oct-2026"},
        {"symbol": "X", "subject": "Bonus", "exDate": "-"},
    ])
    assert actions == [{"symbol": "NSE:NMDC", "date": "2026-10-05", "kind": "ex_date", "title": "Dividend - Re 1 Per Share"}]
    assert nse_events.parse_board_meetings({"error": "blocked"}) == []


def test_upcoming_groups_by_symbol_within_window(monkeypatch):
    doc = {"events": [
        {"symbol": "NSE:AAA", "date": "2026-10-06", "kind": "results", "title": "Financial Results"},
        {"symbol": "NSE:AAA", "date": "2026-10-06", "kind": "results", "title": "Financial Results"},  # duplicate
        {"symbol": "NSE:AAA", "date": "2026-09-30", "kind": "ex_date", "title": "Dividend"},
        {"symbol": "NSE:BBB", "date": "2026-11-30", "kind": "ex_date", "title": "Bonus"},  # beyond 14 days
        {"symbol": "NSE:CCC", "date": "2026-09-01", "kind": "board", "title": "Old"},  # before lookback
    ], "expiries": {"stock": ["2026-09-29", "2026-10-27", "2026-11-23"], "index": ["2026-10-06", "2026-10-19"]},
        "fetched_at": "2026-10-03T19:00:00+00:00", "error": None}
    monkeypatch.setattr(nse_events, "_memo", {"day": "2026-10-04", "doc": doc})
    out = nse_events.upcoming(14, today=date(2026, 10, 4))
    assert out["events"] == {"NSE:AAA": [
        {"date": "2026-09-30", "kind": "ex_date", "title": "Dividend"},
        {"date": "2026-10-06", "kind": "results", "title": "Financial Results"},
    ]}
    assert out["fno_expiries"] == ["2026-10-27", "2026-11-23"]  # past expiries dropped
    assert out["index_expiries"] == ["2026-10-06", "2026-10-19"]


def test_failed_feed_keeps_its_last_copy_and_backs_off(monkeypatch):
    monkeypatch.setenv("FETCH_NSE_DATA", "true")
    nse_events._write_cache({"events": [{"symbol": "NSE:AAA", "date": "2026-10-06", "kind": "results", "title": "R"}],
                             "expiries": {"stock": ["2026-10-27"]},
                             "fetched_day_ist": "2026-10-01", "fetched_at": "x", "error": None, "retry_at": None})

    def boom(opener, today):
        raise OSError("403 Forbidden")

    fresh_events = [{"symbol": "NSE:BBB", "date": "2026-10-07", "kind": "ex_date", "title": "Bonus"}]
    monkeypatch.setattr(nse_events, "FEEDS", {"events": lambda opener, today: fresh_events, "expiries": boom})
    doc = nse_events.refresh()
    assert doc["events"] == fresh_events  # the working feed is updated
    assert doc["expiries"] == {"stock": ["2026-10-27"]}  # the failed one keeps its saved copy
    assert doc["error"] == "expiries: 403 Forbidden" and doc["retry_at"]
    calls = []
    monkeypatch.setattr(nse_events, "FEEDS", {"events": lambda opener, today: calls.append(1) or []})
    nse_events.refresh()  # inside the backoff: no new download
    assert calls == []
