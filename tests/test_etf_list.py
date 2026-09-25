"""Tests for market_data.etf_list module."""

from __future__ import annotations

from pathlib import Path
import pytest
from market_data import etf_list

SAMPLE_ETF_CSV = """SYMBOL,SECURITY NAME,SERIES,DATE OF LISTING,PAID UP VALUE
NIFTYBEES,Nippon India ETF Nifty 50 BeES,EQ,08-Jan-2002,1.00
BANKBEES,Nippon India ETF Nifty Bank BeES,EQ,27-May-2004,10.00
ALPHA,Kotak Nifty Alpha 50 ETF,EQ,15-Dec-2020,1.00
"""


def test_parse_etf_symbols():
    names = etf_list._parse(SAMPLE_ETF_CSV)
    assert set(names.keys()) == {"NIFTYBEES", "BANKBEES", "ALPHA"}
    assert names["NIFTYBEES"] == "Nippon India ETF Nifty 50 BeES"


def test_parse_etf_symbols_empty():
    assert etf_list._parse("") == {}
    assert etf_list._parse("INVALID,HEADER\n1,2") == {}


def test_load_etf_symbols_disk_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / "test_etf.csv"
    cache_file.write_text(SAMPLE_ETF_CSV, encoding="utf-8")
    monkeypatch.setattr(etf_list, "CACHE_PATH", cache_file)
    monkeypatch.setattr(etf_list, "_CACHE", {})
    monkeypatch.setattr(etf_list, "_CACHE_LOADED_AT", 0.0)

    # Disable download to force reading disk cache
    monkeypatch.setattr(etf_list, "_download", lambda: {})

    result = etf_list.load_etf_symbols(force=True, allow_download=False)
    assert "NIFTYBEES" in result
    assert "BANKBEES" in result
    assert "ALPHA" in result
    assert len(result) == 3


def test_load_etf_symbols_non_raising_when_all_fail(tmp_path, monkeypatch):
    cache_file = tmp_path / "nonexistent.csv"
    monkeypatch.setattr(etf_list, "CACHE_PATH", cache_file)
    monkeypatch.setattr(etf_list, "_CACHE", {})
    monkeypatch.setattr(etf_list, "_CACHE_LOADED_AT", 0.0)

    monkeypatch.setattr(etf_list, "_download", lambda: {})

    result = etf_list.load_etf_symbols(force=True, allow_download=False)
    assert result == set()


def test_is_etf(monkeypatch):
    monkeypatch.setattr(etf_list, "load_etf_names", lambda **kwargs: {"NIFTYBEES": "ETF 1"})
    assert etf_list.is_etf("NSE:NIFTYBEES") is True
    assert etf_list.is_etf("NIFTYBEES") is True
    assert etf_list.is_etf("RELIANCE") is False

