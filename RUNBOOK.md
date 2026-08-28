# Runbook

## ICT Web App

Install the backend dependencies and start FastAPI:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn api.main:app --reload --port 8000
```

In a second terminal, start the Next.js frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`. The API is available at `http://localhost:8000/docs`.

## Market-data layer (local SQLite)

Lightweight local cache for daily OHLC bars in `data/market_data.db` (SQLite only — no external DB).

- Package layout:
  - `market_data/database.py` — SQLite storage (WAL mode, batched inserts, `UNIQUE(source, exchange, symbol, date)`, index on `(source, symbol, date)`).
  - `market_data/sources/tradingview_source.py` — TradingView/tvDatafeed (commodities, e.g. `OANDA:XAUUSD`, `FOREXCOM:USOIL`).
  - `market_data/sources/nse_source.py` — NSE Bhavcopy (reuses `_download_bhavcopy_for_date` from `src/all_strategy.py`; downloads only missing dates).
  - `market_data/service.py` — reusable `get_ohlc(source, symbol, start_date, end_date)` cache-aside logic.
  - `market_data/routes.py` — FastAPI endpoints.
  - `market_data/bootstrap.py` — CLI initial load.
- Behaviour of `get_ohlc`: check SQLite → return if all expected trading dates exist → otherwise fetch only missing dates from the correct source → store → return the full range from SQLite. Confirmed holidays are remembered so they are not re-fetched. Duplicates are impossible via the unique constraint.
- Environment flags (all default `true`):
  ```powershell
  $env:FETCH_TRADINGVIEW_DATA = "true"   # allow TradingView fetching
  $env:FETCH_NSE_DATA = "true"           # allow NSE bhavcopy fetching
  $env:AUTO_FETCH_MISSING_DATA = "true"  # allow auto backfill of missing dates
  $env:BACKDATE_LOOKBACK_DAYS = "14"     # window ensured when backdate tests run
  ```
