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

from market_data import state_store
from market_data.timeutil import DEFAULT_DISPLAY_TIMEZONE, DISPLAY_TIMEZONES

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "app_settings.json"

HIDEABLE_PAGES = ("watchlist", "ipo", "backtest")
# Pages a read-only guest may be allowed to open (Settings is always admin-only).
GUEST_PAGES = ("scanner", "alerts", "ipo", "watchlist", "backtest")

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
        # push (here and below): also send alerts to Telegram/ntfy (api/push.py, env vars).
        "silver_bullet_auto": {"enabled": True, "push": False},
        "ipo_scanner": {"enabled": False, "interval_minutes": 60, "lookback_days": 7},
        "data_auto_sync": {"enabled": False, "lookback_days": 14, "interval_hours": 0.25},
        "ltf_confirmation": {"enabled": False, "interval_minutes": 2, "push": False},
        # Price alerts from the chart popup (api/price_alerts.py).
        # near_pct: check every 5 min while price is within this % of a level
        # (0 = off).
        "price_alerts": {"enabled": True, "interval_minutes": 15, "near_pct": 0.5, "push": False},
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
        # Zone every displayed time uses (UI + push messages). Display only:
        # market logic keeps its fixed zones. See market_data/timeutil.py.
        "display_timezone": DEFAULT_DISPLAY_TIMEZONE,
    },
    # Browser alert sounds (frontend/components/alertSound.ts). Quiet hours are
    # 'HH:MM' in the display timezone (ui.display_timezone) and may wrap midnight; news_event.lead_minutes = how long
    # before a high-impact news / EIA release its sound plays; news_event.repeat =
    # how many times that sound plays back to back.
    "sounds": {
        "enabled": True,
        "volume": 70,
        "ltf": {"enabled": True, "sound": "chime"},
        "silver_bullet": {"enabled": True, "sound": "ping"},
        "price_alert": {"enabled": True, "sound": "doorbell"},
        "news_event": {"enabled": False, "sound": "bell", "lead_minutes": 5, "repeat": 3},
        "quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00"},
    },
    # Read-only guests (api/auth.py): pages they may open and their scan limits.
    "access": {
        "guest_pages": ["alerts", "ipo", "scanner"],
        "guest_max_symbols": 100,
        "guest_scan_cooldown_seconds": 60,
        "guest_past_dates": True,
        "guest_extra_info": False,
        "guest_banner": "Read-only view",
        "session_days": 30,
    },
}

# Sound ids offered per alert; keep in step with SOUNDS in alertSound.ts.
SOUND_CHOICES = ("chime", "ping", "doorbell", "beeps", "rising", "falling", "bell", "marimba", "siren", "tick",
                 "pulse_alarm", "harp", "sparkle", "notify_melody", "vibes", "fanfare")
SOUND_KINDS = ("ltf", "silver_bullet", "price_alert", "news_event")

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
    ("price_alerts", "near_pct"): (0.0, 10.0),
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
    access = settings["access"]
    access["guest_pages"] = [p for p in access["guest_pages"] if p in GUEST_PAGES]
    access["guest_max_symbols"] = max(1, min(500, int(access["guest_max_symbols"])))
    access["guest_scan_cooldown_seconds"] = max(0, min(3600, int(access["guest_scan_cooldown_seconds"])))
    access["session_days"] = max(1, min(365, int(access["session_days"])))
    access["guest_banner"] = access["guest_banner"][:200]
    if settings["ui"]["display_timezone"] not in DISPLAY_TIMEZONES:
        settings["ui"]["display_timezone"] = DEFAULT_DISPLAY_TIMEZONE
    settings["news"]["currencies"] = sorted({c.upper() for c in settings["news"]["currencies"]} & set(NEWS_CURRENCIES))
    for key, choices in STRATEGY_CHOICES.items():
        if settings["strategy"][key] not in choices:
            settings["strategy"][key] = choices[0]
    for market, default in DEFAULTS["data_cutoffs"].items():
        settings["data_cutoffs"][market] = _normalize_hhmm(settings["data_cutoffs"][market]) or default
    sounds = settings["sounds"]
    sounds["volume"] = max(0, min(100, int(sounds["volume"])))
    sounds["news_event"]["lead_minutes"] = max(1, min(60, int(sounds["news_event"]["lead_minutes"])))
    sounds["news_event"]["repeat"] = max(1, min(3, int(sounds["news_event"]["repeat"])))
    for kind in SOUND_KINDS:
        if sounds[kind]["sound"] not in SOUND_CHOICES:
            sounds[kind]["sound"] = DEFAULTS["sounds"][kind]["sound"]
    for edge in ("start", "end"):
        quiet = sounds["quiet_hours"]
        quiet[edge] = _normalize_hhmm(quiet[edge]) or DEFAULTS["sounds"]["quiet_hours"][edge]
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
        raw = json.loads(state_store.read_text(SETTINGS_PATH))
    except (OSError, ValueError):
        raw = {}
    return _clamp(_merge(DEFAULTS, raw))


def display_timezone() -> str:
    """Configured display zone (IANA name or "browser") for formatting times."""
    return load_settings()["ui"]["display_timezone"]


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the stored settings, persist, and return the result."""
    merged = _clamp(_merge(load_settings(), patch))
    state_store.write_text(SETTINGS_PATH, json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return merged
