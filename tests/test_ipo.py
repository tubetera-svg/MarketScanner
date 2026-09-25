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


#: Symbols the eligibility gate treats as *main-board equities* in tests. Every
#: other symbol is "not in the master", so the family patterns decide.
_MASTER = {
    symbol: "EQ"
    for symbol in (
        "NEWIPO", "ALREADY", "ANOTHER", "EXIST", "NEW1", "NEW2", "XYZ",
        "RELIANCE", "TATASTEEL", "JETFREIGHT", "ICICIGI", "LICHSGFIN", "REALCO",
        "LOWLIQ", "FLICKER",
    )
} | {"BETA": "EQ"}


@pytest.fixture(autouse=True)
def _equity_master(monkeypatch):
    """Stub the NSE equity master so tests never touch the network."""
    monkeypatch.setattr(
        ipo_mod.equity_master, "load_equity_master", lambda **kwargs: dict(_MASTER)
    )


@pytest.fixture()
def tmp_ipo_files(tmp_path, monkeypatch):
    """Redirect watchlist/categories IO to a throwaway dir."""
    watch = tmp_path / "watchlist.txt"
    cat = tmp_path / "watchlist_categories.json"
    watch.write_text("", encoding="utf-8")
    monkeypatch.setattr(ipo_mod, "WATCHLIST_PATH", watch)
    monkeypatch.setattr(ipo_mod, "CATEGORIES_PATH", cat)
    return watch, cat


def bhavcopy_frame(
    symbols: dict[str, float], volumes: dict[str, float] | None = None
) -> pd.DataFrame:
    """Build a mock EQ-only bhavcopy DataFrame.

    ``volumes`` optionally overrides the traded quantity per symbol (used by the
    liquidity-threshold tests).
    """
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
                "TTL_TRD_QNTY": (volumes or {}).get(sym, 100000),
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
        start: bhavcopy_frame({"EXIST": 30.0, "NEW1": 10.0},
                              volumes={"NEW1": 1_000_000}),
        start + timedelta(days=1): bhavcopy_frame(
            {"EXIST": 31.0, "NEW1": 11.0, "NEW2": 5.0},
            volumes={"NEW1": 1_000_000, "NEW2": 5_000_000},  # clear liquidity gate
        ),
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


def row_dict(source, symbol, exchange, day, close=100.0, volume=12345.0):
    return {
        "source": source,
        "symbol": symbol,
        "exchange": exchange,
        "date": day.isoformat(),
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": volume,
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


# ------------------------------------------------------------------ eligibility
@pytest.mark.parametrize(
    "symbol",
    [
        "NSE:628GS2032",      # dated government security
        "NSE:74GS2035",
        "NSE:SGBDEC26",       # sovereign gold bond
        "NSE:SGBOCT27VI",
        "NSE:BANKETFADD",     # ETF add-on plan
        "NSE:GOLDETFADD",
        "NSE:ITETFADD",
        "NSE:NIF10GETF",      # index fund
        "NSE:NIF5GETF",
        "NSE:NIFITETF",
        "NSE:SILVRETF",
        "NSE:LIQUIDBETA",     # fund basket
        "NSE:ANOND-RE",       # rights entitlement
        "NSE:DUCON-RE1",
        "NSE:JAYKAY-RE1",
        "NSE:MPEL-RE",
        "NSE:RATNA-RE",
        "NSE:VHLTD-RE1",
    ],
)
def test_ineligibility_flags_every_flagged_family(symbol):
    assert ipo_mod.ipo_ineligibility_reason(symbol), symbol
    assert ipo_mod.is_ipo_eligible(symbol) is False


@pytest.mark.parametrize(
    "symbol",
    ["NSE:RELIANCE", "NSE:TATASTEEL", "NSE:JETFREIGHT", "NSE:ICICIGI", "NSE:LICHSGFIN"],
)
def test_ineligibility_allows_real_equities(symbol):
    assert ipo_mod.ipo_ineligibility_reason(symbol) is None


def test_ineligibility_enforces_nse_and_eq_series():
    assert "not NSE" in (ipo_mod.ipo_ineligibility_reason("BSE:RELIANCE") or "")
    assert "main-board EQ" in (
        ipo_mod.ipo_ineligibility_reason("NSE:NEWIPO", series="SM") or ""
    )
    assert ipo_mod.ipo_ineligibility_reason("NSE:NEWIPO", series="EQ") is None


def test_ineligibility_uses_equity_master_when_available():
    reason = ipo_mod.ipo_ineligibility_reason(
        "NSE:UNLISTEDCO", equity_master_map={"RELIANCE": "EQ"}
    )
    assert reason and "equity master" in reason
    # Master unavailable (offline) -> only the pattern fallback applies.
    assert ipo_mod.ipo_ineligibility_reason(
        "NSE:UNLISTEDCO", equity_master_map={}
    ) is None
    assert ipo_mod.ipo_ineligibility_reason(
        "NSE:GOLDETFADD", equity_master_map={}
    ) is not None


def _seed_recent_bars(symbol: str, tmp, n: int = 20,
                      close: float = 100.0, volume: float = 100000.0) -> None:
    """Write `n` trading-day OHLC bars for `symbol` in the recent window (lookback
    from today). Each bar is vol*close = 1 cr, comfortably above the 0.25 cr
    liquidity floor."""
    today = date.today()
    rows = []
    offset = 0
    placed = 0
    while placed < n:
        d = today - timedelta(days=offset)
        offset += 1
        if d.weekday() >= 5:  # Sat/Sun -> skip (market closed)
            continue
        rows.append(row_dict("NSE", symbol, "NSE", d, close=close, volume=volume))
        placed += 1
    database.upsert_ohlc(rows, db_path=tmp)


# ---------------------------------------------------------------- deletion guard

def test_discover_drops_non_equity_instruments(monkeypatch):
    start = date(2026, 3, 2)  # a Monday
    frame = bhavcopy_frame(
        {
            "REALCO": 100.0,
            "GOLDETFADD": 61.0,
            "628GS2032": 99.0,
            "SGBDEC26": 15000.0,
            "ANOND-RE": 246.0,
        }
    )
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frame)
    accepted = ipo_mod.discover_new_ipos(start, start, known_symbols={"IGNORED"})
    assert [item["symbol"] for item in accepted] == ["NSE:REALCO"]
    assert accepted[0]["eligible"] is True

    detailed = ipo_mod.discover_new_ipos(
        start, start, known_symbols={"IGNORED"}, include_rejected=True
    )
    rejected = {item["symbol"]: item for item in detailed if not item["eligible"]}
    assert set(rejected) == {
        "NSE:GOLDETFADD", "NSE:628GS2032", "NSE:SGBDEC26", "NSE:ANOND-RE",
    }
    assert all(item["reject_reason"] for item in rejected.values())


