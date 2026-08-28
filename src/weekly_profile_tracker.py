"""Cross-scan persistence for weekly-profile setups.

The weekly-profile evaluators in all_strategy.py are point-in-time: each call
recomputes the whole current week from scratch and only reports the *latest*
state (armed / triggered / invalidated / expired). That makes a forming setup
invisible once the week rolls over, and there is no record that it ever
existed, so SL/target outcomes can never be tracked.

This module keeps a lightweight, append-only store keyed by
``(symbol, profile, week_start)`` so a setup can be followed day-by-day:

    armed -> triggered -> closed_sl | closed_target
    armed -> invalidated
    armed -> expired

Each transition is recorded as an event with a timestamp + the note/price that
caused it, mirroring the ICT scanner's TrackerStateCache pattern (atomic
tmp-file write so a crash mid-write never corrupts the file).

The store is intentionally decoupled from all_strategy.py: hit-detection needs
live prices, which the caller supplies via ``price_fn`` (the API bridge wires
this to the existing bhavcopy fetch path).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "config", "weekly_profile_tracker.json")

# States in which a setup is still "live" (open in the market).
ACTIVE_STATES = {"armed", "triggered"}
# Terminal states that should not be overwritten by a later scan.
TERMINAL_STATES = {"closed_sl", "closed_target", "invalidated", "expired"}


def _week_start(as_of_date: date) -> date:
    """Monday of the trading week containing ``as_of_date`` (Mon=0)."""
    return as_of_date - timedelta(days=as_of_date.weekday())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _evaluate_close(rec: Dict[str, Any], price: Dict[str, float]) -> Optional[str]:
    """Return a terminal state if ``price`` crossed the stored SL/target.

    SL is checked first (conservative: assume the stop was touched before the
    target when both sit inside the same bar).
    """
    direction = int(rec.get("direction") or 0)
    sl = rec.get("sl")
    target = rec.get("target")
    if direction > 0:  # long
        if sl is not None and price["low"] <= float(sl):
            return "closed_sl"
        if target is not None and price["high"] >= float(target):
            return "closed_target"
    elif direction < 0:  # short
        if sl is not None and price["high"] >= float(sl):
            return "closed_sl"
        if target is not None and price["low"] <= float(target):
            return "closed_target"
    return None


class ProfileTrackerStore:
    """Persisted, event-sourced tracker for weekly-profile setups."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_PATH

    # ---------------------------------------------------------------
    # Load / save (atomic)
    # ---------------------------------------------------------------
    def load(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---------------------------------------------------------------
    # Ingest a scan's rows
    # ---------------------------------------------------------------
    def ingest(
        self,
        rows: List[Dict[str, Any]],
        as_of_date: date,
        price_fn: Optional[Callable[[str, date], Optional[Dict[str, float]]]] = None,
    ) -> Dict[str, Any]:
        """Fold one scan's result rows into the store.

        ``rows`` are the bullish+bearish records (each carries ``symbol``,
        ``profile``, ``state``, ``direction``, ``entry``, ``sl``, ``target``,
        ``rr``, ``track_mode``, ``note``). ``price_fn(symbol, as_of_date)``
        returns the latest bar ``{"high","low","close"}`` for SL/target
        hit-detection on already-triggered setups.

        Returns a summary with the full store plus any new alerts
        (newly triggered, stopped, or target hit) for surfacing to the UI.
        """
        data = self.load()
        alerts: List[Dict[str, Any]] = []
        week = _week_start(as_of_date).isoformat()

        for row in rows:
            symbol = str(row.get("symbol"))
            profile = str(row.get("profile"))
            key = f"{symbol}|{profile}|{week}"
            new_state = str(row.get("state") or "armed")
            direction = int(row.get("direction") or 0)

            rec = data.get(key)
            if rec is not None and rec.get("state") in TERMINAL_STATES:
                # Terminal setups (closed / invalidated / expired) stay put.
                continue

            # Re-check SL/target for a still-open triggered setup using fresh price.
            if rec is not None and rec.get("state") == "triggered" and price_fn is not None:
                price = price_fn(symbol, as_of_date)
                if price:
                    hit = _evaluate_close(rec, price)
                    if hit:
                        new_state = hit

            if rec is None:
                rec = {
                    "symbol": symbol,
                    "profile": profile,
                    "week": week,
                    "state": new_state,
                    "direction": direction,
                    "entry": row.get("entry"),
                    "sl": row.get("sl"),
                    "target": row.get("target"),
                    "rr": row.get("rr"),
                    "track_mode": row.get("track_mode"),
                    "first_seen": as_of_date.isoformat(),
                    "triggered_date": as_of_date.isoformat() if new_state == "triggered" else None,
                    "last_seen": as_of_date.isoformat(),
                    "events": [],
                }
                self._add_event(rec, new_state, row.get("note"), as_of_date)
                if new_state == "triggered":
                    alerts.append(self._alert(rec, "triggered"))
            else:
                # Don't regress a triggered setup back to armed.
                if rec.get("state") == "triggered" and new_state == "armed":
                    new_state = "triggered"
                rec["last_seen"] = as_of_date.isoformat()
                if new_state == "triggered":
                    for field in ("entry", "sl", "target", "rr", "direction", "track_mode"):
                        rec[field] = row.get(field)
                    # Lock in the day the signal actually fired (first time only).
                    rec.setdefault("triggered_date", as_of_date.isoformat())
                if new_state != rec.get("state"):
                    self._add_event(rec, new_state, row.get("note"), as_of_date)
                    rec["state"] = new_state
                    if new_state == "triggered":
                        alerts.append(self._alert(rec, "triggered"))
                    elif new_state in ("closed_sl", "closed_target"):
                        alerts.append(self._alert(rec, new_state))

            data[key] = rec

        self.save(data)
        return {"store": data, "alerts": alerts}

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------
    @staticmethod
    def _add_event(rec: Dict[str, Any], state: str, note: Any, as_of_date: Optional[date] = None) -> None:
        rec.setdefault("events", []).append(
            {
                "ts": _now_iso(),
                "date": as_of_date.isoformat() if as_of_date is not None else None,
                "state": state,
                "note": note,
            }
        )

    @staticmethod
    def _alert(rec: Dict[str, Any], kind: str) -> Dict[str, Any]:
        return {
            "symbol": rec.get("symbol"),
            "profile": rec.get("profile"),
            "week": rec.get("week"),
            "kind": kind,
            "state": rec.get("state"),
            "direction": rec.get("direction"),
            "entry": rec.get("entry"),
            "sl": rec.get("sl"),
            "target": rec.get("target"),
            "rr": rec.get("rr"),
        }

    # ---------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------
    def active(self) -> List[Dict[str, Any]]:
        """Setups still open (armed or triggered), newest first."""
        rows = [r for r in self.load().values() if r.get("state") in ACTIVE_STATES]
        rows.sort(key=lambda r: r.get("last_seen", ""), reverse=True)
        return rows

    def all_setups(self) -> List[Dict[str, Any]]:
        rows = list(self.load().values())
        rows.sort(key=lambda r: (r.get("last_seen", ""), r.get("symbol", "")), reverse=True)
        return rows

    # ---------------------------------------------------------------
    # Maintenance
    # ---------------------------------------------------------------
    def clear(self) -> int:
        """Wipe the store (it is derived cache, not source-of-truth data)."""
        data = self.load()
        count = len(data)
        self.save({})
        return count

    def repair_store(self) -> int:
        """Fix records written by the pre-1:2-target bug.

        The bug placed a long target *behind* the entry (or produced R:R < 1)
        and, because the bogus target sat below entry, falsely flagged many
        longs as ``closed_target`` on any dip. This recomputes such targets to
        the valid-side measured move (entry +/- 2x risk = 1:2 R:R) and reverts
        false ``closed_target`` flags back to ``triggered``. Stop levels were
        always correct, so ``closed_sl`` is left untouched.

        Returns the number of records modified.
        """
        data = self.load()
        fixed = 0
        for key, rec in data.items():
            direction = int(rec.get("direction") or 0)
            entry = rec.get("entry")
            sl = rec.get("sl")
            if direction == 0 or entry is None or sl is None:
                continue
            target = rec.get("target")
            rr = rec.get("rr")
            wrong_side = (direction > 0 and target is not None and target <= entry) or (
                direction < 0 and target is not None and target >= entry
            )
            bad_rr = rr is not None and rr < 1.0
            if wrong_side or bad_rr:
                new_target = (entry + 2.0 * (entry - sl)) if direction > 0 else (entry - 2.0 * (sl - entry))
                rec["target"] = round(float(new_target), 4)
                rec["rr"] = 2.0
                self._add_event(rec, rec.get("state", "triggered"), "repaired: target recomputed to valid side (1:2 R:R)", None)
                fixed += 1
            if rec.get("state") == "closed_target":
                # Close was driven by the invalid (sub-entry) target, not a real fill.
                # If that week has already passed, the setup is no longer live -> expired.
                week_str = rec.get("week")
                if week_str and week_str < _week_start(date.today()).isoformat():
                    rec["state"] = "expired"
                    self._add_event(rec, "expired", "repaired: false closed_target (invalid target); week closed", None)
                else:
                    rec["state"] = "triggered"
                    self._add_event(rec, "triggered", "repaired: closed_target was based on an invalid target", None)
                fixed += 1
        if fixed:
            self.save(data)
        return fixed
