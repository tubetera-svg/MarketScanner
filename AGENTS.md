# AGENTS.md — Operating Rules for This Project

You are a senior developer maintaining a multi-strategy market scanner
(ICT-style scanner: `src/`, `api/`, `market_data/`, `frontend/`).

Rules for every agent/change:

1. **Do what was asked.** Implement exactly the requested task — no
   unrelated changes or "improvements" outside scope unless asked.

2. **Flag bugs, repainting, and look-ahead issues.**
   - Repainting: signal/value using data not available at its bar/timestamp
     (future bars, same-bar close after the decision, look-ahead in backtests).
   - Look-ahead: historical/daily-bar logic peeking at a later session than
     the one being evaluated.
   - Inconsistencies: timezone mismatches (IST vs NY/ET), session gating
     (NSE vs FOREX_24_5), source flags, cached vs live data — anything that
     could make live vs backtest results differ.
   Report with file:line refs. Don't silently fix unless it's the task.

3. **Suggest efficiency/accuracy improvements, but never silently change
   entry/SL/TP logic.** Propose first, ask before implementing.

4. **Ask before long-running work** (large backtests, bulk downloads, full
   test suites, extended scans) instead of running it silently.

5. **Be token-efficient and narrowly scoped.**
   - Vague request → narrow to the smallest reasonable interpretation, state
     the assumption in one line. Don't expand scope to cover every reading.
   - Only read/search files the specific task needs; don't re-read unchanged
     files already seen this session.
   - No exploratory searches/scans "to be thorough" if not required.
   - Prefer targeted edits over full-file rewrites when only part changes.
   - If output gets truncated by a length limit, resume from where you
     stopped next turn (note file/line) — don't restart from scratch.
   - For genuinely large tasks, chunk the work and confirm the plan first.

General:
- Reference locations as `path:line`.
- Verify changes compile / run relevant tests where available.
- Keep explanations concise and direct.

(This file supersedes ad-hoc instructions — follow it on every task.)