def test_discover_enforces_liquidity_threshold(monkeypatch):
    start = date(2026, 3, 2)
    frame = bhavcopy_frame({"LOWLIQ": 100.0}, volumes={"LOWLIQ": 1000})
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frame)
    found = ipo_mod.discover_new_ipos(
        start, start, known_symbols={"IGNORED"}, include_rejected=True
    )
    assert found and found[0]["eligible"] is False
    assert "avg daily traded value" in found[0]["reject_reason"]


def test_discover_enforces_traded_recently(monkeypatch):
    start = date(2026, 3, 2)  # Monday .. Thursday
    other = bhavcopy_frame({"OTHER": 100.0})
    frames = {
        start: bhavcopy_frame({"FLICKER": 100.0}),
        start + timedelta(days=1): other,
        start + timedelta(days=2): other,
        start + timedelta(days=3): other,
    }
    monkeypatch.setattr(ipo_mod, "_download_bhavcopy", lambda d: frames.get(d))
    found = ipo_mod.discover_new_ipos(
        start, start + timedelta(days=3), known_symbols={"IGNORED"},
        include_rejected=True,
    )
    flicker = next(item for item in found if item["symbol"] == "NSE:FLICKER")
    assert flicker["eligible"] is False
    assert "traded on" in flicker["reject_reason"]


def test_register_ipo_rejects_non_equity_instrument(tmp_ipo_files, tmp):
    watch, _cat = tmp_ipo_files
    for symbol in ("NSE:GOLDETFADD", "NSE:628GS2032", "NSE:SGBDEC26", "NSE:ANOND-RE"):
        with pytest.raises(ValueError):
            ipo_mod.register_ipo(
                {"symbol": symbol, "listing_date": "2026-01-05", "listing_price": 10.0},
                db_path=tmp,
            )
    assert watch.read_text(encoding="utf-8") == ""
    assert database.query_ipo_metadata(db_path=tmp) == []


def test_register_ipos_skips_ineligible_without_aborting(monkeypatch, tmp_ipo_files, tmp):
    monkeypatch.setattr(ipo_mod, "load_entries", lambda: [])
    summaries = ipo_mod.register_ipos(
        [
            {"symbol": "NSE:GOLDETFADD", "listing_date": "2026-01-05",
             "listing_price": 61.0},
            {"symbol": "NSE:NEWIPO", "listing_date": "2026-01-05",
             "listing_price": 100.0},
        ],
        db_path=tmp,
    )
    assert summaries[0]["registered"] is False and summaries[0]["reason"]
    assert summaries[1]["registered"] is True
    assert [row["symbol"] for row in database.query_ipo_metadata(db_path=tmp)] == [
        "NSE:NEWIPO"
    ]

def test_ineligibility_uses_etf_list_when_available(monkeypatch):
    """Test that authoritative ETF list rejects ETF units even when not caught by regex."""
    # Use symbol like 'ALPHA' or 'MYCUSTOM' that does not match NON_IPO_SYMBOL_RE
    monkeypatch.setattr(ipo_mod.etf_list, "load_etf_symbols", lambda **kwargs: {"ALPHA", "MYCUSTOM"})
    reason = ipo_mod.ipo_ineligibility_reason("NSE:ALPHA")
    assert reason and "NSE ETF unit" in reason

    # When symbol is not in etf list, check returns None (if in master)
    assert ipo_mod.ipo_ineligibility_reason("NSE:RELIANCE") is None


def test_register_ipo_rejects_etf_units(monkeypatch, tmp_ipo_files, tmp):
    """Test that register_ipo raises ValueError when given an ETF symbol."""
    monkeypatch.setattr(ipo_mod.etf_list, "load_etf_symbols", lambda **kwargs: {"ALPHA"})
    with pytest.raises(ValueError, match="NSE ETF unit"):
        ipo_mod.register_ipo(
            {"symbol": "NSE:ALPHA", "listing_date": "2026-01-05", "listing_price": 50.0},
            db_path=tmp,
        )

