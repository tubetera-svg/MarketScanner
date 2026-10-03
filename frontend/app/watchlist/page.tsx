"use client";

// NOTE: record data still loads ONLY on "Load data" (spec FR-10). The one mount
// request below fetches the STATIC watchlist symbol list (a pure config read of
// config/watchlist.txt) purely to populate the symbol picker — it can never
// trigger NSE/TradingView requests.

import { useEffect, useRef, useState } from "react";
import { Database, Download, Pencil, RefreshCw, Save, SearchX, Trash2, X } from "lucide-react";
import { useStatusFlash } from "../../components/useStatusFlash";
import Navigation from "../../components/Navigation";
import { FavoriteStar, useFavorites } from "../../components/Favorites";
import { IST, addDays, formatDateTime, marketToday, useDisplayTimezone } from "../../components/time";

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

type WatchlistClassification = {
  asset_class?: string;
  exchange?: string;
  scope?: string;
  f_and_o?: string;
  sector?: string;
  industry?: string;
  index?: string;
  market_cap?: string;
  liquidity?: string;
  price_range?: string;
  theme?: string;
};
type ApiWatchlistItem = WatchlistClassification & { symbol: string };
type FnoInfo = { saved_at: string | null; count: number };
type FnoChanges = { to_fno: string[]; to_equity: string[]; flag_updated: string[] };
type FnoPreview = FnoChanges & { preview_id: string; members: number; list_added: string[]; list_removed: string[] };
const fnoChangeGroups: { key: keyof FnoChanges; label: string }[] = [
  { key: "to_equity", label: "F&O → Equity (dropped from F&O)" },
  { key: "to_fno", label: "Equity → F&O (added to F&O)" },
  { key: "flag_updated", label: "F&O flag updated (scope unchanged)" },
];
type PurgeSummary ={ ohlc_rows: number; ipo_metadata: number; tv_symbol_cache: number; ohlc_cache: number; tracker_state: number };
const classificationFields: { key: keyof WatchlistClassification; label: string; placeholder: string }[] = [
  { key: "asset_class", label: "Asset class", placeholder: "equity, crypto, commodity" },
  { key: "exchange", label: "Exchange", placeholder: "NSE, BSE, CRYPTO, OANDA" },
  { key: "scope", label: "Scope", placeholder: "F&O, Crypto, Commodities" },
  { key: "f_and_o", label: "F&O", placeholder: "F&O or Non-F&O" },
  { key: "sector", label: "Sector", placeholder: "Banking, IT, Pharma" },
  { key: "industry", label: "Industry", placeholder: "Private Banks, IT Services" },
  { key: "index", label: "Index", placeholder: "NIFTY 50, NIFTY 500" },
  { key: "market_cap", label: "Market cap", placeholder: "Large, Mid, Small" },
  { key: "liquidity", label: "Liquidity", placeholder: "High, Medium, Low" },
  { key: "price_range", label: "Price range", placeholder: "Penny, Low, Mid, High" },
  { key: "theme", label: "Theme", placeholder: "Defence, EV, Renewable" },
];

