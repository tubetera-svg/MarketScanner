"""Bad bars are rejected before they are stored or can form a level (AGENTS.md §2a)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from market_data import bar_quality, database, health


def _row(day, o, h, l, c, symbol="NSE:TEST", source="NSE"):
    return {"source": source, "symbol": symbol, "exchange": "NSE", "date": day,
            "open": o, "high": h, "low": l, "close": c, "volume": 1000}


@pytest.mark.parametrize("bar, reason", [
    ((100, 105, 95, 102), None),
    ((100, 100, 100, 100), None),                 # flat bar is valid
    ((100, 95, 105, 100), "high < low"),
    ((106, 105, 95, 100), "open outside high-low"),
    ((100, 105, 95, 94), "close outside high-low"),
    ((0, 105, 95, 100), "zero/negative price"),
    ((100, 105, -1, 100), "zero/negative price"),
    ((float("nan"), 105, 95, 100), "NaN/inf price"),
    ((None, 105, 95, 100), "missing/non-numeric price"),
])
def test_bar_problem(bar, reason):
    assert bar_quality.bar_problem(*bar) == reason


def test_clean_rows_drops_bad_bars_and_conflicting_duplicates():
    rows = [
        _row("2026-09-28", 100, 105, 95, 102),
        _row("2026-09-29", 100, 95, 105, 100),        # high < low
        _row("2026-09-30", 101, 106, 99, 104),
        _row("2026-09-30", 101, 106, 99, 104),        # identical copy -> one kept
        _row("2026-10-01", 104, 108, 103, 107),
        _row("2026-10-01", 104, 109, 103, 107),       # disagreeing copies -> both dropped
    ]
    out = bar_quality.clean_rows(rows)
    assert [r["date"] for r in out] == ["2026-09-28", "2026-09-30"]


def test_clean_frame_live_tradingview_shape():
    index = pd.to_datetime(["2026-10-01 09:15", "2026-10-01 09:20", "2026-10-01 09:20", "2026-10-01 09:25", "2026-10-01 09:30"])
    df = pd.DataFrame({
        "symbol": "NSE:TEST",
        "open": [100, 101, 101, 102, float("nan")],
        "high": [101, 102, 102.5, 103, 104],
        "low": [99, 100, 100, 104, 101],             # 09:25 high < low
        "close": [101, 101.5, 101.5, 102.5, 103],
        "volume": [1, 2, 2, 3, 4],
    }, index=index)
    out = bar_quality.clean_frame(df, context="test")
    # 09:20 copies disagree (high 102 vs 102.5), 09:25 high < low, 09:30 NaN open.
    assert list(out.index) == [pd.Timestamp("2026-10-01 09:15")]
    clean = df.iloc[:2]
    assert bar_quality.clean_frame(clean) is clean  # nothing to drop: frame returned as is


def test_upsert_rejects_bad_bars(tmp_db):
    stored = database.upsert_ohlc([
        _row("2026-09-29", 100, 105, 95, 102),
        _row("2026-09-30", 100, 95, 105, 100),
        _row("2026-10-01", 0, 105, 95, 100),
    ], db_path=tmp_db)
    assert stored == 1
    assert [r["date"] for r in database.query_ohlc("NSE", "NSE:TEST", db_path=tmp_db)] == ["2026-09-29"]


def _insert_raw(db_file, *rows):
    """Simulate rows stored before the integrity check existed."""
    conn = sqlite3.connect(db_file)
    conn.executemany(
        "INSERT INTO ohlc_daily (source, symbol, exchange, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["source"], r["symbol"], r["exchange"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
    )
    conn.commit()
    conn.close()


def test_reads_skip_legacy_bad_rows_and_status_counts_them(tmp_db):
    _insert_raw(tmp_db,
                _row("2026-09-28", 100, 105, 95, 102),
                _row("2026-09-29", 100, 200, 95, 250),        # close spike above the high
                _row("2026-09-30", 101, 106, 99, 104))
    assert [r["date"] for r in database.query_ohlc("NSE", "NSE:TEST", db_path=tmp_db)] == ["2026-09-28", "2026-09-30"]
    multi = database.query_ohlc_multi("NSE", ["NSE:TEST"], db_path=tmp_db)
    assert [r["date"] for r in multi["NSE:TEST"]] == ["2026-09-28", "2026-09-30"]
    assert health.source_summary(db_path=tmp_db)[0]["invalid_rows"] == 1


def test_strategy_daily_frame_never_sees_a_bad_bar(tmp_db):
    """End to end: the strategy loader builds its frame without the bad bar (a gap, not a level)."""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    import all_strategy

    _insert_raw(tmp_db,
                _row("2026-09-28", 100, 105, 95, 102, symbol="NSE:BADBAR"),
                _row("2026-09-29", 100, 500, 95, 101, symbol="NSE:BADBAR"),  # valid range, kept
                _row("2026-09-30", 101, 99, 103, 100, symbol="NSE:BADBAR"),  # high < low
                _row("2026-10-01", 101, 106, 99, 104, symbol="NSE:BADBAR"))
    frame = all_strategy._build_daily_map_for_symbols(["NSE:BADBAR"], date(2026, 10, 1), 30)["NSE:BADBAR"]
    assert [d.date().isoformat() for d in frame.index] == ["2026-09-28", "2026-09-29", "2026-10-01"]
    assert frame["Low"].min() == 95  # the inverted 103/99 bar never became a level


@pytest.fixture()
def fake_nse(clean_flags):
    from market_data.service import register_fetcher, reset_fetchers

    yield lambda fetch: register_fetcher("NSE", fetch)
    reset_fetchers()


def test_refetch_replaces_bad_stored_bars(tmp_db, fake_nse):
    _insert_raw(tmp_db,
                _row("2026-09-28", 100, 105, 95, 102),
                _row("2026-09-29", 100, 95, 105, 100),        # high < low
                _row("2026-09-30", 101, 106, 99, 250))        # close outside range
    calls = []

    def fetch(spec):
        calls.append([d.isoformat() for d in spec["dates"]])
        return [_row(d.isoformat(), 100, 104, 98, 103) for d in spec["dates"]]

    fake_nse(fetch)
    out = health.refetch_invalid("NSE", db_path=tmp_db)
    assert out["checked"] == 2 and out["still_missing"] == [] and not out["more"]
    assert out["fixed"] == ["NSE:TEST 2026-09-29", "NSE:TEST 2026-09-30"]
    assert calls == [["2026-09-29", "2026-09-30"]]  # only the bad dates were fetched again
    assert health.source_summary(db_path=tmp_db)[0]["invalid_rows"] == 0
    stored = database.query_ohlc("NSE", "NSE:TEST", db_path=tmp_db)
    assert [(r["date"], r["high"]) for r in stored] == [("2026-09-28", 105), ("2026-09-29", 104), ("2026-09-30", 104)]


def test_refetch_keeps_a_gap_when_the_source_is_still_bad(tmp_db, fake_nse):
    _insert_raw(tmp_db, _row("2026-09-29", 100, 95, 105, 100))
    fake_nse(lambda spec: [_row(d.isoformat(), 100, 95, 105, 100) for d in spec["dates"]])
    out = health.refetch_invalid("NSE", db_path=tmp_db)
    assert out["fixed"] == [] and out["still_missing"] == ["NSE:TEST 2026-09-29"]
    assert health.source_summary(db_path=tmp_db) == []  # bad row gone, nothing stored in its place
