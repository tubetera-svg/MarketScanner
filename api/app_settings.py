"""Persisted app-level settings (automation, intervals, look-back days, show/hide).

Stored in config/app_settings.json. Strategy on/off flags keep their own file
(see strategy_bridge); this file holds the rest,
including the ``strategy`` parameter block (LTF confirmation timeframe,
propulsion mean-threshold definition) that src/all_strategy.py reads.
Defaults preserve pre-existing behavior: only the Silver Bullet auto-schedule
armed itself on boot, everything else was started manually.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "app_settings.json"

HIDEABLE_PAGES = ("watchlist", "ipo", "backtest")

# Currencies offered for the high-impact news filter (api/news_calendar.py).
NEWS_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY")

# Strategy parameters read by src/all_strategy.py (strategy_setting) and the
# LTF confirmation watcher. First choice = default; keep in step with
# all_strategy.STRATEGY_SETTING_CHOICES.
STRATEGY_CHOICES: dict[str, tuple[str, ...]] = {
    "ltf_timeframe": ("1h", "15m"),
    "propulsion_mean_threshold": ("range", "body"),
}

DEFAULTS: dict[str, Any] = {
    "automation": {
        "silver_bullet_auto": {"enabled": True},
        "ipo_scanner": {"enabled": False, "interval_minutes": 60, "lookback_days": 7},
        "data_auto_sync": {"enabled": False, "lookback_days": 14, "interval_hours": 0.25},
        "ltf_confirmation": {"enabled": False, "interval_minutes": 2},
        # Price alerts from the chart popup (api/price_alerts.py).
        "price_alerts": {"enabled": True, "interval_minutes": 15},
    },
    "strategy": {key: choices[0] for key, choices in STRATEGY_CHOICES.items()},
    # Daily-bar "final" cut-off per market, 'HH:MM' in the market's fixed
    # timezone (read by src/ict_scanner.daily_bar_cutoff; keep defaults in step
    # with DAILY_BAR_CUTOFF_DEFAULTS there).
    "data_cutoffs": {
        "nse": "17:00",          # IST, same trading day (bhavcopy published)
        "commodities": "17:00",  # New York, same day (daily rollover; forex too)
        "crypto": "00:00",       # UTC, next day
        "gift_nifty": "03:00",   # IST, next day (NSEIX)
    },
    # High-impact news filter; empty list = all currencies.
    "news": {"currencies": []},
    "ui": {
        "hidden_strategies": [],
        "hidden_pages": [],
    },
}

# (section, key) -> (min, max) for integer fields
_INT_LIMITS = {
    ("ipo_scanner", "interval_minutes"): (1, 1440),
    ("ipo_scanner", "lookback_days"): (1, 90),
    ("data_auto_sync", "lookback_days"): (1, 120),
    ("ltf_confirmation", "interval_minutes"): (1, 60),
    ("price_alerts", "interval_minutes"): (5, 240),
}

# (section, key) -> (min, max) for float fields
_FLOAT_LIMITS = {
    ("data_auto_sync", "interval_hours"): (0.25, 24.0),
}


def _merge(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    """Overlay ``patch`` onto ``base``, keeping only known keys and coercing types."""
    out = copy.deepcopy(base)
    if not isinstance(patch, dict):
        return out
    for key, default in base.items():
        if key not in patch:
            continue
        value = patch[key]
        if isinstance(default, dict):
            out[key] = _merge(default, value)
        elif isinstance(default, bool):
            out[key] = bool(value)
        elif isinstance(default, int):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, float):
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, list) and isinstance(value, list):
            out[key] = sorted({str(item).strip() for item in value if str(item).strip()})
        elif isinstance(default, str):
            out[key] = str(value).strip()
    return out


def _clamp(settings: dict[str, Any]) -> dict[str, Any]:
    for (section, key), (low, high) in _INT_LIMITS.items():
        block = settings["automation"][section]
        block[key] = max(low, min(high, int(block[key])))
    for (section, key), (low, high) in _FLOAT_LIMITS.items():
        block = settings["automation"][section]
        block[key] = max(low, min(high, float(block[key])))
    settings["ui"]["hidden_pages"] = [p for p in settings["ui"]["hidden_pages"] if p in HIDEABLE_PAGES]
    settings["news"]["currencies"] = sorted({c.upper() for c in settings["news"]["currencies"]} & set(NEWS_CURRENCIES))
    for key, choices in STRATEGY_CHOICES.items():
        if settings["strategy"][key] not in choices:
            settings["strategy"][key] = choices[0]
    for market, default in DEFAULTS["data_cutoffs"].items():
        settings["data_cutoffs"][market] = _normalize_hhmm(settings["data_cutoffs"][market]) or default
    return settings


def _normalize_hhmm(value: Any) -> str | None:
    """'H:MM' / 'HH:MM' (24h) -> 'HH:MM'; None when invalid."""
    try:
        hours, minutes = (int(part) for part in str(value).strip().split(":"))
    except (TypeError, ValueError):
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return f"{hours:02d}:{minutes:02d}"
    return None


def load_settings() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return _clamp(_merge(DEFAULTS, raw))


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the stored settings, persist, and return the result."""
    merged = _clamp(_merge(load_settings(), patch))
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return merged
