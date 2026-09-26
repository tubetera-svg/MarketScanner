# Watchlist Page — Technical Specification & Implementation Plan

**Status:** Proposed — ready for implementation
**Date:** 2026-08-25
**Scope:** New dedicated "Watchlist" page that browses records stored in the local
SQLite market-data database (`data/market_data.db`), with a filter that toggles
between *watchlist-only* rows and *all* rows in the database.
**Hard requirement:** the page loads **no data by default** — nothing is queried
(or fetched from any upstream provider) until the user explicitly presses
"Load data".

---

## 1. Summary

Add a read-only browser over the existing `ohlc_daily` table:

- A new page at `/watchlist` in the Next.js frontend.
- Two new backend endpoints served by the existing `market_data` router:
  - `GET /api/market-data/records` — paged, filtered rows from SQLite.
  - `GET /api/market-data/meta` — lightweight aggregates (counts, distinct sources/exchanges, date range) used to populate filter dropdowns and status strips.
- A **scope filter** on both endpoints: `scope=watchlist` restricts rows to
  symbols present in `config/watchlist.txt`; `scope=all` returns every row.
- Strict **read-only** behaviour: the endpoints query SQLite directly and never
  call `get_ohlc`'s cache-aside fetch path, so opening/browsing this page can
  never trigger NSE Bhavcopy or TradingView network requests.

This mirrors how the existing scanner UI works (single client-side page,
plain `fetch` against `http://127.0.0.1:8000`, CSS utility classes from
`globals.css`) so it slots into the codebase without new dependencies.

## 2. Current architecture (relevant pieces)

| Concern | Location | Notes |
|---|---|---|
| FastAPI app | `api/main.py` | Includes `market_data.routes.router`; CORS allows `localhost:3000` |
| Market-data routes | `market_data/routes.py` | `GET /ohlc`, `GET /api/ohlc`, `POST /api/market-data/sync`; has best-effort `_watchlist_entries()` helper |
| Storage layer | `market_data/database.py` | All SQL lives here; `connect()`, `init_db()`, `upsert_ohlc()`, `query_ohlc()`, `count_rows()`; short-lived connections, parameterized queries |
| Config | `market_data/config.py` | DB path (`MARKET_DATA_DB_PATH` override), `KNOWN_SOURCES = ("TRADINGVIEW", "NSE")` |
| Cache-aside service | `market_data/service.py` | `get_ohlc()` — the ONLY place allowed to hit upstream sources; `resolve_session_source()` maps `NSE:*` → NSE else TRADINGVIEW |
| Watchlist source of truth | `config/watchlist.txt` | One `EXCHANGE:SYMBOL` per line (e.g. `NSE:INFY`, `OANDA:XAUUSD`), loaded via `ict_scanner.load_watchlist` |
| Frontend | `frontend/app/page.tsx` | Single client page; `const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000"`; panels/tables styled with `globals.css` classes (`panel`, `table-wrap`, `badge`, `empty`, `filter-label`, …) |
| Tests | `tests/test_market_data.py`, `tests/conftest.py` | `tmp_db` fixture monkeypatches `MARKET_DATA_DB_PATH`; FastAPI `TestClient` |

Schema being browsed (`ohlc_daily`):

```sql
CREATE TABLE ohlc_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- 'NSE' | 'TRADINGVIEW'
    symbol TEXT NOT NULL,          -- prefixed, e.g. 'NSE:INFY', 'OANDA:XAUUSD'
    exchange TEXT NOT NULL,        -- 'NSE' | 'OANDA' | 'CAPITALCOM' | 'FOREXCOM' ...
    date TEXT NOT NULL,            -- 'YYYY-MM-DD' (ISO text, lexicographic-safe)
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL,                   -- nullable
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, exchange, symbol, date)
);
-- indexes: idx_ohlc_source_symbol_date (source, symbol, date)
--          idx_ohlc_symbol_date (symbol, date)
```

## 3. Requirements

### Functional

- **FR-1 Dedicated page:** `/watchlist` renders a records browser independent of the scanner page (own URL, own state).
- **FR-2 Scope toggle:** segmented control with exactly two options —
  - `Watchlist only` (default): rows whose `symbol` is in `config/watchlist.txt`.
  - `All records`: every row in `ohlc_daily`.
  Toggling scope alone never loads data; it marks the view "stale" until the user presses **Load data**.
