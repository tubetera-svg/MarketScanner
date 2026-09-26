---
name: backtest-validation
description: Verify backtest engine matches live scanner behavior (entry, SL/TP, session gating)
---

# Backtest Validation Skill

Ensures `src/backtest/` replays strategies point-in-time identically to live `AdaptiveScanner.run_once()`.

## Critical Parity Checks

| Component | Live Scanner | Backtest | Must Match |
|-----------|--------------|----------|------------|
| Data source | `TvDatafeedFetcher` / `OandaDataFetcher` | SQLite `ohlc_daily` + intraday | Same bars, same timestamps |
| Session gating | `is_market_open()` / `is_fresh_trading_day()` | Same functions | Identical session logic |
| Daily bar ready | `is_daily_bar_ready()` | Same function | NSE 17:00 IST gate |
| Bias calculation | `ict_scanner.py:1116-1132` | Replay via `get_ohlc()` | Same PDH/PDL/PWH/PWL |
| FVG detection | `detect_bullish/bearish_fvg()` | Same functions | Same lookback, same bars |
| Displacement | `detect_directional_displacement()` | Same function | Same body multiplier |
| Entry/SL/TP | `ict_scanner.py:1195-1229` | Backtest replay | Same formulas |
| RR filter | `minimum_rr=2.0` | Config | Same threshold |

## Validation Steps

1. **Run historical test** via API: `POST /api/historical-test` with `anchor_date`
2. **Run backtest** via API: `POST /api/backtest` with same date range
3. **Compare signals** for overlapping symbols/dates:
   - Entry price ±0.01%
   - Stop loss ±0.01%
   - TP1/TP2 ±0.01%
   - Trade confirmed Y/N
   - Tier/state classification

## Common Drift Sources

- **Intraday bars**: Live uses 5m from TV/OANDA; backtest needs same resolution in SQLite
- **Cache**: Live uses `OHLCCache` (JSON); backtest reads SQLite — verify sync
- **Timezone**: Live uses `datetime.now(NY/IST/UTC)`; backtest uses `anchor_date` — verify session boundaries
- **Forming bar exclusion**: Silver Bullet excludes current 15m (silver_bullet.py:90) — backtest must too

## Quick Test

```bash
# Compare one symbol on one date
python -c "
from api.main import service
from backtest import BacktestConfig, run_backtest
from datetime import date

# Live historical test
live = service.historical_test(['OANDA:XAUUSD'], date(2026,9,23))

# Backtest
config = BacktestConfig(symbols=['OANDA:XAUUSD'], strategies=['ict'], start_date=date(2026,9,23), end_date=date(2026,9,23))
bt = run_backtest(config)
print('Live:', live['results'][0] if live['results'] else 'none')
print('BT:', bt['OANDA:XAUUSD'].trades[0] if bt['OANDA:XAUUSD'].trades else 'none')
"
```