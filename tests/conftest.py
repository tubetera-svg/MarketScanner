"""Shared fixtures for market_data tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the whole market_data package at a throwaway SQLite file."""
    db_file = tmp_path / "market_data_test.db"
    monkeypatch.setenv("MARKET_DATA_DB_PATH", str(db_file))
    from market_data import database

    database.init_db(db_file)
    return db_file


@pytest.fixture()
def clean_flags(monkeypatch):
    """Reset source flags to enabled + auto-fetch on for each test."""
    monkeypatch.setenv("FETCH_TRADINGVIEW_DATA", "true")
    monkeypatch.setenv("FETCH_NSE_DATA", "true")
    monkeypatch.setenv("AUTO_FETCH_MISSING_DATA", "true")
    return monkeypatch
