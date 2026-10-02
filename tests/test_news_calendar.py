"""Economic calendar: TradingView first, ForexFactory fallback, once-per-IST-day cache, backoff."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from api import main as api_main

news = api_main.news_calendar

TV = {"status": "ok", "result": [
    {"title": "Non Farm Payrolls", "country": "US", "currency": "USD", "date": "2026-10-02T12:30:00.000Z", "importance": 1, "forecast": 90, "previous": 133, "scale": "K", "unit": None},
    {"title": "Inflation Rate YoY Prel", "country": "DE", "currency": "EUR", "date": "2026-09-30T12:00:00.000Z", "importance": 1, "forecast": 3.2, "previous": 2.9, "scale": None, "unit": "%"},
    {"title": "Balance of Trade", "country": "AU", "currency": "AUD", "date": "2026-10-01T01:30:00.000Z", "importance": 1, "forecast": 2, "previous": 1.351, "scale": "B", "unit": "A$"},
    {"title": "Retail Sales MoM", "country": "US", "currency": "USD", "date": "2026-10-01T12:30:00.000Z", "importance": 0},
    {"title": "EIA Crude Oil Stocks Change", "country": "US", "currency": "USD", "date": "2026-10-15T16:00:00.000Z", "importance": 0},
    {"title": "EIA Natural Gas Stocks Change", "country": "US", "currency": "USD", "date": "2026-10-08T14:30:00.000Z", "importance": -1},
]}
FF = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High", "forecast": "150K", "previous": "142K"},
    {"title": "SPPI y/y", "country": "JPY", "date": "2026-09-27T19:50:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "CPI Flash Estimate y/y", "country": "EUR", "date": "2026-10-01T05:00:00-04:00", "impact": "High", "forecast": "2.1%", "previous": "2.0%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-09-30T10:30:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Natural Gas Storage", "country": "USD", "date": "2026-10-01T10:30:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
]


@pytest.fixture()
def feeds(tmp_path, monkeypatch):
    """Both sources up by default; set state["tv"] / state["ff"] to an exception to fail one."""
    state = {"tv": None, "ff": None, "calls": []}
    now = [datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)]

    def fake_urlopen(request, timeout=0):
        source = "tv" if request.full_url.startswith(news.TV_URL) else "ff"
        state["calls"].append(source)
        if state[source] is not None:
            raise state[source]
        return io.BytesIO(json.dumps(TV if source == "tv" else FF).encode("utf-8"))

    monkeypatch.setattr(news, "CACHE_PATH", tmp_path / "economic_calendar.json")
    monkeypatch.setattr(news, "_now", lambda: now[0])
    monkeypatch.setattr(news.urllib.request, "urlopen", fake_urlopen)
    state["now"] = now
    return state


def test_tradingview_high_events_and_inventory_times(feeds):
    result = news.get_events()
    assert result["source"] == "tradingview" and not result["stale"]
    assert [(e["currency"], e["title"], e["time_utc"], e["forecast"], e["previous"]) for e in result["events"]] == [
        ("EUR", "DE Inflation Rate YoY Prel", "2026-09-30T12:00:00Z", "3.2%", "2.9%"),
        ("AUD", "Balance of Trade", "2026-10-01T01:30:00Z", "A$2B", "A$1.351B"),
        ("USD", "Non Farm Payrolls", "2026-10-02T12:30:00Z", "90K", "133K"),
    ]
    assert result["inventory"] == [
        {"report": "natgas", "time_utc": "2026-10-08T14:30:00Z"},
        {"report": "crude", "time_utc": "2026-10-15T16:00:00Z"},
    ]
    assert feeds["calls"] == ["tv"]


def test_forexfactory_fallback_when_tradingview_fails(feeds):
    feeds["tv"] = OSError("tv down")
    result = news.get_events()
    assert result["source"] == "forexfactory" and not result["stale"]
    assert [(e["currency"], e["time_utc"]) for e in result["events"]] == [
        ("EUR", "2026-10-01T09:00:00Z"),
        ("USD", "2026-10-02T12:30:00Z"),
    ]
    assert result["inventory"] == [
        {"report": "crude", "time_utc": "2026-09-30T14:30:00Z"},
        {"report": "natgas", "time_utc": "2026-10-01T14:30:00Z"},
    ]
    assert feeds["calls"] == ["tv", "ff"]


def test_fetches_once_per_ist_day_unless_refreshed(feeds):
    news.get_events()
    news.get_events()
    assert len(feeds["calls"]) == 1
    news.get_events(refresh=True)
    assert len(feeds["calls"]) == 2
    feeds["now"][0] += timedelta(days=1)
    news.get_events()
    assert len(feeds["calls"]) == 3


def test_currency_filter_applied_on_read(feeds):
    assert [e["currency"] for e in news.get_events(["usd"])["events"]] == ["USD"]
    assert len(feeds["calls"]) == 1


def test_both_failing_serves_stale_cache_without_retry(feeds):
    news.get_events()
    feeds["tv"] = feeds["ff"] = OSError("offline")
    result = news.get_events(refresh=True)
    assert result["stale"] and len(result["events"]) == 3 and len(result["inventory"]) == 2
    news.get_events()
    assert feeds["calls"].count("ff") == 1


def test_both_failing_first_fetch_backs_off_honouring_retry_after(feeds):
    headers = Message()
    headers["Retry-After"] = "3600"
    feeds["tv"] = OSError("tv down")
    feeds["ff"] = news.urllib.error.HTTPError(news.FF_URL, 429, "Too Many Requests", headers, None)
    result = news.get_events()
    assert result["stale"] and result["events"] == [] and result["inventory"] == [] and result["fetched_at"] is None
    news.get_events(refresh=True)  # inside Retry-After: no new request
    assert feeds["calls"] == ["tv", "ff"]

    feeds["now"][0] += timedelta(seconds=3601)
    feeds["tv"] = None
    result = news.get_events()
    assert result["source"] == "tradingview" and not result["stale"]


def test_settings_currency_filter_is_validated():
    settings = api_main.app_settings._clamp(api_main.app_settings._merge(api_main.app_settings.DEFAULTS, {"news": {"currencies": ["usd", "XYZ", "EUR"]}}))
    assert settings["news"]["currencies"] == ["EUR", "USD"]
