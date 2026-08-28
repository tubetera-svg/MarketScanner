"""Tests for the read-only records browser (/api/market-data/*).

Covers watchlist/all scoping, pagination totals, filters, metadata,
empty-watchlist handling, IN-clause chunking, validation errors, and the
guarantee that browsing NEVER triggers upstream fetches."""

from __future__ import annotations

from datetime import date, timedelta

import pytest


def _row(symbol, day, *, source="NSE", exchange="NSE", close=100.0, volume=1000.0):
    return {"source": source, "symbol": symbol, "exchange": exchange,
            "date": day.isoformat() if isinstance(day, date) else day,
            "open": close - 1, "high": close + 2, "low": close - 2, "close": close,
            "volume": volume}


def _seed(rows):
    from market_data import database

    assert database.upsert_ohlc(rows) == len(rows)


@pytest.fixture()
def client():
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from api import main as api_main

    return TestClient(api_main.app)


@pytest.fixture()
def seeded_watchlist(monkeypatch):
    """Replace market_data.routes' best-effort watchlist loader."""
    def _set(symbols):
        from market_data import routes

        monkeypatch.setattr(routes, "_watchlist_entries",
                            lambda: [(s, None) for s in symbols])

    return _set


def test_records_scope_watchlist_filters_rows(tmp_db, client, seeded_watchlist):
    _seed([_row("NSE:INFY", "2026-08-20"), _row("NSE:INFY", "2026-08-21"),
           _row("NSE:TCS", "2026-08-20"), _row("NSE:NOPE", "2026-08-20")])
    seeded_watchlist(["NSE:INFY", "NSE:TCS"])
    payload = client.get("/api/market-data/records", params={"scope": "watchlist"}).json()
    assert {r["symbol"] for r in payload["rows"]} == {"NSE:INFY", "NSE:TCS"}
    assert payload["total"] == 3 and payload["watchlist_size"] == 2


def test_records_scope_all_returns_everything(tmp_db, client, seeded_watchlist):
    _seed([_row("NSE:INFY", "2026-08-20"), _row("NSE:NOPE", "2026-08-20")])
    seeded_watchlist(["NSE:INFY"])
    payload = client.get("/api/market-data/records", params={"scope": "all"}).json()
    assert payload["total"] == 2
    assert payload["watchlist_size"] is None
    assert payload["watchlist_missing_in_db"] == []