- **FR-3 Additional filters (all optional, combinable):** source (`NSE`/`TRADINGVIEW`), exchange (select options from `/meta`), symbol substring search (server-side `symbol LIKE '%q%'`), date range `from`/`to` (inclusive).
- **FR-4 Sorting:** by `date` ascending/descending (desc default); secondary sort `symbol ASC` for stable paging.
- **FR-5 Pagination:** server-side `LIMIT/OFFSET` with `total` count; page-size selector (25/50/100/250; hard cap 1000).
- **FR-6 Metadata strip:** after a successful load show row counts and DB date coverage (`min_date`…`max_date`) plus per-source totals from `/meta`.
- **FR-7 Watchlist diagnostics:** when `scope=watchlist`, the response reports watchlist symbols with **no** rows in the DB (`watchlist_missing_in_db`) so coverage gaps are obvious.
- **FR-8 Empty states:** distinct messaging for "not loaded yet", "loaded but 0 rows match filters", and "watchlist is empty".
- **FR-9 Navigation:** links between the scanner page (`/`) and `/watchlist`.

### The "no data by default" rule (core requirement)

- **FR-10** On mount the page performs **zero** network requests for record data — no fetch in any mount `useEffect`, no prefetch. Initial UI shows: *"No data loaded yet — set filters and press Load data."*
- **FR-11** Changing any control (scope, filters, sort, page size) never triggers a fetch. It sets a `dirty` flag; the previously loaded table stays visible with a hint *"Filters changed — press Load data."* The only fetch triggers are: pressing **Load data**, and pagination controls (an explicit request for a specific page of the already-chosen query).
- **FR-12 Backend guarantee:** `/api/market-data/records` and `/api/market-data/meta` are pure SQLite reads. They must not call `market_data.service.get_ohlc`, source modules, or any network code — enforced by a unit test that registers a booby-trapped fetcher via `service.register_fetcher(...)` and asserts it is never invoked.

### Non-functional

- **NFR-1** No new Python or npm dependencies.
- **NFR-2** Parameterized SQL only; read-only statements; WAL readers run safely alongside scan/auto-sync writes.
- **NFR-3** Respect SQLite's host-parameter ceiling by chunking long `symbol IN (...)` lists (§5.4).
- **NFR-4** Local single-user tool (same trust model as the rest of the app): no auth added.
- **NFR-5** Server-side paging keeps the page responsive with ≥100k rows; no response ever materializes the whole table.

## 4. High-level design

```
Browser (/watchlist)                          FastAPI (port 8000)
┌─────────────────────────────┐  GET /api/market-data/records?scope=…&…
│ scope toggle                │ ──────────────────────────────────────► market_data/routes.py
│ filter bar                  │                                           │ builds WHERE + paging
│ [Load data] (manual only)   │ ◄──────────────────────────────────────  ▼
│ records table + pager       │  {total, rows[], notes[]}            market_data/database.py
└─────────────────────────────┘                                        SELECT … FROM ohlc_daily
                                        GET /api/market-data/meta ───► aggregates only
                                       ✗ NEVER calls service.get_ohlc / sources / network
```

Load sequence: user clicks **Load data** → frontend aborts any in-flight request →
`GET /api/market-data/records` (+ `/meta` once per session) → table + status strip
render → done. Errors surface in the existing status-message style.

## 5. Backend specification

### 5.1 New read helpers — `market_data/database.py`

Add alongside `query_ohlc` (same style: parameterized, short-lived connection,
`row_factory = sqlite3.Row`, reads take no write lock):

```python
def query_ohlc_page(
    *,
    symbols: Optional[Sequence[str]] = None,   # None/[] => no symbol filter
    source: Optional[str] = None,
    exchange: Optional[str] = None,
    symbol_contains: Optional[str] = None,     # substring match, case-insensitive
    start_date: Optional[date | str] = None,   # inclusive; validated via _to_date_text
    end_date: Optional[date | str] = None,     # inclusive
    order: str = "desc",                       # 'asc' | 'desc' on date; tiebreak symbol ASC
    limit: int = 100,                          # routes layer owns the caps
    offset: int = 0,
    db_path: Optional[Path | str] = None,
) -> tuple[list[dict], int]:
    """One page of ohlc_daily rows plus total count for the same filter set.

    Pure read. Returns ([row dicts in _ROW_COLUMNS order], total_matching).
    """
```

