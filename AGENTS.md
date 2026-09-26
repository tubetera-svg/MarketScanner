# AGENTS.md — Operating Rules for This Project

Senior developer maintaining a multi-strategy ICT-style market scanner. This file supersedes ad-hoc instructions unless the user gives an explicit higher-priority one.

## 0. Project Map (read this instead of exploring)
| Path | What | Notes |
|---|---|---|
| `src/all_strategy.py` | Strategy defs, `strategy_registry()`, NSE bhavcopy downloader | ~2.7k lines — **grep, never read whole** |
| `src/ict_scanner.py` | Live scan loop, sessions, `is_daily_bar_ready` | ~2.4k lines — grep |
| `src/{silver_bullet,propulsion_blocks,protected_swings,weekly_profile_tracker}.py` | Individual strategy modules | |
| `src/backtest/` | Backtest engine + metrics | Spec: `docs/BACKTEST_ENGINE_SPEC.md` |
| `api/main.py` | FastAPI app (`uvicorn api.main:app`) | `api/strategy_bridge.py` = UI ↔ strategy flags |
| `market_data/` | SQLite OHLC layer (`database.py`, `service.py`, `routes.py`, `sources/`), IPO (`ipo.py`, `equity_master.py`, `liquidity_screener.py`) | DB: `data/market_data.db` |
| `frontend/app/` | Next.js pages: `page.tsx` (scanner, ~1.7k lines), `watchlist/`, `ipo/`, `backtest/` | Shared: `frontend/components/` |
| `config/` | Watchlist, strategy flags/profiles, symbol aliases | Some files are runtime-written caches |
| `main.py` | CLI: run strategies → CSVs in `strategy_outputs/` | |
| `scripts/` | Launchers (`start/stop_market_scanner.bat`), IPO CLIs | `scripts/debug/` = ad-hoc one-off checks, not production |
| `tests/` | pytest suite | |
| `docs/` | Specs | `RUNBOOK.md` (root) = setup + dated changelog |

**Do not read/scan** unless the task requires it: `data/` (116 MB DB, bhavcopy cache, logs), `backups/`, `strategy_outputs/`, `.venv/`, `frontend/node_modules/`, `frontend/.next/`, `frontend/package-lock.json`, `__pycache__/`, `config/*cache*.json`, `config/watchlist*.{txt,json}` (1k+ lines — grep for a symbol). In `RUNBOOK.md`, read only the relevant section; the "Current Context" changelog is long history — grep it by keyword/date.

**Commands** (Windows, PowerShell; use `.venv\Scripts\python.exe` if `python` is not the venv):
- Targeted test: `python -m pytest tests/test_<area>.py -q`
- Frontend typecheck: `Push-Location frontend; npx.cmd tsc --noEmit; Pop-Location`
- API: `python -m uvicorn api.main:app --reload --port 8000`
- Known pre-existing failure: `tests/test_market_data.py::test_source_flags_are_independent` (needs live TradingView).

**Domain facts:** NSE sessions are IST; forex/commodities (`FOREX_24_5`) and ICT kill zones use NY/ET. NSE daily bars are ready after bhavcopy publish (17:00 IST, `is_daily_bar_ready`). Sources are gated independently by env flags `FETCH_TRADINGVIEW_DATA` / `FETCH_NSE_DATA` / `AUTO_FETCH_MISSING_DATA`.

Review checklists for recurring tasks live in `.kilo/skill/` (backtest-validation, data-quality-check, session-timezone-audit, trading-strategy-review) — use when the task matches.

## 1. Scope
- Do exactly what was asked — the smallest viable change.
- No unrelated refactors, "improvements," new abstractions/deps/config, or formatting/line-ending changes. Don't touch generated files or dependencies unless required.
- Follow existing repo patterns. Put throwaway diagnostics in `scripts/debug/` (never repo root, never named `test_*.py` outside `tests/`), and delete them if not reusable.

## 2. Trading Logic Safety
- Entry, filters, position sizing, SL/TP, session, timeframe, and signal-timing logic are sensitive: never change silently.
- If a request risks altering strategy behavior, flag the risk before implementing. Efficiency/accuracy ideas that touch behavior: propose first, implement after approval.

## 3. Flag (don't silently fix, unless fixing is the task)
- **Repainting** — signal uses data unavailable at its bar/timestamp (future bars, same-bar close after decision, future-derived indicators).
- **Look-ahead** — historical/daily/backtest logic peeks at a later session than the one evaluated.
- **Live/backtest drift** — IST vs NY/ET, session gating (NSE vs FOREX_24_5), source flags, cached vs live data.

## 4. Context & Tool-Call Discipline
- Use §0 first; then locate symbol (`rg`) → read minimal range → act. Stop once behavior is understood.
- Every tool call must answer a question that could change the implementation, validation, or report. Don't re-read files or re-establish facts already known this session.
- Keep output small: quiet flags, `head`/`tail`/`Select-Object`, `rg -n` with tight patterns. Never dump full files, logs, DB tables, or test output; if truncated, resume rather than restart.
- Prefer targeted edits over full-file rewrites.

## 5. Ambiguity
- Vague request → smallest reasonable interpretation, state the assumption in one line.
- Ask only when ambiguity materially affects behavior, trading logic, data handling, architecture, or scope.

## 6. Expensive Work — ask first
Large backtests, bulk downloads (bhavcopy/TradingView), full test suite, full-repo scans, DB-wide writes/deletes, IPO backfills. Lightweight targeted checks need no approval.

## 7. Verification
- Run the smallest relevant check (targeted pytest, `tsc --noEmit`, import check). Never claim a pass without running it; if you couldn't validate, say why.
- Pre-existing unrelated failures: report, don't fix.
- For notable behavior changes, append a dated entry to `RUNBOOK.md` "Current Context" (one short paragraph).

## 8. Final Response
Concise, no narrative. Cite locations as `path:line`. Sections:
- **What changed**
- **Files changed**
- **Validation**
- **Remaining issues / risks**
