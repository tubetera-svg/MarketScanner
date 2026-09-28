"""NSE F&O membership for watchlist classification.

The F&O stock list lives locally in config/nse_fno_cache.json and is the only
source used when classifying symbols (`cached_members`) - nothing downloads it
automatically. Refreshing from NSE (fo_mktlots.csv via
all_strategy.load_default_symbols) is a manual two-step action from the
watchlist manager:

- `preview` downloads the list and reports what would change; writes nothing.
- `apply` saves that previewed list and re-tags the watchlist.

Re-tag rules (`_retag`): NSE equities get ``f_and_o`` = "F&O" / "Non-F&O", and
only the automatic scopes flip between "F&O" and "Equity". IPO / custom scopes
are never changed.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import strategy_bridge

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "config" / "nse_fno_cache.json"
WATCHLIST_PATH = ROOT / "config" / "watchlist.txt"
CATEGORIES_PATH = ROOT / "config" / "watchlist_categories.json"
# A garbled/partial download must not mass-reclassify the watchlist.
MIN_EXPECTED_MEMBERS = 100
PREVIEW_TTL = timedelta(minutes=15)

# Downloaded-but-not-applied lists, keyed by preview id (apply uses exactly
# what the user reviewed, not a fresh download).
_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        return None
    return data


def cached_members() -> set[str] | None:
    """F&O base tickers from the local list; None if it was never saved."""
    data = _read_cache()
    return {str(symbol).strip().upper() for symbol in data["symbols"]} if data else None


def cache_info() -> dict[str, Any]:
    data = _read_cache()
    return {"saved_at": data.get("fetched_at") if data else None, "count": len(data["symbols"]) if data else 0}


def _retag(scanner: Any, categories: dict[str, dict[str, str]], members: set[str]) -> dict[str, list[str]]:
    """Apply the re-tag rules to ``categories`` in place; returns what changed."""
    changes: dict[str, list[str]] = {"to_fno": [], "to_equity": [], "flag_updated": []}
    for symbol, _session in scanner.load_watchlist(str(WATCHLIST_PATH), allow_empty=True):
        try:
            detected = scanner.categorize_symbol(symbol)
        except ValueError:
            continue
        if detected["exchange"] != "NSE" or detected["asset_class"] != "equity":
            continue
        fields = categories.setdefault(symbol.upper(), {})
        if fields.get("asset_class", "equity") != "equity":
            continue  # user reclassified it (e.g. as an index)
        is_fno = detected["base"] in members
        flag = "F&O" if is_fno else "Non-F&O"
        flag_changed = fields.get("f_and_o") != flag
        fields["f_and_o"] = flag
        scope = fields.get("scope", detected["scope"])
        if scope == "F&O" and not is_fno:
            fields["scope"] = "Equity"
            changes["to_equity"].append(symbol)
        elif scope == "Equity" and is_fno:
            fields["scope"] = "F&O"
            changes["to_fno"].append(symbol)
        elif flag_changed:
            changes["flag_updated"].append(symbol)  # scope unchanged (IPO / custom / already right)
    return changes


def preview(scanner: Any) -> dict[str, Any]:
    """Download NSE's F&O list and report the changes it would make. Writes nothing."""
    symbols = sorted(set(strategy_bridge.load_module().load_default_symbols()))
    if len(symbols) < MIN_EXPECTED_MEMBERS:
        raise RuntimeError(f"NSE F&O list looks incomplete ({len(symbols)} symbols); nothing changed")
    members = set(symbols)
    categories = scanner.load_watchlist_categories(str(CATEGORIES_PATH))
    changes = _retag(scanner, categories, members)  # on a throwaway copy
    saved = cached_members()
    preview_id = uuid.uuid4().hex
    now = datetime.now()
    with _pending_lock:
        for key in [key for key, item in _pending.items() if now - item["created"] > PREVIEW_TTL]:
            del _pending[key]
        _pending[preview_id] = {"created": now, "symbols": symbols}
    return {
        "preview_id": preview_id,
        "members": len(symbols),
        "saved": cache_info(),
        "list_added": sorted(members - saved) if saved is not None else [],
        "list_removed": sorted(saved - members) if saved is not None else [],
        **changes,
    }


def apply(scanner: Any, preview_id: str) -> dict[str, Any]:
    """Save a previewed list locally and re-tag the watchlist."""
    with _pending_lock:
        pending = _pending.pop(preview_id, None)
    if pending is None or datetime.now() - pending["created"] > PREVIEW_TTL:
        raise LookupError("Preview expired or unknown - run the F&O refresh preview again")
    symbols = pending["symbols"]
    with scanner.watchlist_file_lock(str(WATCHLIST_PATH)):
        categories = scanner.load_watchlist_categories(str(CATEGORIES_PATH))
        changes = _retag(scanner, categories, set(symbols))
        if any(changes.values()):
            scanner.save_watchlist_categories(categories, str(CATEGORIES_PATH))
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "symbols": symbols}, indent=1) + "\n",
            encoding="utf-8",
        )
    log.info("F&O list applied (%d symbols): -> F&O %s; -> Equity %s", len(symbols), changes["to_fno"], changes["to_equity"])
    return {"members": len(symbols), "saved": cache_info(), **changes}
