import sys
sys.path.insert(0, r'D:\Sid\MarketScanner')
from market_data.liquidity_screener import screen_all_ipos
result = screen_all_ipos(lookback_days=60, auto_remove=False)
print(f'Screened {len(result)} symbols')
for r in result[:5]:
    print(f'  {r["symbol"]}: {r["liquidity_tier"]} | {r["decision"]} | {r["reason"][:60]}...')