def test_records_pagination_and_total(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:INFY"])
    base = date(2026, 7, 1)
    _seed([_row("NSE:INFY", base + timedelta(days=i)) for i in range(25)])
    first = client.get("/api/market-data/records",
                       params={"scope": "watchlist", "limit": 10}).json()
    assert first["total"] == 25 and len(first["rows"]) == 10
    assert first["rows"][0]["date"] == "2026-07-25"  # default newest-first
    last = client.get("/api/market-data/records",
                      params={"scope": "watchlist", "limit": 10, "offset": 20}).json()
    assert len(last["rows"]) == 5
    asc = client.get("/api/market-data/records",
                     params={"scope": "watchlist", "sort": "asc", "limit": 1}).json()
    assert asc["rows"][0]["date"] == "2026-07-01"


def test_records_filters(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:INFY", "OANDA:XAUUSD"])
    _seed([_row("NSE:INFY", "2026-08-10"), _row("NSE:INFY", "2026-08-11"),
           _row("OANDA:XAUUSD", "2026-08-10", source="TRADINGVIEW",
                exchange="OANDA", close=2500.0, volume=None)])

    got = lambda **params: client.get("/api/market-data/records", params=params).json()
    assert got(scope="all", source="TRADINGVIEW")["total"] == 1
    assert got(scope="all", exchange="OANDA")["rows"][0]["symbol"] == "OANDA:XAUUSD"
    assert got(scope="all", q="xauusd")["total"] == 1
    rng = got(scope="all", start_date="2026-08-11", end_date="2026-08-11")
    assert rng["total"] == 1 and rng["rows"][0]["symbol"] == "NSE:INFY"


def test_records_never_fetches_upstream(tmp_db, client, seeded_watchlist, clean_flags):
    from market_data import service

    def _boom(spec):
        raise AssertionError("network fetch attempted")

    service.register_fetcher("NSE", _boom)
    service.register_fetcher("TRADINGVIEW", _boom)
    try:
        seeded_watchlist(["NSE:GHOST"])  # watched but never synced -> stays empty
        resp = client.get("/api/market-data/records", params={"scope": "watchlist"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 0 and payload["rows"] == []
        assert payload["watchlist_missing_in_db"] == ["NSE:GHOST"]
    finally:
        service.reset_fetchers()


def test_records_empty_watchlist_returns_empty_ok(tmp_db, client, monkeypatch):
    from market_data import routes

    monkeypatch.setattr(routes, "_watchlist_entries", lambda: [])
    payload = client.get("/api/market-data/records", params={"scope": "watchlist"}).json()
    assert payload["total"] == 0 and payload["rows"] == [] and payload["watchlist_size"] == 0
    assert any("empty" in note.lower() for note in payload["notes"])


def test_records_watchlist_missing_in_db(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:ABSENT", "NSE:INFY"])
    _seed([_row("NSE:INFY", "2026-08-10")])
    payload = client.get("/api/market-data/records", params={"scope": "watchlist"}).json()
    assert payload["watchlist_missing_in_db"] == ["NSE:ABSENT"]


def test_meta_endpoint(tmp_db, client, seeded_watchlist):
    _seed([_row("NSE:INFY", "2026-08-10"), _row("NSE:INFY", "2026-08-11"),
           _row("OANDA:XAUUSD", "2026-08-10", source="TRADINGVIEW", exchange="OANDA")])
    seeded_watchlist(["NSE:INFY"])
    meta = client.get("/api/market-data/meta").json()
    assert meta["row_count"] == 3
    assert (meta["min_date"], meta["max_date"]) == ("2026-08-10", "2026-08-11")
    assert meta["sources"] == ["NSE", "TRADINGVIEW"]
    assert meta["exchanges"] == ["NSE", "OANDA"]
    assert {"source": "NSE", "rows": 2} in meta["rows_per_source"]
    scoped = client.get("/api/market-data/meta", params={"scope": "watchlist"}).json()
    assert scoped["row_count"] == 2 and scoped["sources"] == ["NSE"]


def test_records_explicit_symbol_selection(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:INFY", "NSE:TCS", "OANDA:XAUUSD", "NSE:ABSENT"])
    _seed([_row("NSE:INFY", "2026-08-20"), _row("NSE:TCS", "2026-08-20"),
           _row("OANDA:XAUUSD", "2026-08-20", source="TRADINGVIEW", exchange="OANDA")])

    got = lambda **params: client.get("/api/market-data/records", params=params).json()
    subset = got(scope="watchlist", symbols="nse:tcs, OANDA:XAUUSD")
    assert {r["symbol"] for r in subset["rows"]} == {"NSE:TCS", "OANDA:XAUUSD"}
    assert subset["watchlist_size"] == 4  # full-watchlist context is preserved
    # Symbols outside the watchlist are ignored when scope=watchlist…
    assert got(scope="watchlist", symbols="NSE:NOPE")["total"] == 0
    # …but honoured verbatim when scope=all.
    assert got(scope="all", symbols="NSE:NOPE,NSE:INFY")["total"] == 1
    # Missing-in-db diagnostics follow the (intersected) selection…
    absent = got(scope="watchlist", symbols="NSE:TCS,NSE:ABSENT")
    assert absent["total"] == 1 and absent["watchlist_missing_in_db"] == ["NSE:ABSENT"]
    # …while non-watchlist picks are dropped up-front and reported via notes.
    ghost = got(scope="watchlist", symbols="NSE:TCS,NSE:GHOST")
    assert ghost["total"] == 1 and ghost["watchlist_missing_in_db"] == []
    assert any("NSE:GHOST" in note for note in ghost["notes"])
    bad = client.get("/api/market-data/records", params={"symbols": " ,,"})
    assert bad.status_code == 400


def test_watchlist_endpoint(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:TCS", "OANDA:XAUUSD"])
    payload = client.get("/api/market-data/watchlist").json()
    assert payload["symbols"] == ["NSE:TCS", "OANDA:XAUUSD"]


def test_query_ohlc_page_symbol_chunking(tmp_db):
    from market_data import database

    _seed([_row("NSE:INFY", "2026-08-10")])
    many = [f"NSE:NOPE{i}" for i in range(1200)] + ["NSE:INFY"]  # forces >=3 chunks
    rows, total = database.query_ohlc_page(symbols=many, limit=10)
    assert total == 1 and rows[0]["symbol"] == "NSE:INFY"


def test_records_grid_filters_search_whole_dataset(tmp_db, client, seeded_watchlist):
    """Grid column filters must match rows across ALL pages, not just the page
    currently loaded in memory (the bug this guards against)."""
    seeded_watchlist(["NSE:INFY", "OANDA:XAUUSD"])
    _seed([_row("NSE:INFY", "2026-08-09"), _row("NSE:INFY", "2026-08-10"),
           _row("NSE:INFY", "2026-08-11"),
           _row("OANDA:XAUUSD", "2026-08-10", source="TRADINGVIEW",
                exchange="OANDA", close=2500.0, volume=None)])

    got = lambda **params: client.get("/api/market-data/records", params=params).json()
    # date_contains substring matches across the whole table (not just one page).
    assert got(scope="all", date_contains="2026-08-1")["total"] == 3
    assert got(scope="all", date_contains="-11")["total"] == 1
    # grid symbol/exchange/source refine the same dataset.
    assert got(scope="all", q="xauusd")["total"] == 1
    assert got(scope="all", exchange="OANDA")["total"] == 1
    assert got(scope="all", source="TRADINGVIEW")["total"] == 1
    # combined: date + source narrow the full set.
    assert got(scope="all", source="NSE", date_contains="2026-08-10")["total"] == 1
    assert got(scope="all", source="NSE", date_contains="2026-08-10")["rows"][0]["symbol"] == "NSE:INFY"


def test_validation_errors(tmp_db, client, seeded_watchlist):
    seeded_watchlist(["NSE:INFY"])
    assert client.get("/api/market-data/records", params={"scope": "bogus"}).status_code == 422
    assert client.get("/api/market-data/records", params={"source": "YAHOO"}).status_code == 400
    bad = client.get("/api/market-data/records",
                     params={"start_date": "2026-08-10", "end_date": "2026-08-01"})
    assert bad.status_code == 400
