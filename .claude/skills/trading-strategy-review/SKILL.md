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

| File | Lines | Risk |
|------|-------|------|
| `src/ict_scanner.py` | 657-702 | FVG detection — ensure `lookback` doesn't include current forming bar |
| `src/ict_scanner.py` | 710-747 | Liquidity sweep — `candles[-1]` is current forming bar |
| `src/ict_scanner.py` | 755-796 | Displacement — compares current candle to historical average |
| `src/ict_scanner.py` | 1149-1181 | POI/FVG selection — only uses bias-matching FVG |
| `src/silver_bullet.py` | 68-115 | AM Silver Bullet — excludes forming 15m candle (line 90) |

## Look-Ahead Checks

- Daily/weekly bias must use **completed** prior session only
- `is_daily_bar_ready()` (line 415) gates when today's bar is usable
- Historical test `anchor_date` must resolve to previous working day
- NSE bhavcopy only available after 17:00 IST (line 311)

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
2. Verify `anchor_date` propagation in `historical_test()` (api/main.py:278-327)
3. Confirm `is_daily_bar_ready()` called before sync (api/main.py:703)
4. Check session detection consistency: `detect_session()` (ict_scanner.py:157) vs API categorization