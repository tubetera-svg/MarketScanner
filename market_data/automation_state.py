"""Restart-safe markers for the background automations.

Each automation keeps the small "already done" facts it needs to avoid repeating
work after an API restart (LTF arm sessions, the last IPO scan, synced sessions,
Silver Bullet pushes) in one section of ``data/state/automation_state.json``.
Reads and writes go through ``state_store``, so ``APP_STATE_STORE=db`` keeps the
file as an ``app_state`` row like the other runtime state.

A missing or unreadable file means "nothing done yet", which is the same as a
fresh start before this existed. Failures are logged, never raised.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from . import state_store

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "state" / "automation_state.json"
# Sections are written from the event loop and from the auto-sync thread.
_LOCK = threading.Lock()


def _read_all(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_store.read_text(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); starting without saved automation state.", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


class AutomationState:
    """One automation's section of the shared state file."""

    def __init__(self, section: str, path: Path | str = DEFAULT_PATH) -> None:
        self.section = section
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        with _LOCK:
            value = _read_all(self.path).get(self.section)
        return value if isinstance(value, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        with _LOCK:
            everything = _read_all(self.path)
            everything[self.section] = data
            try:
                if not state_store.handles(self.path):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                state_store.write_text(self.path, json.dumps(everything, indent=2, sort_keys=True) + "\n")
            except OSError as exc:
                log.warning("Could not save automation state %s (%s).", self.section, exc)


__all__ = ["AutomationState", "DEFAULT_PATH"]
