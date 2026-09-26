import sys
from datetime import date
sys.path.insert(0, r'D:\Sid\MarketScanner\src')
import all_strategy

# Read watchlist
with open(r'D:\Sid\MarketScanner\config\watchlist.txt') as f:
    watchlist = [line.strip() for line in f if line.strip() and not line.startswith('#')]

nse_symbols = [s.split(':', 1)[1] for s in watchlist if s.startswith('NSE:')]

# Check multiple dates for BZ series symbols
dates_to_check = [date(2026, 9, 11), date(2026, 9, 15), date(2026, 9, 18)]
all_bz_symbols = set()

for check_date in dates_to_check:
    df = all_strategy._download_bhavcopy_for_date(check_date)
    if df is None:
        print(f"No bhavcopy for {check_date}")
        continue
    symbol_col = all_strategy._find_column(df.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df.columns, ["SERIES"])
    
    # Find all BZ series symbols
    bz_df = df[df[series_col].astype(str).str.strip().str.upper() == "BZ"]
    bz_syms = set(bz_df[symbol_col].astype(str).str.strip().str.upper().tolist())
    print(f"{check_date}: {len(bz_syms)} BZ series symbols")
    all_bz_symbols.update(bz_syms)

# Check which watchlist symbols are in BZ
watchlist_bz = [s for s in nse_symbols if s in all_bz_symbols]
print(f"\nWatchlist symbols in BZ series: {watchlist_bz}")

# Also check BE series (another common illiquid series)
all_be_symbols = set()
for check_date in dates_to_check:
    df = all_strategy._download_bhavcopy_for_date(check_date)
    if df is None:
        continue
    symbol_col = all_strategy._find_column(df.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df.columns, ["SERIES"])
    
    be_df = df[df[series_col].astype(str).str.strip().str.upper() == "BE"]
    be_syms = set(be_df[symbol_col].astype(str).str.strip().str.upper().tolist())
    all_be_symbols.update(be_syms)

watchlist_be = [s for s in nse_symbols if s in all_be_symbols]
print(f"Watchlist symbols in BE series: {watchlist_be}")

# Check all unique series in bhavcopy
df = all_strategy._download_bhavcopy_for_date(date(2026, 9, 18))
series_col = all_strategy._find_column(df.columns, ["SERIES"])
all_series = df[series_col].astype(str).str.strip().str.upper().unique()
print(f"\nAll series in bhavcopy: {sorted(all_series)}")