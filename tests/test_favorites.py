"""Favorites: shared starred-symbol store behind /api/favorites."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import main as api_main

fav = api_main.favorites


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_PATH", tmp_path / "favorites.json")


def test_add_normalizes_and_dedupes():
    assert fav.add_favorite(" nse:infy ") == ["NSE:INFY"]
    assert fav.add_favorite("NSE:INFY") == ["NSE:INFY"]
    assert fav.add_favorite("NSE:TCS") == ["NSE:INFY", "NSE:TCS"]
    assert fav.load_favorites() == ["NSE:INFY", "NSE:TCS"]


def test_remove_and_missing_file():
    assert fav.load_favorites() == []
    fav.add_favorite("NSE:INFY")
    assert fav.remove_favorite("nse:infy") == []
    assert fav.remove_favorite("NSE:UNKNOWN") == []


def test_blank_symbol_rejected():
    with pytest.raises(ValueError):
        fav.add_favorite("   ")


def test_corrupt_file_reads_empty(tmp_path):
    fav.FAVORITES_PATH.write_text("not json", encoding="utf-8")
    assert fav.load_favorites() == []


def test_routes_round_trip():
    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    assert client.post("/api/favorites", json={"symbol": "nse:sbin"}).json() == {"symbols": ["NSE:SBIN"]}
    assert client.get("/api/favorites").json() == {"symbols": ["NSE:SBIN"]}
    assert client.request("DELETE", "/api/favorites", json={"symbol": "NSE:SBIN"}).json() == {"symbols": []}
    assert client.post("/api/favorites", json={"symbol": "   "}).status_code == 400


def test_remove_favorites_and_rename():
    fav.add_favorite("NSE:A")
    fav.add_favorite("NSE:B")
    assert fav.remove_favorites(["nse:a", "", "NSE:UNKNOWN"]) == ["NSE:B"]
    assert fav.rename_favorite("NSE:B", "nse:b2") == ["NSE:B2"]
    assert fav.rename_favorite("NSE:NOTSTARRED", "NSE:X") == ["NSE:B2"]


def test_watchlist_delete_drops_star(monkeypatch):
    fav.add_favorite("NSE:SBIN")
    monkeypatch.setattr(api_main.service, "remove_from_watchlist", lambda symbol: [])
    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    assert client.request("DELETE", "/api/watchlist", json={"symbol": "NSE:SBIN"}).status_code == 200
    assert fav.load_favorites() == []


def test_ipo_delete_drops_star(monkeypatch):
    from market_data import ipo

    fav.add_favorite("NSE:NEWIPO")
    fav.add_favorite("NSE:KEEP")
    monkeypatch.setattr(ipo, "remove_ipo_completely", lambda symbol: {})
    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    assert client.request("DELETE", "/api/market-data/ipo", json={"symbols": ["nse:newipo"]}).status_code == 200
    assert fav.load_favorites() == ["NSE:KEEP"]


def test_watchlist_rename_moves_star(monkeypatch):
    fav.add_favorite("NSE:OLD")
    monkeypatch.setattr(api_main.service, "rename_in_watchlist", lambda *args: [{"symbol": "NSE:NEW"}])
    monkeypatch.setattr(api_main.service.module, "categorize_symbol", lambda value: {"symbol": "NSE:" + value.upper()})
    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    response = client.put("/api/watchlist", json={"old_symbol": "NSE:OLD", "new_symbol": "new"})
    assert response.status_code == 200
    assert fav.load_favorites() == ["NSE:NEW"]
