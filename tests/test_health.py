"""Status page data-freshness report (market_data.health)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from market_data import database, health

# Sat 04-Oct-2026 01:30 IST / Sat 03-Oct 16:00 New York. 02-Oct is an NSE holiday,
# so NSE's latest final bar is Thu 01-Oct; forex's is Fri 02-Oct (before 17:00 NY).
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def _bars(source, symbol, days):
    exchange = symbol.split(":", 1)[0]
    return [{"source": source, "symbol": symbol, "exchange": exchange, "date": d.isoformat(),
             "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100} for d in days]


def _weekdays(start, end, skip=()):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_symbol_freshness_statuses(tmp_db):
    holidays = {date(2026, 8, 15), date(2026, 9, 14), date(2026, 10, 2)}
    start = date(2026, 6, 1)
    database.upsert_ohlc(
        _bars("NSE", "NSE:FRESH", _weekdays(start, date(2026, 10, 1), holidays))
        + _bars("NSE", "NSE:GAPPY", _weekdays(start, date(2026, 10, 1), holidays | {date(2026, 9, 22)}))
        + _bars("NSE", "NSE:STALE", _weekdays(start, date(2026, 9, 29), holidays))
        # Forex: one exchange holiday is tolerated.
        + _bars("TRADINGVIEW", "OANDA:EURUSD", _weekdays(start, date(2026, 10, 2), {date(2026, 9, 7)})),
        db_path=tmp_db,
    )
    database.mark_no_data([{"source": "NSE", "symbol": "NSE:GAPPY", "exchange": "NSE", "date": "2026-09-23"}], db_path=tmp_db)
    watchlist = [
        {"symbol": "NSE:FRESH", "session": "nse"},
        {"symbol": "NSE:GAPPY", "session": "nse"},
        {"symbol": "NSE:STALE", "session": "nse"},
        {"symbol": "NSE:NEWIPO", "session": "nse"},
        {"symbol": "OANDA:EURUSD", "session": "forex_24_5"},
    ]
    report = health.symbol_freshness(watchlist, now=NOW, db_path=tmp_db)
    rows = {row["symbol"]: row for row in report["symbols"]}
    assert report["expected_last"]["nse"] == "2026-10-01"
    assert report["expected_last"]["tradingview"] == "2026-10-02"
    assert rows["NSE:FRESH"]["status"] == "ok" and rows["NSE:FRESH"]["missing"] == 0
    assert rows["NSE:GAPPY"]["status"] == "gaps"
    assert rows["NSE:GAPPY"]["missing_sample"] == ["2026-09-22"]  # 23-Sep has a no-data marker
    assert rows["NSE:STALE"]["status"] == "stale" and rows["NSE:STALE"]["behind"] == 2
    assert rows["NSE:NEWIPO"]["status"] == "empty"
    assert rows["OANDA:EURUSD"]["status"] == "ok" and rows["OANDA:EURUSD"]["missing"] == 1
    assert [row["status"] for row in report["symbols"]][:2] == ["empty", "stale"]
    assert report["counts"] == {"empty": 1, "stale": 1, "gaps": 1, "ok": 2}


def test_source_summary(tmp_db):
    database.upsert_ohlc(_bars("NSE", "NSE:A", [date(2026, 9, 30), date(2026, 10, 1)]), db_path=tmp_db)
    summary = health.source_summary(db_path=tmp_db)
    assert summary == [{"source": "NSE", "rows": 2, "symbols": 1, "latest_date": "2026-10-01", "no_data_markers": 0, "invalid_rows": 0}]
