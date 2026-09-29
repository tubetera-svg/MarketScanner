"""ForexFactory high-impact news: High-only filter, once-per-IST-day cache, live refresh."""

from __future__ import annotations

import io
import json

import pytest

from api import main as api_main

news = api_main.news_calendar

FEED = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High", "forecast": "150K", "previous": "142K"},
    {"title": "SPPI y/y", "country": "JPY", "date": "2026-09-27T19:50:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "CPI Flash Estimate y/y", "country": "EUR", "date": "2026-10-01T05:00:00-04:00", "impact": "High", "forecast": "2.1%", "previous": "2.0%"},
    {"title": "Bank Holiday", "country": "CNY", "date": "2026-10-01T00:00:00-04:00", "impact": "Holiday", "forecast": "", "previous": ""},
]


@pytest.fixture()
def feed(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(FEED).encode("utf-8"))

    monkeypatch.setattr(news, "CACHE_PATH", tmp_path / "ff_high_impact.json")
    monkeypatch.setattr(news.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_keeps_only_high_impact_sorted_utc(feed):
    result = news.get_events()
    assert [(e["currency"], e["time_utc"]) for e in result["events"]] == [
        ("EUR", "2026-10-01T09:00:00Z"),
        ("USD", "2026-10-02T12:30:00Z"),
    ]
    assert result["available_currencies"] == ["EUR", "USD"]


def test_fetches_once_per_ist_day_unless_refreshed(feed, monkeypatch):
    news.get_events()
    news.get_events()
    assert len(feed) == 1
    news.get_events(refresh=True)
    assert len(feed) == 2
    monkeypatch.setattr(news, "_today_ist", lambda: "2099-01-01")
    news.get_events()
    assert len(feed) == 3


def test_currency_filter_applied_on_read(feed):
    assert [e["currency"] for e in news.get_events(["usd"])["events"]] == ["USD"]
    assert len(feed) == 1


def test_failed_fetch_serves_stale_cache_without_retry(feed, monkeypatch):
    news.get_events()

    def boom(request, timeout=0):
        feed.append("fail")
        raise OSError("offline")

    monkeypatch.setattr(news.urllib.request, "urlopen", boom)
    result = news.get_events(refresh=True)
    assert result["stale"] and len(result["events"]) == 2
    news.get_events()
    assert feed.count("fail") == 1


def test_settings_currency_filter_is_validated():
    settings = api_main.app_settings._clamp(api_main.app_settings._merge(api_main.app_settings.DEFAULTS, {"news": {"currencies": ["usd", "XYZ", "EUR"]}}))
    assert settings["news"]["currencies"] == ["EUR", "USD"]
