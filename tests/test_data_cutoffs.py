"""Configurable per-market daily-bar cut-offs (Settings -> data_cutoffs)."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "src", ROOT / "api"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import app_settings  # noqa: E402
import ict_scanner  # noqa: E402
from market_data.service import bar_final_at, latest_final_session  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def _write_cutoffs(**cutoffs):
    path = Path(os.environ["MARKET_SCANNER_SETTINGS_PATH"])
    path.write_text(json.dumps({"data_cutoffs": cutoffs}), encoding="utf-8")
    # Force a re-read even when the mtime resolution hides the rewrite.
    ict_scanner._CUTOFF_CACHE["key"] = None


def test_defaults_without_settings_file():
    assert {m: ict_scanner.daily_bar_cutoff(m).strftime("%H:%M") for m in ict_scanner.DAILY_BAR_CUTOFF_DEFAULTS} == app_settings.DEFAULTS["data_cutoffs"]


def test_gift_cutoff_is_independent_and_configurable():
    now = datetime(2026, 9, 23, 3, 5, tzinfo=IST)
    assert latest_final_session("TRADINGVIEW", now, "NSEIX:NIFTY1!") == date(2026, 9, 22)
    _write_cutoffs(gift_nifty="04:00")
    assert latest_final_session("TRADINGVIEW", now, "NSEIX:NIFTY1!") == date(2026, 9, 21)
    assert bar_final_at("TRADINGVIEW", "NSEIX:NIFTY1!", date(2026, 9, 22)) == datetime(2026, 9, 23, 4, 0, tzinfo=IST)
    # Other markets keep their defaults.
    assert ict_scanner.daily_bar_cutoff("crypto").strftime("%H:%M") == "00:00"


def test_crypto_cutoff_configurable():
    now = datetime(2026, 9, 20, 0, 30, tzinfo=UTC)
    assert latest_final_session("TRADINGVIEW", now, "CRYPTO:BTCUSD") == date(2026, 9, 19)
    _write_cutoffs(crypto="01:00")
    assert latest_final_session("TRADINGVIEW", now, "CRYPTO:BTCUSD") == date(2026, 9, 18)


def test_nse_cutoff_drives_daily_bar_ready():
    now = datetime(2026, 9, 22, 17, 30, tzinfo=IST)
    assert ict_scanner.is_daily_bar_ready(ict_scanner.Session.NSE, now)
    _write_cutoffs(nse="18:00")
    assert not ict_scanner.is_daily_bar_ready(ict_scanner.Session.NSE, now)
    assert latest_final_session("NSE", now) == date(2026, 9, 21)


def test_invalid_value_falls_back_to_default():
    _write_cutoffs(commodities="25:99")
    assert ict_scanner.daily_bar_cutoff("commodities").strftime("%H:%M") == "17:00"


def test_app_settings_normalizes_cutoffs():
    merged = app_settings._clamp(app_settings._merge(
        app_settings.DEFAULTS, {"data_cutoffs": {"nse": "7:5", "crypto": "bad", "gift_nifty": "24:00"}}
    ))
    assert merged["data_cutoffs"] == {"nse": "07:05", "commodities": "17:00", "crypto": "00:00", "gift_nifty": "03:00"}
