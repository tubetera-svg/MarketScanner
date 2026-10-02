---
name: session-timezone-audit
description: Verify IST/NY/UTC boundaries in session logic (src/ict_scanner.py session helpers, Silver Bullet window)
---

# Session Timezone Audit Skill

Verifies correct timezone handling across NSE (IST), Forex/Commodities (NY/ET), Crypto (UTC).

Locate code by symbol, not line number: `rg -n "^(IST|NY|UTC|NSE_|FOREX_)|def (is_|_forex_day_key|_crypto_day_key|resolve_previous_working_date|detect_session)" src/ict_scanner.py`

## Timezone Definitions (`src/ict_scanner.py` module constants)

```python
IST = ZoneInfo("Asia/Kolkata")      # UTC+5:30, no DST
NY  = ZoneInfo("America/New_York")  # UTC-5/UTC-4 (DST)
UTC = ZoneInfo("UTC")
```

## Session Boundaries

### NSE (`NSE_OPEN`, `NSE_CLOSE`, `NSE_HOLIDAYS`, `is_nse_market_open`, `is_fresh_nse_day`)

| Event | Time (IST) | Code |
|-------|------------|------|
| Market open | 09:15 | `NSE_OPEN = dtime(9,15)` |
| Market close | 15:30 | `NSE_CLOSE = dtime(15,30)` |
| Bhavcopy ready | 17:00 | `NSE_BHAVCOPY_READY = dtime(17,0)` |
| Holidays | current-year dates | `NSE_HOLIDAYS` set — verify it covers the year being scanned/backtested |

**Checks:**
- `is_nse_market_open()` uses `datetime.now(IST)` — correct
- `is_fresh_nse_day()` triggers at/after 09:15 IST — correct
- `is_daily_bar_ready()` returns True only after 17:00 IST on trading day — correct

### Forex/Commodities 24/5 (`FOREX_DAILY_ROLLOVER`, `is_forex_24_5_open`, `_forex_day_key`)

| Event | Time (ET/NY) | Code |
|-------|--------------|------|
| Weekly open | Sun 17:00 | `FOREX_DAILY_ROLLOVER = dtime(17,0)` |
| Weekly close | Fri 17:00 | Same |
| Day key | NY 17:00 rollover | `_forex_day_key()` subtracts 17h |

**Checks:**
- `is_forex_24_5_open()` uses `datetime.now(NY)` — correct
- Sunday before 17:00 NY = closed — correct
- Friday after 17:00 NY = closed — correct
- `_forex_day_key()` shifts by 17h for daily bar alignment — correct

### Crypto 24/7 (`_crypto_day_key`, `is_fresh_crypto_day`)

| Event | Time | Code |
|-------|------|------|
| Day boundary | 00:00 UTC | `_crypto_day_key()` uses UTC date |

**Checks:**
- `is_fresh_crypto_day()` uses UTC — correct
- No session close — trades continuously

## Silver Bullet Window (`src/silver_bullet.py`, `SilverBulletLiveScanner` in `api/main.py`)

| Window | Time (NY) | Code |
|--------|-----------|------|
| Range | 09:00-10:00 | `time(9,0)` to `time(10,0)` |
| Signal | 10:00-11:00 | `time(10,0)` to `time(11,0)` |
| Auto-check | Every 3 min | `AUTO_CHECK_SECONDS = 180` |

**Critical:** `silver_bullet._timestamp()` attaches IST to naive TV bars THEN converts to NY. This is correct because TV returns IST wall time.

## Common Bugs to Flag

1. **Using `datetime.now()` without tz** — always specify `IST`, `NY`, or `UTC`
2. **Mixing `date.today()` with timezone-aware logic** — use `datetime.now(tz).date()`
3. **DST transitions** — NY `ZoneInfo` handles automatically; verify March/Nov boundaries
4. **Anchor date resolution** — `resolve_previous_working_date()` must use IST for NSE; `run_scan()` in `api/strategy_bridge.py` defaults to `date.today()` (host-local) when no anchor is given
5. **Cache keys** — `_cache_period_keys()` must use same day boundaries as session logic

## Audit Command

```bash
python -c "
from src.ict_scanner import *
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
NY = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')

now_ist = datetime.now(IST)
now_ny = datetime.now(NY)
now_utc = datetime.now(UTC)

print(f'IST: {now_ist:%Y-%m-%d %H:%M:%S %Z} (weekday={now_ist.weekday()})')
print(f'NY:  {now_ny:%Y-%m-%d %H:%M:%S %Z} (weekday={now_ny.weekday()})')
print(f'UTC: {now_utc:%Y-%m-%d %H:%M:%S %Z}')

print(f'NSE open: {is_nse_market_open()}')
print(f'Forex open: {is_forex_24_5_open()}')
print(f'NSE bar ready: {is_daily_bar_ready(Session.NSE)}')
print(f'Forex day key: {_forex_day_key(now_ny)}')
print(f'Crypto day key: {_crypto_day_key(now_utc)}')
"
```