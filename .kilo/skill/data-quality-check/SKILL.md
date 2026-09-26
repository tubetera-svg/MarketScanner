---
name: data-quality-check
description: Validate OHLC completeness, detect gaps in market_data.db
---

# Data Quality Check Skill

Validates completeness and correctness of `data/market_data.db` (SQLite) used by scanner and backtest.

## Core Checks

### 1. OHLC Completeness

```sql
-- Missing dates per symbol (should only be weekends/holidays for NSE)
SELECT symbol, COUNT(*) as bars,
       MIN(date) as first, MAX(date) as last
FROM ohlc_daily
WHERE source IN ('NSE','TV','OANDA')
GROUP BY symbol
HAVING COUNT(*) < (julianday(MAX(date)) - julianday(MIN(date))) * 0.7;
```

### 2. Gap Detection (NSE)

```sql
-- NSE trading days missing (excl weekends + NSE_HOLIDAYS from ict_scanner.py:313-319)
WITH trading_days AS (
  SELECT date(d) as dt FROM (
    SELECT date('2026-01-01', '+' || (abs(random()) % 365) || ' days') as d
  ) WHERE strftime('%w', dt) NOT IN ('0','6')
    AND dt NOT IN ('2026-01-26','2026-03-03','2026-03-26','2026-03-31','2026-04-03',
                   '2026-04-14','2026-05-01','2026-05-27','2026-06-26','2026-09-14',
                   '2026-10-02','2026-10-20','2026-11-09','2026-11-24','2026-12-25')
)
SELECT td.dt FROM trading_days td
LEFT JOIN ohlc_daily o ON o.date = td.dt AND o.source = 'NSE' AND o.symbol = 'RELIANCE'
WHERE o.date IS NULL;
```

### 3. Data Integrity

```sql
-- Impossible OHLC (high < low, open/close outside range)
SELECT * FROM ohlc_daily
WHERE high < low
   OR open < low OR open > high
   OR close < low OR close > high;

-- Zero volume on liquid symbols
SELECT symbol, date, volume FROM ohlc_daily
WHERE source = 'NSE' AND volume = 0 AND symbol IN ('RELIANCE','TCS','HDFCBANK');
```

### 4. Source Consistency

```sql
-- Same symbol from different sources on same date (should match ~0.1%)
SELECT o1.symbol, o1.date, o1.source, o1.close as c1, o2.source, o2.close as c2,
       ABS(o1.close - o2.close)/o1.close * 100 as pct_diff
FROM ohlc_daily o1
JOIN ohlc_daily o2 ON o1.symbol = o2.symbol AND o1.date = o2.date AND o1.source < o2.source
WHERE ABS(o1.close - o2.close)/o1.close > 0.001;
```

## Automated Check Script

```python
# D:\Sid\MarketScanner\check_data_quality.py
from market_data.database import connect, distinct_symbols, query_ohlc
from datetime import date, timedelta

conn = connect()
symbols = distinct_symbols(source='NSE', db_path=conn)
for sym in symbols[:10]:  # sample
    bars = query_ohlc('NSE', sym, start_date=date(2026,1,1), end_date=date.today(), db_path=conn)
    dates = {b['date'] for b in bars}
    expected = ...  # trading day calendar
    missing = expected - dates
    if missing:
        print(f"{sym}: {len(missing)} missing days, e.g. {sorted(missing)[:5]}")
```

## Pre-Backtest Gate

Run before any backtest: `python check_data_quality.py --symbols <list> --start <date> --end <date>`
Fail if any symbol has >2% missing bars in range.