"""Alert-sound settings (Settings -> Alert sounds) are validated on save."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "api") not in sys.path:
    sys.path.insert(0, str(ROOT / "api"))

import app_settings  # noqa: E402


def _normalized(patch: dict) -> dict:
    return app_settings._clamp(app_settings._merge(app_settings.DEFAULTS, {"sounds": patch}))["sounds"]


def test_sound_defaults_use_known_choices():
    sounds = _normalized({})
    assert all(sounds[kind]["sound"] in app_settings.SOUND_CHOICES for kind in app_settings.SOUND_KINDS)
    assert sounds["news_event"]["enabled"] is False


def test_unknown_sound_falls_back_to_default():
    sounds = _normalized({"ltf": {"sound": "kazoo"}, "price_alert": {"sound": "siren", "enabled": False}})
    assert sounds["ltf"]["sound"] == app_settings.DEFAULTS["sounds"]["ltf"]["sound"]
    assert sounds["price_alert"] == {"enabled": False, "sound": "siren"}


def test_sound_numbers_are_clamped():
    sounds = _normalized({"volume": 250, "news_event": {"lead_minutes": 0}})
    assert sounds["volume"] == 100
    assert sounds["news_event"]["lead_minutes"] == 1
    assert _normalized({"volume": -5, "news_event": {"lead_minutes": 500}})["volume"] == 0
    assert _normalized({"news_event": {"lead_minutes": 500}})["news_event"]["lead_minutes"] == 60
    assert _normalized({"news_event": {"repeat": 0}})["news_event"]["repeat"] == 1
    assert _normalized({"news_event": {"repeat": 50}})["news_event"]["repeat"] == 3


def test_quiet_hours_times_are_normalized():
    quiet = _normalized({"quiet_hours": {"enabled": True, "start": "22:5", "end": "25:00"}})["quiet_hours"]
    assert quiet == {"enabled": True, "start": "22:05", "end": app_settings.DEFAULTS["sounds"]["quiet_hours"]["end"]}