Implementation notes:

- Build `clauses`/`params` exactly like `query_ohlc` does (`source = ?`,
  `exchange = ?`, `date >= ?`, `date <= ?`, and `symbol LIKE ?` with pattern
  `"%" + value.upper() + "%"` since symbols are stored uppercase).
- Symbol filtering uses chunked OR-groups to stay inside SQLite's
  host-parameter ceiling (§5.4).
- Two statements per call:
  1. `SELECT COUNT(*) FROM ohlc_daily [WHERE …]` → total.
  2. `SELECT {', '.join(_ROW_COLUMNS)} FROM ohlc_daily [WHERE …]
     ORDER BY date {order}, symbol ASC LIMIT ? OFFSET ?`.
- Clamp defensively anyway: `limit = max(1, min(limit, 1000))`,
  `offset = max(0, offset)`.

Additional small helpers:

```python
def distinct_values(column: str, db_path=None) -> list[str]:
    """Distinct values of 'source' or 'exchange' (uppercase, sorted) for dropdowns."""

def date_range(db_path=None) -> tuple[Optional[str], Optional[str]]:
    """(MIN(date), MAX(date)) across ohlc_daily; (None, None) when empty."""

def rows_per_source(db_path=None) -> list[dict]:
    """SELECT source, COUNT(*) AS rows … GROUP BY source ORDER BY source."""
```

### 5.2 New endpoints — `market_data/routes.py`

Follows existing router conventions (`APIRouter(tags=["market-data"])`, inline
`Query(...)` declarations, `_serve_*` helper wrapping errors).

```python
@router.get("/api/market-data/records")
def read_records(
    scope: str = Query("watchlist", pattern="^(watchlist|all)$"),
    source: Optional[str] = Query(None),            # validated via normalize_source()
    exchange: Optional[str] = Query(None, max_length=40),
    symbol_query: Optional[str] = Query(None, alias="q", max_length=80),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict: ...
```

Behaviour:

