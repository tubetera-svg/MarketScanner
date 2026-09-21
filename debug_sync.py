import sys
sys.path.insert(0, r'D:\Sid\MarketScanner\src')
import all_strategy

# Try to download bhavcopy for 2026-09-11 (last known date)
from datetime import date
df = all_strategy._download_bhavcopy_for_date(date(2026, 9, 11))
if df is not None:
    print(f"Columns: {df.columns.tolist()}")
    # Find ANKITMETAL
    symbol_col = all_strategy._find_column(df.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df.columns, ["SERIES"])
    if symbol_col and series_col:
        match = df[
            (df[series_col].astype(str).str.strip().str.upper() == "EQ")
            & (df[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL")
        ]
        print(f"ANKITMETAL in EQ series on 2026-09-11: {len(match)} rows")
        if len(match) == 0:
            # Check all series
            match_all = df[df[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL"]
            print(f"ANKITMETAL in ALL series on 2026-09-11: {len(match_all)} rows")
            if len(match_all) > 0:
                print(f"  Series values: {match_all[series_col].unique()}")
    else:
        print("Could not find SYMBOL/SERIES columns")
else:
    print("No bhavcopy for 2026-09-11")

# Also check 2026-09-15 (first missing date after 9/11)
df2 = all_strategy._download_bhavcopy_for_date(date(2026, 9, 15))
if df2 is not None:
    print(f"\n2026-09-15 bhavcopy: {len(df2)} rows")
    symbol_col = all_strategy._find_column(df2.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df2.columns, ["SERIES"])
    if symbol_col and series_col:
        match = df2[
            (df2[series_col].astype(str).str.strip().str.upper() == "EQ")
            & (df2[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL")
        ]
        print(f"ANKITMETAL in EQ series on 2026-09-15: {len(match)} rows")
        if len(match) == 0:
            match_all = df2[df2[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL"]
            print(f"ANKITMETAL in ALL series on 2026-09-15: {len(match_all)} rows")
            if len(match_all) > 0:
                print(f"  Series values: {match_all[series_col].unique()}")
else:
    print("\nNo bhavcopy for 2026-09-15 (maybe holiday or not yet published)")

# Check 2026-09-18
df3 = all_strategy._download_bhavcopy_for_date(date(2026, 9, 18))
if df3 is not None:
    print(f"\n2026-09-18 bhavcopy: {len(df3)} rows")
    symbol_col = all_strategy._find_column(df3.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df3.columns, ["SERIES"])
    if symbol_col and series_col:
        match = df3[
            (df3[series_col].astype(str).str.strip().str.upper() == "EQ")
            & (df3[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL")
        ]
        print(f"ANKITMETAL in EQ series on 2026-09-18: {len(match)} rows")
        if len(match) == 0:
            match_all = df3[df3[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL"]
            print(f"ANKITMETAL in ALL series on 2026-09-18: {len(match_all)} rows")
            if len(match_all) > 0:
                print(f"  Series values: {match_all[series_col].unique()}")
else:
    print("\nNo bhavcopy for 2026-09-18")

# Check 2026-09-22 (today)
df4 = all_strategy._download_bhavcopy_for_date(date(2026, 9, 22))
if df4 is not None:
    print(f"\n2026-09-22 bhavcopy: {len(df4)} rows")
    symbol_col = all_strategy._find_column(df4.columns, ["SYMBOL"])
    series_col = all_strategy._find_column(df4.columns, ["SERIES"])
    if symbol_col and series_col:
        match = df4[
            (df4[series_col].astype(str).str.strip().str.upper() == "EQ")
            & (df4[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL")
        ]
        print(f"ANKITMETAL in EQ series on 2026-09-22: {len(match)} rows")
        if len(match) == 0:
            match_all = df4[df4[symbol_col].astype(str).str.strip().str.upper() == "ANKITMETAL"]
            print(f"ANKITMETAL in ALL series on 2026-09-22: {len(match_all)} rows")
            if len(match_all) > 0:
                print(f"  Series values: {match_all[series_col].unique()}")
else:
    print("\nNo bhavcopy for 2026-09-22 (market may still be open or not published)")