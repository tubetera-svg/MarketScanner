"use client";

import TradingViewChartModal, { type ChartTarget } from "../../components/TradingViewChartModal";
import { useStatusFlash } from "../../components/useStatusFlash";

// IPO tracker: reads NSE IPO metadata + live performance from the local API.
// Data loads from the local SQLite store on mount/refresh.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowDown, ArrowUp, Database, RefreshCw, Rocket, SearchX } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

const number = (value: number | null | undefined) =>
  value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: 2 });

const pctClass = (value: number | null | undefined) => {
  if (value == null) return "bias-neutral";
  if (value > 0) return "bias-bull";
  if (value < 0) return "bias-bear";
  return "bias-neutral";
};

type PerformanceItem = {
  symbol: string;
  exchange: string;
  listing_date: string;
  listing_price: number | null;
  issue_price: number | null;
  latest_date: string | null;
  current_price: number | null;
  high_since_listing: number | null;
  low_since_listing: number | null;
  pct_vs_listing: number | null;
};

type LiquidityScreenResult = {
  symbol: string;
  liquidity_tier: "LIQUID" | "BORDERLINE" | "ILLIQUID" | "N/A";
  flags: string[];
  decision: "ADD" | "KEEP" | "WATCH" | "REMOVE";
  reason: string;
  removed?: {
    watchlist: boolean;
    categories: boolean;
    ohlc_daily: number;
    ohlc_no_data: number;
    ipo_metadata: number;
    tv_symbol_cache: number;
  };
};

type ScannerStatus = {
  running: boolean;
  interval_minutes: number;
  lookback_days: number;
  last_ran_at: string | null;
  last_error: string | null;
  run_count: number;
};

type SortKey =
  | "symbol"
  | "listing_date"
  | "listing_price"
  | "current_price"
  | "high_since_listing"
  | "low_since_listing"
  | "pct_vs_listing";

type PerfBucket = "all" | "gainers" | "losers" | "flat";

type Freshness = "all" | "15d" | "1m" | "3m";

const FRESHNESS_DAYS: Record<Exclude<Freshness, "all">, number> = {
  "15d": 15,
  "1m": 31,
  "3m": 92,
};

