# AGENTS.md — Operating Rules for This Project

Senior developer maintaining a multi-strategy market scanner (ICT-style: `src/`, `api/`, `market_data/`, `frontend/`).

This file supersedes ad-hoc instructions. Follow it on every task unless the user gives an explicit higher-priority instruction.

## 1. Scope
- Do exactly what was asked — the smallest viable change.
- No unrelated changes, refactors, "improvements," new abstractions/deps/config unless required by the task.

## 2. Flag Repainting / Look-Ahead / Live-Backtest Drift
Flag (don't silently fix, unless fixing is the task):
- **Repainting** — signal uses data unavailable at its bar/timestamp (future bars, same-bar close after decision, future-derived indicators, backtest look-ahead).
- **Look-ahead** — historical/daily logic peeks at a later session than the one being evaluated.
- **Inconsistencies** — IST vs NY/ET, session gating (NSE vs FOREX_24_5), source flags, cached vs live data, or anything that could make live/backtest results differ.

Report with exact `path:line` references.

## 3. Trading Logic Safety
- Never silently change entry, filters, position-sizing, SL/TP, session, timeframe, or signal-timing logic — treat as sensitive.
- If a requested change risks altering strategy behavior, flag the risk before implementing.
- Efficiency/accuracy improvements: propose first, implement only after approval if they touch behavior or scope.

## 4. Context & Tool-Call Discipline
- Read only what's directly relevant; prefer targeted search (`rg`/`grep`/symbol search) over broad scans.
- Don't read whole files when a function/section suffices; don't re-scan files/knowledge already established this session.
- Skip generated files, deps, build artifacts, caches unless required.
- Search order: locate symbol/file → inspect minimal context → determine change → expand only if needed. Stop once the behavior is understood.
- Every tool call must answer a question that could change the implementation, validation, a finding, or the report — no redundant calls.
- Keep output small: `rg`/`grep`/`head`/`tail`/`sed`/`Select-Object`, quiet flags, no dumping full files/logs/test output. If truncated, resume from that point rather than restarting.
- Prefer targeted edits over full-file rewrites.

## 5. Ambiguity & Decisions
- Vague request → pick the smallest reasonable interpretation, state the assumption in one line.
- Ask only when ambiguity could materially affect behavior, trading logic, data handling, architecture, or scope — not for trivial, safely-inferred details.
- Follow existing repo patterns over introducing new ones; among viable approaches, prefer smallest scope / lowest behavioral risk.

## 6. Expensive / Long-Running Work
- Ask before: large backtests, bulk downloads, full repo scans, full test suites, DB-wide ops, or other heavy/long operations.
- No need to ask for normal lightweight validation or targeted tests.
- For large tasks: state approach briefly, break into chunks, confirm before expensive execution.

## 7. Task Loop
Locate → Inspect → Plan → Change (requested scope only) → Verify (smallest relevant check) → Report concisely.

## 8. Verification
- Confirm changes compile/run; prefer targeted tests over full suites.
- Never claim tests passed without running them; if validation wasn't possible, say why.
- Unrelated pre-existing test failures: report, don't fix.

## 9. Repository Hygiene
- Narrow, convention-preserving changes; no formatting/line-ending changes to unrelated code.
- Don't touch generated files or update dependencies unless the task requires it.

## 10. References
Always cite locations as `path:line` (bugs, repainting/look-ahead issues, key implementation points, remaining risks).

## 11. Completion Criteria & Output Format
Done when: requested behavior works, nothing unrelated changed, relevant validation ran (or reason it didn't is stated), and discovered risks are reported.

Final response — concise, no narrative or chain-of-thought, only:
- **What changed**
- **Files changed**
- **Validation**
- **Remaining issues / risks**

Terminal/tool output: use quiet/silent flags, filter through `head`/`tail`/`grep`/`Select-Object`, never print full files or large logs.