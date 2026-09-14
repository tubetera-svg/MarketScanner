import sqlite3
c = sqlite3.connect(r'd:/Sid/MarketScanner/data/market_data.db')
metas = c.execute("select symbol, listing_date, listing_price from ipo_metadata").fetchall()
zero, mismatch = [], []
for sym, ld, lp in metas:
    n, mn = c.execute(
        "select count(*), min(date) from ohlc_daily where symbol=?",
        (sym,)).fetchone()
    if not n:
        zero.append((sym, ld, lp))
    elif mn != ld:
        mismatch.append((sym, ld, mn, n))
print('tracked:', len(metas))
print('zero-bar:', len(zero), zero[:10])
print('start != listing_date:', len(mismatch), mismatch[:10])