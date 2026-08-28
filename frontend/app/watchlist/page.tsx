"use client";

// NOTE: record data still loads ONLY on "Load data" (spec FR-10). The one mount
// request below fetches the STATIC watchlist symbol list (a pure config read of
// config/watchlist.txt) purely to populate the symbol picker — it can never
// trigger NSE/TradingView requests.

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Database, RefreshCw, SearchX } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const number = (value: number | null) => value == null ? "-" : value.toLocaleString(undefined, { maximumFractionDigits: 4 });

const sourceClass = (src: string) => {
  const s = src.toLowerCase();
  if (s === "nse") return "source-nse";
  if (s.includes("tradingview")) return "source-tv";
  if (s.includes("yahoo") || s.includes("yfinance")) return "source-yf";
  return "source-other";
};

type RecordRow = {
  source: string;
  exchange: string;
  symbol: string;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
};
type RecordsPayload = {
  scope: string;
  total: number;
  limit: number;
  offset: number;
  rows: RecordRow[];
  watchlist_size: number | null;
  watchlist_missing_in_db: string[];
  notes: string[];
};
type MetaPayload = {
  scope: string;
  row_count: number;
  min_date: string | null;
  max_date: string | null;
  sources: string[];
  exchanges: string[];
  rows_per_source: { source: string; rows: number }[];
};

type WatchlistPayload = { symbols: string[] };

const SCOPE = "watchlist";
const pageSizeOptions = [25, 50, 100, 250];
const todayISO = () => new Date().toISOString().slice(0, 10);
const defaultFilters = { source: "", exchange: "", q: "", from: todayISO(), to: "", sort: "desc" };

// Per-column grid filters (Date/Symbol/Exchange/Source). These now drive the
// SERVER query so they search the WHOLE dataset, not just the loaded page:
// changing one refetches (via loadRecords) against the full SQLite store.
const emptyGridFilters = {
  date: "",
  symbol: "",
  exchange: "",
  source: "",
};
type GridFilters = typeof emptyGridFilters;

