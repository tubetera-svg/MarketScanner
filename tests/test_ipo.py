"""Tests for the IPO discovery / backfill / registration module."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from market_data import database
from market_data import ipo as ipo_mod


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("FETCH_NSE_DATA", "true")


@pytest.fixture()
def tmp_ipo_files(tmp_path, monkeypatch):
    """Redirect watchlist/categories IO to a throwaway dir."""
    watch = tmp_path / "watchlist.txt"
    cat = tmp_path / "watchlist_categories.json"
    watch.write_text("", encoding="utf-8")
    monkeypatch.setattr(ipo_mod, "WATCHLIST_PATH", watch)
    monkeypatch.setattr(ipo_mod, "CATEGORIES_PATH", cat)
    return watch, cat


def bhavcopy_frame(symbols: dict[str, float]) -> pd.DataFrame:
    """Build a mock EQ-only bhavcopy DataFrame."""
    rows = []
    for sym, open_price in symbols.items():
        rows.append(
            {
                "SYMBOL": sym,
                "SERIES": "EQ",
                "OPEN_PRICE": open_price,
                "HIGH_PRICE": open_price + 2,
                "LOW_PRICE": open_price - 2,
                "CLOSE_PRICE": open_price + 1,
                "TTL_TRD_QNTY": 100000,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture()
def tmp(tmp_path, monkeypatch):
    """Standalone temp DB file for DB-only tests."""
    db_file = tmp_path / "ipo_test.db"
    database.init_db(db_file)
    return db_file


# ------------------------------------------------------------------ database
def test_upsert_and_query_ipo_metadata(tmp):
    database.upsert_ipo_metadata(
        {
            "symbol": "NSE:XYZ",
            "exchange": "NSE",
            "source": "NSE",
            "listing_date": "2026-01-05",
            "listing_price": 120.0,
        },
        db_path=tmp,
    )
    rows = database.query_ipo_metadata(db_path=tmp)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NSE:XYZ"
    assert rows[0]["listing_price"] == 120.0
    # Upsert refreshes rather than duplicating
    database.upsert_ipo_metadata(
        {
            "symbol": "NSE:XYZ",
            "exchange": "NSE",
            "source": "NSE",
            "listing_date": "2026-01-05",
            "listing_price": 130.0,
        },
        db_path=tmp,
    )
    assert len(database.query_ipo_metadata(db_path=tmp)) == 1
    assert database.remove_ipo_metadata("NSE:XYZ", db_path=tmp) == 1
    assert database.query_ipo_metadata(db_path=tmp) == []


def test_ipo_metadata_requires_listing_date(tmp):
    with pytest.raises(KeyError):
        database.upsert_ipo_metadata(
            {"symbol": "NSE:XYZ", "listing_price": 10.0}, db_path=tmp
        )


# ------------------------------------------------------------------ discovery
def test_discover_new_ipos_basic(monkeypatch):
    start = date(2026, 1, 5)  # a Monday
    frames = {
        start: bhavcopy_frame({"ALREADY": 50.0, "NEWIPO": 100.0}),
        start + timedelta(days=1): bhavcopy_frame({"ALREADY": 50.0, "NEWIPO": 110.0, "ANOTHER": 20.0}),
    }
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frames.get(d))
    # baseline excludes the new symbol -> it is discovered
    found = ipo_mod.discover_new_ipos(start, start, known_symbols={"ALREADY"})
    assert [x["symbol"] for x in found] == ["NSE:NEWIPO"]
    assert found[0]["listing_date"] == "2026-01-05"
    assert found[0]["listing_price"] == 100.0
    assert found[0]["source"] == "NSE"


def test_discover_skips_known_and_tracks_first_day(monkeypatch):
    start = date(2026, 2, 9)  # a Monday
    frames = {
        start: bhavcopy_frame({"EXIST": 30.0, "NEW1": 10.0}),
        start + timedelta(days=1): bhavcopy_frame({"EXIST": 31.0, "NEW1": 11.0, "NEW2": 5.0}),
    }
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frames.get(d))
    found = ipo_mod.discover_new_ipos(start, start + timedelta(days=1), known_symbols={"EXIST"})
    assert {x["symbol"] for x in found} == {"NSE:NEW1", "NSE:NEW2"}
    new1 = next(x for x in found if x["symbol"] == "NSE:NEW1")
    assert new1["listing_date"] == start.isoformat()  # first appearance wins
    assert new1["listing_price"] == 10.0


def test_discover_requires_valid_range(monkeypatch):
    with pytest.raises(ValueError):
        ipo_mod.discover_new_ipos(date(2026, 5, 1), date(2026, 4, 1), known_symbols=set())


# ------------------------------------------------------------------ registration
def test_register_ipo_writes_watchlist_category_and_db(monkeypatch, tmp_ipo_files, tmp):
    watch, cat = tmp_ipo_files
    monkeypatch.setattr(ipo_mod, "load_entries", lambda: [])

    summary = ipo_mod.register_ipo(
        {"symbol": "NSE:NEWIPO", "listing_date": "2026-01-05", "listing_price": 100.0},
        db_path=tmp,
    )
    assert summary["symbol"] == "NSE:NEWIPO"

    # watchlist.txt got the prefixed symbol
    assert watch.read_text(encoding="utf-8").strip() == "NSE:NEWIPO"

    # category file has scope=IPO and listing info
    import json

    cat_data = json.loads(cat.read_text(encoding="utf-8"))
    assert cat_data["NSE:NEWIPO"]["scope"] == "IPO"
    assert cat_data["NSE:NEWIPO"]["listing_date"] == "2026-01-05"
    assert cat_data["NSE:NEWIPO"]["listing_price"] == "100.0"

    # ipo_metadata table populated
    row = database.query_ipo_metadata(symbols=["NSE:NEWIPO"], db_path=tmp)
    assert len(row) == 1
    assert row[0]["listing_price"] == 100.0


def test_register_ipo_idempotent(monkeypatch, tmp_ipo_files, tmp):
    watch, cat = tmp_ipo_files
    # Simulate load_entries reading the redirected watchlist file so the second
    # call correctly sees the symbol already present.
    monkeypatch.setattr(
        ipo_mod, "load_entries",
        lambda: [(line.strip(), None) for line in watch.read_text(encoding="utf-8").splitlines() if line.strip()],
    )
    ipo_mod.register_ipo(
        {"symbol": "NSE:NEWIPO", "listing_date": "2026-01-05", "listing_price": 100.0},
        db_path=tmp,
    )
    # second call with the symbol already in watchlist must not duplicate it
    ipo_mod.register_ipo(
        {"symbol": "NSE:NEWIPO", "listing_date": "2026-01-05", "listing_price": 100.0},
        db_path=tmp,
    )
    assert watch.read_text(encoding="utf-8").strip() == "NSE:NEWIPO"
    assert len(database.query_ipo_metadata(db_path=tmp)) == 1


# ------------------------------------------------------------------ known universe
def test_known_symbols_from_bhavcopy(monkeypatch):
    frame = bhavcopy_frame({"AAA": 1.0, "BBB": 2.0})
    frame = pd.concat(
        [frame, pd.DataFrame([{"SYMBOL": "NON", "SERIES": "BE", "OPEN_PRICE": 3.0}])],
        ignore_index=True,
    )
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frame)
    symbols = ipo_mod.known_symbols_from_bhavcopy(date(2026, 3, 2))
    assert symbols == {"AAA", "BBB"}  # BE series excluded


def row_dict(source, symbol, exchange, day, close=100.0):
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


# ------------------------------------------------------------------ performance
def test_ipo_performance(tmp):
    database.upsert_ipo_metadata(
        {"symbol": "NSE:NEWIPO", "exchange": "NSE", "source": "NSE",
         "listing_date": "2026-01-05", "listing_price": 100.0}, db_path=tmp,
    )
    # seed a few OHLC bars after listing
    database.upsert_ohlc([
        row_dict("NSE", "NSE:NEWIPO", "NSE", date(2026, 1, 6), 105.0),
        row_dict("NSE", "NSE:NEWIPO", "NSE", date(2026, 1, 7), 90.0),
        row_dict("NSE", "NSE:NEWIPO", "NSE", date(2026, 1, 8), 120.0),
    ], db_path=tmp)
    perf = ipo_mod.ipo_performance(db_path=tmp, reference_date=date(2026, 1, 8))
    assert len(perf) == 1
    item = perf[0]
    assert item["listing_price"] == 100.0
    assert item["current_price"] == 120.0
    assert item["high_since_listing"] == 122.0  # max(high=close+2) across bars
    assert item["low_since_listing"] == 88.0    # min(low=close-2) across bars
    assert item["pct_vs_listing"] == 20.0