export default function IPOPage() {
  const [items, setItems] = useState<PerformanceItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("Load IPO performance from the local database.");
  const statusFlash = useStatusFlash(message);
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [scanning, setScanning] = useState(false);
  const [screening, setScreening] = useState(false);
  const [screenResults, setScreenResults] = useState<LiquidityScreenResult[] | null>(null);
  const [query, setQuery] = useState("");
  const [year, setYear] = useState("all");
  const [bucket, setBucket] = useState<PerfBucket>("all");
  const [freshness, setFreshness] = useState<Freshness>("3m");
  const [minPct, setMinPct] = useState("");
  const [maxPct, setMaxPct] = useState("");
  const [neverAbove, setNeverAbove] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("listing_date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [chart, setChart] = useState<ChartTarget | null>(null);

  const years = useMemo(
    () => Array.from(new Set(items.map((i) => i.listing_date.slice(0, 4)))).sort().reverse(),
    [items],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    const lo = minPct === "" ? null : Number(minPct);
    const hi = maxPct === "" ? null : Number(maxPct);
    const out = items.filter((item) => {
      if (q && !item.symbol.toUpperCase().includes(q)) return false;
      if (year !== "all" && !item.listing_date.startsWith(year)) return false;
      if (freshness !== "all") {
        const listed = new Date(`${item.listing_date}T00:00:00`).getTime();
        const cutoff = Date.now() - FRESHNESS_DAYS[freshness] * 86_400_000;
        if (!(listed >= cutoff)) return false;
      }
      const pct = item.pct_vs_listing;
      if (bucket === "gainers" && !(pct != null && pct > 0)) return false;
      if (bucket === "losers" && !(pct != null && pct < 0)) return false;
      if (bucket === "flat" && !(pct != null && Math.abs(pct) < 1)) return false;
      if (lo != null && (pct == null || pct < lo)) return false;
      if (hi != null && (pct == null || pct > hi)) return false;
      if (neverAbove) {
        const lp = item.listing_price;
        const hiSince = item.high_since_listing;
        if (lp == null || hiSince == null || hiSince > lp) return false;
      }
      return true;
    });
    const dir = sortDir === "asc" ? 1 : -1;
    const pctOf = (item: (typeof items)[number], key: SortKey): number | null => {
      if (key === "high_since_listing" || key === "low_since_listing") {
        const lp = item.listing_price;
        const v = item[key];
        return lp && v != null ? ((v - lp) / lp) * 100 : null;
      }
      return null;
    };
    out.sort((a, b) => {
      const pa = pctOf(a, sortKey);
      const pb = pctOf(b, sortKey);
      if (pa != null || pb != null) {
        return ((pa ?? -Infinity) - (pb ?? -Infinity)) * dir;
      }
      const va = a[sortKey];
      const vb = b[sortKey];
      if (typeof va === "string" || typeof vb === "string") {
        return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
      }
      const na = va == null ? -Infinity : Number(va);
      const nb = vb == null ? -Infinity : Number(vb);
      return (na - nb) * dir;
    });
    return out;
  }, [items, query, year, bucket, freshness, minPct, maxPct, neverAbove, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "symbol" || key === "listing_date" ? "asc" : "desc");
    }
  };

  const loadPerformance = useCallback(async (silent = false): Promise<void> => {
    if (!silent) setLoading(true);
    try {
      const response = await fetch(`${API}/api/market-data/ipo/performance`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      setItems(payload.items ?? []);
      setMessage(`${payload.count ?? 0} tracked IPO(s) · read-only from data/market_data.db`);
    } catch (error) {
      setMessage(`Could not load IPO performance: ${error instanceof Error ? error.message : error}`);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  const loadStatus = useCallback(async (): Promise<void> => {
    try {
      const response = await fetch(`${API}/api/ipo-scan`, { cache: "no-store" });
      if (!response.ok) return;
      setStatus(await response.json());
    } catch {
      // best-effort
    }
  }, []);

  useEffect(() => {
    void loadPerformance(true);
    void loadStatus();
  }, [loadPerformance, loadStatus]);

  const refreshAll = () => {
    void loadPerformance();
    void loadStatus();
  };

  const runScanner = async (
    action: "start" | "stop" | "run-once"
  ): Promise<void> => {
    setScanning(true);
    try {
      const response = await fetch(`${API}/api/ipo-scan/${action}`, {
        method: "POST",
        cache: "no-store",
        ...(action === "start"
          ? { body: JSON.stringify({ lookback_days: 7 }), headers: { "Content-Type": "application/json" } }
          : {}),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setStatus(await response.json());
      await loadPerformance(true);
    } catch (error) {
      setMessage(`Scanner ${action} failed: ${error instanceof Error ? error.message : error}`);
    } finally {
      setScanning(false);
    }
  };

  const runLiquidityScreen = async (autoRemove: boolean): Promise<void> => {
    setScreening(true);
    setScreenResults(null);
    try {
      const response = await fetch(`${API}/api/ipo-liquidity/screen`, {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lookback_days: 60, auto_remove: autoRemove }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      setScreenResults(payload.results ?? []);
      const removedCount = (payload.results ?? []).filter((r: LiquidityScreenResult) => r.decision === "REMOVE").length;
      setMessage(`Liquidity screen complete: ${payload.count} symbols, ${removedCount} removed${autoRemove ? " (auto-removed)" : " (dry-run)"}`);
      await loadPerformance(true);
    } catch (error) {
      setMessage(`Liquidity screen failed: ${error instanceof Error ? error.message : error}`);
    } finally {
      setScreening(false);
    }
  };

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>IPO tracker · since listing</h1>
        </div>
        <div className="top-actions">
          <button className="scan-now" type="button" onClick={refreshAll} disabled={loading} title="Refresh IPO performance from the local database">
            <RefreshCw size={14} className={loading ? "spin" : undefined} />
            {loading ? "Loading…" : "Refresh"}
          </button>
          <div className={`status${statusFlash ? " status-flash" : ""}`}><span className="pulse" />{message}</div>
          <a className="top-link" href="/watchlist"><Database size={12} /> Database</a>
          <a className="top-link" href="/"><ArrowLeft size={12} /> Scanner</a>
        </div>
      </header>

      <section className="auto-scan">
        <span className="auto-title"><Rocket size={14} /> Automation</span>
        {status?.running ? (
          <button className="test-button stop" type="button" onClick={() => runScanner("stop")} disabled={scanning} title="Stop the automatic IPO detection scanner">Stop IPO scan</button>
        ) : (
          <>
            <button className="test-button" type="button" onClick={() => runScanner("start")} disabled={scanning} title="Start automatic IPO detection (runs on a schedule)">Start IPO scan</button>
            <button className="test-button" type="button" onClick={() => runScanner("run-once")} disabled={scanning} title="Run IPO detection once immediately (bhavcopy scan)">Scan now</button>
          </>
        )}
        <div className="auto-separator" />
        <span className="auto-title"><SearchX size={14} /> Liquidity Screen</span>
        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
          <button className="test-button" type="button" onClick={() => runLiquidityScreen(false)} disabled={screening || scanning} title="Screen all IPO-scope symbols for liquidity (dry-run, no removal)">
            <SearchX size={14} /> Screen (dry-run)
          </button>
          <button className="test-button stop" type="button" onClick={() => runLiquidityScreen(true)} disabled={screening || scanning} title="Screen and auto-remove illiquid IPOs">
            <SearchX size={14} /> Screen & Auto-Remove
          </button>
          {screening && <span className="pulse" style={{ marginLeft: 8 }} />}
        </div>
        <small className="auto-meta">
          {status?.running ? `RUNNING · every ${status.interval_minutes ?? 60} min` : "IPO detection idle"}
          {status ? ` · lookback ${status.lookback_days ?? 7}d` : ""}
          {status?.last_ran_at ? ` · last ${new Date(status.last_ran_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
          {status?.last_error ? ` · ${status.last_error}` : ""}
        </small>
      </section>

      <section className="panel filter-bar">
        <input
          className="filter-input"
          type="search"
          placeholder="Search symbol..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="filter-input"
          value={year}
          onChange={(e) => setYear(e.target.value)}
        >
          <option value="all">All listing years</option>
          {years.map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
        <select
          className="filter-input"
          value={bucket}
          onChange={(e) => setBucket(e.target.value as PerfBucket)}
        >
          <option value="all">All performance</option>
          <option value="gainers">Gainers (&gt; 0%)</option>
          <option value="losers">Losers (&lt; 0%)</option>
          <option value="flat">Flat (within 1%)</option>
        </select>
        <select
          className="filter-input"
          value={freshness}
          onChange={(e) => setFreshness(e.target.value as Freshness)}
        >
          <option value="all">All ages</option>
          <option value="15d">Fresh: last 15 days</option>
          <option value="1m">Last 1 month</option>
          <option value="3m">Last 3 months</option>
        </select>
        <label className="filter-label">
          Min %
          <input
            className="filter-input filter-num"
            type="number"
            placeholder="-"
            value={minPct}
            onChange={(e) => setMinPct(e.target.value)}
          />
        </label>
        <label className="filter-label">
          Max %
          <input
            className="filter-input filter-num"
            type="number"
            placeholder="-"
            value={maxPct}
            onChange={(e) => setMaxPct(e.target.value)}
          />
        </label>
        <label className="filter-label filter-check" title="Since-listing high never went above the listing price">
          <input
            type="checkbox"
            checked={neverAbove}
            onChange={(e) => setNeverAbove(e.target.checked)}
          />
          Never above listing
        </label>
        {(query || year !== "all" || bucket !== "all" || freshness !== "all" || minPct || maxPct || neverAbove) && (
          <button
            className="test-button"
            type="button"
            onClick={() => {
              setQuery("");
              setYear("all");
              setBucket("all");
              setFreshness("all");
              setMinPct("");
              setMaxPct("");
              setNeverAbove(false);
            }}
            title="Clear all active filters"
          >
            Clear filters
          </button>
        )}
      </section>

      {screenResults && (
        <section className="panel strategy-panel">
          <div className="panel-heading">
            <span>Liquidity screen results</span>
            <div className="panel-heading-actions">
              <small>{screenResults.length} symbols screened</small>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Tier</th>
                  <th>Decision</th>
                  <th>Flags</th>
                  <th>Reason</th>
                  <th>Removed</th>
                </tr>
              </thead>
              <tbody>
                {screenResults.map((item) => {
                  const removedInfo = item.removed ? (
                    <span className="badge bias-bear">
                      OHLC: {item.removed.ohlc_daily}, IPO: {item.removed.ipo_metadata}, WL: {item.removed.watchlist ? "yes" : "no"}
                    </span>
                  ) : "—";
                  return (
                    <tr key={item.symbol} style={{ backgroundColor: item.decision === "REMOVE" ? "#3a2a2a" : item.decision === "WATCH" ? "#3a3a2a" : item.decision === "KEEP" ? "#2a3a2a" : "transparent" }}>
                      <td><strong>{item.symbol}</strong></td>
                      <td><span className={`badge ${item.liquidity_tier === "LIQUID" ? "bias-bull" : item.liquidity_tier === "BORDERLINE" ? "bias-neutral" : item.liquidity_tier === "ILLIQUID" ? "bias-bear" : ""}`}>{item.liquidity_tier}</span></td>
                      <td><span className={`badge ${item.decision === "KEEP" ? "bias-bull" : item.decision === "WATCH" ? "bias-neutral" : item.decision === "REMOVE" ? "bias-bear" : "bias-bull"}`}>{item.decision}</span></td>
                      <td>{item.flags.join(", ") || "—"}</td>
                      <td className="muted" style={{ maxWidth: 400, whiteSpace: "normal" }}>{item.reason}</td>
                      <td>{removedInfo}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel strategy-panel">
        <div className="panel-heading">
          <span>IPO performance · listing vs today & since-listing range</span>
          <div className="panel-heading-actions">
            <small>{filtered.length} of {items.length} shown</small>
          </div>
        </div>
        <div className="table-wrap">
          {filtered.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th className="sortable" onClick={() => toggleSort("symbol")}>Symbol{sortKey === "symbol" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("listing_date")}>Listed{sortKey === "listing_date" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("listing_price")}>Listing price{sortKey === "listing_price" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("current_price")}>Today{sortKey === "current_price" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("high_since_listing")}>High since{sortKey === "high_since_listing" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("low_since_listing")}>Low since{sortKey === "low_since_listing" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                  <th className="sortable" onClick={() => toggleSort("pct_vs_listing")}>vs listing{sortKey === "pct_vs_listing" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => {
                  const lp = item.listing_price;
                  const hiPct = lp && item.high_since_listing != null
                    ? ((item.high_since_listing - lp) / lp) * 100 : null;
                  const loPct = lp && item.low_since_listing != null
                    ? ((item.low_since_listing - lp) / lp) * 100 : null;
                  return (
                  <tr key={item.symbol}>
                    <td>
                      <a
                        href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(item.symbol)}`}
                        className="chart-link symbol-link"
                        title={`Open ${item.symbol} TradingView chart`}
                        onClick={(event) => {
                          if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                          event.preventDefault();
                          setChart({ symbol: item.symbol, sourceLink: null });
                        }}
                      >
                        <strong>{item.symbol}</strong>
                      </a>
                    </td>
                    <td>{item.listing_date}</td>
                    <td className="number">{number(item.listing_price)}</td>
                    <td className="number">{number(item.current_price)}</td>
                    <td className="number">
                      {number(item.high_since_listing)}
                      {hiPct != null ? <small className="muted"> ({hiPct > 0 ? "+" : ""}{hiPct.toFixed(1)}%)</small> : null}
                    </td>
                    <td className="number">
                      {number(item.low_since_listing)}
                      {loPct != null ? <small className="muted"> ({loPct > 0 ? "+" : ""}{loPct.toFixed(1)}%)</small> : null}
                    </td>
                    <td>
                      <span className={`badge ${pctClass(item.pct_vs_listing)}`}>
                        {item.pct_vs_listing == null ? "—" : `${item.pct_vs_listing > 0 ? "+" : ""}${item.pct_vs_listing}%`}
                      </span>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="empty">
              <SearchX size={20} />
              No IPOs tracked yet.
              <br />
              Use “Scan now” to detect newly-listed NSE stocks (bhavcopy), or backfill to pull their history.
            </div>
          )}
        </div>
      </section>

      <TradingViewChartModal
        key={chart?.symbol ?? "none"}
        chart={chart}
        onClose={() => setChart(null)}
      />

      <footer>Read-only view of IPO metadata + daily OHLC in data/market_data.db. Backfill is a separate long-running action exposed by the API.</footer>
    </main>
  );
}