1. `scope == "watchlist"` → resolve symbols with the existing best-effort
   `_watchlist_entries()` (already in this module); use
   `[sym.upper() for sym, _ in entries]`. Empty watchlist ⇒ **200** with
   `rows: []`, `total: 0` and a note (viewing must not hard-fail like sync's 404).
2. Validate `source` via `config.normalize_source()` → 400 on unknown;
   `start_date > end_date` → 400.
3. Call `database.query_ohlc_page(...)`; never touch `service.get_ohlc`.
4. Watchlist scope additionally runs a paged-free
   `SELECT DISTINCT symbol … WHERE <filters>` to compute
   `watchlist_missing_in_db = sorted(set(watch) - {r["symbol"] for r in rows})`.

Response contract:

```jsonc
{
  "scope": "watchlist",
  "total": 1284,               // matching rows across all pages
  "limit": 100, "offset": 0,
  "rows": [                     // _ROW_COLUMNS order
    {"source":"NSE","symbol":"NSE:INFY","exchange":"NSE","date":"2026-08-24",
     "open":1598.5,"high":1604.0,"low":1592.2,"close":1601.7,"volume":5123400}
  ],
  "watchlist_size": 214,                        // null when scope=all
  "watchlist_missing_in_db": ["NSE:SOMENEW"],   // [] when scope=all
  "notes": []
}
```

```python
@router.get("/api/market-data/meta")
def read_meta(scope: str = Query("all", pattern="^(watchlist|all)$")) -> dict:
```

Aggregates used by filter dropdowns + status strip:

```jsonc
{
  "scope": "all",
  "row_count": 45230,
  "min_date": "2026-08-11", "max_date": "2026-08-24",
  "sources": ["NSE", "TRADINGVIEW"],
  "exchanges": ["CAPITALCOM", "FOREXCOM", "NSE", "OANDA"],
  "rows_per_source": [{"source":"NSE","rows":43000},{"source":"TRADINGVIEW","rows":2230}]
}
```

With `scope=watchlist`, aggregates apply the same watchlist symbol filter
(chunked IN-groups). Update the module docstring's "Exposed paths" list.

### 5.3 Error handling & guarantees

- Map `ValueError` → 400 (mirrors `_serve_ohlc`); unexpected exceptions → 500
  with `log.exception`. No `FetchError`/`NoDataError` paths exist here because
  nothing fetches.
- **Read-only guarantee:** no `INSERT/UPDATE/DELETE`; no calls into
  `market_data.service` or `market_data.sources.*`. Safe while a scan or
  auto-sync is writing (WAL allows concurrent readers; short-lived connections).
- Missing DB file: `init_db()` already creates an empty schema idempotently —
  endpoints return zero-row results rather than errors.

### 5.4 Query logic details

- **Chunking:** SQLite's legacy host-parameter limit is 999; with a ~500-symbol
  watchlist plus other filters, emit `(symbol IN (?,…?) OR symbol IN (?,…?))`
  groups of ≤400 placeholders each, flattening params in order.
  Constant: `_SYMBOL_CHUNK = 400`.
- **Watchlist matching:** exact match on the stored prefixed symbol
  (`'NSE:INFY'`). Valid because every writer path (`sync_symbol_range` →
  `get_ohlc`) stores the prefixed form from `watchlist.txt`. (A future
  `match_base` flag could also match bare symbols — out of scope.)
- **Index usage:** symbol/exchange/source filters ride
  `idx_ohlc_symbol_date` / `idx_ohlc_source_symbol_date`. The unfiltered
  `scope=all` page ordered by `date DESC` will sort — acceptable at local scale
  (<1M rows); a covering `(date)` index is a listed future optimization.
- **Counting:** one `COUNT(*)` per request over the identical WHERE clause keeps
  pager math exact even while rows are being inserted concurrently.

## 6. Frontend specification

### 6.1 Files

| File | Action | Purpose |
|---|---|---|
| `frontend/app/watchlist/page.tsx` | **new** | `"use client"` page — scope toggle, filters, table, pager |
| `frontend/app/watchlist/layout.tsx` | **new** | Server layout exporting `metadata: { title: "Watchlist – ICT Scanner" }` (client pages can't export metadata) |
| `frontend/app/globals.css` | edit | Small additions: `.scope-toggle`, `.seg`, `.seg.active`, `.filter-grid`, `.pager`, `.top-link` |
| `frontend/app/page.tsx` | edit | One nav link in the topbar: `<a className="top-link" href="/watchlist">Database</a>` |

### 6.2 Component tree

```
WatchlistPage (client)
├─ header (title "Watchlist · Database browser", back link "← Scanner", status text)
├─ section.controls
│  ├─ ScopeToggle        — segmented: [Watchlist only] [All records]
│  ├─ FilterBar          — Source select · Exchange select · Symbol search input
│  │                       · Date from · Date to · Sort asc/desc
│  └─ ActionBar          — [Load data] primary · page-size select · dirty hint
├─ section.records
│  ├─ MetaStrip          — "{total} rows · coverage {min}→{max} · per-source counts"
│  ├─ RecordsTable       — Date | Symbol | Exchange | Source | Open | High | Low |
│  │                       Close | Volume   (volume null renders "-")
│  └─ Pagination         — Prev/Next + "Showing X–Y of Z"
└─ empty / error blocks  — reuse existing `.empty` styling
```

### 6.3 Types & state model

```ts
type Scope = "watchlist" | "all";
type RecordRow = { source: string; exchange: string; symbol: string; date: string;
                   open: number; high: number; low: number; close: number; volume: number | null };
type RecordsPayload = { scope: Scope; total: number; limit: number; offset: number;
                        rows: RecordRow[]; watchlist_size: number | null;
                        watchlist_missing_in_db: string[]; notes: string[] };
type MetaPayload = { scope: Scope; row_count: number; min_date: string | null;
                     max_date: string | null; sources: string[]; exchanges: string[];
                     rows_per_source: { source: string; rows: number }[] };

const [scope, setScope]       = useState<Scope>("watchlist");
const [records, setRecords]   = useState<RecordsPayload | null>(null); // null = NOT LOADED
const [meta, setMeta]         = useState<MetaPayload | null>(null);
const [filters, setFilters]   = useState({ source: "", exchange: "", q: "", from: "", to: "", sort: "desc" });
const [pageSize, setPageSize] = useState(100);
const [offset, setOffset]     = useState(0);
const [loading, setLoading]   = useState(false);
const [dirty, setDirty]       = useState(false);   // controls changed since last load
const [message, setMessage]   = useState("No data loaded yet — set filters and press Load data.");
```

### 6.4 Interaction rules (implements FR-10/FR-11)

- **Mount:** the only effects present are none-for-data by design. Add a code
  comment at the top: `// NOTE: intentionally no data fetch on mount — the DB
  browser loads only when the user presses “Load data” (FR-10).`
- Any control change → update state, `setDirty(true)`, reset `offset` to 0.
  The stale table stays visible; when `dirty && records` show the hint chip
  *"Filters changed — press Load data"*.
- **Load data** click → `loadRecords()`:
  - abort previous via `AbortController` stored in a ref;
  - build `URLSearchParams` (omit empty filters), `fetch(\`${API}/api/market-data/records?…\`, { cache: "no-store", signal })`;
  - on first successful load also fetch `/api/market-data/meta` (same trigger,
    `Promise.allSettled`) to fill dropdowns and the meta strip;
  - success → `setRecords(payload)`, `setDirty(false)`, message
    `"Loaded {rows.length} of {total} rows"`;
  - failure → keep old data, message shows `data.detail ?? error.message`
    (same pattern as `runScan`).
- Pagination clicks call the same `loadRecords(nextOffset)` — an explicit user
  action, so allowed despite FR-11.
- Empty states: `records === null` → placeholder panel; `total === 0` → if
  scope=watchlist and `watchlist_size === 0`: *"Watchlist is empty"*, else
  *"No rows match these filters."* Also render a small warning list from
  `watchlist_missing_in_db` when non-empty ("in watchlist but not in DB").
- Accessibility: `aria-label`s on every select/input (existing convention).

### 6.5 Styling

Reuse existing `globals.css` vocabulary (`shell`, `panel`, `table-wrap`,
`badge`, `empty`, `filter-label`, `kicker`). New rules are minimal:

```css
.scope-toggle { display: inline-flex; gap: 4px; }
.seg { padding: 6px 14px; border-radius: 8px; border: 1px solid var(--line); background: transparent; }
.seg.active { background: var(--accent-soft); border-color: var(--accent); font-weight: 600; }
.filter-grid { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.pager { display: flex; gap: 8px; align-items: center; justify-content: flex-end; }
.top-link { margin-left: auto; font-size: 13px; opacity: .85; }
```

(Adjust custom-property names to whatever `globals.css` actually defines —
match the file's existing variables rather than inventing new ones.)

## 7. Testing plan

### 7.1 Automated (pytest) — new file `tests/test_watchlist_records.py`

Reuse the existing fixtures/patterns from `tests/test_market_data.py`
(`tmp_db` env override, FastAPI `TestClient` on `api_main.app`, seeding via
`database.upsert_ohlc`, fake fetchers via `service.register_fetcher`):

1. **test_records_scope_watchlist_filters_rows** — seed rows for two watchlist
   symbols and one non-watchlist symbol; `scope=watchlist` returns only
   watchlist rows; `total` matches.
2. **test_records_scope_all_returns_everything** — same DB, `scope=all`
   returns all seeded rows regardless of watchlist.
3. **test_records_pagination_and_total** — 25 matching rows, `limit=10`:
   first page has 10 rows / `total=25`; `offset=20` returns 5; stable order
   (`date desc, symbol asc`).
4. **test_records_filters** — combine source/exchange/date-range/`q` filters;
   assert each narrows the result set as expected.
5. **test_records_never_fetches_upstream** — register a fetcher for both
   sources that raises `AssertionError("network fetch attempted")`; query dates
   with no stored data; endpoint must return 200 with empty/short results and
   the booby-trap must never fire (proves FR-12).
6. **test_records_empty_watchlist_returns_empty_ok** — monkeypatch
   `routes._watchlist_entries` → `[]`; expect 200, `total=0`,
   note mentioning the watchlist is empty.
7. **test_records_watchlist_missing_in_db** — watchlist contains a symbol with
   no rows; response lists it in `watchlist_missing_in_db`.
8. **test_meta_endpoint** — counts, min/max dates, distinct sources/exchanges,
   per-source totals; also honours `scope=watchlist`.
9. **test_validation_errors** — bad `scope`, unknown `source`,
   `start_date > end_date` → 400.

Run with: `python -m pytest tests/ -q` (from repo root).

### 7.2 Manual QA script

1. Start backend `python -m uvicorn api.main:app --reload --port 8000` and
   frontend `cd frontend; npm run dev`.
2. Open `/watchlist`: confirm the Network tab shows **no** XHR requests and the
   placeholder message is visible.
3. Press **Load data**: table fills from SQLite only (uvicorn log must show no
   NSE/TradingView fetch lines).
4. Toggle to **All records**, change filters — verify nothing refetches until
   **Load data** is pressed again; verify dirty hint appears/disappears.
5. Page through results; verify "Showing X–Y of Z" math and page-size changes.
6. Verify scanner page still works and both nav links function.

## 8. Rollout plan (implementation order)

1. **Backup first** (repo convention): copy the current tree to
   `backups/Backup_25-08_watchlistpage/` (mirrors `Backup_25-08_marketdata` etc.).
2. **DB layer:** add `query_ohlc_page`, `distinct_values`, `date_range`,
   `rows_per_source` to `market_data/database.py`.
3. **Routes:** add `_serve_records`, `read_records`, `read_meta` to
   `market_data/routes.py`; update module docstring path list.
4. **Tests:** create `tests/test_watchlist_records.py`; run full suite.
5. **Frontend:** add `frontend/app/watchlist/{layout.tsx,page.tsx}`, extend
   `globals.css`, add the topbar link in `frontend/app/page.tsx`.
6. **Manual QA** per §7.2.
7. **Docs:** append a dated entry to `RUNBOOK.md`, e.g.

   > 2026-08-25 — Added a read-only Watchlist page (`/watchlist`) for browsing
   > the SQLite market-data store with a scope filter (watchlist vs all rows),
   > source/exchange/symbol/date filters, sorting and server-side pagination.
   > New endpoints: GET `/api/market-data/records`, GET `/api/market-data/meta`
   > — pure reads, never trigger upstream fetches. Page loads no data until the
   > user presses "Load data". Tests: `tests/test_watchlist_records.py`.
   > Pre-change backup: Backup_25-08_watchlistpage.

## 9. Edge cases & limitations

| Case | Handling |
|---|---|
| Empty watchlist file | 200 + empty result + explanatory note (never 404) |
| Watchlist symbol absent from DB | Listed in `watchlist_missing_in_db`; UI shows hint to run *Sync* on the scanner side |
| DB file missing | `init_db()` creates empty schema → zero rows, no error |
| Concurrent scan writing rows | WAL readers see consistent snapshots; counts may grow between pages — acceptable |
| Very large `scope=all` | Hard `limit ≤ 1000` per page; total via COUNT |
| Symbol param flood | Chunked IN-groups (≤400 placeholders) |
| `volume` NULL | Rendered as "-" |
| Dates stored as TEXT | ISO strings compare lexicographically; validate via FastAPI `date` type |
| Stale UI after background sync | User presses Load data again; no auto-polling by design |

## 10. Acceptance criteria

- [ ] `/watchlist` exists, is linked from `/`, and renders without any network request on mount.
- [ ] Data appears only after pressing **Load data**; changing controls never auto-fetches.
- [ ] Scope toggle returns watchlist-only rows by default and all rows when switched.
- [ ] Source/exchange/symbol/date filters + sort + paging all work against the API.
- [ ] Meta strip shows row count, date coverage, per-source totals.
- [ ] Endpoints provably never call upstream sources (unit test green).
- [ ] Full pytest suite passes; manual QA checklist completed.
- [ ] RUNBOOK.md updated; pre-change backup created.

## 11. Assumptions & future enhancements

**Assumptions:** single-user local deployment (no auth); symbols are stored
prefixed exactly as in `config/watchlist.txt`; English-only UI; existing CORS
origins suffice.

**Future (out of scope):** CSV export of the current filtered view;
URL-query-state sync for shareable views; covering index on `date` if the
unfiltered view grows; per-symbol row-count breakdown endpoint;
"Sync missing from this page" action that reuses `/api/market-data/sync`.

---
*End of specification.*