const SCOPE = "watchlist";
const pageSizeOptions = [25, 50, 100, 250];
// Stored bars are dated by trading day; "today" is the IST (NSE) date, whatever the browser zone.
const todayISO = () => marketToday(IST);
const localTodayISO = todayISO;
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
  const displayTz = useDisplayTimezone();
  const [records, setRecords] = useState<RecordsPayload | null>(null); // null = not loaded yet
  const [meta, setMeta] = useState<MetaPayload | null>(null);
  const [filters, setFilters] = useState(defaultFilters);
  const [pageSize, setPageSize] = useState(250);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("No data loaded yet — pick symbols and press Load data.");
  const statusFlash = useStatusFlash(message);
  const abortRef = useRef<AbortController | null>(null);
  const [grid, setGrid] = useState<GridFilters>(emptyGridFilters);
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<Set<string>>(new Set());
  const [aliases, setAliases] = useState<Record<string, string[]>>({});
  const [editingSymbol, setEditingSymbol] = useState<string | null>(null);
  const [editSymbolText, setEditSymbolText] = useState("");
  const [editAliasesText, setEditAliasesText] = useState("");
  const [editClassification, setEditClassification] = useState<WatchlistClassification>({});
  const [savingEdit, setSavingEdit] = useState(false);
  const [classifications, setClassifications] = useState<Record<string, WatchlistClassification>>({});
  const [fnoInfo, setFnoInfo] = useState<FnoInfo | null>(null);
  const [fnoPreview, setFnoPreview] = useState<FnoPreview | null>(null);
  const [fnoBusy, setFnoBusy] = useState(false);
  const [managing, setManaging] = useState(false);
  const [manageQuery, setManageQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerQuery, setPickerQuery] = useState("");
  const { isFavorite, toggle: toggleFavorite, reload: reloadFavorites } = useFavorites();
  const pickerRef = useRef<HTMLDetailsElement>(null);
  // Symbol list from the previous refresh (null until the first load).
  const allSymbolsRef = useRef<string[] | null>(null);

  // Native <details> doesn't close on outside click; do that here so the
  // symbol picker doesn't feel "sticky" / stuck open.
  useEffect(() => {
    if (!pickerOpen) return;
    const onDown = (event: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) {
        setPickerOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [pickerOpen]);

  // Mount: config-only read of the static watchlist symbols for the picker,
  // plus the symbol-alias fallback map used by the manager below.
  useEffect(() => {
    void refreshWatchlist();
    void loadFnoInfo();
  }, []);

  const refreshWatchlist = async () => {
    try {
      const [aliasData, categoryData] = await Promise.all([
        fetch(`${API}/api/market-data/aliases`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${API}/api/watchlist`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)),
      ]);
      const rawItems = categoryData?.symbols as ApiWatchlistItem[] | undefined;
      const categoryItems = Array.isArray(rawItems) ? rawItems : [];
      if (categoryData) {
        const symbols = categoryItems.map((item) => item.symbol);
        const previousAll = allSymbolsRef.current;
        setAllSymbols(symbols);
        allSymbolsRef.current = symbols;
        setSelectedSymbols((current) => {
          // First load selects everything; afterwards keep the user's selection
          // (including an empty one), dropping symbols that no longer exist and
          // adding new ones only when everything was selected before.
          if (previousAll === null) return new Set(symbols);
          const valid = new Set(symbols);
          const next = new Set([...current].filter((symbol) => valid.has(symbol)));
          if (previousAll.length > 0 && current.size === previousAll.length) {
            symbols.forEach((symbol) => next.add(symbol));
          }
          return next;
        });
      }
      if (aliasData && typeof aliasData.aliases === "object") {
        setAliases(aliasData.aliases as Record<string, string[]>);
      }
      if (categoryData) {
        setClassifications(Object.fromEntries(categoryItems.map((item) => [item.symbol, item])));
      }
    } catch {
      // best-effort config reads
    }
  };

  const markStale = () => {
    setDirty(true);
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
    if (records) {
      void loadRecords(0, next);
    } else {
      setDirty(true);
    }
  };

  const clearGridFilters = () => {
    setGrid(emptyGridFilters);
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

  const daysAgoISO = (days: number) => addDays(todayISO(), -days);
  const applyPreset = (from: string, to = "") => {
    setFilters((current) => ({ ...current, from, to }));
    markStale();
  };
  const resetFilters = () => {
    setFilters(defaultFilters);
    setGrid(emptyGridFilters);
    markStale();
  };

  const exportCsv = () => {
    if (rows.length === 0) return;
    const header = ["date", "symbol", "exchange", "source", "open", "high", "low", "close", "volume"];
    const lines = rows.map((r) => [r.date, r.symbol, r.exchange, r.source, r.open, r.high, r.low, r.close, r.volume ?? ""].join(","));
    const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `market_data_${todayISO()}_offset${records?.offset ?? 0}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    setMessage(`Exported ${rows.length} rows from this page to CSV`);
  };

  // ---- NSE F&O list: local file, refreshed only on demand (preview → apply) ----
  const loadFnoInfo = async () => {
    try {
      const response = await fetch(`${API}/api/watchlist/fno`, { cache: "no-store" });
      if (response.ok) setFnoInfo(await response.json());
    } catch {
      // best-effort
    }
  };

  const previewFnoRefresh = async () => {
    setFnoBusy(true);
    setMessage("Downloading the NSE F&O list…");
    try {
      const response = await fetch(`${API}/api/watchlist/fno/preview`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "F&O preview failed");
      setFnoPreview(data as FnoPreview);
      const moves = data.to_fno.length + data.to_equity.length;
      setMessage(moves || data.flag_updated.length ? `F&O preview: ${moves} scope change(s) — review and Apply` : "F&O preview: no watchlist changes");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "F&O preview failed");
    } finally {
      setFnoBusy(false);
    }
  };

  const applyFnoRefresh = async () => {
    if (!fnoPreview) return;
    setFnoBusy(true);
    try {
      const response = await fetch(`${API}/api/watchlist/fno/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preview_id: fnoPreview.preview_id }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "F&O apply failed");
      setFnoPreview(null);
      setFnoInfo(data.saved);
      await refreshWatchlist();
      setMessage(`F&O list saved (${data.members} symbols) · ${data.to_fno.length} → F&O · ${data.to_equity.length} → Equity · ${data.flag_updated.length} flag update(s)`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "F&O apply failed");
    } finally {
      setFnoBusy(false);
    }
  };

  // ---- Watchlist manager: edit (rename + alias) / delete symbols ----------
  const confirmDeleteStoredData = (symbol: string) => window.confirm(
    `Also delete ALL stored data for ${symbol}?\n\n` +
    `• Database: OHLC rows + no-data markers (every source), IPO metadata, TradingView symbol cache\n` +
    `• Cache files: ohlc_cache.json and tracker_state_cache.json entries\n\n` +
    `OK = delete data (cannot be undone) · Cancel = keep data`,
  );

  const describePurge = (purged: PurgeSummary | null | undefined) => {
    if (!purged) return " (stored data kept)";
    const cacheEntries = purged.ohlc_cache + purged.tracker_state;
    return ` · deleted ${purged.ohlc_rows} DB row(s), ${purged.ipo_metadata + purged.tv_symbol_cache} metadata row(s), ${cacheEntries} cache entr${cacheEntries === 1 ? "y" : "ies"}`;
  };

  const deleteSymbol = async (symbol: string) => {
    if (!window.confirm(`Remove ${symbol} from the watchlist? Its alias mapping will also be cleared.`)) return;
    const purgeData = confirmDeleteStoredData(symbol);
    setMessage(`Removing ${symbol}…`);
    try {
      const response = await fetch(`${API}/api/watchlist`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, delete_data: purgeData }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Remove failed");
      setSelectedSymbols((current) => {
        const next = new Set(current);
        next.delete(symbol);
        return next;
      });
      void reloadFavorites();
      await refreshWatchlist();
      setMessage(`Removed ${symbol} from the watchlist${describePurge(data.purged)}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Remove failed");
    }
  };

  const beginEdit = (symbol: string) => {
    setEditingSymbol(symbol);
    setEditSymbolText(symbol);
    setEditAliasesText((aliases[symbol] ?? []).join(", "));
    setEditClassification({ ...(classifications[symbol] ?? {}) });
  };

  const cancelEdit = () => {
    setEditingSymbol(null);
    setEditSymbolText("");
    setEditAliasesText("");
    setEditClassification({});
  };

  const saveEdit = async () => {
    if (editingSymbol == null || savingEdit) return;
    const newSymbol = editSymbolText.trim().toUpperCase();
    const aliasList = editAliasesText
      .split(",")
      .map((value) => value.trim().toUpperCase())
      .filter((value) => value.length > 0);
    // Send every editable field, including emptied ones, so the server can clear
    // them; detected-only keys (symbol/base/session) are never sent back.
    const classification: Record<string, string> = Object.fromEntries(
      classificationFields.map(({ key }) => [key, editClassification[key]?.trim() ?? ""]),
    );
    const category = classification.scope ?? "";
    if (!newSymbol || !newSymbol.includes(":")) {
      setMessage("Symbol must be exchange-qualified, e.g. NSE:INFY");
      return;
    }
    // Only a real rename leaves data behind under the old symbol.
    const deleteOldData = newSymbol !== editingSymbol.toUpperCase() && confirmDeleteStoredData(editingSymbol);
    let renameNote = "";
    setSavingEdit(true);
    try {
      {
        const renameResponse = await fetch(`${API}/api/watchlist`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old_symbol: editingSymbol, new_symbol: newSymbol, category: category || null, classification, delete_old_data: deleteOldData }),
        });
        const renameData = await renameResponse.json();
        if (!renameResponse.ok) throw new Error(typeof renameData.detail === "string" ? renameData.detail : "Rename failed");
        void reloadFavorites();
        if (newSymbol !== editingSymbol.toUpperCase()) {
          renameNote = renameData.purged ? `${describePurge(renameData.purged)} for ${editingSymbol}` : ` · ${editingSymbol} data kept`;
        }
        setSelectedSymbols((current) => {
          const next = new Set(current);
          if (next.has(editingSymbol)) {
            next.delete(editingSymbol);
            next.add(newSymbol);
          }
          return next;
        });
        // The rename is saved; if the alias save below fails, the form stays
        // open on the new row and a retry targets the new symbol.
        setEditingSymbol(newSymbol);
        await refreshWatchlist();
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
      setMessage(`Saved ${newSymbol} · ${category || "automatic category"}${aliasList.length ? ` · ${aliasList.length} alias(es)` : " · aliases cleared"}${renameNote}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSavingEdit(false);
    }
  };
  const totalPages = records ? Math.max(1, Math.ceil(records.total / pageSize)) : 1;
  const currentPage = records ? Math.floor(records.offset / pageSize) + 1 : 1;
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
    if (Object.values(classifications[symbol] ?? {}).some((value) => value?.toUpperCase().includes(manageNeedle))) return true;
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
            <Trash2 size={14} className={deleting ? "spin" : undefined} />
            {deleting ? "Deleting…" : "Delete data"}
          </button>
          <div className={`status${statusFlash ? " status-flash" : ""}`}><span className="pulse" />{message}</div>
          <Navigation active="/watchlist" />
        </div>
      </header>

      <section className="auto-scan watchlist-manage">
        <span className="auto-title"><Database size={14} /> Manage watchlist</span>
        <button className="seg" type="button" onClick={() => setManaging((current) => !current)}>
          {managing ? <><X size={13} /> Hide</> : <>Show ({allSymbols.length})</>}
        </button>
        {managing && (
          <div className="watchlist-editor">
            <div className="fno-refresh">
              <span className="symbol-row muted">
                F&amp;O list: {fnoInfo?.count ? `${fnoInfo.count} symbols · saved ${fnoInfo.saved_at ? formatDateTime(fnoInfo.saved_at, displayTz) : "?"}` : "not saved yet — new NSE stocks are tagged Equity"}
              </span>
              {!fnoPreview && (
                <button className="test-button" type="button" onClick={() => void previewFnoRefresh()} disabled={fnoBusy}>
                  <RefreshCw size={13} className={fnoBusy ? "spin" : undefined} /> {fnoBusy ? "Loading NSE list…" : "Refresh F&O from NSE"}
                </button>
              )}
              {fnoPreview && (
                <div className="fno-preview">
                  <strong>
                    NSE F&amp;O list: {fnoPreview.members} symbols
                    {fnoPreview.list_added.length + fnoPreview.list_removed.length > 0
                      ? ` (${fnoPreview.list_added.length} added, ${fnoPreview.list_removed.length} removed since saved list)`
                      : fnoInfo?.count ? " (same as saved list)" : ""}
                  </strong>
                  {fnoChangeGroups.map(({ key, label }) => (
                    <small key={key}>
                      {label}: {fnoPreview[key].length ? `${fnoPreview[key].length} — ${fnoPreview[key].join(", ")}` : "none"}
                    </small>
                  ))}
                  <span className="wl-actions">
                    <button className="test-button" type="button" onClick={() => void applyFnoRefresh()} disabled={fnoBusy}><Save size={13} /> {fnoBusy ? "Applying…" : "Apply"}</button>
                    <button className="test-button button-secondary" type="button" onClick={() => setFnoPreview(null)} disabled={fnoBusy}><X size={13} /> Cancel</button>
                  </span>
                </div>
              )}
            </div>
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
              <div className={`watchlist-row${editingSymbol === symbol ? " editing" : ""}`} key={symbol}>
                {editingSymbol === symbol ? (
                  <form
                    className="watchlist-edit"
                    onSubmit={(event) => { event.preventDefault(); void saveEdit(); }}
                    onKeyDown={(event) => { if (event.key === "Escape") cancelEdit(); }}
                  >
                    <label className="wl-field">
                      Symbol
                      <input autoFocus aria-label={`Symbol for ${symbol}`} value={editSymbolText} onChange={(event) => setEditSymbolText(event.target.value)} placeholder="NSE:INFY" />
                    </label>
                    {classificationFields.map(({ key, label, placeholder }) => (
                      <label key={key} className="wl-field">
                        {label}
                        <input
                          aria-label={`${label} for ${symbol}`}
                          value={editClassification[key] ?? ""}
                          onChange={(event) => setEditClassification((current) => ({ ...current, [key]: event.target.value }))}
                          placeholder={placeholder}
                        />
                      </label>
                    ))}
                    <label className="wl-field wl-field-wide">
                      Aliases
                      <input aria-label={`Aliases for ${symbol}`} value={editAliasesText} onChange={(event) => setEditAliasesText(event.target.value)} placeholder="comma-separated aliases, e.g. BSE:INFY" />
                    </label>
                    <div className="wl-edit-actions">
                      <button className="test-button" type="submit" disabled={savingEdit}><Save size={13} /> {savingEdit ? "Saving…" : "Save"}</button>
                      <button className="test-button button-secondary" type="button" onClick={cancelEdit} disabled={savingEdit}><X size={13} /> Cancel</button>
                    </div>
                  </form>
                ) : (
                  <div className="watchlist-view">
                    <span className="wl-symbol"><strong>{symbol}<FavoriteStar symbol={symbol} active={isFavorite(symbol)} onToggle={() => void toggleFavorite(symbol)} /></strong><small>{classificationFields.map(({ key, label }) => classifications[symbol]?.[key] ? `${label}: ${classifications[symbol]?.[key]}` : null).filter(Boolean).join(" · ") || "automatic classification"}</small>{aliases[symbol]?.length ? <small>aliases: {aliases[symbol].join(", ")}</small> : null}</span>
                    <span className="wl-actions">
                      <button className="test-button" type="button" onClick={() => beginEdit(symbol)} disabled={savingEdit}><Pencil size={13} /> Edit</button>
                      <button className="test-button danger" type="button" onClick={() => deleteSymbol(symbol)}><Trash2 size={13} /> Delete</button>
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
        <details className="symbol-picker" open={pickerOpen} ref={pickerRef}>
          <summary
            aria-label="Choose which watchlist symbols to load"
            aria-expanded={pickerOpen}
            onClick={(event) => {
              event.preventDefault();
              setPickerOpen((current) => !current);
            }}
          >
            {selectedSymbols.size}/{allSymbols.length} symbols
          </summary>
          <div className="symbol-menu">
            <div className="symbol-list">
              <input
                aria-label="Filter symbols in picker"
                className="manage-search"
                style={{ marginBottom: 6, minHeight: 28, fontSize: 11 }}
                placeholder="Filter symbols…"
                value={pickerQuery}
                onChange={(e) => setPickerQuery(e.target.value)}
                onClick={(e) => e.stopPropagation()}
              />
              <label className="symbol-row master">
                <input aria-label="Select all symbols" type="checkbox" checked={allSelected} onChange={toggleAllSymbols} />
                {allSelected ? "Select none" : "Select all"}
              </label>
              {allSymbols.filter((s) => s.toLowerCase().includes(pickerQuery.toLowerCase())).map((symbol) => (
                <label key={symbol} className="symbol-row">
                  <input type="checkbox" checked={selectedSymbols.has(symbol)} onChange={() => toggleSymbol(symbol)} />
                  {symbol}
                  <FavoriteStar symbol={symbol} active={isFavorite(symbol)} onToggle={() => void toggleFavorite(symbol)} />
                </label>
              ))}
              {allSymbols.length > 0 && allSymbols.filter((s) => s.toLowerCase().includes(pickerQuery.toLowerCase())).length === 0 && (
                <span className="symbol-row muted">No symbols match.</span>
              )}
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
          <input aria-label="Start date" type="date" max={filters.to || localTodayISO()} value={filters.from} onChange={(event) => updateFilter("from", event.target.value)} />
          <span className="filter-label">To</span>
          <input aria-label="End date" type="date" min={filters.from || undefined} max={localTodayISO()} value={filters.to} onChange={(event) => updateFilter("to", event.target.value)} />
          <button className="seg" type="button" onClick={() => applyPreset(todayISO())}>Today</button>
          <button className="seg" type="button" onClick={() => applyPreset(daysAgoISO(7))}>7D</button>
          <button className="seg" type="button" onClick={() => applyPreset(daysAgoISO(30))}>30D</button>
          <button className="seg" type="button" onClick={() => applyPreset(daysAgoISO(365))}>1Y</button>
          <button className="seg" type="button" onClick={() => applyPreset("")}>All</button>
          <button className="seg" type="button" onClick={resetFilters} title="Reset all filters to defaults">Reset</button>
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
            <button className="seg" type="button" onClick={exportCsv} disabled={rows.length === 0} style={{ marginLeft: "auto" }}>
              <Download size={12} /> Export page CSV
            </button>
          </div>
        )}
        <div className="table-wrap" style={loading && records ? { opacity: 0.55, transition: "opacity .15s" } : undefined}>
          <table>
            <thead>
              <tr><th>Date</th><th>Symbol</th><th>Exchange</th><th>Source</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Chg %</th><th>Volume</th></tr>
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
                  <th colSpan={6} />
                </tr>
              )}
            </thead>
            <tbody>
              {visibleRows.map((row) => {
                const chg = row.open ? ((row.close - row.open) / row.open) * 100 : null;
                return (
                <tr key={`${row.source}-${row.symbol}-${row.date}`}>
                  <td>{row.date}</td>
                  <td><strong style={{ cursor: "pointer" }} title="Click to filter by this symbol" onClick={() => updateGridFilter("symbol", row.symbol)}>{row.symbol}</strong></td>
                  <td>{row.exchange}</td>
                  <td><span className={`badge ${sourceClass(row.source)}`}>{row.source}</span></td>
                  <td className="number">{number(row.open)}</td>
                  <td className="number">{number(row.high)}</td>
                  <td className="number">{number(row.low)}</td>
                  <td className="number">{number(row.close)}</td>
                  <td className="number" style={{ color: chg == null ? undefined : chg >= 0 ? "var(--teal-ink)" : "var(--coral-ink)" }}>
                    {chg == null ? "-" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}
                  </td>
                  <td className="number">{number(row.volume)}</td>
                </tr>
                );
              })}
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
            <span className="auto-meta">Page {currentPage} / {totalPages}</span>
            <button className="test-button" type="button" disabled={!canPrev || loading} onClick={() => loadRecords(0)}>First</button>
            <button className="test-button" type="button" disabled={!canPrev || loading} onClick={() => loadRecords(records.offset - pageSize)}>Prev</button>
            <button className="test-button" type="button" disabled={!canNext || loading} onClick={() => loadRecords(records.offset + pageSize)}>Next</button>
            <button className="test-button" type="button" disabled={!canNext || loading} onClick={() => loadRecords((totalPages - 1) * pageSize)}>Last</button>
          </div>
        )}
      </section>

      <footer>Read-only view of data/market_data.db. Use Sync on the scanner page to backfill missing history.</footer>
    </main>
  );
}
