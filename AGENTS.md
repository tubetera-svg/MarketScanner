# AGENTS.md — Operating Rules for This Project

Senior developer maintaining a multi-strategy ICT/TTrades-style technical-analysis market scanner (NSE equities/ETFs/IPOs, forex, commodities, crypto). This file supersedes ad-hoc instructions unless the user gives an explicit higher-priority one. It is the single source of truth for every agent: `CLAUDE.md` just imports it; Kilo reads it directly.

## 0. Project Map (read this instead of exploring)
| Path | What | Notes |
|---|---|---|
| `src/all_strategy.py` | Strategy defs, `strategy_registry()`, `run_strategies`, NSE bhavcopy downloader | ~2.7k lines — **grep, never read whole** |
| `src/ict_scanner.py` | Live scan loop (`AdaptiveScanner.run_once`), sessions/holidays, `is_daily_bar_ready`, FVG/sweep/displacement detectors, `OHLCCache` | ~2.7k lines — grep |
| `src/{silver_bullet,propulsion_blocks,protected_swings}.py` | Individual strategy modules | |
| `src/ltf_confirmation.py` | 1h/15m confirmation of daily setups | Signal-sensitive |
| `src/daily_context.py` | Display-only `ctx_*` columns appended to strategy rows | Must never gate a signal |
| `src/backtest/` | `engine.py` + `metrics.py` | Spec: `docs/BACKTEST_ENGINE_SPEC.md` |
| `api/main.py` | FastAPI app (`uvicorn api.main:app`), routes, Silver Bullet scheduler | ~1.3k lines — grep `@app.` |
| `api/strategy_bridge.py` | UI ↔ strategy flags, `run_scan()` (anchor_date → historical scan) | |
| `api/{app_settings,price_alerts,push,news_calendar,fno_membership}.py` | Settings JSON, price alerts, Telegram/ntfy push, econ calendar, F&O list | Push creds only from env |
| `market_data/` | SQLite OHLC layer: `database.py` (schema), `service.py`, `routes.py`, `sources/{nse,tradingview}_source.py`, `auto_sync.py`, `bootstrap.py`, `tv_symbol.py`, `state_store.py` (`APP_STATE_STORE=db`), `favorites.py`, `etf_list.py`, `config.py` | DB: `data/market_data.db` (`ohlc_daily`, `ohlc_no_data`, `app_state`, …) |
| `market_data/{ipo,equity_master,liquidity_screener}.py` | IPO tracking + liquidity screen | CLIs in `scripts/ipo/` |
| `market_data/timeutil.py`, `frontend/components/time.ts` | Time contract helpers (parse/serialize instants, display formatting, market dates) | See §2b — the only place time is formatted |
| `frontend/app/` | Next.js pages: `page.tsx` (scanner, ~1.8k lines), `watchlist/`, `ipo/`, `backtest/`, `alerts/`, `settings/` | Shared: `frontend/components/` |
| `config/` | Watchlist, `strategy_flags.json`, `strategy_info.txt`, `symbol_aliases.json`, `app_settings.json`, `favorites.json` | Edited by users/UI; runtime state in `data/state/` (git-ignored) |
| `main.py` | CLI: run strategies → CSVs in `strategy_outputs/` | |
| `scripts/` | Launchers (`start/stop_market_scanner.bat`), `ipo/` | `scripts/debug/` = throwaway checks |
| `tests/` | pytest suite (one file per area) | |
| `docs/` | Specs + `pine/ict_scanner.pine` | `RUNBOOK.md` (root) = setup + dated changelog |

**Do not read/scan** unless the task requires it: `data/` (116 MB DB, bhavcopy cache, logs, `state/`), `backups/` (if present), `strategy_outputs/` (CLI output), `.venv/`, `frontend/node_modules/`, `frontend/.next/`, `.kilo/node_modules/`, `*package-lock.json`, `__pycache__/`, `config/watchlist*.{txt,json}` (1k+ lines — grep for a symbol). In `RUNBOOK.md`, read only the relevant section; "Current Context" is long history — grep by keyword/date.

