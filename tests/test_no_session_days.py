"""Days with no session upstream (NSE IX / forex holidays) are marked "no data", not retried."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from market_data import database
from market_data.errors import FetchError
from market_data.service import _closed_days, bar_final_at, get_ohlc, register_fetcher, reset_fetchers

GIFT = "NSEIX:NIFTY1!"


@pytest.fixture(autouse=True)
def _restore_fetchers():
    yield
    reset_fetchers()


def bar(day: date, symbol: str = GIFT, close: float = 22500.0) -> dict:
    return {"source": "TRADINGVIEW", "symbol": symbol, "exchange": symbol.split(":")[0], "date": day.isoformat(),
            "open": close - 10, "high": close + 50, "low": close - 50, "close": close, "volume": 1.0}


def feed(*days: date):
    """Fake TradingView feed: returns these bars, whatever window is asked for (like tvDatafeed's last-N bars)."""
    calls = []

    def fetch(spec):
        calls.append(spec)
        return [bar(d, spec.get("store_symbol") or GIFT) for d in days]

    return fetch, calls


def test_friday_holiday_marked_no_data_once_final_for_a_day(tmp_db, clean_flags):
    # Fri 2026-09-25 missing; the feed (which answered) ends Thu 09-24.
    fetch, calls = feed(date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24))
    register_fetcher("TRADINGVIEW", fetch)
    day = date(2026, 9, 25)
    result = get_ohlc("TRADINGVIEW", GIFT, day, day, db_path=tmp_db)
    assert result.rows == [] and result.missing_dates == []
    assert result.no_data_dates == ["2026-09-25"]
    assert database.no_data_dates("TRADINGVIEW", GIFT, day, day, exchange="NSEIX", db_path=tmp_db) == {"2026-09-25"}
    get_ohlc("TRADINGVIEW", GIFT, day, day, db_path=tmp_db)
    assert len(calls) == 1  # known closed day: never fetched again


def test_later_bar_proves_the_missing_day_was_closed(tmp_db, clean_flags):
    database.upsert_ohlc([bar(date(2026, 9, 21)), bar(date(2026, 9, 22))], db_path=tmp_db)
    # Window Mon-Wed, Wed missing; the feed has Thu but no Wed.
    fetch, _ = feed(date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 24))
    register_fetcher("TRADINGVIEW", fetch)
    result = get_ohlc("TRADINGVIEW", GIFT, date(2026, 9, 21), date(2026, 9, 23), db_path=tmp_db)
    assert [r["date"] for r in result.rows] == ["2026-09-21", "2026-09-22"]
    assert result.no_data_dates == ["2026-09-23"]
    assert database.query_ohlc("TRADINGVIEW", GIFT, date(2026, 9, 24), date(2026, 9, 24), db_path=tmp_db) == []  # outside window: not stored


def test_empty_feed_proves_nothing(tmp_db, clean_flags):
    fetch, _ = feed()
    register_fetcher("TRADINGVIEW", fetch)
    day = date(2026, 9, 25)
    with pytest.raises(FetchError):
        get_ohlc("TRADINGVIEW", GIFT, day, day, db_path=tmp_db)
    assert database.no_data_dates("TRADINGVIEW", GIFT, day, day, exchange="NSEIX", db_path=tmp_db) == set()


def test_grace_period_before_calling_a_day_closed():
    day = date(2026, 9, 25)
    final = bar_final_at("TRADINGVIEW", GIFT, day)  # 03:00 IST Sat 09-26
    assert _closed_days("TRADINGVIEW", GIFT, [day], "2026-09-24", now=final + timedelta(hours=1)) == []
    assert _closed_days("TRADINGVIEW", GIFT, [day], "2026-09-24", now=final + timedelta(hours=25)) == ["2026-09-25"]
    assert _closed_days("TRADINGVIEW", GIFT, [day], "2026-09-28", now=final) == ["2026-09-25"]  # later bar: no wait
    assert _closed_days("TRADINGVIEW", GIFT, [day], None, now=final + timedelta(days=5)) == []
    # NSE needs a later bar: its holidays come from the NSE calendar instead.
    assert _closed_days("NSE", "NSE:INFY", [day], "2026-09-24", now=final + timedelta(days=5)) == []


def test_auto_sync_settles_a_closed_session_in_one_attempt(tmp_db, clean_flags):
    import market_data.auto_sync as auto_sync
    import market_data.service as service

    target = date(2026, 9, 25)
    for module in (service, auto_sync):
        clean_flags.setattr(module, "latest_final_session", lambda source, now=None, symbol=None: target)
    fetch, calls = feed(date(2026, 9, 24))
    register_fetcher("TRADINGVIEW", fetch)
    syncer = auto_sync.DataAutoSync(lambda: [(GIFT, "forex_24_5")])
    syncer.lookback_days = 1
    report = syncer.run_once()
    state = syncer.markets["NSEIX"]
    assert report["NSEIX"]["failed"] == 0
    assert state["last_synced_session"] == "2026-09-25" and state["attempts"] == 1
    assert "1 closed (no session)" in state["last_result"]
    assert syncer.run_once()["NSEIX"]["skipped"] is True
    assert len(calls) == 1
