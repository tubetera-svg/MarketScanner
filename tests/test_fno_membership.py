"""NSE F&O list: local file only, refreshed on demand via preview -> apply."""

from __future__ import annotations

import json

import pytest

from api import main as api_main

fno = api_main.fno_membership
scanner = api_main.service.module
NSE_LIST = ["NEWFNO", "SWIGGY"] + [f"PAD{i}" for i in range(fno.MIN_EXPECTED_MEMBERS)]


@pytest.fixture()
def fno_files(tmp_path, monkeypatch):
    watch = tmp_path / "watchlist.txt"
    cats = tmp_path / "watchlist_categories.json"
    cache = tmp_path / "nse_fno_cache.json"
    for module in (fno, api_main):
        monkeypatch.setattr(module, "WATCHLIST_PATH", watch)
    monkeypatch.setattr(fno, "CATEGORIES_PATH", cats)
    monkeypatch.setattr(api_main, "WATCHLIST_CATEGORIES_PATH", cats)
    monkeypatch.setattr(fno, "CACHE_PATH", cache)
    watch.write_text("NSE:OLDFNO\nNSE:NEWFNO\nNSE:SWIGGY\nNSE:SMALL\nOANDA:XAUUSD\n", encoding="utf-8")
    cats.write_text(json.dumps({
        "NSE:OLDFNO": {"scope": "F&O"},
        "NSE:NEWFNO": {"scope": "Equity"},
        "NSE:SWIGGY": {"scope": "IPO"},
        "NSE:SMALL": {"scope": "IPO"},
        "OANDA:XAUUSD": {"scope": "Commodities"},
    }), encoding="utf-8")
    return watch, cats, cache


@pytest.fixture()
def nse_download(monkeypatch):
    calls = []

    class Stub:
        @staticmethod
        def load_default_symbols():
            calls.append(True)
            return list(NSE_LIST)

    monkeypatch.setattr(fno.strategy_bridge, "load_module", lambda: Stub)
    return calls


def test_add_uses_local_list_and_never_downloads(fno_files, nse_download):
    _, cats, cache = fno_files
    cache.write_text(json.dumps({"fetched_at": "2000-01-01T00:00:00", "symbols": ["INFY"]}), encoding="utf-8")
    api_main.service.add_to_watchlist("NSE:INFY")
    assert json.loads(cats.read_text(encoding="utf-8"))["NSE:INFY"]["scope"] == "F&O"
    assert nse_download == []


def test_add_without_saved_list_is_equity(fno_files, nse_download):
    _, cats, _ = fno_files
    api_main.service.add_to_watchlist("NSE:INFY")
    assert json.loads(cats.read_text(encoding="utf-8"))["NSE:INFY"]["scope"] == "Equity"
    assert nse_download == []


def test_preview_writes_nothing_then_apply_retags(fno_files, nse_download):
    _, cats, cache = fno_files
    before = cats.read_text(encoding="utf-8")

    preview = fno.preview(scanner)

    assert cats.read_text(encoding="utf-8") == before and not cache.exists()
    assert preview["to_equity"] == ["NSE:OLDFNO"] and preview["to_fno"] == ["NSE:NEWFNO"]
    assert preview["flag_updated"] == ["NSE:SWIGGY", "NSE:SMALL"]

    result = fno.apply(scanner, preview["preview_id"])

    data = json.loads(cats.read_text(encoding="utf-8"))
    assert result["to_equity"] == ["NSE:OLDFNO"]
    assert data["NSE:OLDFNO"] == {"scope": "Equity", "f_and_o": "Non-F&O"}
    assert data["NSE:NEWFNO"] == {"scope": "F&O", "f_and_o": "F&O"}
    assert data["NSE:SWIGGY"] == {"scope": "IPO", "f_and_o": "F&O"}
    assert data["NSE:SMALL"] == {"scope": "IPO", "f_and_o": "Non-F&O"}
    assert data["OANDA:XAUUSD"] == {"scope": "Commodities"}
    assert fno.cached_members() == set(NSE_LIST) and len(nse_download) == 1
    # A preview can be applied only once; a new preview now shows no changes.
    with pytest.raises(LookupError):
        fno.apply(scanner, preview["preview_id"])
    again = fno.preview(scanner)
    assert not (again["to_fno"] or again["to_equity"] or again["flag_updated"] or again["list_added"] or again["list_removed"])


def test_preview_rejects_incomplete_list(fno_files, monkeypatch):
    class Stub:
        @staticmethod
        def load_default_symbols():
            return ["ONLY", "TWO"]

    monkeypatch.setattr(fno.strategy_bridge, "load_module", lambda: Stub)
    with pytest.raises(RuntimeError):
        fno.preview(scanner)


def test_endpoints(fno_files, nse_download):
    from fastapi.testclient import TestClient

    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    assert client.get("/api/watchlist/fno").json() == {"saved_at": None, "count": 0}
    preview = client.post("/api/watchlist/fno/preview").json()
    assert client.post("/api/watchlist/fno/apply", json={"preview_id": "nope"}).status_code == 409
    applied = client.post("/api/watchlist/fno/apply", json={"preview_id": preview["preview_id"]}).json()
    assert applied["saved"]["count"] == len(NSE_LIST) and applied["to_fno"] == ["NSE:NEWFNO"]
    assert {item["symbol"] for item in applied["symbols"]} >= {"NSE:OLDFNO", "NSE:NEWFNO"}
