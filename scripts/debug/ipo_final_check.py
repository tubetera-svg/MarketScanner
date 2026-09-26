import sqlite3, pickle
from pathlib import Path
ROOT = Path(r'd:\Sid\MarketScanner')
c = sqlite3.connect(str(ROOT / 'data' / 'market_data.db'))
for s in ['NSE:AUGMONT', 'NSE:NIF500BETA', 'NSE:TEMPSENS', 'NSE:TATATECH']:
    rows = c.execute('select date, open, close from ohlc_daily where symbol=? order by date', (s,)).fetchall()
    print(s, 'bars:', len(rows), 'first:', rows[0] if rows else None, 'last:', rows[-1] if rows else None)

cache = ROOT / 'data' / 'bhavcopy_cache'
print('Sept 2026 cache:', sorted(p.stem for p in cache.glob('2026-09-*.pkl')))
print('late Aug cache:', sorted(p.stem for p in cache.glob('2026-08-2*.pkl')))

for d in ['2026-08-31', '2026-09-01', '2026-09-07']:
    p = cache / f'{d}.pkl'
    if p.exists():
        df = pickle.load(open(p, 'rb'))
        syms = set(df['SYMBOL'].astype(str).str.strip())
        print(d, 'rows:', len(df), 'AUGMONT:', 'AUGMONT' in syms, 'TEMPSENS:', 'TEMPSENS' in syms)
    else:
        print(d, 'NOT CACHED')
