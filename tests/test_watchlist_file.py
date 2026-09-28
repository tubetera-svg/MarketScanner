"""Watchlist file editing: helpers in ict_scanner + API add/remove/rename."""

from __future__ import annotations

import json

import pytest

from api import main as api_main

scanner = api_main.service.module


@pytest.fixture()
def wl_files(tmp_path, monkeypatch):
    watch = tmp_path / "watchlist.txt"
    cats = tmp_path / "watchlist_categories.json"
    monkeypatch.setattr(api_main, "WATCHLIST_PATH", watch)
    monkeypatch.setattr(api_main, "WATCHLIST_CATEGORIES_PATH", cats)
    monkeypatch.setattr(api_main, "nse_fno_members", lambda: {"INFY"})
    return watch, cats


def _cats(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_modify_preserves_comments_order_overrides_and_crlf(tmp_path):
    watch = tmp_path / "watchlist.txt"
    watch.write_bytes(b"# header\r\nNSE:ZED\r\nOANDA:XAUUSD|forex\r\nNSE:ABC\r\n")
    assert scanner.modify_watchlist_file(str(watch), remove="NSE:ZED")
    assert scanner.modify_watchlist_file(str(watch), rename=("OANDA:XAUUSD", "OANDA:XAGUSD"))
    assert scanner.modify_watchlist_file(str(watch), add="NSE:NEW")
    assert not scanner.modify_watchlist_file(str(watch), add="NSE:NEW")
    assert watch.read_bytes() == b"# header\r\nOANDA:XAGUSD|forex\r\nNSE:ABC\r\nNSE:NEW\r\n"


def test_rename_can_drop_override(tmp_path):
    watch = tmp_path / "watchlist.txt"
    watch.write_text("NSE:ABC|nse\n", encoding="utf-8")
    scanner.modify_watchlist_file(str(watch), rename=("NSE:ABC", "OANDA:EURUSD"), keep_override=False)
    assert watch.read_text(encoding="utf-8") == "OANDA:EURUSD\n"


def test_categories_drop_derived_keys(tmp_path):
    cats = tmp_path / "c.json"
    scanner.save_watchlist_categories({"nse:abc": {"scope": "IPO", "symbol": "NSE:OLD", "session": "nse", "sector": ""}}, str(cats))
    assert _cats(cats) == {"NSE:ABC": {"scope": "IPO"}}
    cats.write_text(json.dumps({"NSE:ABC": {"scope": "IPO", "symbol": "NSE:OLD"}}), encoding="utf-8")
    assert scanner.load_watchlist_categories(str(cats)) == {"NSE:ABC": {"scope": "IPO"}}


def test_lock_is_exclusive(tmp_path):
    watch = str(tmp_path / "watchlist.txt")
    with scanner.watchlist_file_lock(watch):
        with pytest.raises(TimeoutError):
            with scanner.watchlist_file_lock(watch, timeout=0.1):
                pass
    with scanner.watchlist_file_lock(watch, timeout=0.1):
        pass


def test_remove_last_symbol_returns_empty_list(wl_files):
    watch, cats = wl_files
    watch.write_text("NSE:ABC\n", encoding="utf-8")
    assert api_main.service.remove_from_watchlist("NSE:ABC") == []
    assert api_main.service.watchlist() == []
    added = api_main.service.add_to_watchlist("NSE:INFY")
    assert [item["symbol"] for item in added] == ["NSE:INFY"]


def test_add_scope_uses_fno_membership(wl_files):
    watch, cats = wl_files
    watch.write_text("", encoding="utf-8")
    api_main.service.add_to_watchlist("NSE:INFY")
    api_main.service.add_to_watchlist("NSE:SMALLCO")
    data = _cats(cats)
    assert data["NSE:INFY"]["scope"] == "F&O" and data["NSE:INFY"]["f_and_o"] == "F&O"
    assert data["NSE:SMALLCO"]["scope"] == "Equity" and data["NSE:SMALLCO"]["f_and_o"] == "Non-F&O"


def test_add_scope_without_fno_list_is_equity(wl_files, monkeypatch):
    watch, cats = wl_files
    monkeypatch.setattr(api_main, "nse_fno_members", lambda: None)
    watch.write_text("", encoding="utf-8")
    api_main.service.add_to_watchlist("NSE:INFY")
    assert _cats(cats)["NSE:INFY"] == {"asset_class": "equity", "exchange": "NSE", "scope": "Equity"}


def test_edit_can_clear_fields(wl_files):
    watch, cats = wl_files
    watch.write_text("NSE:ABC\n", encoding="utf-8")
    cats.write_text(json.dumps({"NSE:ABC": {"asset_class": "equity", "exchange": "NSE", "scope": "IPO", "sector": "IT"}}), encoding="utf-8")
    api_main.service.rename_in_watchlist("NSE:ABC", "NSE:ABC", "IPO", {"scope": "IPO", "sector": ""})
    assert "sector" not in _cats(cats)["NSE:ABC"]


def test_rename_across_exchange_resets_untouched_detected_fields(wl_files):
    watch, cats = wl_files
    watch.write_text("NSE:ABC|nse\n", encoding="utf-8")
    old = {"asset_class": "equity", "exchange": "NSE", "scope": "IPO", "theme": "EV"}
    cats.write_text(json.dumps({"NSE:ABC": old}), encoding="utf-8")
    # The form echoes back the old (untouched) values.
    result = api_main.service.rename_in_watchlist("NSE:ABC", "OANDA:EURUSD", "IPO", dict(old))
    assert _cats(cats) == {"OANDA:EURUSD": {"asset_class": "forex", "exchange": "OANDA", "scope": "Forex", "theme": "EV"}}
    assert watch.read_text(encoding="utf-8") == "OANDA:EURUSD\n"
    assert result[0]["symbol"] == "OANDA:EURUSD" and result[0]["session"] == "forex_24_5"


def test_rename_across_exchange_keeps_explicit_scope(wl_files):
    watch, cats = wl_files
    watch.write_text("NSE:ABC\n", encoding="utf-8")
    cats.write_text(json.dumps({"NSE:ABC": {"asset_class": "equity", "exchange": "NSE", "scope": "IPO"}}), encoding="utf-8")
    api_main.service.rename_in_watchlist("NSE:ABC", "OANDA:EURUSD", "Majors", {"scope": "Majors"})
    assert _cats(cats)["OANDA:EURUSD"]["scope"] == "Majors"


def test_nseix_is_index_instrument():
    info = scanner.categorize_symbol("NSEIX:NIFTY1!")
    assert (info["asset_class"], info["scope"]) == ("index", "Nifty indexes")


def _ohlc(symbol, source, day):
    return {"source": source, "symbol": symbol, "exchange": symbol.split(":")[0], "date": day,
            "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}


def test_purge_symbol_data_clears_db_and_caches(tmp_db, tmp_path, monkeypatch):
    from market_data import database

    ohlc_cache = tmp_path / "ohlc_cache.json"
    tracker = tmp_path / "tracker_state_cache.json"
    ohlc_cache.write_text(json.dumps({"NSE:ABC": {"D": {}}, "NSE:KEEP": {"D": {}}}), encoding="utf-8")
    tracker.write_text(json.dumps({"NSE:ABC": {"tier": "C"}}), encoding="utf-8")
    monkeypatch.setattr(api_main, "OHLC_CACHE_PATH", ohlc_cache)
    monkeypatch.setattr(api_main, "TRACKER_STATE_CACHE_PATH", tracker)
    database.upsert_ohlc([_ohlc("NSE:ABC", "NSE", "2026-01-02"), _ohlc("NSE:ABC", "TRADINGVIEW", "2026-01-02"),
                          _ohlc("NSE:KEEP", "NSE", "2026-01-02")])

    purged = api_main.service.purge_symbol_data("nse:abc")

    assert purged["ohlc_rows"] == 2 and purged["ohlc_cache"] == 1 and purged["tracker_state"] == 1
    kept = database.query_ohlc_multi("NSE", ["NSE:KEEP"])
    assert sum(len(rows) for rows in (kept.values() if isinstance(kept, dict) else [kept])) == 1
    assert set(_cats(ohlc_cache)) == {"NSE:KEEP"} and _cats(tracker) == {}
    assert api_main.service.purge_symbol_data("NSE:ABC")["ohlc_rows"] == 0


def test_remove_endpoint_purges_only_when_asked(wl_files, monkeypatch):
    from fastapi.testclient import TestClient

    watch, _ = wl_files
    watch.write_text("NSE:A\nNSE:B\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(api_main.service, "purge_symbol_data", lambda s: calls.append(s) or {"ohlc_rows": 0})
    client = TestClient(api_main.app)
    assert client.request("DELETE", "/api/watchlist", json={"symbol": "NSE:A"}).json()["purged"] is None
    assert client.request("DELETE", "/api/watchlist", json={"symbol": "NSE:B", "delete_data": True}).json()["purged"] == {"ohlc_rows": 0}
    watch.write_text("NSE:C\n", encoding="utf-8")
    client.put("/api/watchlist", json={"old_symbol": "NSE:C", "new_symbol": "NSE:C", "delete_old_data": True})
    client.put("/api/watchlist", json={"old_symbol": "NSE:C", "new_symbol": "NSE:D", "delete_old_data": True})
    assert calls == ["NSE:B", "NSE:C"]