**Where to look by task**
| Task | Start at | Also update |
|---|---|---|
| New/changed strategy | `strategy_registry()` in `all_strategy.py`, or a `src/<name>.py` module | `config/strategy_info.txt`, `strategy_flags.json` default, `tests/test_<name>.py`, backtest parity |
| Session/timezone/holiday | `detect_session`, `NSE_HOLIDAYS` (live view from `market_data/nse_holidays.py`, NSE CM only), `is_*_open`, `is_daily_bar_ready` in `ict_scanner.py`; time helpers §2b | `session-timezone-audit` skill, `tests/test_time_contract.py` |
| New data source / sync | `market_data/sources/`, `service.py`, `auto_sync.py`, `config.py` env flags | `tests/test_market_data.py`, `test_data_cutoffs.py` |
| Backtest | `src/backtest/engine.py`, `POST /api/backtest` | `docs/BACKTEST_ENGINE_SPEC.md`, `backtest-validation` skill |
| Alerts / push | `api/price_alerts.py`, `api/push.py`, `frontend/components/PriceAlert*` | `tests/test_price_alerts.py`, `test_push.py` |
| UI page | `frontend/app/<page>/page.tsx`, `frontend/components/` | API route in `api/main.py`; run `tsc --noEmit` |

**Commands** (Windows, PowerShell; use `.venv\Scripts\python.exe` if `python` is not the venv):
- Targeted test: `python -m pytest tests/test_<area>.py -q`
- Frontend typecheck: `Push-Location frontend; npx.cmd tsc --noEmit; Pop-Location`
- API: `python -m uvicorn api.main:app --reload --port 8000`

**Domain facts:** NSE sessions are IST (09:15–15:30); forex/commodities (`FOREX_24_5`) and ICT kill zones / Silver Bullet use NY/ET with 17:00 NY rollover; crypto days are UTC. NSE daily bars are final after bhavcopy publish (17:00 IST, `is_daily_bar_ready`). TradingView intraday rows carry UTC open instants; daily rows carry the market's trading date (§2b). Sources are gated independently by env flags `FETCH_TRADINGVIEW_DATA` / `FETCH_NSE_DATA` / `AUTO_FETCH_MISSING_DATA`.

**Skills** (review checklists): `backtest-validation`, `data-quality-check`, `session-timezone-audit`, `trading-strategy-review`. Use when the task matches.

## 1. Scope
- Do exactly what was asked — the smallest viable change.
- No unrelated refactors, "improvements," new abstractions/deps/config, or formatting/line-ending changes. Don't touch generated files or dependencies unless required.
- Follow existing repo patterns. Put throwaway diagnostics in `scripts/debug/` (never repo root, never named `test_*.py` outside `tests/`), and delete them if not reusable.
- New strategy → also add it to `config/strategy_info.txt`.

### 1a. Hosting Environment (check on every change)
The app runs locally on Windows **and** hosted (Linux, UTC clock; PaaS such as Render free tier with an ephemeral disk that is wiped on deploy/restart and sleeps when idle; frontend possibly on Vercel). Before finishing any change, check it against these and mention real impact under "Remaining issues / risks":
- **Time:** no host-local dates/times (§2a). Schedules must tolerate the host sleeping or restarting mid-session and be computed in the market's zone.
- **State & disk:** runtime writes go only to `config/` or `data/state/` through `market_data.state_store` (`APP_STATE_STORE=db`, DB on persistent storage via `MARKET_DATA_DB_PATH`). No new loose output files/folders. A restart must not repeat finished work or re-send alerts — persist "already done" markers (`market_data.automation_state`).
- **Processes:** in-process schedulers assume one API worker/instance; more would double scans and pushes. Background work must not depend on a page being open.
- **OS/paths:** `pathlib`, case-sensitive file names, no Windows-only commands or paths in app code (`.bat`/PowerShell stay in `scripts/`).
- **Network:** TradingView, NSE and ForexFactory can block or rate-limit cloud IPs (403/429, Cloudflare). Every outbound call needs a timeout, failure backoff and a fallback or clear error — no tight polling or bulk fetches on boot.
- **Resources:** free tiers have ~512 MB RAM and little CPU; avoid loading the whole DB/watchlist history into memory, keep SQLite single-writer, keep boot fast.
- **Security:** hosted means internet-exposed. Secrets only via env (never in repo or `config/*.json`, never logged); endpoints that write, delete or trigger expensive work must stay behind the site's auth; CORS only explicit origins (`CORS_ORIGINS`).
- **Config:** a new env var needs a default that works locally and a RUNBOOK note; the frontend reaches the API only via `NEXT_PUBLIC_API_URL`.