export default function WatchlistPage() {
  const [records, setRecords] = useState<RecordsPayload | null>(null); // null = not loaded yet
  const [meta, setMeta] = useState<MetaPayload | null>(null);
  const [filters, setFilters] = useState(defaultFilters);
  const [pageSize, setPageSize] = useState(250);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("No data loaded yet — pick symbols and press Load data.");
  const abortRef = useRef<AbortController | null>(null);
  const [grid, setGrid] = useState<GridFilters>(emptyGridFilters);
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<Set<string>>(new Set());
  const [aliases, setAliases] = useState<Record<string, string[]>>({});
  const [editingSymbol, setEditingSymbol] = useState<string | null>(null);
  const [editSymbolText, setEditSymbolText] = useState("");
  const [editAliasesText, setEditAliasesText] = useState("");
  const [managing, setManaging] = useState(false);
  const [manageQuery, setManageQuery] = useState("");

  // Mount: config-only read of the static watchlist symbols for the picker,
  // plus the symbol-alias fallback map used by the manager below.
  useEffect(() => {
    void refreshWatchlist();
  }, []);

  const refreshWatchlist = async () => {
    try {
      const [watchData, aliasData] = await Promise.all([
        fetch(`${API}/api/market-data/watchlist`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${API}/api/market-data/aliases`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)),
      ]);
      const symbols = (watchData?.symbols as string[] | undefined) ?? [];
      if (Array.isArray(symbols)) {
        setAllSymbols(symbols);
        setSelectedSymbols((current) => {
          // Keep current selections valid; drop any that no longer exist.
          const valid = new Set(symbols);
          const next = new Set([...current].filter((symbol) => valid.has(symbol)));
          return next.size === current.size && next.size > 0 ? current : (next.size ? next : new Set(symbols));
        });
      }
      if (aliasData && typeof aliasData.aliases === "object") {
        setAliases(aliasData.aliases as Record<string, string[]>);
      }
    } catch {
      // best-effort config reads
    }
  };

  const markStale = () => {
    setDirty(true);
    setOffset(0);
  };

  const updateFilter = (key: keyof typeof defaultFilters, value: string) => {
    setFilters((current) => ({ ...current, [key]: value }));
    markStale();
  };

  // Grid column filters now search the WHOLE dataset: changing one refetches the
  // server with the grid filter merged in. If data is already loaded we reload
  // immediately; otherwise we just mark the view stale for the next Load.
  const updateGridFilter = (key: keyof GridFilters, value: string) => {
    const next = { ...grid, [key]: value };
    setGrid(next);
    setOffset(0);
    if (records) {
      void loadRecords(0, next);
    } else {
      setDirty(true);
    }
  };

  const clearGridFilters = () => {
    setGrid(emptyGridFilters);
    setOffset(0);
    if (records) {
      void loadRecords(0, emptyGridFilters);
    } else {
      setDirty(true);
    }
  };

  const allSelected = allSymbols.length > 0 && selectedSymbols.size === allSymbols.length;

  const toggleSymbol = (symbol: string) => {
    setSelectedSymbols((current) => {
      const next = new Set(current);
      if (next.has(symbol)) {
        next.delete(symbol);
      } else {
        next.add(symbol);
      }
      return next;
    });
    markStale();
  };

  const toggleAllSymbols = () => {
    setSelectedSymbols(allSelected ? new Set<string>() : new Set(allSymbols));
    markStale();
  };

  const loadRecords = async (nextOffset: number, overrideGrid?: GridFilters) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setMessage("Loading records…");
    const g = overrideGrid ?? grid;
    // Grid column filters are merged into the SERVER query so they search the
    // whole dataset (not just the loaded page). The grid's source/exchange/symbol
    // refine (override) the equivalent top-bar filter; the grid date is a new
    // substring match combined (AND) with the top-bar From/To range.
    const effSource = g.source || filters.source;
    const effExchange = g.exchange || filters.exchange;
    const effSymbol = g.symbol || filters.q;
    const params = new URLSearchParams();
    params.set("scope", SCOPE);
    params.set("sort", filters.sort);
    // Omitting ?symbols means "the whole watchlist"; any partial selection is explicit.
    if (allSymbols.length > 0 && selectedSymbols.size !== allSymbols.length) {
      params.set("symbols", [...selectedSymbols].sort().join(","));
    }
    params.set("limit", String(pageSize));
    params.set("offset", String(Math.max(0, nextOffset)));
    if (effSource) params.set("source", effSource);
    if (effExchange) params.set("exchange", effExchange);
    if (effSymbol.trim()) params.set("q", effSymbol.trim());
    if (g.date.trim()) params.set("date_contains", g.date.trim());
    if (filters.from) params.set("start_date", filters.from);
    if (filters.to) params.set("end_date", filters.to);
    try {
      const response = await fetch(`${API}/api/market-data/records?${params.toString()}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load records");
      setRecords(data as RecordsPayload);
      setDirty(false);
      setMessage(`Loaded ${(data as RecordsPayload).rows.length} of ${(data as RecordsPayload).total.toLocaleString()} rows`);
      // Always refresh aggregates so the Source/Exchange dropdowns reflect the DB
      // even when a source (e.g. TRADINGVIEW) synced since the previous load.
      void fetch(`${API}/api/market-data/meta?scope=${SCOPE}`, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then(setMeta)
        .catch(() => {});
    } catch (error) {
      if ((error as Error).name === "AbortError") return;
      setMessage(error instanceof Error ? error.message : "Could not load records");
    } finally {
      setLoading(false);
    }
  };

  const deleteData = async () => {
    if (selectedSymbols.size === 0) {
      setMessage("Select at least one symbol to delete.");
      return;
    }
    const hasRange = !!filters.from && !!filters.to;
    const scope = hasRange ? `${filters.from} → ${filters.to}` : "all dates (full history)";
    if (!window.confirm(
      `Delete stored market data for ${selectedSymbols.size} symbol(s) over ${scope}?\n\n` +
      `This clears cached history so it can be re-synced. This cannot be undone.`,
    )) return;
    setDeleting(true);
    setMessage("Deleting stored data…");
    try {
      const response = await fetch(`${API}/api/market-data/records`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: [...selectedSymbols],
          source: filters.source || undefined,
          exchange: filters.exchange || undefined,
          start_date: filters.from || undefined,
          end_date: filters.to || undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Delete failed");
      setMessage(`Deleted ${data.deleted} stored row(s) over ${scope}.`);
      // Refresh the browser view so the removed rows disappear immediately.
      void loadRecords(0);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  };

  const rows = records?.rows ?? [];

  // ---- Watchlist manager: edit (rename + alias) / delete symbols ----------
  const deleteSymbol = async (symbol: string) => {
    if (!window.confirm(`Remove ${symbol} from the watchlist? Its alias mapping will also be cleared.`)) return;
    setMessage(`Removing ${symbol}…`);
    try {
      const response = await fetch(`${API}/api/watchlist`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Remove failed");
      setSelectedSymbols((current) => {
        const next = new Set(current);
        next.delete(symbol);
        return next;
      });
      await refreshWatchlist();
      setMessage(`Removed ${symbol} from the watchlist.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Remove failed");
    }
  };

  const beginEdit = (symbol: string) => {
    setEditingSymbol(symbol);
    setEditSymbolText(symbol);
    setEditAliasesText((aliases[symbol] ?? []).join(", "));
  };

  const cancelEdit = () => {
    setEditingSymbol(null);
    setEditSymbolText("");
    setEditAliasesText("");
  };

  const saveEdit = async () => {
    if (editingSymbol == null) return;
    const newSymbol = editSymbolText.trim().toUpperCase();
    const aliasList = editAliasesText
      .split(",")
      .map((value) => value.trim().toUpperCase())
      .filter((value) => value.length > 0);
    if (!newSymbol || !newSymbol.includes(":")) {
      setMessage("Symbol must be exchange-qualified, e.g. NSE:INFY");
      return;
    }
    try {
      if (newSymbol !== editingSymbol) {
        const renameResponse = await fetch(`${API}/api/watchlist`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old_symbol: editingSymbol, new_symbol: newSymbol }),
        });
        const renameData = await renameResponse.json();
        if (!renameResponse.ok) throw new Error(typeof renameData.detail === "string" ? renameData.detail : "Rename failed");
        setSelectedSymbols((current) => {
          const next = new Set(current);
          if (next.has(editingSymbol)) {
            next.delete(editingSymbol);
            next.add(newSymbol);
          }
          return next;
        });
      }
      const aliasResponse = await fetch(`${API}/api/market-data/aliases`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: newSymbol, aliases: aliasList }),
      });
      const aliasData = await aliasResponse.json();
      if (!aliasResponse.ok) throw new Error(typeof aliasData.detail === "string" ? aliasData.detail : "Alias save failed");
      cancelEdit();
      await refreshWatchlist();
      setMessage(`Saved ${newSymbol}${aliasList.length ? ` with ${aliasList.length} alias(es)` : " (aliases cleared)"}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Save failed");
    }
  };
  const canPrev = !!records && records.offset > 0;
  const canNext = !!records && records.offset + rows.length < records.total;
  const gridActive = Object.values(grid).some((value) => value !== "");
  // Grid dropdowns list ALL values known for the current scope (same data as the
  // top filter bar), not just what happens to be on the loaded page — otherwise a
  // source like TRADINGVIEW disappears whenever the page contains only NSE rows.
  const exchangeValues = meta && meta.scope === SCOPE ? [...meta.exchanges].sort() : Array.from(new Set(rows.map((row) => row.exchange))).sort();
  const sourceValues = meta && meta.scope === SCOPE ? [...meta.sources].sort() : Array.from(new Set(rows.map((row) => row.source))).sort();
  // Grid filters are now applied SERVER-SIDE (see loadRecords), so the returned
  // rows are already the filtered set across the whole dataset.
  const visibleRows = rows;

  const manageNeedle = manageQuery.trim().toUpperCase();
  const filteredManageSymbols = allSymbols.filter((symbol) => {
    if (!manageNeedle) return true;
    if (symbol.toUpperCase().includes(manageNeedle)) return true;
    return (aliases[symbol] ?? []).some((alias) => alias.toUpperCase().includes(manageNeedle));
  });

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Watchlist · Database browser</h1>
        </div>
        <div className="top-actions">
          {dirty && records && <span className="dirty-hint">Selection or filters changed — press Load data</span>}
          <button
            className="scan-now"
            type="button"
            onClick={() => loadRecords(0)}
            disabled={loading || deleting || selectedSymbols.size === 0}
            title={selectedSymbols.size === 0 ? "Select at least one symbol" : undefined}
          >
            <RefreshCw size={14} className={loading ? "spin" : undefined} />
            {loading ? "Loading…" : "Load data"}
          </button>
          <button
            className="scan-now danger"
            type="button"
            onClick={deleteData}
            disabled={loading || deleting || selectedSymbols.size === 0}
            title="Delete stored data for the selected symbols (and From/To date range if set)"
          >
            <RefreshCw size={14} className={deleting ? "spin" : undefined} />
            {deleting ? "Deleting…" : "Delete data"}
          </button>
          <div className="status"><span className="pulse" />{message}</div>
          <a className="top-link" href="/"><ArrowLeft size={12} /> Scanner</a>
        </div>
      </header>

      <section className="auto-scan watchlist-manage">
        <span className="auto-title"><Database size={14} /> Manage watchlist</span>
        <button className="seg" type="button" onClick={() => setManaging((current) => !current)}>
          {managing ? "Hide" : `Show (${allSymbols.length})`}
        </button>
        {managing && (
          <div className="watchlist-editor">
            <input
              aria-label="Search watchlist symbols"
              className="manage-search"
              placeholder="Search symbol or alias…"
              value={manageQuery}
              onChange={(event) => setManageQuery(event.target.value)}
            />
            {allSymbols.length === 0 && <span className="symbol-row muted">Watchlist is empty.</span>}
            {allSymbols.length > 0 && filteredManageSymbols.length === 0 && (
              <span className="symbol-row muted">No symbols match “{manageQuery}”.</span>
            )}
            {filteredManageSymbols.map((symbol) => (
              <div className="watchlist-row" key={symbol}>
                {editingSymbol === symbol ? (
                  <div className="watchlist-edit">
                    <input aria-label={`Symbol for ${symbol}`} value={editSymbolText} onChange={(event) => setEditSymbolText(event.target.value)} placeholder="NSE:INFY" />
                    <input aria-label={`Aliases for ${symbol}`} value={editAliasesText} onChange={(event) => setEditAliasesText(event.target.value)} placeholder="comma-separated aliases, e.g. BSE:INFY" />
                    <button className="test-button" type="button" onClick={saveEdit}>Save</button>
                    <button className="test-button" type="button" onClick={cancelEdit}>Cancel</button>
                  </div>
                ) : (
                  <div className="watchlist-view">
                    <span className="wl-symbol"><strong>{symbol}</strong>{aliases[symbol]?.length ? <small>aliases: {aliases[symbol].join(", ")}</small> : null}</span>
                    <span className="wl-actions">
                      <button className="test-button" type="button" onClick={() => beginEdit(symbol)}>Edit</button>
                      <button className="test-button danger" type="button" onClick={() => deleteSymbol(symbol)}>Delete</button>
                    </span>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="auto-scan">
        <span className="auto-title"><Database size={14} /> Symbols</span>
        <details className="symbol-picker">
          <summary aria-label="Choose which watchlist symbols to load">
            {selectedSymbols.size}/{allSymbols.length} symbols
          </summary>
          <div className="symbol-menu">
            <div className="symbol-list">
              <label className="symbol-row master">
                <input aria-label="Select all symbols" type="checkbox" checked={allSelected} onChange={toggleAllSymbols} />
                {allSelected ? "Select none" : "Select all"}
              </label>
              {allSymbols.map((symbol) => (
                <label key={symbol} className="symbol-row">
                  <input type="checkbox" checked={selectedSymbols.has(symbol)} onChange={() => toggleSymbol(symbol)} />
                  {symbol}
                </label>
              ))}
              {allSymbols.length === 0 && <span className="symbol-row muted">Watchlist unavailable.</span>}
            </div>
          </div>
        </details>
        <div className="filter-grid">
          <span className="filter-label">Source</span>
          <select aria-label="Filter by source" value={filters.source} onChange={(event) => updateFilter("source", event.target.value)}>
            <option value="">Any</option>
            {(meta?.sources ?? []).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <span className="filter-label">Exchange</span>
          <select aria-label="Filter by exchange" value={filters.exchange} onChange={(event) => updateFilter("exchange", event.target.value)}>
            <option value="">Any</option>
            {(meta?.exchanges ?? []).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <input aria-label="Search symbol" placeholder="Symbol contains…" value={filters.q} onChange={(event) => updateFilter("q", event.target.value)} />
          <span className="filter-label">From</span>
          <input aria-label="Start date" type="date" value={filters.from} onChange={(event) => updateFilter("from", event.target.value)} />
          <span className="filter-label">To</span>
          <input aria-label="End date" type="date" value={filters.to} onChange={(event) => updateFilter("to", event.target.value)} />
          <span className="filter-label">Sort</span>
          <select aria-label="Sort direction" value={filters.sort} onChange={(event) => updateFilter("sort", event.target.value)}>
            <option value="desc">Newest first</option>
            <option value="asc">Oldest first</option>
          </select>
          <span className="filter-label">Page size</span>
          <select aria-label="Rows per page" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); markStale(); }}>
            {pageSizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
        </div>
      </section>

      {records && (
        <section className="meta-strip">
          <span><strong>{records.total.toLocaleString()}</strong> rows match</span>
          {typeof records.watchlist_size === "number" && <span> · watchlist: {records.watchlist_size}</span>}
          {meta && <span> · DB coverage {meta.min_date ?? "-"} → {meta.max_date ?? "-"}</span>}
          {meta && meta.rows_per_source.length > 0 && (
            <span> · {meta.rows_per_source.map((entry) => `${entry.source} ${entry.rows.toLocaleString()}`).join(" · ")}</span>
          )}
          {records.watchlist_missing_in_db.length > 0 && (
            <small className="missing-note">
              In watchlist but not in DB ({records.watchlist_missing_in_db.length}):{" "}
              {records.watchlist_missing_in_db.slice(0, 12).join(", ")}{records.watchlist_missing_in_db.length > 12 ? "…" : ""}
              {" — never synced yet; use Sync on the Scanner page to backfill these."}
            </small>
          )}
        </section>
      )}

      <section className="results">
        {records && (
          <div className="grid-toolbar">
            <span>
              {gridActive
                ? `Grid filters applied across all data · ${rows.length.toLocaleString()} rows on this page (of ${records.total.toLocaleString()} matching)`
                : `${rows.length.toLocaleString()} rows loaded (this page of ${records.total.toLocaleString()})`}
            </span>
            {gridActive && (
              <button className="seg active" type="button" onClick={clearGridFilters}>Clear grid filters</button>
            )}
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Date</th><th>Symbol</th><th>Exchange</th><th>Source</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th></tr>
              {records && (
                <tr className="grid-filter-row">
                  <th><input aria-label="Filter date contains" placeholder="contains…" value={grid.date} onChange={(event) => updateGridFilter("date", event.target.value)} /></th>
                  <th><input aria-label="Filter symbol contains" placeholder="contains…" value={grid.symbol} onChange={(event) => updateGridFilter("symbol", event.target.value)} /></th>
                  <th>
                    <select aria-label="Filter by exchange" value={grid.exchange} onChange={(event) => updateGridFilter("exchange", event.target.value)}>
                      <option value="">Any</option>
                      {exchangeValues.map((value) => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </th>
                  <th>
                    <select aria-label="Filter by source" value={grid.source} onChange={(event) => updateGridFilter("source", event.target.value)}>
                      <option value="">Any</option>
                      {sourceValues.map((value) => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </th>
                </tr>
              )}
            </thead>
            <tbody>
              {visibleRows.map((row) => (
                <tr key={`${row.source}-${row.symbol}-${row.date}`}>
                  <td>{row.date}</td>
                  <td><strong>{row.symbol}</strong></td>
                  <td>{row.exchange}</td>
                  <td><span className={`badge ${sourceClass(row.source)}`}>{row.source}</span></td>
                  <td className="number">{number(row.open)}</td>
                  <td className="number">{number(row.high)}</td>
                  <td className="number">{number(row.low)}</td>
                  <td className="number">{number(row.close)}</td>
                  <td className="number">{number(row.volume)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!records && (
            <div className="empty"><SearchX size={20} /> No data loaded yet — choose symbols above and press “Load data”.<br />Browsing is read-only and never fetches from NSE or TradingView.</div>
          )}
          {records && rows.length === 0 && (
            <div className="empty">
              <SearchX size={20} />
              {records.watchlist_size === 0
                ? "Watchlist is empty."
                : "No rows match these filters."}
            </div>
          )}
          </div>

        {records && records.total > 0 && (
          <div className="pager">
            <span className="auto-meta">
              Showing {records.offset + 1}–{records.offset + rows.length} of {records.total.toLocaleString()}
              {gridActive ? " · grid filters applied" : ""}
            </span>
            <button className="test-button" type="button" disabled={!canPrev || loading} onClick={() => loadRecords(records.offset - pageSize)}>Prev</button>
            <button className="test-button" type="button" disabled={!canNext || loading} onClick={() => loadRecords(records.offset + pageSize)}>Next</button>
          </div>
        )}
      </section>

      <footer>Read-only view of data/market_data.db. Use Sync on the scanner page to backfill missing history.</footer>
    </main>
  );
}
