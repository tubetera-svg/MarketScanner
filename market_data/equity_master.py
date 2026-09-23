"""NSE main-board equity master (``EQUITY_L.csv``) for the IPO eligibility gate.

NSE's ``EQUITY_L.csv`` is the authoritative list of *main-board listed equity
companies* (symbol, company name, series, listing date, ISIN). It deliberately
does **not** contain the non-equity instruments that nonetheless trade in the
``EQ`` series of the daily bhavcopy:

* ETFs / index funds (``BANKETFADD``, ``NIF10GETF``, ``LIQUIDBETA``, ``NIFTYBEES``…)
* Sovereign Gold Bonds (``SGBDEC26``…)
* Dated government securities (``628GS2032``, ``74GS2035``…)
* Rights entitlements (``ANOND-RE``, ``DUCON-RE1``…)

The bhavcopy has no ISIN column, so cross-checking a candidate against this file
is the only reliable way to answer *NSE -> Equity -> Main board -> EQ series*.

Caching / failure behaviour
---------------------------
The file is cached in memory and on disk (``data/nse_equity_master.csv``) and
refreshed at most once per ``IPO_MASTER_REFRESH_DAYS``. Every failure mode
(offline, NSE blocking, malformed CSV) degrades gracefully: callers receive an
empty/partial mapping and the caller (``market_data.ipo``) falls back to its
symbol-pattern heuristics. This module never raises.
"""

from __future__ import annotations

import csv
import logging
import time
from io import StringIO
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

from .config import DATA_DIR, IPO_MASTER_REFRESH_DAYS

log = logging.getLogger(__name__)

MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CACHE_PATH = DATA_DIR / "nse_equity_master.csv"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}

_CACHE: dict[str, str] = {}
_CACHE_LOADED_AT: float = 0.0


def _parse(content: str) -> dict[str, str]:
    """Return ``{SYMBOL: SERIES}`` from the equity-master CSV text."""
    out: dict[str, str] = {}
    reader = csv.DictReader(StringIO(content))
    if not reader.fieldnames:
        return out
    fields = {str(name).strip().upper(): name for name in reader.fieldnames if name}
    symbol_field = fields.get("SYMBOL")
    series_field = fields.get("SERIES")
    if not symbol_field or not series_field:
        return out
    for row in reader:
        symbol = str(row.get(symbol_field, "") or "").strip().upper()
        if symbol:
            out[symbol] = str(row.get(series_field, "") or "").strip().upper()
    return out


def _read_disk_cache() -> dict[str, str]:
    try:
        return _parse(CACHE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (FileNotFoundError, OSError) as exc:
        log.debug("Equity master cache unreadable (%s)", exc)
        return {}


def _download() -> dict[str, str]:
    try:
        request = Request(MASTER_URL, headers=_HEADERS)
        with urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8", errors="ignore")
        if not content or "<html>" in content.lower():
            log.warning("Equity master download returned HTML/empty body")
            return {}
        parsed = _parse(content)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("Equity master download failed: %s", exc)
        return {}
    if parsed:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(content, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not write equity master cache: %s", exc)
    return parsed



def load_equity_master(
    *,
    refresh_days: Optional[int] = None,
    allow_download: bool = True,
    force: bool = False,
) -> dict[str, str]:
    """Return ``{SYMBOL: SERIES}`` for the NSE main-board equity master.

    Empty (or stale/partial) results mean "unknown" — callers must treat that as
    "cannot verify", never as "not an equity". Never raises.
    """
    global _CACHE, _CACHE_LOADED_AT

    ttl = max(1, int(IPO_MASTER_REFRESH_DAYS if refresh_days is None else refresh_days))
    ttl_seconds = ttl * 24 * 3600
    now = time.time()

    if _CACHE and not force and (now - _CACHE_LOADED_AT) < ttl_seconds:
        return _CACHE

    if not force:
        try:
            fresh_on_disk = (now - CACHE_PATH.stat().st_mtime) < ttl_seconds
        except OSError:
            fresh_on_disk = False
        if fresh_on_disk:
            cached = _read_disk_cache()
            if cached:
                _CACHE, _CACHE_LOADED_AT = cached, now
                return _CACHE

    if allow_download:
        downloaded = _download()
        if downloaded:
            _CACHE, _CACHE_LOADED_AT = downloaded, now
            return _CACHE

    # Offline / blocked: fall back to whatever is on disk, however old.
    cached = _read_disk_cache()
    if cached:
        _CACHE, _CACHE_LOADED_AT = cached, now
        return _CACHE
    return _CACHE


def main_board_symbols(**kwargs) -> set[str]:
    """Symbols in the master trading in the main-board ``EQ`` series."""
    return {
        symbol
        for symbol, series in load_equity_master(**kwargs).items()
        if series == "EQ"
    }


def is_main_board_equity(symbol: str, **kwargs) -> Optional[bool]:
    """True/False when the master is available, ``None`` when it is not."""
    master = load_equity_master(**kwargs)
    if not master:
        return None
    key = str(symbol).split(":", 1)[-1].strip().upper()
    return master.get(key) == "EQ"


def reset_cache() -> None:
    """Test helper: drop the in-memory cache."""
    global _CACHE, _CACHE_LOADED_AT
    _CACHE, _CACHE_LOADED_AT = {}, 0.0


def cache_path() -> Path:
    """Expose the on-disk cache location (tests / diagnostics)."""
    return CACHE_PATH


__all__ = [
    "MASTER_URL",
    "cache_path",
    "is_main_board_equity",
    "load_equity_master",
    "main_board_symbols",
    "reset_cache",
]
