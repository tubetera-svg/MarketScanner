"""Starred symbols shared by the scanner, Watchlist and IPO pages.

Stored in config/favorites.json as ``{"symbols": [...]}``. Symbols use the
watchlist form (``NSE:INFY``), upper-cased and de-duplicated. Deleting a symbol
(watchlist or IPO delete) drops its star; renaming it moves the star.
Display/filter only: favorites never change what or how the scanner scans.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAVORITES_PATH = ROOT / "config" / "favorites.json"

_lock = threading.Lock()


def _normalize(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if not value:
        raise ValueError("Symbol is required")
    return value


def _read() -> list[str]:
    try:
        raw = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
    out: list[str] = []
    for item in symbols if isinstance(symbols, list) else []:
        value = str(item).strip().upper()
        if value and value not in out:
            out.append(value)
    return out


def _write(symbols: list[str]) -> None:
    FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAVORITES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"symbols": symbols}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, FAVORITES_PATH)


def load_favorites() -> list[str]:
    with _lock:
        return _read()


def add_favorite(symbol: str) -> list[str]:
    value = _normalize(symbol)
    with _lock:
        symbols = _read()
        if value not in symbols:
            symbols.append(value)
            _write(symbols)
        return symbols


def remove_favorite(symbol: str) -> list[str]:
    value = _normalize(symbol)
    with _lock:
        symbols = _read()
        if value in symbols:
            symbols.remove(value)
            _write(symbols)
        return symbols


def remove_favorites(symbols: list[str]) -> list[str]:
    """Drop stars for deleted symbols (unknown / blank symbols are ignored)."""
    gone = {str(symbol).strip().upper() for symbol in symbols} - {""}
    with _lock:
        symbols_now = _read()
        kept = [symbol for symbol in symbols_now if symbol not in gone]
        if kept != symbols_now:
            _write(kept)
        return kept


def rename_favorite(old_symbol: str, new_symbol: str) -> list[str]:
    """Move a star to the renamed symbol; no-op when the old symbol was not starred."""
    old, new = _normalize(old_symbol), _normalize(new_symbol)
    with _lock:
        symbols = _read()
        if old not in symbols:
            return symbols
        symbols = [new if symbol == old else symbol for symbol in symbols]
        symbols = list(dict.fromkeys(symbols))
        _write(symbols)
        return symbols