- Endpoints:
  - `GET /ohlc?source=NSE&symbol=RELIANCE&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
    (`/api/ohlc` is an alias; optional `auto_fetch=true|false` query override)
  - `POST /api/market-data/sync` — bulk backfill (defaults to watchlist, last 14 days)
  - `POST /api/historical-test` now first fetches+stores any OHLC missing around the tested anchor date before scanning (see `backdate_data_sync` in the response).
- Initial 2-week load from CLI:
  ```powershell
  python -m market_data.bootstrap                # whole watchlist, last 14 days
  python -m market_data.bootstrap --source NSE --symbols RELIANCE,INFY --days 10
  ```
- Tests:
  ```powershell
  python -m pytest tests/test_market_data.py -v
  ```

## One-click Windows launch

Double-click `start_market_scanner.bat` in the project folder. It opens the API and frontend in separate windows and opens the app in your browser. The script uses `.venv` automatically when that environment exists.

Double-click `stop_market_scanner.bat` to close both service windows.

## Current Context
-This is about trading automation and improvement
-When new strategy is added, also add it to strategy_info.txt file
-Always take backup of all code files and put in folder named backup_date_timestamp

- 2026-08-12T20:48:35.511110 — snapshot: strategy_outputs\tradingview_ohlc_20260812_204835.csv; prompt_log: prompt_logs\prompt_log_20260812_204835.md; note: run now
- 2026-08-12T20:50:48.721171 — snapshot: strategy_outputs\tradingview_ohlc_20260812_205048.csv; prompt_log: prompt_logs\prompt_log_20260812_205048.md; note: 
- 2026-08-12T20:51:34.198044 — snapshot: strategy_outputs\tradingview_ohlc_20260812_205134.csv; prompt_log: prompt_logs\prompt_log_20260812_205134.md; note: 
- 2026-08-12T20:56:20.847860 — snapshot: strategy_outputs\tradingview_ohlc_20260812_205620.csv; prompt_log: prompt_logs\prompt_log_20260812_205620.md; note: include attached prompt
- 2026-08-12T20:57:17.589361 — snapshot: strategy_outputs\tradingview_ohlc_20260812_205717.csv; prompt_log: prompt_logs\prompt_log_20260812_205717.md; note: 
- 2026-08-12T20:57:29.050105 — snapshot: strategy_outputs\tradingview_ohlc_20260812_205729.csv; prompt_log: prompt_logs\prompt_log_20260812_205729.md; note: 
- 2026-08-12T21:00:10.021164 — snapshot: strategy_outputs\tradingview_ohlc_20260812_210010.csv; prompt_log: prompt_logs\prompt_log_20260812_210010.md; note: apply adaptive logic smoke test
- 2026-08-12T21:02:04.198823 — snapshot: strategy_outputs\tradingview_ohlc_20260812_210204.csv; prompt_log: prompt_logs\prompt_log_20260812_210204.md; note: 

- 2026-08-25 — Added six weekly profile strategies to `all_strategy.py`: classic_expansion_sweep, midweek_reversal_sweep, consolidation_reversal_sweep, intraweek_reversal_sweep, thursday_counter_sweep, tgif_setup_sweep. Master flag `WEEKLY_PROFILES_ENABLED` plus per-profile `WEEKLY_PROFILE_FLAGS` gate them inside `strategy_registry()`; disabled profiles never run. Result frames carry `profile` and `note` columns so bull/bear/all/combined outputs stay clubbed per strategy. Documented in strategy_info.txt. Pre-change backup: Backup_25-08_025140. Validated with synthetic-data harness (28 checks, all passing).
- 2026-08-25 — Strategy flags are now selectable from the web UI. New `api/strategy_bridge.py` loads `all_strategy.py`, persists on/off toggles in `strategy_flags.json`, and runs scans clubbed per strategy. New endpoints: GET/PUT `/api/strategies` (list/toggle flags) and POST `/api/strategy-scan` (runs enabled strategies over selected watchlist symbols, optional testing date). New "Strategy profiles" panel in the frontend with Core + Weekly profile toggle chips, per-strategy BULL/BEAR counts, and TradingView signal chips. Pre-change backup: Backup_25-08_032733_ui.
- 2026-08-25 — Added a lightweight local SQLite market-data layer (`market_data/` package, db at `data/market_data.db`). Daily OHLC stored with UNIQUE(source, exchange, symbol, date) + WAL mode; separate source modules: tvDatafeed for commodities (`sources/tradingview_source.py`) and NSE bhavcopy reusing all_strategy's downloader (`sources/nse_source.py`). Env flags FETCH_TRADINGVIEW_DATA / FETCH_NSE_DATA / AUTO_FETCH_MISSING_DATA gate each source independently. Reusable cache-aside API: `get_ohlc(source, symbol, start_date, end_date)` returns from SQLite when complete, otherwise fetches only missing dates, stores them, and never duplicates rows. New endpoints: GET `/ohlc` (+`/api/ohlc` alias) and POST `/api/market-data/sync`. Backdate test hook: POST `/api/historical-test` now ensures OHLC around the anchor date is fetched+stored before scanning (BACKDATE_LOOKBACK_DAYS, default 14 = two weeks initial load). CLI bootstrap: `python -m market_data.bootstrap`. Tests in `tests/test_market_data.py` (duplicates, cached hits, missing-date fetch, independent flags, WAL, endpoint). Pre-change backup: Backup_25-08_marketdata.
- 2026-08-25 — Added a read-only Watchlist page (`frontend/app/watchlist`) for browsing the SQLite market-data store with a scope filter (`Watchlist only` vs `All records`), source/exchange/symbol/date filters, sort direction and server-side pagination. New endpoints: GET `/api/market-data/records` (paged rows + total + watchlist coverage diagnostics) and GET `/api/market-data/meta` (counts, date range, distinct sources/exchanges) — both pure SQLite reads that never trigger upstream fetches (enforced by a booby-trapped-fetcher test). The page loads NO data by default: nothing is requested until the user presses "Load data"; changing filters only marks the view stale. Scanner page gained a "Database" nav link. Tests: `tests/test_watchlist_records.py` (10 cases). Spec: `WATCHLIST_PAGE_SPEC.md`. Pre-change backup: Backup_25-08_watchlistpage.
- 2026-08-25 — Watchlist browser rework: replaced the Watchlist-only/All-records toggle with a per-symbol checkbox picker (defaults to every watchlist symbol; "Load data" disabled at zero selection). New query param on GET `/api/market-data/records`: `symbols=` comma-separated explicit filter (uppercased/deduped, ≤500, intersected with the watchlist when scope=watchlist; missing-in-db diagnostics follow the selection). New read-only endpoint GET `/api/market-data/watchlist` returns the static symbol list for the picker (config read only). `/api/market-data/meta` is now refreshed on every Load data so Source/Exchange dropdowns always reflect current DB contents (both NSE and TRADINGVIEW appear under "Any"). Grid column filters stay client-side; numeric min/max pairs removed earlier this session. Tests: +2 cases (`test_records_explicit_symbol_selection`, `test_watchlist_endpoint`).
- 2026-08-25 — Fixed TradingView symbol storage convention: `tradingview_source.fetch_daily` now returns exchange-qualified symbols (`CAPITALCOM:NATURALGAS`) instead of bare names (`NATURALGAS`), matching the spec (`symbol` column is prefixed, e.g. `'OANDA:XAUUSD'`) and how `get_ohlc()` keys its cache lookups — previously TV rows could never cache-hit. One-time migration qualified the 55 existing TV rows in `data/market_data.db` (`UPDATE … SET symbol = exchange || ':' || symbol WHERE source='TRADINGVIEW' AND instr(symbol,':')=0`; idempotent, no unique-constraint violations). This also resolves the false "In watchlist but not in DB" warning for CAPITALCOM:NATURALGAS / FOREXCOM:UKOIL on the Watchlist page, whose missing-note now suggests using Sync on the Scanner page for never-synced symbols. Validated: 22/22 market-data tests pass; frontend build green.
- 2026-08-25 — Same convention fix for NSE: `nse_source.fetch_daily` now stores exchange-qualified symbols (`NSE:INFY`) instead of bare bhavcopy names — the mirror image of the TV fix. Root cause of "0 rows match · TRADINGVIEW 55 only": after a DB rebuild, NSE rows were bare so prefixed watchlist keys matched nothing. One-time migration qualified all 1,947 NSE rows (`UPDATE … SET symbol = 'NSE:' || symbol WHERE source='NSE' AND instr(symbol,':')=0`; idempotent). DB is now uniformly prefixed: NSE 1,947 rows / 177 symbols + TRADINGVIEW 55 rows / 5 symbols; watchlist coverage check and `get_ohlc` cache lookups line up for every symbol. Validated: 22/22 tests pass.
- 2026-08-25 — Scanner UI: moved the "Run scan" button out of the top bar into the Auto-scan panel, directly beside "Start auto-scan"/"Stop auto-scan" (same `runScan` handler, still disabled while scanning or with zero selected symbols).
- 2026-08-26 — Fixed market-hours sync gating so each source's data-availability window is respected instead of a blanket "market must be open" check. Added `is_daily_bar_ready(session, now)` + `NSE_BHAVCOPY_READY=17:00` in `src/ict_scanner.py`: NSE bars sync once the bhavcopy is published (after 17:00 IST); commodity/forex bars defer to the next day. `market_data/service.py::sync_symbol_range` now gates on `is_daily_bar_ready` (defers to last completed session, or keeps today and clears any stale `no_data` marker when ready); added `market_data/database.py::clear_no_data`. Live scan loop (`ict_scanner.py:1795`) and scheduled-scan gate (`api/main.py:336`) also run for NSE once `is_daily_bar_ready`. `/api/market-data/sync` now defaults `gate_market_hours=True`. Root cause of the observed bug: the scan skipped symbols whose market was closed, so NSE (closed at night) never backfilled while 24/5 commodities did. Backup (state at change): Backup_26-08_231814.
