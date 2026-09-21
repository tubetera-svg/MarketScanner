import sqlite3
conn = sqlite3.connect(r'D:\Sid\MarketScanner\data\market_data.db')
c = conn.cursor()
c.execute("SELECT * FROM ohlc_no_data WHERE symbol='NSE:ANKITMETAL' ORDER BY date DESC LIMIT 20")
rows = c.fetchall()
print('Columns:', [desc[0] for desc in c.description])
for r in rows:
    print(r)