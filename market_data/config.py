"""Market-data layer configuration.

Central place for:
- Database location (data/market_data.db)
- Source-selection environment flags:
    FETCH_TRADINGVIEW_DATA=true   -> allow TradingView/tvDatafeed fetching (commodities)
    FETCH_NSE_DATA=true           -> allow NSE Bhavcopy fetching (NSE stocks)
    AUTO_FETCH_MISSING_DATA=true  -> allow automatic backfill of missing dates
- Backdate-test sync window:
    BACKDATE_LOOKBACK_DAYS=14     -> days fetched/stored when a backdate test runs
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "market_data.db"

CONFIG_DIR = ROOT_DIR / "config"
SYMBOL_ALIASES_PATH = CONFIG_DIR / "symbol_aliases.json"

SOURCE_TRADINGVIEW = "TRADINGVIEW"
SOURCE_NSE = "NSE"
KNOWN_SOURCES = (SOURCE_TRADINGVIEW, SOURCE_NSE)

log = logging.getLogger(__name__)

FLAG_FETCH_TRADINGVIEW = "FETCH_TRADINGVIEW_DATA"
FLAG_FETCH_NSE = "FETCH_NSE_DATA"
FLAG_AUTO_FETCH_MISSING = "AUTO_FETCH_MISSING_DATA"

_FLAG_FOR_SOURCE = {
    SOURCE_TRADINGVIEW: FLAG_FETCH_TRADINGVIEW,
    SOURCE_NSE: FLAG_FETCH_NSE,
}

DB_PATH_ENV_VAR = "MARKET_DATA_DB_PATH"


def normalize_source(source: str) -> str:
    """Validate/normalize a source name ('nse' -> 'NSE'). Raises ValueError."""
    value = str(source or "").strip().upper()
    if value not in KNOWN_SOURCES:
        raise ValueError(
            f"Unknown source '{source}'. Supported sources: {', '.join(KNOWN_SOURCES)}"
        )
    return value


def env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean env flag. Accepts 1/0/true/false/yes/no (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def source_flag_name(source: str) -> str:
    return _FLAG_FOR_SOURCE[normalize_source(source)]


def source_enabled(source: str) -> bool:
    """True when fetching from `source` is allowed by its flag."""
    return env_flag(source_flag_name(source), default=True)


def auto_fetch_missing(default: bool = True) -> bool:
    """True when missing dates may be fetched automatically."""
    return env_flag(FLAG_AUTO_FETCH_MISSING, default=default)


def db_path() -> Path:
    """Resolve the SQLite file path (env override friendly for tests)."""
    override = os.environ.get(DB_PATH_ENV_VAR)
    if override and str(override).strip():
        return Path(str(override).strip())
    return DEFAULT_DB_PATH


def backdate_lookback_days(default: int = 14) -> int:
    """Calendar days of history ensured per symbol when a backdate test runs."""
    raw = os.environ.get("BACKDATE_LOOKBACK_DAYS")
    try:
        value = int(str(raw).strip()) if raw and str(raw).strip() else default
    except ValueError:
        value = default
    return max(1, min(value, 120))


def load_symbol_aliases() -> dict[str, list[str]]:
    """Map a watchlist symbol to fallback symbols tried when the primary fetch fails.

    Configured in config/symbol_aliases.json, for example::

        {
          "OANDA:XAUUSD": ["FOREXCOM:XAUUSD", "CAPITALCOM:XAUUSD"],
          "NSE:INFY": ["BSE:INFY"]
        }

    Keys and aliases are normalised to upper-case. Missing/unreadable files
    return an empty mapping (alias fallback is then a no-op).
    """
    path = SYMBOL_ALIASES_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not read symbol aliases %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, list[str]] = {}
    for key, value in raw.items():
        norm_key = str(key).strip().upper()
        if not norm_key:
            continue
        items = value if isinstance(value, list) else [value]
        norm_vals = [str(item).strip().upper() for item in items if str(item).strip()]
        if norm_vals:
            aliases[norm_key] = norm_vals
    return aliases


def save_symbol_aliases(aliases: dict[str, list[str]]) -> None:
    """Persist the alias map (normalised, upper-cased) to config/symbol_aliases.json.

    An empty list for a symbol removes that entry. Writes a pretty-printed JSON
    file so it stays human-editable alongside load_symbol_aliases().
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cleaned: dict[str, list[str]] = {}
    for key, value in (aliases or {}).items():
        norm_key = str(key).strip().upper()
        if not norm_key:
            continue
        items = value if isinstance(value, list) else [value]
        norm_vals = [str(item).strip().upper() for item in items if str(item).strip()]
        if norm_vals:
            cleaned[norm_key] = norm_vals
    SYMBOL_ALIASES_PATH.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
