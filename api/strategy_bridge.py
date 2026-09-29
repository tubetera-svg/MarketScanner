"""Bridge between the FastAPI app and all_strategy.py.

Loads all_strategy.py by path (same importlib pattern api/main.py uses for the
ICT scanner), keeps per-strategy on/off flags in strategy_flags.json next to
the other config files, and runs strategy scans whose results are returned
clubbed per strategy (one block per strategy with its bullish/bearish rows).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ALL_STRATEGY_PATH = ROOT / "src" / "all_strategy.py"
FLAGS_PATH = ROOT / "config" / "strategy_flags.json"
OUTPUT_DIR = ROOT / "strategy_outputs"
INFO_PATH = ROOT / "config" / "strategy_info.txt"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from market_data.service import is_crypto_symbol  # noqa: E402

CORE_STRATEGIES = [
    ("inside_bar_pattern_daily_sweep", "Inside Bar Pattern"),
    ("ema5_sweep", "EMA5 Sweep"),
    ("multi_timeframe_bias", "Multi-TimeFrame Bias"),
    ("daily_bias_invalidation", "Daily Bias Invalidation"),
    ("protected_swings", "Protected Swings"),
    ("points_of_interest", "Points of Interest"),
    ("candle_3_closure", "Candle 3 Closure"),
    ("propulsion_blocks", "Propulsion Blocks"),
]

WEEKLY_STRATEGIES = [
    ("classic_expansion_sweep", "Classic Expansion"),
    ("midweek_reversal_sweep", "Midweek Reversal"),
    ("consolidation_reversal_sweep", "Consolidation Reversal"),
    ("intraweek_reversal_sweep", "Intraweek Reversal"),
    ("thursday_counter_sweep", "Thursday Counter"),
    ("tgif_setup_sweep", "TGIF Setup"),
]

_CATALOG = (
    [(name, label, "Core") for name, label in CORE_STRATEGIES]
    + [(name, label, "Weekly profiles") for name, label in WEEKLY_STRATEGIES]
)
_WEEKLY_NAMES = {name for name, _ in WEEKLY_STRATEGIES}

_module: Any = None


def load_module() -> Any:
    """Import all_strategy.py once and reuse the module on later calls."""
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location("all_strategy_bridge", ALL_STRATEGY_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {ALL_STRATEGY_PATH.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _module = module
    return _module


def _read_overrides() -> dict[str, bool]:
    try:
        data = json.loads(FLAGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key).strip(): bool(value) for key, value in data.items()}


def _write_overrides(overrides: dict[str, bool]) -> None:
    FLAGS_PATH.write_text(json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_strategy_descriptions() -> dict[str, str]:
    """Parse config/strategy_info.txt and return strategy_name -> description."""
    if not INFO_PATH.exists():
        return {}
    text = INFO_PATH.read_text(encoding="utf-8")
    descriptions: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("### `") and line.endswith("`"):
            if current_name is not None:
                descriptions[current_name] = "\n".join(current_lines).strip()
            current_name = line[5:-1]
            current_lines = []
        elif current_name is not None and line.startswith("##"):
            descriptions[current_name] = "\n".join(current_lines).strip()
            current_name = None
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        descriptions[current_name] = "\n".join(current_lines).strip()
    return {key: value for key, value in descriptions.items() if value}


def list_strategies() -> tuple[list[dict[str, Any]], bool]:
    """Catalog of every strategy plus the weekly-profiles master switch state."""
    module = load_module()
    registered = set(module.strategy_registry().keys())
    overrides = _read_overrides()
    descriptions = _load_strategy_descriptions()
    catalog: list[dict[str, Any]] = []
    for name, label, group in _CATALOG:
        runnable = name in registered
        stored = overrides.get(name)
        enabled = runnable if stored is None else bool(stored) and runnable
        catalog.append({
            "name": name,
            "label": label,
            "group": group,
            "enabled": bool(enabled),
            "runnable": runnable,
            "description": descriptions.get(name),
        })
    return catalog, bool(module.WEEKLY_PROFILES_ENABLED)


def set_strategy_flag(name: str, enabled: bool) -> None:
    """Persist one strategy on/off toggle into strategy_flags.json."""
    valid = {entry[0]: entry[1] for entry in _CATALOG}
    if name not in valid:
        raise ValueError(f"Unknown strategy '{name}'. Valid: {', '.join(sorted(valid))}")
    module = load_module()
    registered = set(module.strategy_registry().keys())
    if enabled and name not in registered and name in _WEEKLY_NAMES:
        if not module.WEEKLY_PROFILES_ENABLED:
            raise ValueError("WEEKLY_PROFILES_ENABLED is False in all_strategy.py — enable the master switch there first.")
        if not module.WEEKLY_PROFILE_FLAGS.get(name, False):
            raise ValueError(f"WEEKLY_PROFILE_FLAGS['{name}'] is False in all_strategy.py — enable it there first.")
    overrides = _read_overrides()
    overrides[name] = bool(enabled)
    _write_overrides(overrides)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    clean = frame.astype(object).where(pd.notna(frame), None)
    return [{key: value for key, value in row.items()} for row in clean.to_dict("records")]


def run_scan(
    symbols: list[str] | None,
    strategy_names: list[str] | None,
    anchor_date: date | None = None,
    timeframe: str = "daily",
) -> dict[str, Any]:
    """Run the enabled/requested strategies over the given symbols."""
    module = load_module()
    catalog, _master = list_strategies()
    known = {item["name"]: item["label"] for item in catalog}

    requested = [str(name).strip() for name in (strategy_names or []) if str(name).strip()]
    if not requested:
        requested = [item["name"] for item in catalog if item["enabled"]]
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise ValueError(f"Unknown strategy '{unknown[0]}'. Valid: {', '.join(sorted(known))}")
    if not requested:
        raise ValueError("No strategies are enabled — turn on at least one strategy chip.")

    cleaned_symbols: list[str] = []
    for value in symbols or []:
        token = str(value).strip().upper()
        if token and token not in cleaned_symbols:
            cleaned_symbols.append(token)
    if not cleaned_symbols:
        raise ValueError("No symbols selected — pick watchlist symbols first.")

    requested_date = anchor_date if isinstance(anchor_date, date) else date.today()
    if any(is_crypto_symbol(sym) for sym in cleaned_symbols):
        # Crypto trades 24x7: weekends/NSE holidays are valid testing dates.
        resolved_date, reason = requested_date, None
    else:
        _requested, resolved_date, reason = module.resolve_previous_working_date(requested_date)

    executions = module.run_strategies(
        strategy_names=requested,
        symbols=cleaned_symbols,
        as_of_date=resolved_date,
        verbose=False,
        print_values=False,
        timeframe=timeframe,
    )

    labels = {**dict(CORE_STRATEGIES), **dict(module.WEEKLY_PROFILE_LABELS)}
    groups: list[dict[str, Any]] = []
    for execution in executions:
        bullish = _records(execution.bullish)
        bearish = _records(execution.bearish)
        all_results = bullish + bearish
        has_live = any(
            str(row.get("track_mode", "")).lower() == "live"
            for row in all_results
        )
        groups.append(
            {
                "strategy": execution.name,
                "label": labels.get(execution.name, execution.name),
                "total": int(len(execution.results)),
                "bull_count": len(bullish),
                "bear_count": len(bearish),
                "bullish": bullish,
                "bearish": bearish,
                "has_live_data": has_live,
            }
        )

    combined_path = _write_combined(groups, resolved_date)

    return {
        "results": groups,
        "requested_date": requested_date.isoformat(),
        "resolved_date": resolved_date.isoformat(),
        "resolution_reason": reason,
        "combined_file": combined_path,
        "scanned_at": pd.Timestamp.now().isoformat(),
    }


# Strategies whose daily signals arm an intraday (LTF) CISD confirmation.
LTF_STRATEGIES = {
    "daily_bias_invalidation", "protected_swings", "candle_3_closure", "propulsion_blocks",
} | _WEEKLY_NAMES


def collect_ltf_setups(symbols: list[str], as_of: date) -> list[Any]:
    """Run the enabled LTF-capable strategies on daily bars for ``as_of`` and
    return an ``LtfSetup`` for every row that armed an intraday confirmation."""
    from ltf_confirmation import LtfSetup, market_for_symbol, setup_key

    module = load_module()
    catalog, _master = list_strategies()
    names = [item["name"] for item in catalog if item["enabled"] and item["name"] in LTF_STRATEGIES]
    cleaned = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    if not names or not cleaned:
        return []
    executions = module.run_strategies(
        strategy_names=names, symbols=cleaned, as_of_date=as_of, parallel=True, timeframe="daily",
    )
    setups: list[Any] = []
    for execution in executions:
        if "ltf_valid_until" not in execution.results.columns:
            continue
        for rec in _records(execution.results):
            if not rec.get("ltf_valid_until") or not rec.get("ltf_signal_date"):
                continue
            direction = 1 if rec.get("bullish_match") else (-1 if rec.get("bearish_match") else 0)
            if direction == 0:
                continue
            symbol = str(rec["symbol"]).upper()
            setups.append(LtfSetup(
                key=setup_key(symbol, execution.name, str(rec["ltf_signal_date"]), direction),
                symbol=symbol,
                strategy=execution.name,
                direction=direction,
                zone_low=float(rec["ltf_zone_low"]),
                zone_high=float(rec["ltf_zone_high"]),
                invalidation=float(rec["ltf_invalidation"]),
                signal_date=str(rec["ltf_signal_date"]),
                valid_until=str(rec["ltf_valid_until"]),
                market=market_for_symbol(symbol),
                target=(float(rec["target"]) if rec.get("target") is not None else None),
                note=str(rec.get("note") or ""),
            ))
    return setups


def _write_combined(groups: list[dict[str, Any]], resolved_date: date) -> str | None:
    frames: list[pd.DataFrame] = []
    for group in groups:
        for side, rows in (("bull", group["bullish"]), ("bear", group["bearish"])):
            if rows:
                frame = pd.DataFrame(rows)
                frame.insert(0, "signal_type", side)
                frame.insert(0, "strategy", group["strategy"])
                frames.append(frame)
    if not frames:
        return None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"api_strategy_matches_{resolved_date.strftime('%d_%m_%Y')}.csv"
    pd.concat(frames, ignore_index=True).to_csv(path, index=False)
    return str(path)
