"""Tests for TradingView symbol resolution (market_data/tv_symbol.py).

The module caches resolutions in `tv_symbol_cache`; these tests stub the
network (`_search`) so no upstream calls are made.
"""

from __future__ import annotations

import pytest

from market_data import database
from market_data import tv_symbol as tv


@pytest.fixture()
def tmp(tmp_path):
    """Standalone temp DB file (same pattern as tests/test_ipo.py)."""
    db_file = tmp_path / "tv_symbol_test.db"
    database.init_db(db_file)
    return db_file


# ------------------------------------------------------------------ helpers
def test_strip_exchange():
    assert tv.strip_exchange("NSE:ACHYUT") == "ACHYUT"
    assert tv.strip_exchange("nse:achyut") == "ACHYUT"
    assert tv.strip_exchange("ACHYUT") == "ACHYUT"
    assert tv.strip_exchange("  ") == ""


def test_pick_symbol_prefers_nse():
    assert tv.pick_symbol(["BSE:ACHYUT", "NSE:ACHYUT"]) == "NSE:ACHYUT"
    # BSE is the documented fallback when TradingView has no NSE listing.
    assert tv.pick_symbol(["BSE:ACHYUT"]) == "BSE:ACHYUT"
    # Other exchanges / no hits are not usable.
    assert tv.pick_symbol(["NASDAQ:AAPL"]) is None
    assert tv.pick_symbol([]) is None


def test_search_keeps_exact_preferred_matches_only(monkeypatch):
    """Only exact symbol matches on NSE/BSE survive, with <em> stripped."""

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json

            return json.dumps(
                {
                    "symbols": [
                        {"exchange": "NSE", "symbol": "<em>AB</em>C"},
                        {"exchange": "BSE", "symbol": "ABC"},
                        {"exchange": "NSE", "symbol": "ABCD"},  # not exact
                        {"exchange": "NASDAQ", "symbol": "ABC"},  # not preferred
                    ]
                }
            ).encode()

    monkeypatch.setattr(tv.urllib.request, "urlopen", lambda *a, **k: _Response())
    assert tv._search("ABC") == ["NSE:ABC", "BSE:ABC"]


# ------------------------------------------------------------------ resolution
def test_resolve_caches_and_reuses(tmp, monkeypatch):
    calls = []

    def fake_search(base):
        calls.append(base)
        return ["NSE:ACHYUT"]

    monkeypatch.setattr(tv, "_search", fake_search)

    first = tv.resolve_tv_symbol("NSE:ACHYUT", db_path=tmp)
    assert first["tv_symbol"] == "NSE:ACHYUT"
    assert first["exchange"] == "NSE"
    assert first["cached"] is False
    assert calls == ["ACHYUT"]

    # Second call must be served from the cache - no further network hit.
    second = tv.resolve_tv_symbol("NSE:ACHYUT", db_path=tmp)
    assert second["tv_symbol"] == "NSE:ACHYUT"
    assert second["cached"] is True
    assert calls == ["ACHYUT"]


def test_resolve_falls_back_to_bse(tmp, monkeypatch):
    monkeypatch.setattr(tv, "_search", lambda base: ["BSE:ACHYUT"])
    result = tv.resolve_tv_symbol("NSE:ACHYUT", db_path=tmp)
    assert result["tv_symbol"] == "BSE:ACHYUT"
    assert result["exchange"] == "BSE"
    cached = database.query_tv_symbol(["NSE:ACHYUT"], db_path=tmp)["NSE:ACHYUT"]
    assert cached["tv_symbol"] == "BSE:ACHYUT"


