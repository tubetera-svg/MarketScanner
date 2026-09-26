import pickle, re, sys
from pathlib import Path
import sqlite3

CACHE = Path(r'd:\Sid\MarketScanner\data\bhavcopy_cache')
metas = sqlite3.connect(r'd:\Sid\MarketScanner\data\market_data.db').execute(
    'select symbol, listing_date from ipo_metadata').fetchall()
print('tracked:', len(metas))

GS = re.compile(r'^\d{2,3}(GS|GR|SG)\d{4}$')
RE = re.compile(r'-RE$')
gs = [s for s, _ in metas if GS.match(s.split(':', 1)[-1])]
re_ = [s for s, _ in metas if RE.search(s.split(':', 1)[-1])]
other_hyphen = [s for s, _ in metas
                if '-' in s.split(':', 1)[-1] and not RE.search(s.split(':', 1)[-1])]
print('GS-bond-like:', len(gs), gs[:5])
print('-RE like:', len(re_), re_[:5])
print('other hyphen:', len(other_hyphen), other_hyphen[:8])

# series observed in the newest cached files for a few junk samples
samples = (gs[:3] + re_[:3] + other_hyphen[:3])
files = sorted(CACHE.glob('*.pkl'))[-3:]
seen = {}
for p in files:
    df = pickle.load(open(p, 'rb'))
    if df is None:
        continue
    for _, r in df.iterrows():
        s = str(r['SYMBOL']).strip()
        if s in [x.split(':', 1)[-1] for x in samples]:
            seen.setdefault(s, set()).add(str(r['SERIES']).strip())
print('series seen:', {k: sorted(v) for k, v in seen.items()})
print('files used:', [p.stem for p in files])