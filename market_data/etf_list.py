"""NSE ETF securities list (``eq_etfseclist.csv``) for instrument classification.

NSE's ``eq_etfseclist.csv`` is the authoritative list of *ETF units* listed on
NSE (symbol, underlying asset, security name, ISIN …). ETF units trade in the
``EQ`` series of the daily bhavcopy, so a bhavcopy row cannot tell an ETF from a
company — and many ETF tickers look like company names (``ALPHA``, ``IT``,
``METAL``, ``VALUE``). ``market_data.ipo`` cross-checks this list so ETF /
index-fund units are never tracked as equity IPOs.

Caching / failure behaviour
---------------------------
Same contract as ``market_data.equity_master``: cached in memory and on disk
(``data/nse_etf_list.csv``) and refreshed at most once per
``IPO_MASTER_REFRESH_DAYS``. Every failure mode (offline, NSE blocking,
malformed CSV) degrades gracefully: callers receive an empty mapping, which means
"cannot verify" — never "not an ETF". This module never raises.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from io import StringIO
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

from .config import DATA_DIR, IPO_MASTER_REFRESH_DAYS

log = logging.getLogger(__name__)

ETF_LIST_URL = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"
CACHE_PATH = DATA_DIR / "nse_etf_list.csv"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}

#: ``{SYMBOL: security name}`` for the parsed ETF list.
_CACHE: dict[str, str] = {}
_CACHE_LOADED_AT: float = 0.0


def _parse(content: str) -> dict[str, str]:
    """Return ``{SYMBOL: security name}`` from the ETF-list CSV text."""
    out: dict[str, str] = {}
    reader = csv.DictReader(StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames:
        return out
    fields = {re.sub(r"[\s_]+", "", str(name).strip().upper()): name for name in reader.fieldnames if name}
    symbol_field = fields.get("SYMBOL")
    if not symbol_field:
        return out
    name_field = fields.get("SECURITYNAME")
    for row in reader:
        symbol = str(row.get(symbol_field, "") or "").strip().upper()
        if not symbol:
            continue
        name = str(row.get(name_field, "") or "").strip() if name_field else ""
        out[symbol] = name
    return out


def _read_disk_cache() -> dict[str, str]:
    try:
        return _parse(CACHE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (FileNotFoundError, OSError) as exc:
        log.debug("ETF list cache unreadable (%s)", exc)
        return {}


def _download() -> dict[str, str]:
    try:
        request = Request(ETF_LIST_URL, headers=_HEADERS)
        with urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8", errors="ignore")
        if not content or "<html>" in content.lower():
            log.warning("ETF list download returned HTML/empty body")
            return {}
        parsed = _parse(content)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("ETF list download failed: %s", exc)
        return {}
    if parsed:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(content, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not write ETF list cache: %s", exc)
    return parsed


def load_etf_names(
    *,
    refresh_days: Optional[int] = None,
    allow_download: bool = True,
    force: bool = False,
) -> dict[str, str]:
    """Return ``{SYMBOL: security name}`` for every NSE-listed ETF unit.

    Empty results mean "unknown" — callers must treat that as "cannot verify",
    never as "not an ETF". Never raises.
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


def load_etf_symbols(**kwargs) -> set[str]:
    """Symbols (without exchange prefix) of every NSE-listed ETF unit."""
    return set(load_etf_names(**kwargs))


def is_etf(symbol: str, **kwargs) -> Optional[bool]:
    """True/False when the ETF list is available, ``None`` when it is not."""
    etfs = load_etf_names(**kwargs)
    if not etfs:
        return None
    key = str(symbol).split(":", 1)[-1].strip().upper()
    return key in etfs


def reset_cache() -> None:
    """Test helper: drop the in-memory cache."""
    global _CACHE, _CACHE_LOADED_AT
    _CACHE, _CACHE_LOADED_AT = {}, 0.0


def cache_path() -> Path:
    """Expose the on-disk cache location (tests / diagnostics)."""
    return CACHE_PATH


__all__ = [
    "ETF_LIST_URL",
    "cache_path",
    "is_etf",
    "load_etf_names",
    "load_etf_symbols",
    "reset_cache",
]
