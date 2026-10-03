"""Tests for the SQLite market-data layer.

Covers: duplicate prevention, cached requests (no fetch), missing-date
fetching (only what is absent), independent source flags, WAL mode and the
FastAPI /ohlc endpoint.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from market_data import database
from market_data.errors import FetchError, NoDataError
from market_data.service import (
    get_ohlc,
    register_fetcher,
    reset_fetchers,
    resolve_session_source,
    sync_symbol_range,
)


@pytest.fixture(autouse=True)
def _restore_fetchers():
    yield
    reset_fetchers()


# Fixed past window (Mon 2026-09-21 .. Wed 2026-09-30) with no NSE holidays, so
# "every weekday is a trading day" holds regardless of the host clock.
FIXED_START, FIXED_END = date(2026, 9, 21), date(2026, 9, 30)


def weekdays(start: date, end: date) -> list[date]:
    days = (start + timedelta(offset) for offset in range((end - start).days + 1))
    return [day for day in days if day.weekday() < 5]


def row(source, symbol, exchange, day, close=100.0):
    return {
        "source": source,
        "symbol": symbol,
        "exchange": exchange,
        "date": day.isoformat(),
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 12345.0,
    }


def seed_range(source, symbol, exchange, days):
    database.upsert_ohlc([row(source, symbol, exchange, day) for day in days])


# ---------------------------------------------------------------- duplicates
def test_duplicate_prevention(tmp_db):
    record = row("NSE", "RELIANCE", "NSE", FIXED_START)

    assert database.upsert_ohlc([record]) == 1
    # Same (source, exchange, symbol, date) again -> no new row, values kept
    assert database.upsert_ohlc([record]) == 0
    assert database.upsert_ohlc([record | {"close": 999.0}]) == 0
    assert database.count_rows(tmp_db) == 1

    stored = database.query_ohlc("NSE", "RELIANCE", db_path=tmp_db)
    assert len(stored) == 1 and stored[0]["close"] == pytest.approx(record["close"])

    # Concurrent writers must not create duplicates either
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: database.upsert_ohlc([record]), range(8)))
    assert sum(results) == 0
    assert database.count_rows(tmp_db) == 1


# ------------------------------------------------------------- cached reads
def test_cached_request_skips_fetch(tmp_db, clean_flags):
    start, end = FIXED_START, FIXED_END
    expected = weekdays(start, end)
    seed_range("NSE", "RELIANCE", "NSE", expected)

    calls = []

    def spy_fetch(spec):
        calls.append(spec)
        return []

    register_fetcher("NSE", spy_fetch)

    result = get_ohlc("NSE", "RELIANCE", start, end, db_path=tmp_db)
    assert result.cached is True
    assert result.fetched_new == 0
    assert result.missing_dates == []
    assert calls == []  # SQLite had everything -> zero API calls


# ------------------------------------------------------- missing-date fetch
def test_missing_dates_are_fetched_and_stored(tmp_db, clean_flags):
    start, end = FIXED_START, FIXED_END
    all_days = weekdays(start, end)
    have_days = all_days[:-2]  # last two trading days missing
    seed_range("NSE", "RELIANCE", "NSE", have_days)
    missing = [d for d in all_days if d not in set(have_days)]

    seen = {}

    def fake_nse_fetch(spec):
        seen.update(spec)
        return [row("NSE", "RELIANCE", "NSE", day, close=110.0) for day in spec["dates"]]

    register_fetcher("NSE", fake_nse_fetch)

    result = get_ohlc("NSE", "RELIANCE", start, end, db_path=tmp_db)
    assert result.cached is False
    assert result.fetched_new == len(missing)
    # Only the missing dates were requested from the source
    assert [d.isoformat() for d in seen["dates"]] == [d.isoformat() for d in missing]
    assert result.missing_dates == []
    assert [r["date"] for r in result.rows] == [d.isoformat() for d in all_days]

    # Second request is fully cached now
    calls = {"n": 0}

    def counting_fetch(spec):
        calls["n"] += 1
        return []

    register_fetcher("NSE", counting_fetch)
    again = get_ohlc("NSE", "RELIANCE", start, end, db_path=tmp_db)
    assert again.cached is True and calls["n"] == 0


# ------------------------------------------------------------ source flags
def test_source_flags_are_independent(tmp_db, clean_flags):
    start, end = FIXED_START, FIXED_END

    nse_calls, tv_calls = [], []

    def fake_nse(spec):
        nse_calls.append(spec)
        return []

    def fake_tv(spec):
        tv_calls.append(spec)
        # Empty upstream data raises FetchError, so return rows to prove the fetch ran.
        return [row("TRADINGVIEW", spec["store_symbol"], "OANDA", day) for day in spec["dates"]]

    register_fetcher("NSE", fake_nse)
    register_fetcher("TRADINGVIEW", fake_tv)

    clean_flags.setenv("FETCH_NSE_DATA", "false")  # NSE off, TradingView on

    with pytest.raises(NoDataError):
        get_ohlc("NSE", "RELIANCE", start, end, db_path=tmp_db)
    assert nse_calls == []  # blocked by flag

    result = get_ohlc("TRADINGVIEW", "OANDA:XAUUSD", start, end, db_path=tmp_db)
    assert result.fetched_new > 0
    assert tv_calls != []  # TradingView still allowed

    # Fresh TV symbol so earlier no-data marks do not short-circuit this check
    clean_flags.setenv("FETCH_TRADINGVIEW_DATA", "false")
    clean_flags.setenv("FETCH_NSE_DATA", "true")
    tv_calls.clear()
    with pytest.raises(NoDataError):
        get_ohlc("TRADINGVIEW", "OANDA:XAGUSD", start, end, db_path=tmp_db)
    assert tv_calls == []


# ------------------------------------------------- auto-fetch master switch
def test_auto_fetch_disabled_returns_local_only(tmp_db, clean_flags):
    clean_flags.setenv("AUTO_FETCH_MISSING_DATA", "false")
    start, end = FIXED_START, FIXED_END
    days = weekdays(start, end)

    calls = []

    def spy(spec):
        calls.append(spec)
        return []

    register_fetcher("NSE", spy)

    # Partial local data -> served as-is, no fetch attempted
    seed_range("NSE", "TCS", "NSE", days[:-1])
    result = get_ohlc("NSE", "TCS", start, end, db_path=tmp_db)
    assert len(result.rows) == len(days) - 1
    assert result.fetched_new == 0
    assert result.missing_dates != []
    assert any("AUTO_FETCH_MISSING_DATA" in note for note in result.notes)
    assert calls == []

    # No local data at all -> descriptive error, still no fetch
    with pytest.raises(NoDataError) as exc_info:
        get_ohlc("NSE", "INFY", start, end, db_path=tmp_db)
    assert "AUTO_FETCH_MISSING_DATA" in str(exc_info.value)
    assert calls == []


def test_fetch_failure_raises_and_keeps_local_rows(tmp_db, clean_flags):
    start, end = FIXED_START, FIXED_END
    days = weekdays(start, end)
    seed_range("NSE", "SBIN", "NSE", days[:1])

    def broken_fetch(spec):
        raise RuntimeError("upstream down")

    register_fetcher("NSE", broken_fetch)

    with pytest.raises(FetchError) as exc_info:
        get_ohlc("NSE", "SBIN", start, end, db_path=tmp_db)
    assert exc_info.value.rows  # local rows ride along for the API layer


# ------------------------------------------------------------------- sqlite
def test_wal_mode_enabled(tmp_db):
    from market_data.database import connect

    conn = connect(tmp_db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"ohlc_daily", "ohlc_no_data"} <= tables
        indexes = {
            r[1]
            for r in conn.execute("PRAGMA index_list('ohlc_daily')")
        }
        assert any("idx_ohlc" in name for name in indexes)
    finally:
        conn.close()


def test_unique_constraint_columns(tmp_db):
    """Same (source, exchange, symbol, date) collides even across sources."""
    day = FIXED_START
    database.upsert_ohlc([row("NSE", "XYZ", "NSE", day)])
    database.upsert_ohlc([row("TRADINGVIEW", "XYZ", "MCX", day)])  # other source: ok
    assert database.count_rows(tmp_db) == 2
    database.upsert_ohlc([row("NSE", "xyz", "NSE", day)])  # case-insensitive dup
    assert database.count_rows(tmp_db) == 2


def test_delete_ohlc_counts_only_ohlc_rows(tmp_db):
    """No-data markers are removed too but not counted as deleted rows."""
    days = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    database.upsert_ohlc([row("NSE", "XYZ", "NSE", d) for d in days])
    database.mark_no_data([{"source": "NSE", "symbol": "XYZ", "exchange": "NSE", "date": "2026-10-01"}])
    assert database.delete_ohlc(symbols=["XYZ"], start_date="2026-09-29") == 2
    assert database.count_rows(tmp_db) == 1
    assert database.delete_ohlc(symbols=["XYZ"]) == 1
    assert database.delete_ohlc(symbols=["XYZ"]) == 0  # marker already gone


def test_resolve_session_source():
    assert resolve_session_source("NSE:RELIANCE") == "NSE"
    assert resolve_session_source("RELIANCE") == "NSE"
    assert resolve_session_source("OANDA:XAUUSD") == "TRADINGVIEW"
    assert resolve_session_source("FOREXCOM:USOIL") == "TRADINGVIEW"


def test_sync_symbol_range_accepts_explicit_date_range(tmp_db):
    start, end = date(2026, 9, 21), date(2026, 9, 25)
    requested = []

    def fake_nse(spec):
        requested.append(spec)
        return [row("NSE", "RELIANCE", "NSE", day) for day in spec["dates"]]

    register_fetcher("NSE", fake_nse)
    result = sync_symbol_range(
        "NSE", "RELIANCE", end, start_date=start, db_path=tmp_db
    )

    assert result.start_date == start.isoformat()
    assert result.end_date == end.isoformat()
    assert requested[0]["start"] == min(requested[0]["dates"])
    assert requested[0]["end"] == max(requested[0]["dates"])
    assert requested[0]["dates"]


# ------------------------------------------------------------------ endpoint
def test_ohlc_endpoint(tmp_db, clean_flags):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    start, end = FIXED_START, FIXED_END
    days = weekdays(start, end)
    seed_range("NSE", "RELIANCE", "NSE", days)

    def fake_nse(spec):
        return [row("NSE", "RELIANCE", "NSE", day, close=111.0) for day in spec["dates"]]

    register_fetcher("NSE", fake_nse)

    from api import main as api_main

    client = fastapi_testclient.TestClient(api_main.app, client=("127.0.0.1", 50000))

    resp = client.get(
        "/ohlc",
        params={
            "source": "NSE",
            "symbol": "RELIANCE",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == len(days)
    assert body["cached"] is True and body["fetched_new"] == 0
    assert [r["close"] for r in body["rows"]] == [100.0] * len(days)

    # alias route works too
    assert client.get(
        "/api/ohlc",
        params={
            "source": "nse",
            "symbol": "reliance",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    ).status_code == 200

    # unknown source -> 400
    resp = client.get(
        "/ohlc",
        params={
            "source": "BINANCE",
            "symbol": "RELIANCE",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    assert resp.status_code == 400

    # auto-fetch disabled + nothing local -> 404
    clean_flags.setenv("AUTO_FETCH_MISSING_DATA", "false")
    resp = client.get(
        "/api/ohlc",
        params={
            "source": "NSE",
            "symbol": "UNKNOWNCO",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    assert resp.status_code == 404


def test_query_ohlc_multi_groups_by_symbol(tmp_db):
    """query_ohlc_multi fetches many symbols in one query, grouped by symbol."""
    from datetime import date as _date

    base = _date(2026, 1, 5)  # a Monday
    days = [base + timedelta(days=i) for i in range(3)]
    for sym in ("NSE:AAA", "NSE:BBB", "NSE:CCC"):
        database.upsert_ohlc([row("NSE", sym, "NSE", d, close=100.0) for d in days])

    grouped = database.query_ohlc_multi(
        "NSE", ["NSE:AAA", "NSE:BBB", "NSE:CCC"], days[0], days[-1]
    )
    assert set(grouped) == {"NSE:AAA", "NSE:BBB", "NSE:CCC"}
    for sym, rows in grouped.items():
        assert [r["date"] for r in rows] == [d.isoformat() for d in days]

    # Empty symbol list returns no rows without querying.
    assert database.query_ohlc_multi("NSE", [], days[0], days[-1]) == {}


# ------------------------------------------------------------------ per-market sync cut-offs
def test_latest_final_session_uses_each_markets_cutoff():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from market_data.service import latest_final_session

    ist = ZoneInfo("Asia/Kolkata")
    # NSE: Tue 2026-09-22 bar is final only after the 17:00 IST bhavcopy.
    assert latest_final_session("NSE", datetime(2026, 9, 22, 16, 0, tzinfo=ist)) == date(2026, 9, 21)
    assert latest_final_session("NSE", datetime(2026, 9, 22, 17, 30, tzinfo=ist)) == date(2026, 9, 22)
    # Forex: Mon 2026-09-21 bar closes at 17:00 New York (= Tue 02:30 IST), not IST midnight.
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 22, 1, 0, tzinfo=ist)) == date(2026, 9, 18)
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 22, 3, 0, tzinfo=ist)) == date(2026, 9, 21)


def test_gated_sync_trims_to_last_final_session(tmp_db, clean_flags):
    import market_data.service as service

    final = date(2026, 9, 21)
    clean_flags.setattr(service, "latest_final_session", lambda source, now=None, symbol=None: final)
    requested = []

    def fake_tv(spec):
        requested.append(spec)
        return [row("TRADINGVIEW", "OANDA:XAUUSD", "OANDA", day) for day in spec["dates"]]

    register_fetcher("TRADINGVIEW", fake_tv)
    result = sync_symbol_range(
        "TRADINGVIEW", "OANDA:XAUUSD", date(2026, 9, 22), start_date=date(2026, 9, 15),
        gate_market_hours=True, db_path=tmp_db,
    )
    assert result.end_date == final.isoformat()
    assert max(requested[0]["dates"]) == final
    assert any("last completed session" in note for note in result.notes)

    # Range entirely after the last final bar: nothing fetched, no error.
    requested.clear()
    empty = sync_symbol_range(
        "TRADINGVIEW", "OANDA:XAUUSD", date(2026, 9, 22), start_date=date(2026, 9, 22),
        gate_market_hours=True, db_path=tmp_db,
    )
    assert requested == [] and empty.rows == []
    assert empty.notes[0].startswith("Nothing to sync yet")


def test_auto_sync_runs_once_per_final_session(tmp_db, clean_flags):
    import market_data.auto_sync as auto_sync
    import market_data.service as service

    final = FIXED_END
    for module in (service, auto_sync):
        clean_flags.setattr(module, "latest_final_session", lambda source, now=None, symbol=None: final)
    calls = []

    def fake_tv(spec):
        calls.append(spec)
        return [row("TRADINGVIEW", "OANDA:XAUUSD", "OANDA", day) for day in spec["dates"]]

    register_fetcher("TRADINGVIEW", fake_tv)
    syncer = auto_sync.DataAutoSync(lambda: [("OANDA:XAUUSD", "forex_24_5")])
    first = syncer.run_once()
    assert first["TRADINGVIEW"]["have_target"] == 1
    assert syncer.markets["TRADINGVIEW"]["last_synced_session"] == final.isoformat()
    second = syncer.run_once()
    assert second["TRADINGVIEW"]["skipped"] is True
    assert len(calls) == 1


def test_crypto_is_24x7_with_utc_day_cutoff():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from market_data.service import expected_trading_dates, latest_final_session

    ist = ZoneInfo("Asia/Kolkata")
    # Weekend bars are expected for crypto, not for forex.
    sat, sun = date(2026, 9, 19), date(2026, 9, 20)
    assert expected_trading_dates("TRADINGVIEW", sat, sun, "CRYPTO:BTCUSD") == [sat, sun]
    assert expected_trading_dates("TRADINGVIEW", sat, sun, "OANDA:XAUUSD") == []
    # Sat 2026-09-19 UTC candle closes at 00:00 UTC Sun = 05:30 IST Sun.
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 20, 5, 0, tzinfo=ist), "CRYPTO:BTCUSD") == date(2026, 9, 18)
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 20, 6, 0, tzinfo=ist), "CRYPTO:BTCUSD") == sat


def test_sync_replaces_bars_stored_before_their_daily_close(tmp_db, clean_flags):
    import sqlite3

    day = date(2026, 9, 21)
    database.upsert_ohlc([row("TRADINGVIEW", "OANDA:XAUUSD", "OANDA", day, close=1.0)], db_path=tmp_db)
    # Stored at 2026-09-21 10:00 UTC, before the 17:00 NY (21:00 UTC) close -> partial.
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE ohlc_daily SET created_at = '2026-09-21 10:00:00'")
    register_fetcher(
        "TRADINGVIEW",
        lambda spec: [row("TRADINGVIEW", "OANDA:XAUUSD", "OANDA", d, close=2.0) for d in spec["dates"]],
    )
    result = sync_symbol_range("TRADINGVIEW", "OANDA:XAUUSD", day, start_date=day, db_path=tmp_db)
    assert [r["close"] for r in result.rows] == [2.0]
    assert any("partial bars" in note for note in result.notes)


def test_gift_nifty_final_after_overnight_session():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from market_data.service import latest_final_session

    ist = ZoneInfo("Asia/Kolkata")
    # Tue 2026-09-22 session runs to 02:45 IST Wed; final from 03:00 IST Wed.
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 23, 2, 50, tzinfo=ist), "NSEIX:NIFTY1!") == date(2026, 9, 21)
    assert latest_final_session("TRADINGVIEW", datetime(2026, 9, 23, 3, 5, tzinfo=ist), "NSEIX:NIFTY1!") == date(2026, 9, 22)