## 2. Trading Logic Safety
- Entry, filters, position sizing, SL/TP, RR threshold, session, timeframe, and signal-timing logic are sensitive: never change silently.
- If a request risks altering strategy behavior, flag the risk before implementing. Efficiency/accuracy ideas that touch behavior: propose first, implement after approval.
- Changing a constant (lookback, multiplier, window, threshold) is a behavior change.

### 2a. Technical-Analysis Correctness Rules
Apply to all strategy, indicator, scanner and backtest code. Violations are flagged per §3, not silently fixed.
- **Closed bars only.** Decisions use completed bars. The last bar from a live fetch may be forming — exclude it or treat it as provisional. Daily bars count only after the market's cut-off (`is_daily_bar_ready` for NSE, 17:00 NY for FX, 00:00 UTC for crypto).
- **Confirmation lag.** Swing highs/lows, pivots, fractals, protected swings, and order blocks are known only N bars after the extreme. Signals must be stamped at the confirmation bar, not the pivot bar.
- **Signal time = detection time.** Each signal carries the bar timestamp it was decided on; the same setup must not re-fire on later scans (dedupe by symbol + strategy + setup bar).
- **Higher-timeframe context** (PDH/PDL, PWH/PWL, weekly profile, daily bias) comes from the last *completed* HTF period relative to the bar being evaluated — never today's/this week's partial bar.
- **Timezone-aware everywhere.** Follow §2b. Never use host-local `date.today()` / naive `datetime.now()` (the app may run on a UTC/US server); market dates come from `market_data.service.market_today(source, symbol)` / `ist_today()`.
- **Calendars.** NSE holidays, weekends, half/special sessions (Muhurat) and FX DST shifts must not create phantom or missing bars. A missing bar is "no data", not "flat price".
- **Price adjustments.** NSE splits/bonuses/rights change historical prices; mixing adjusted (TradingView) and unadjusted (bhavcopy) series breaks levels. Flag any cross-source comparison that ignores this.
- **Data integrity before signals.** Reject bars with `high < low`, open/close outside range, zero/NaN prices, or duplicate timestamps rather than letting them form levels.
- **Backtest realism.** Entries fill at the next tradable price after the signal bar (not the signal bar's close/extreme). If SL and TP both sit inside one bar, assume SL first unless intrabar data proves otherwise. Gaps through stops fill at the open. Account for costs (brokerage, STT/fees, slippage) or report results as gross. Universe must be point-in-time (no survivorship: delisted/SME-migrated symbols, IPO listing date).
- **Parity.** Live scanner, historical test (`anchor_date`), and backtest must call the same detection functions with the same parameters.
- **No over-fitting.** Don't tune parameters on the same window used to report performance; mention sample size (trades, period) with any metric.

### 2b. Time & Timezone Contract
Two different things — never mix them:
| | **Market zone** (logic) | **Display zone** (people) |
|---|---|---|
| Decides | sessions, bar finality/cut-offs, "today" per market, which trading day a bar belongs to | how a time *looks* in the UI and push messages |
| Value | fixed: NSE/NSE IX = IST, forex/commodities = New York, crypto = UTC | `config/app_settings.json` → `ui.display_timezone` (default `Asia/Kolkata`; Settings page; `"browser"` = viewer's zone, server falls back to IST) |
| Changing it | changes signals → §2 trading-logic change | display only, never alters data or signals |

Rules
1. **Instants** (bar open time, alerts, news, signal/trigger/last-run times) are tz-aware and serialized as **UTC ISO-8601 with offset** (`2026-10-03T04:15:00+00:00`). Never write a naive time. Backend: `timeutil.utc_now()`, `to_utc_iso()`.
2. **Trading-day dates** (`ohlc_daily.date`, `signal_date`, `valid_until`, date pickers, scan anchors) are plain `YYYY-MM-DD` in the market's calendar and are **never timezone-converted**.
3. **Reading**: parse with `timeutil.parse_instant()` / TS `toInstant()`. A naive value is legacy **IST wall time** (old rows, cached state) — never assume host-local.
4. **Intraday bars**: `tradingview_source` emits UTC instants. Consumers convert to their market zone (`silver_bullet` → NY, `ltf_confirmation.session_date`). Strategy frames in `all_strategy._protected_swing_frame` are re-indexed to naive IST wall time so strategy outputs stay identical; keep that conversion at the edge.
5. **"Today"**: backend `service.market_today(source, symbol)` / `ist_today()`; frontend `marketToday(zone)` (IST for NSE scan/sync dates). Never the browser or host date.
6. **Formatting for people**: only via helpers — backend `timeutil.fmt_display(value, app_settings.display_timezone())` (push texts); frontend `components/time.ts` (`formatTime`, `formatDateTime`, `formatDayDateTime`, `formatInZone`, `zoneLabel`, `wallLabel` for chart axes) with the zone from `useDisplayTimezone()` (re-renders on change). Show the zone label (`zoneLabel`) next to times.
7. **User-entered times** (quiet hours, "mute until") are in the display zone; market cut-offs on the Settings page stay in their market zone and say so.
8. **Banned** outside the helper modules (enforced by `tests/test_time_contract.py`): Python `date.today()`, `datetime.now()` without tz, `datetime.utcnow()`, `.astimezone()` without tz; TS `toLocale{Date,Time}String`, `new Date(...).toLocaleString`, `toISOString().slice(...)`, `Date#getHours/getDate/getDay/getMonth/getFullYear/getMinutes`, `new Intl.DateTimeFormat`, hard-coded IST offsets.
9. **New display surface or setting**: add a helper to `time.ts` / `timeutil.py` if one is missing, never inline `Intl`/`strftime`; new choices for the display zone go in `timeutil.DISPLAY_TIMEZONES`.
10. **Tests**: time logic gets a test at a moment where IST, NY and UTC dates differ (e.g. `2026-10-03T20:00Z`), plus a DST boundary when NY is involved.

Example — a new API field `last_synced_at` and its UI:
`state["last_synced_at"] = utc_now().isoformat(timespec="seconds")` → TS `{formatDateTime(s.last_synced_at, tz)} {zoneLabel(tz, s.last_synced_at)}` with `const tz = useDisplayTimezone()`.

## 3. Flag (don't silently fix, unless fixing is the task)
- **Repainting** — signal uses data unavailable at its bar/timestamp (future bars, same-bar close after decision, future-derived indicators).
- **Look-ahead** — historical/daily/backtest logic peeks at a later session than the one evaluated.
- **Live/backtest drift** — IST vs NY/ET, session gating (NSE vs FOREX_24_5), source flags, cached vs live data.
- Any §2a violation found while working on something else.

## 4. Context & Tool-Call Discipline
- Use §0 first; then locate symbol (`rg -n "def <name>"`) → read minimal range → act. Stop once behavior is understood.
- Reference code by **function/class name**, not line numbers, in docs and skills (line numbers rot).
- Every tool call must answer a question that could change the implementation, validation, or report. Don't re-read files or re-establish facts already known this session.
- Keep output small: quiet flags, `head`/`tail`/`Select-Object`, `rg -n` with tight patterns. Never dump full files, logs, DB tables, or test output; if truncated, resume rather than restart.
- Prefer targeted edits over full-file rewrites.

## 5. Ambiguity
- Vague request → smallest reasonable interpretation, state the assumption in one line.
- Ask only when ambiguity materially affects behavior, trading logic, data handling, architecture, or scope.

## 6. Expensive Work — ask first
Large backtests, bulk downloads (bhavcopy/TradingView), full test suite, full-repo scans, DB-wide writes/deletes, IPO backfills. Backups: code is versioned in git — never create code-copy/backup folders. Before a DB-wide delete or state purge, snapshot to `backups/pre_<op>_YYYYMMDD/` (existing pattern). Lightweight targeted checks need no approval.

## 7. Verification
- Run the smallest relevant check (targeted pytest, `tsc --noEmit`, import check). Never claim a pass without running it; if you couldn't validate, say why.
- Strategy/indicator changes: add or extend a test with a hand-built candle fixture that pins the expected signal bar and levels; tests must not hit the network.
- Pre-existing unrelated failures: report, don't fix.
- For notable behavior changes, append a dated entry to `RUNBOOK.md` "Current Context" (one short paragraph).

## 8. Multi-Agent Sync
Several agents (Claude Code, Kilo, …) work on this repo.
- Rules live only here. `CLAUDE.md` imports this file — don't duplicate rules into it.
- Skills exist in two copies: `.claude/skills/` (canonical) and `.kilo/skill/` (mirror for Kilo). Edit the canonical copy, then run `powershell -File scripts/sync_agent_skills.ps1`. `tests/test_agent_context.py` fails if they drift.

## 9. Final Response
Concise, no narrative. Cite locations as `path:line`. Sections:
- **What changed**
- **Files changed**
- **Validation**
- **Remaining issues / risks**
