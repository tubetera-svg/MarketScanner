---
name: trading-strategy-review
description: Audit ICT logic for repainting, look-ahead bias, and live/backtest drift per AGENTS.md §2
---

# Trading Strategy Review Skill

Use this skill when reviewing or modifying any trading logic in `src/`, `api/strategy_bridge.py`, or backtest engine.

## Repainting Checks

**Flag immediately** (do not silently fix) any signal that uses:
- Future bars (e.g., `candles[i+1]` or accessing "next" candle)
- Same-bar close after decision point (entry triggered on bar close but logic uses that close)
- Future-derived indicators (VWAP of session not yet complete, daily bias from incomplete bar)
- Backtest look-ahead (using `anchor_date` data that wouldn't be available at signal time)

### Key Locations to Audit

| File | Symbol | Risk |
|------|--------|------|
| `src/ict_scanner.py` | `detect_bullish_fvg` / `detect_bearish_fvg` | FVG detection — ensure `lookback` doesn't include current forming bar |
| `src/ict_scanner.py` | `detect_liquidity_sweep` | Liquidity sweep — `candles[-1]` is current forming bar |
| `src/ict_scanner.py` | `detect_directional_displacement` | Displacement — compares current candle to historical average |
| `src/ict_scanner.py` | POI/FVG selection in `AdaptiveScanner` | POI/FVG selection — only uses bias-matching FVG |
| `src/silver_bullet.py` | `evaluate_am_silver_bullet` | AM Silver Bullet — excludes forming 15m candle |
| `src/protected_swings.py`, `src/propulsion_blocks.py` | module entry points | Swing/pivot confirmation lag — signal stamped at confirmation bar |
| `src/ltf_confirmation.py` | module entry points | LTF bars must be closed and not after the evaluated time |
| `src/daily_context.py` | `ctx_*` columns | Display-only — must never gate a signal |

## Look-Ahead Checks

- Daily/weekly bias must use **completed** prior session only
- `is_daily_bar_ready()` gates when today's bar is usable
- Historical test `anchor_date` must resolve to previous working day
- NSE bhavcopy only available after 17:00 IST (`NSE_BHAVCOPY_READY`)
- Full rule list: AGENTS.md §2a

## Live/Backtest Drift

| Source | Live | Backtest | Check |
|--------|------|----------|-------|
| OHLC | TV/OANDA fetch | SQLite `ohlc_daily` | Same bars? |
| Session | Real-time | `anchor_date` | Same session boundaries? |
| Cache | `OHLCCache` (JSON) | SQLite | Consistent? |
| Time | `datetime.now(tz)` | `datetime.combine(anchor, time)` | NY/IST/UTC correct? |

## Usage

When asked to review strategy changes:
1. Run `grep -n "candles\[-1\]" src/ict_scanner.py` — check forming-bar usage
2. Verify `anchor_date` propagation in `run_scan()` (`api/strategy_bridge.py`) and `SilverBulletLiveScanner.test()` (`api/main.py`)
3. Confirm `is_daily_bar_ready()` is called before sync (`rg -n is_daily_bar_ready api/`)
4. Check session detection consistency: `detect_session()` vs API categorization