def test_resolve_negative_result_is_cached(tmp, monkeypatch):
    """A symbol TradingView does not carry is remembered, not re-queried."""
    calls = []

    def fake_search(base):
        calls.append(base)
        return []

    monkeypatch.setattr(tv, "_search", fake_search)

    first = tv.resolve_tv_symbol("NSE:NOSUCH", db_path=tmp)
    assert first["tv_symbol"] is None

    second = tv.resolve_tv_symbol("NSE:NOSUCH", db_path=tmp)
    assert second["tv_symbol"] is None
    assert second["cached"] is True
    assert calls == ["NOSUCH"]  # only one live lookup


def test_resolve_network_failure_does_not_poison_cache(tmp, monkeypatch):
    def boom(base):
        raise OSError("network down")

    monkeypatch.setattr(tv, "_search", boom)
    result = tv.resolve_tv_symbol("NSE:FAIL", db_path=tmp)
    assert result["error"] is True
    assert result["tv_symbol"] is None
    # Nothing cached -> a later call retries instead of remembering the failure.
    assert database.query_tv_symbol(["NSE:FAIL"], db_path=tmp) == {}


def test_resolve_requires_symbol(tmp):
    with pytest.raises(ValueError):
        tv.resolve_tv_symbol("   ", db_path=tmp)


def test_resolve_refresh_bypasses_cache(tmp, monkeypatch):
    monkeypatch.setattr(tv, "_search", lambda base: ["NSE:ABC"])
    tv.resolve_tv_symbol("NSE:ABC", db_path=tmp)

    calls = []

    def fake_search(base):
        calls.append(base)
        return ["BSE:ABC"]  # mapping changed upstream

    monkeypatch.setattr(tv, "_search", fake_search)
    refreshed = tv.resolve_tv_symbol("NSE:ABC", refresh=True, db_path=tmp)
    assert refreshed["cached"] is False
    assert refreshed["tv_symbol"] == "BSE:ABC"
    assert calls == ["ABC"]


# ------------------------------------------------------------------ batch
def test_resolve_batch_uses_cache_and_respects_limit(tmp, monkeypatch):
    # Pre-cache one symbol.
    database.upsert_tv_symbol(
        {"symbol": "NSE:CACHED", "tv_symbol": "NSE:CACHED", "exchange": "NSE"},
        db_path=tmp,
    )
    calls = []

    def fake_search(base):
        calls.append(base)
        return [f"NSE:{base}"]

    monkeypatch.setattr(tv, "_search", fake_search)
    monkeypatch.setattr(tv, "LOOKUP_DELAY", 0)  # keep the test fast

    out = tv.resolve_tv_symbols(
        ["NSE:CACHED", "NSE:AAA", "NSE:BBB", "NSE:CCC"], limit=2, db_path=tmp
    )
    assert out["NSE:CACHED"]["cached"] is True
    assert calls == ["AAA", "BBB"]  # limit caps live lookups
    assert "NSE:CCC" not in out  # deferred to the next call


def test_resolve_batch_empty():
    assert tv.resolve_tv_symbols([]) == {}
    assert tv.resolve_tv_symbols(["  "]) == {}


# ------------------------------------------------------------------ storage
def test_tv_symbol_cache_roundtrip(tmp):
    assert database.upsert_tv_symbol(
        {"symbol": "NSE:XYZ", "tv_symbol": "BSE:XYZ", "exchange": "BSE"}, db_path=tmp
    ) == 1
    stored = database.query_tv_symbol(["NSE:XYZ"], db_path=tmp)["NSE:XYZ"]
    assert stored["tv_symbol"] == "BSE:XYZ"
    assert stored["exchange"] == "BSE"
    assert stored["resolved_at"]
    # Upsert replaces rather than duplicating.
    database.upsert_tv_symbol(
        {"symbol": "NSE:XYZ", "tv_symbol": "NSE:XYZ", "exchange": "NSE"}, db_path=tmp
    )
    assert len(database.query_tv_symbol(db_path=tmp)) == 1
    assert database.remove_tv_symbol("NSE:XYZ", db_path=tmp) == 1
    assert database.query_tv_symbol(db_path=tmp) == {}