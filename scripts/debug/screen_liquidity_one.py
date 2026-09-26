import sys
sys.path.insert(0, r'D:\Sid\MarketScanner')
from market_data.liquidity_screener import compute_liquidity_metrics

metrics = compute_liquidity_metrics("NSE:3MINDIA")
print(metrics)