@echo off
cd /d d:\Sid\MarketScanner
set PYTHONPATH=d:\Sid\MarketScanner
python scripts\ipo_full_history.py --scrub-only > data\ipo_scrub.log 2>&1
