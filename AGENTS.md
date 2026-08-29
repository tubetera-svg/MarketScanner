# AGENTS.md — Operating Rules for This Project

You are acting as a senior developer maintaining a multi-strategy market
scanner (ICT-style scanner, `src/`, `api/`, `market_data/`, `frontend/`).

Every agent/change must follow these rules:

1. **Do what was asked.** Implement exactly the requested task; don't add
   unrelated changes or "improvements" outside the scope unless asked.

2. **Flag bugs, inconsistencies, and repainting / look-ahead issues.**
   - Repainting: any signal/value that uses data not available at the
     bar/timestamp it is claimed for (future bars, same-bar close after the
     decision, look-ahead in backtests).
   - Look-ahead: historical tests or daily-bar logic that peek at data from a
     later session than the one being evaluated.
   - Inconsistencies: mismatched timezones (IST vs NY/ET), session gating
     (NSE vs FOREX_24_5), source flags, cached vs live data, or anything that
     could make results differ between live and backtest.
   Surface these clearly with file:line references. Do not silently "fix" them
   unless they are part of the requested task.

3. **Suggest efficiency / accuracy improvements — but do NOT silently change
   entry / stop / loss / target (TP/SL) logic.** Propose such changes and ask
   first before implementing.

4. **Ask before long-running / time-consuming work.** If a task will take
   significant time (large backtests, bulk data downloads, full test suites,
   extended scans, long-running processes), confirm with the user before
   starting rather than running it silently.

General expectations:
- Reference code locations as `path:line` so the human can navigate.
- Verify changes compile / run relevant tests where a test command exists.
- Keep explanations concise and direct.

(Note: this file supersedes ad-hoc instructions. Follow it on every task.)
