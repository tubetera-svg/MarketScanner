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

## IPO tracking

New-IPO discovery, backfill, and listing-price performance tracking for NSE
IPOs (last 3 years).

- `market_data/ipo.py` â IPO discovery from NSE bhavcopy history (a symbol is
  an IPO if its first bhavcopy appearance is within the lookback window;
  listing price = first trading day's open), watchlist integration
  (adds `IPO` category entries to `config/watchlist.txt` /
  `config/watchlist_categories.json`), and DB backfill via the existing
  NSE bhavcopy pipeline.
- `market_data/database.py` â `ipo_metadata` table (symbol, exchange,
  listing_date, listing_price, first/high/low since listing).
- Endpoints (all under `/api/market-data`):
  - `GET  /ipo` â tracked IPOs with metadata
  - `GET  /ipo/performance` â listing price vs current, high/low since listing, % change
  - `POST /ipo/discover` â scan recent bhavcopies for newly listed symbols
  - `POST /ipo/backfill` â add IPOs to the watchlist + fetch their full OHLC history
- Frontend: `/ipo` page shows the performance table with filters (symbol search,
  listing-age selector defaulting to the last 3 months, performance bucket,
  "never above listing", min/max % change) and click-to-sort headers. High/Low
  columns sort by their % versus listing price.
- 3-year history backfill CLI (`scripts/ipo_full_history.py`, run per month so
  long scans stay resumable):
  ```powershell
  python scripts/ipo_full_history.py --months 2023-09            # one month
  python scripts/ipo_full_history.py --months 2023-09 2023-10    # several
  python scripts/ipo_full_history.py --months all                # whole window
  ```
  Each trading day's bhavcopy is downloaded once and cached in
  `data/bhavcopy_cache/YYYY-MM-DD.pkl`, so re-runs and later batches reuse it.
  Detection uses the full series universe (not just `EQ`) because NSE bhavcopy
  only lists stocks that actually traded — illiquid old stocks flicker in/out
  and would otherwise look like new listings. Candidates are additionally
  validated on post-listing trading activity before being registered.
- Tests:
  ```powershell
  python -m pytest tests/test_ipo.py -v
  python -m pytest tests/test_tv_symbol.py -v
  ```

## TradingView chart popup (shared)

`frontend/components/TradingViewChartModal.tsx` is the single chart popup used
app-wide (home scanner results and `/ipo` results). Reuse it with:

```tsx
const [chart, setChart] = useState<ChartTarget | null>(null);
// ...onClick={() => setChart({ symbol, sourceLink })}
<TradingViewChartModal key={chart.symbol} chart={chart} onClose={() => setChart(null)} />
```

It renders the timeframe selector, RSI/MACD/EMA/VWAP toggles and the widget
iframe; remount per symbol via `key` to reset the controls.

- Symbol resolution (`market_data/tv_symbol.py`): TradingView's embed widget
  shows "This symbol doesn't exist" for symbols it does not carry — many SME
  IPOs are `BSE:`-only (e.g. `ACHYUT`). The modal resolves each app symbol via
  the endpoint below, preferring `NSE:` and falling back to `BSE:`, and shows a
  "not listed on TradingView" message with search links when neither exists.
- Resolution is cached in the `tv_symbol_cache` table (`symbol` PK, `tv_symbol`,
  `exchange`, `resolved_at`). Negative results are cached too, so a missing
  symbol is not re-queried on every page view. Network/WAF failures are *not*
  cached, so they retry later.
- Endpoint: `GET /api/market-data/tv-symbol?symbol=NSE:ACHYUT` (or
  `?symbols=A,B,C`, plus optional `refresh=true` and `limit=1..200`). `limit`
  caps live lookups per call, so rendering 1,500 rows cannot trigger a burst.

## One-click Windows launch

Double-click `scripts/start_market_scanner.bat`. It opens the API and frontend in separate windows and opens the app in your browser. The script uses `.venv` automatically when that environment exists.

Double-click `scripts/stop_market_scanner.bat` to close both service windows.

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
- 2026-08-25 — Added a read-only Watchlist page (`frontend/app/watchlist`) for browsing the SQLite market-data store with a scope filter (`Watchlist only` vs `All records`), source/exchange/symbol/date filters, sort direction and server-side pagination. New endpoints: GET `/api/market-data/records` (paged rows + total + watchlist coverage diagnostics) and GET `/api/market-data/meta` (counts, date range, distinct sources/exchanges) — both pure SQLite reads that never trigger upstream fetches (enforced by a booby-trapped-fetcher test). The page loads NO data by default: nothing is requested until the user presses "Load data"; changing filters only marks the view stale. Scanner page gained a "Database" nav link. Tests: `tests/test_watchlist_records.py` (10 cases). Spec: `docs/WATCHLIST_PAGE_SPEC.md`. Pre-change backup: Backup_25-08_watchlistpage.
- 2026-08-25 — Watchlist browser rework: replaced the Watchlist-only/All-records toggle with a per-symbol checkbox picker (defaults to every watchlist symbol; "Load data" disabled at zero selection). New query param on GET `/api/market-data/records`: `symbols=` comma-separated explicit filter (uppercased/deduped, ≤500, intersected with the watchlist when scope=watchlist; missing-in-db diagnostics follow the selection). New read-only endpoint GET `/api/market-data/watchlist` returns the static symbol list for the picker (config read only). `/api/market-data/meta` is now refreshed on every Load data so Source/Exchange dropdowns always reflect current DB contents (both NSE and TRADINGVIEW appear under "Any"). Grid column filters stay client-side; numeric min/max pairs removed earlier this session. Tests: +2 cases (`test_records_explicit_symbol_selection`, `test_watchlist_endpoint`).
- 2026-08-25 — Fixed TradingView symbol storage convention: `tradingview_source.fetch_daily` now returns exchange-qualified symbols (`CAPITALCOM:NATURALGAS`) instead of bare names (`NATURALGAS`), matching the spec (`symbol` column is prefixed, e.g. `'OANDA:XAUUSD'`) and how `get_ohlc()` keys its cache lookups — previously TV rows could never cache-hit. One-time migration qualified the 55 existing TV rows in `data/market_data.db` (`UPDATE … SET symbol = exchange || ':' || symbol WHERE source='TRADINGVIEW' AND instr(symbol,':')=0`; idempotent, no unique-constraint violations). This also resolves the false "In watchlist but not in DB" warning for CAPITALCOM:NATURALGAS / FOREXCOM:UKOIL on the Watchlist page, whose missing-note now suggests using Sync on the Scanner page for never-synced symbols. Validated: 22/22 market-data tests pass; frontend build green.
- 2026-08-25 — Same convention fix for NSE: `nse_source.fetch_daily` now stores exchange-qualified symbols (`NSE:INFY`) instead of bare bhavcopy names — the mirror image of the TV fix. Root cause of "0 rows match · TRADINGVIEW 55 only": after a DB rebuild, NSE rows were bare so prefixed watchlist keys matched nothing. One-time migration qualified all 1,947 NSE rows (`UPDATE … SET symbol = 'NSE:' || symbol WHERE source='NSE' AND instr(symbol,':')=0`; idempotent). DB is now uniformly prefixed: NSE 1,947 rows / 177 symbols + TRADINGVIEW 55 rows / 5 symbols; watchlist coverage check and `get_ohlc` cache lookups line up for every symbol. Validated: 22/22 tests pass.
- 2026-08-25 — Scanner UI: moved the "Run scan" button out of the top bar into the Auto-scan panel, directly beside "Start auto-scan"/"Stop auto-scan" (same `runScan` handler, still disabled while scanning or with zero selected symbols).
- 2026-08-26 — Fixed market-hours sync gating so each source's data-availability window is respected instead of a blanket "market must be open" check. Added `is_daily_bar_ready(session, now)` + `NSE_BHAVCOPY_READY=17:00` in `src/ict_scanner.py`: NSE bars sync once the bhavcopy is published (after 17:00 IST); commodity/forex bars defer to the next day. `market_data/service.py::sync_symbol_range` now gates on `is_daily_bar_ready` (defers to last completed session, or keeps today and clears any stale `no_data` marker when ready); added `market_data/database.py::clear_no_data`. Live scan loop (`ict_scanner.py:1795`) and scheduled-scan gate (`api/main.py:336`) also run for NSE once `is_daily_bar_ready`. `/api/market-data/sync` now defaults `gate_market_hours=True`. Root cause of the observed bug: the scan skipped symbols whose market was closed, so NSE (closed at night) never backfilled while 24/5 commodities did. Backup (state at change): Backup_26-08_231814.

- 2026-09-14 - IPO tracking: new market_data/ipo.py (discovery, registration, metadata, performance) + scripts/ipo_full_history.py one-pass 3-year NSE scan (per-day bhavcopy cached in data/bhavcopy_cache/, listing price = first-day OPEN, bulk OHLC upsert, validation + rename/flicker scrub). 1,508 IPOs tracked in ipo_metadata + OHLC in DB + watchlist/categories tagged scope=IPO. Endpoints: GET /api/market-data/ipo, /ipo/performance, POST /ipo/discover, /ipo/backfill. Frontend: /ipo page (listing vs current, high/low since listing). Tests: tests/test_ipo.py (9 pass). Known limitation: symbol renames indistinguishable from IPOs via bhavcopy alone.

- 2026-09-23 - IPO eligibility gate + non-equity cleanup. `market_data/ipo.py` now only reports/registers an IPO when it is NSE -> main-board equity -> `EQ` series -> traded recently -> liquidity threshold: new `ipo_ineligibility_reason()` (NSE check, `MAIN_BOARD_SERIES = {"EQ"}`, `NON_IPO_SYMBOL_RE` families, plus the NSE main-board equity master) and `ipo_trading_ineligibility_reason()` (>= `IPO_MIN_ACTIVE_RATIO`=0.5 of sessions traded since listing, avg daily value >= `IPO_MIN_AVG_DAILY_VALUE_CR`=0.25 cr from bhavcopy `TURNOVER_LACS`, else volume*close). New `market_data/equity_master.py` fetches/caches NSE `EQUITY_L.csv` (data/nse_equity_master.csv, `IPO_MASTER_REFRESH_DAYS`=1) - the bhavcopy has no ISIN column and name patterns alone would both miss ETFs (NIFTYBEES) and wrongly exclude real equities (ICICIGI/LICHSGFIN), so master membership is the authoritative "equity/main board" test; on any failure it degrades to the pattern fallback. `discover_new_ipos(include_rejected=...)` returns rejects with `reject_reason`; `sync_new_ipos` adds `skipped`; `register_ipo` raises ValueError for ineligible symbols and `register_ipos` skips them instead of aborting; POST /api/market-data/ipo/discover read-only mode also returns `skipped`. scripts/ipo_full_history.py `is_ipo_candidate`/`--clean-junk` now reuse the shared rule (so a future `--rebuild` admits main-board EQ names only). One-off cleanup: `scripts/remove_non_ipo_symbols.py` deleted the 53 mis-tracked symbols (34 dated govt securities, 5 SGB, 8 ETFs/funds, 6 rights entitlements) from watchlist.txt (1438 -> 1385 lines), watchlist_categories.json (-53), ipo_metadata (1255 -> 1202) and ohlc_daily (-3,632 rows; tv_symbol_cache -2) via `ipo.remove_ipo_completely`; `--audit` reports the remaining 95 tracked entries that still fail the family check (older ETFs/bonds not in the user's list - review separately). Tests: tests/test_ipo.py (39 pass, incl. family/liquidity/activity/registration-gate cases). Pre-existing unrelated failure: tests/test_market_data.py::test_source_flags_are_independent needs live TradingView data.

- 2026-09-26 - Market-data sync fixes + per-market auto-sync. Fixed `sync_symbol_range` NameError (`source_name` undefined) that silently skipped clearing a stale "no data" marker once the day's bar was published. Gating now uses `service.latest_final_session()` with each market's own cut-off: NSE after bhavcopy (17:00 IST), TradingView forex/commodities after the 17:00 New York rollover (previously IST calendar date, so a sync between IST midnight and ~02:30/03:30 IST could store an in-progress bar). Sync response omits OHLC rows unless `include_rows=true`. New `market_data/auto_sync.py` + `GET /api/market-data/auto-sync`, `POST .../auto-sync/start|stop`: 15-min tick, syncs each market once per new final session (retries up to 8 ticks if the bar is not yet upstream). Scanner UI: Auto toggle next to Sync (re-armed from localStorage after API restart), start<=end validation, summary shows new bars / missing-date symbols, date defaults use local (not UTC) date.

- 2026-09-26 - Final-bar-only OHLC + crypto 24x7. Found partial candles stored for all TradingView symbols (e.g. XAUUSD/BTCUSD 21-23 Sep closes differed from TradingView's final close): `ensure_backdate_data` / `/ohlc` fetched in-progress bars and `upsert` never overwrites. `get_ohlc` now never fetches/stores bars newer than `latest_final_session()`; `sync_symbol_range` deletes+re-fetches TradingView rows whose `created_at` predates their close (`bar_final_at`). CRYPTO:* now expects all 7 days and uses the UTC-day cut-off (00:00 UTC = 05:30 IST); auto-sync tracks it as its own CRYPTO market. TradingView daily dates are labelled by the IST date of the bar open explicitly (machine-timezone independent): forex/commodities = NY session day, crypto = UTC day.

- 2026-09-26 - GIFT Nifty (NSEIX:*) daily cut-off: bar D final at 03:00 IST on D+1 (session ends 02:45 IST; previously used the forex 17:00 NY rollover, 15 min early in US summer); own auto-sync market. Repaired all remaining provisional TradingView bars (7 symbols, 31-Aug..25-Sep re-synced; 0 rows now stored before their close).

- 2026-09-26 - Data policy: intraday timeframes use live TradingView bars, daily/weekly (historic) strategies read SQLite final bars only. Fixed look-ahead in `all_strategy._protected_swing_frame` (protected swings / POI / candle-3 / propulsion blocks on 15m/1h/4h): backdated runs fetched intraday bars through today; the window now ends at `as_of_date` (live through today when as_of is today).

- 2026-09-26 — Settings page (`/settings`): `config/app_settings.json` (written by `api/app_settings.py`, `GET/PUT /api/settings`) holds automation on/off + interval/look-back days (scan scheduler, Silver Bullet auto-schedule, IPO scanner, market-data auto-sync) and show/hide for strategies and nav pages; PUT applies changes live and startup re-applies them. Defaults keep prior behavior (only Silver Bullet auto-arms on boot). Strategy and cross-scan tracker toggles reuse their existing endpoints. Hiding a strategy only removes it from the scanner list; its enabled flag still governs scans.

- 2026-09-26 — AM Silver Bullet is weekday-only (New York Mon–Fri): the auto-schedule, live loop and `_check` skip Sat/Sun and the next-check sleep rolls to Monday 10:00 NY (DST-safe). Failure text in the scanner UI is now a compact warning chip (full detail on hover) plus a weekend note. Historical "Test <date>" runs are unchanged.
- 2026-09-26 - SME/legacy IPO purge. 503 tracked IPOs absent from the NSE main-board equity master (SME; sync failed with "no upstream data for any candidate" because the NSE fetcher only keeps EQ/BE/BZ) were removed via new `scripts/remove_non_ipo_symbols.py --master-absent [--dry-run]` (watchlist 1123 -> 620 lines, ipo_metadata -503, ohlc_daily -177,991 rows). Backup: `backups/pre_sme_purge_20260926/`. No new gate was needed: `ipo_ineligibility_reason()` already rejects master-absent symbols for discover/register; these were legacy entries. 11 tracked symbols in master series `BE` remain (main-board trade-to-trade).
- 2026-09-26 - Follow-up purge: removed the 11 master-series `BE` tracked IPOs (AMANTA, DSKULKARNI, LOTUSDEV, MAJESAUT, MBECL, MILKYMIST, MINALIND, RAJOOENG, SEYAIND, TRANSWORLD, VHLTD) via `remove_ipo_completely`, and deleted `ohlc_daily` rows (5,699) for 26 symbols not in watchlist.txt (incl. RELIANCE; on-demand-fetched). Watchlist 609 lines, ohlc_daily 609 symbols / 110,017 rows, ipo_metadata 427. Backup: `backups/pre_be_orphan_purge_20260926/`.
- 2026-09-26 - IPO tracker: `ipo_performance` now adds liquidity (20/60-bar avg traded value, zero-volume days -> LIQUID/BORDERLINE/ILLIQUID) and daily-bar trend metrics (20d/5d return, % from since-listing high, 6-point trend score -> LEADER/IMPROVING/WEAK/NEW, 20d-breakout and 20DMA-pullback entry cues; no look-ahead). New read-only `GET /api/market-data/ipo/review` suggests DISCARD (non-EQ/ETF, non-liquid, stale, or listing date = discovery-window start i.e. existing stock) vs KEEP; nothing is deleted. `/ipo` page: Liquid-only (default on), Leaders/Entry-cue filters, new columns, Review list panel; default age filter changed to All. Tests: tests/test_ipo.py.
- 2026-09-27 - Removed the `daily_fvg_sweep` strategy (runner + helpers in src/all_strategy.py, registry/lookback entries, API catalog in api/strategy_bridge.py, config/strategy_flags.json key, strategy_info.txt and BACKTEST_ENGINE_SPEC.md references). It was restrictive/rarely firing and its bearish gap-size filter was vacuous.
- 2026-09-27 - Strategy alignment with the TTrades source + intraday confirmation. Fixes: protected_swings/propulsion_blocks/points_of_interest now fire on frames longer than their 80-bar scan window (event indices were window-relative, so backtests and scans that loaded more history never confirmed); run_strategies passes the timeframe to points_of_interest/candle_3_closure when run alone; daily_bias_invalidation fetches its own data and drops targets behind entry; MTF bias targets the nearest unbroken weekly/monthly level (2R fallback); every runner reports status=stale instead of signalling on an old last bar. Behaviour changes (see config/strategy_info.txt): candle 2/3 closure rules per the fractal model; protected swings use the first-candle CISD open, accept either sweep-candle colour and invalidate/stop off the sweep extreme; propulsion entry = propulsion open, mean threshold range|body (Settings), invalidation checked before confirmation; daily-bias invalidation needs a true opposite candle 2 or EQ close plus an unreclaimed-EQ continuation; MTF daily/weekly bias = close beyond or sweep-and-close-inside (ties Neutral); weekly profiles fire once per week, a first match at Friday close is a label only, weekday-based day checks, Midweek Thursday fallback, Intraweek candle-3 closure, Thursday Counter -> weekly open target, Consolidation -> range-side target, TGIF adds swing/FVG objectives and a 25% retrace target. New: src/ltf_confirmation.py + LtfConfirmationWatcher (api/main.py, Settings -> Intraday confirmation watcher, default off; timeframe 1h|15m) arming daily setups and confirming a 1h/15m CISD live; GET /api/ltf-confirmation, POST /api/ltf-confirmation/check; scanner page "Intraday confirmations" panel. Backtests remain daily-only (no intraday history stored). Tests: tests/test_strategy_alignment.py, tests/test_ltf_confirmation.py, updated protected-swing/propulsion tests.
- 2026-09-27 - Chart popup (frontend/components/TradingViewChartModal.tsx) now draws its own SVG candlestick chart (frontend/components/OhlcChart.tsx, no new dependency), fed by the new read-only `GET /api/market-data/chart?symbol=&interval=5m|15m|1h|4h|1d|1w|1M&bars=` (market_data/routes.py -> tradingview_source.fetch_recent_bars). TradingView's embeddable widget refuses NSE/BSE symbols ("only available on TradingView"); tvDatafeed does not, so NSE charts now work for every interval. The endpoint is display only (nothing is stored), caches for 20s-30min per interval, resolves NSE->BSE through tv_symbol_cache, and respects FETCH_TRADINGVIEW_DATA. When that source is off or fails, 1d/1w/1M fall back to stored daily OHLC and intraday returns 503. The chart has EMA 20/50/200, a session VWAP (reset per IST day, intraday only), volume, RSI 14, 52W high/low lines, a crosshair legend, wheel zoom/drag pan/arrow keys, Reset/Fit, a PNG snapshot, 60s auto-refresh for intraday, and a stats strip. The embedded TradingView widget is no longer used for any market; "Open" links to the full TradingView chart. Default candles are green up / black down, with volume off. Times are IST. Preferences are saved in localStorage.
- 2026-09-28 - New standalone TradingView Pine Script v6 indicator `docs/pine/ict_scanner.pine` (not part of the Python app; lives outside `src/`/`api/`, hand-maintained for the user's own charts). Extends a user-supplied OHLC/BB/SMA/VWAP/CPR/Silver-Bullet script with a "Strategy Suite" covering the rest of `strategy_registry()`: multi_timeframe_bias, weekly_vs_daily_sweep, inside_bar_pattern_daily_sweep, protected_swings + points_of_interest + candle_3_closure (shared engine), propulsion_blocks, daily_bias_invalidation, and a best-effort weekly-profile structural read; ema5_sweep's existing ad-hoc "5 EMA Reversal" signal was replaced with the exact `_ema5_sweep_bullish`/`_bearish` conditions. All of it is gated to `timeframe.isdaily` (these are daily-bar strategies in Python, fed EOD-published bars) and reads history via `[1]/[2]/[3]` so closed bars never repaint; only 3 total `request.security()` calls (bundled weekly + monthly) cover the whole suite; state machines use O(1) `var` scalars, no loops/arrays. Known simplifications vs. the Python reference (flagged in the file's header comment and to the user): Protected Swings/POI/Candle-2-3 and Propulsion Blocks track only the single most-recent active/anticipated setup per symbol (incremental state machine), not Python's full multi-candidate historical replay; MTF Bias's target uses only the nearest single prior closed week/month extreme (+2R fallback), not an 8-week/6-month scan; the Weekly Profile row is an unverified structural read (the six sub-profile functions in `all_strategy.py` were not read line-by-line). Not run through TradingView's Pine compiler in this session (no such tool available here) — paste into the Pine Editor and check for compiler errors before relying on it live.
- 2026-09-27 - IPO page: the 'Review list' button now runs the list review (GET /api/market-data/ipo/review) and the liquidity screen dry-run (POST /api/ipo-liquidity/screen, auto_remove=false) together and shows one merged table per symbol (review DISCARD, screen REMOVE/WATCH), with checkboxes, 'Select suggested' (DISCARD or REMOVE), 'Delete selected' and per-row Delete. The separate 'Screen (dry-run)' / 'Screen & Auto-Remove' buttons in Maintenance were removed; nothing is deleted without an explicit selection + confirm. New bulk endpoint DELETE /api/market-data/ipo with body {symbols: [...]} (market_data/routes.py -> ipo.remove_ipo_completely per symbol). Auto-remove is gone everywhere: POST /api/ipo-liquidity/screen no longer accepts auto_remove, screen_all_ipos() has no auto_remove argument, and scripts/screen_ipo_liquidity.py lost --auto-remove/--dry-run (screening is read-only; delete only via the IPO page review). Also removed the UI-unused POST /api/ipo-liquidity/remove, GET /api/ipo-liquidity/status and liquidity_screener.remove_symbol_everywhere (duplicate of ipo.remove_ipo_completely).
- 2026-09-27 - IPO page Maintenance: 'Start/Stop IPO scan' removed; only 'Scan now' (POST /api/ipo-scan/run-once) remains, plus a status line linking to Settings. Recurring IPO scans are controlled solely by Settings -> Automation -> IPO scanner (persisted enabled/interval/lookback). Removed UI-only POST /api/ipo-scan/start and /stop (start bypassed Settings and forced lookback_days=7, so the Settings switch could disagree with the live scanner and a restart silently stopped it). Scan now also stops overwriting the status panel with the run result; it reports candidates/added or the skip reason.
- 2026-09-28 - Fixed `docs/pine/ict_scanner.pine` (see 2026-09-28 entry above): compiled clean but threw runtime error RE10026 on OANDA:XAUUSD 1D ("Bar index value of the left argument ... too far from the current bar index") at `box.new()` for the Propulsion Blocks zone. Root cause: `ps_zoneBox`/`pb_zoneBox`/`pb_meanLine` re-anchored their left edge at the original `ps_setupBar`/`pb_setupBar` on every bar; once a setup stays "confirmed" for thousands of bars (a long-lived trend that never breaches its invalidation level — enabled by the same-day re-arm fix), that anchor exceeds Pine's bar-index distance limit for drawing objects. Fixed with `f_clampLeft(setupBar) => math.max(setupBar, bar_index - 400)` applied to all three anchors, so an old setup's box/line now starts at a bounded recent window instead of its true (possibly very old) origin — cosmetic only, no change to the state machine's confirm/invalidate logic. Verified via a live TradingView Pine Editor compile (browser session); TradingView gates its Pine Editor behind a "create a free account" prompt for sustained anonymous use, so full compilation was confirmed by the user pasting into their own account, not by an automated check in this repo.
- 2026-09-28 - `docs/pine/ict_scanner.pine` labelling only (no signal/state logic changed): every zone and level now names its strategy and source timeframe. The Protected Swings box is tagged "PSw zone [D] Daily FVG/Sweep · bull/bear" and its label reads "PSw [D]: <state> (daily FVG)". These FVGs are daily 3-candle gaps, not lower-timeframe gaps. The Propulsion Block box was previously unlabelled and was easily mistaken for part of the nearby WeeklyProfile label; it now carries "Propulsion Block [D]: <state> · bull/bear". The PB mean line and the MTF/DBI SL/TP lines get end tags ("PB mean [D]", "MTF SL [D]", ...). The Silver Bullet range box and SL/TP lines are tagged with the chart timeframe via `f_tfName()` ("SB range [5m]"). The WeeklyProfile label is tagged "[W]".
- 2026-09-28 - Removed Candle 2/3 Closure, Weekly-vs-Daily Sweep and Daily Bias Invalidation from `docs/pine/ict_scanner.pine`, along with their calculations, markers (C2/C3/WvD/DBI), DBI SL/TP lines, table rows, inputs and alerts. The status table now has 5 rows: EMA5, MTF, Inside Bar, Protected Swing / POI, Propulsion Block. The weekly `request.security` calls stay because MTF Bias uses them; the closed-week tuple is trimmed to the prior week's high/low, which leaves MTF values unchanged. This affects only the Pine indicator; the Python scanner still runs all three strategies.
- 2026-09-27 - Strategy scan (api/strategy_bridge.py run_scan): when any selected symbol is CRYPTO:* the testing date is used as-is, no longer rolled back to the previous NSE working day, so weekend/NSE-holiday dates work for crypto-only scans. Selections with no crypto still resolve weekends/holidays to the previous working day; in a mixed selection, non-crypto symbols have no bar on a weekend date and report stale.
- 2026-09-27 - IPO review/liquidity screen: the liquidity screener's "bid-ask spread" was really the average daily (high-low)/close range (the bhavcopy has no quotes), and it was removing liquid new IPOs (CAPILLARY 33.5 cr/day, EXCELSOFT 8.7 cr/day). Renamed it `avg_daily_range_pct` (informational only) and dropped it from the LIQUID/BORDERLINE/ILLIQUID gate (market_data/liquidity_screener.py); `BID_ASK_SPREAD_MAX_PCT` is now unused. `ipo_review` now uses the official NSE listing date from EQUITY_L.csv (`equity_master.load_listing_dates`): a symbol whose official listing date is more than 7 days before its first bhavcopy appearance is flagged as an existing stock (renames, demergers, SME migrations, e.g. SUMEETINDS 2010, UNITDSPR, TMPV). The old "listing date = discovery-window start" rule is only a fallback now. Verified: volume x close in the DB matches bhavcopy TURNOVER_LACS within about ±4% on a 10-day sample for 3 symbols.
- 2026-09-27 - IPO liquidity screener tier now uses the median 60-day traded value instead of the mean, because a listing-day or block-deal spike can inflate the mean (SUMEETINDS: avg 10.12 cr, median 2.50 cr). Circuit-locked days (volume > 0, high == low) now count alongside zero-volume days against LIQUID_MAX_ZERO_DAYS / BORDERLINE_MAX_ZERO_DAYS. Across the 235 tracked IPOs only 3 changed, all KEEP to WATCH: GAUDIUMIVF (34 locked days), SUMEETINDS (18 locked days), MODIS (median 0.92 cr). Reasons now show median, avg, zero-trade and circuit-locked days. Tests: tests/test_liquidity_screener.py.
- 2026-09-28 - IPO age tracking: new IPO_MAX_AGE_DAYS setting (market_data/config.py, default 1095 = 3y, env-overridable). ipo_performance rows now include age_days/age_label. ipo_review adds "aged out: listed X, Ny ago" (DISCARD suggestion only, nothing auto-deleted) once an IPO is older than the limit; on 2026-09-28 that flagged 7 tracked IPOs listed in Sep 2023. The same setting replaces the hard-coded 3*366 / 1098 cap on backfill depth (run_ipo_backfill, POST backfill default) and the `ipo_full_history.py --months all` scan start. The IPO page shows an Age column in both tables (sortable in setups) and adds 6m/1y/2y options to the listing-age filter.
- 2026-09-28 - Points of Interest: _select_point_of_interest (src/all_strategy.py) now skips FVGs that a later candle has closed through (bullish: close below gap low, bearish: close above gap high) and swing POIs whose high/low a later candle already took. The POI note now shows the POI level and labels the protected swing level separately (it used to print only the protected level, which read like the POI). Also affects candle_3_closure, which reuses the same POI selection.
