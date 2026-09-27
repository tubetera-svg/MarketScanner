"use client";

import TradingViewChartModal, { type ChartTarget } from "../../components/TradingViewChartModal";
import { useStatusFlash } from "../../components/useStatusFlash";
import Navigation from "../../components/Navigation";

// IPO tracker: reads NSE IPO metadata + live performance from the local API.
// Data loads from the local SQLite store on mount/refresh.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Play, RefreshCw, Rocket, SearchX, ScanLine, Square, SlidersHorizontal, Wrench } from "lucide-react";

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
  avg_value_cr_60d: number | null;
  liquidity: "LIQUID" | "BORDERLINE" | "ILLIQUID" | "N/A";
  ret_5d: number | null;
  ret_20d: number | null;
  pct_from_high: number | null;
  strength_score: number | null;
  signal: "LEADER" | "IMPROVING" | "WEAK" | "NEW";
  breakout_20d: boolean;
  pullback_20dma: boolean;
};

type ReviewItem = PerformanceItem & { verdict: "KEEP" | "DISCARD"; reasons: string[] };
type Signal = PerformanceItem["signal"];

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
  | "pct_vs_listing"
  | "avg_value_cr_60d"
  | "ret_20d"
  | "pct_from_high"
  | "strength_score";

type SignalFilter = "all" | "leaders" | "entry" | "breakout" | "pullback";

const PRESETS: { key: SignalFilter; label: string; hint: string }[] = [
  { key: "all", label: "All liquid", hint: "Every tracked IPO passing the liquidity filter" },
  { key: "leaders", label: "Leaders", hint: "Trend score 5-6 of 6" },
  { key: "entry", label: "Entry cues", hint: "Breakout or pullback setups" },
  { key: "breakout", label: "Breakouts", hint: "Close above the prior 20 bars' high" },
  { key: "pullback", label: "Pullbacks", hint: "Trend intact, within 3% above the 20-DMA" },
];

const STORAGE_KEY = "ipo-page-filters-v1";

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
  const [freshness, setFreshness] = useState<Freshness>("all");
  const [liquidOnly, setLiquidOnly] = useState(true);
  const [signalFilter, setSignalFilter] = useState<SignalFilter>("all");
  const [hydrated, setHydrated] = useState(false);
  const [review, setReview] = useState<{ discard: ReviewItem[]; keep_count: number } | null>(null);
  const [minPct, setMinPct] = useState("");
  const [maxPct, setMaxPct] = useState("");
  const [neverAbove, setNeverAbove] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("strength_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [chart, setChart] = useState<ChartTarget | null>(null);

  // Remember filters between visits (best-effort; storage may be unavailable).
  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
      if (saved) {
        if (typeof saved.liquidOnly === "boolean") setLiquidOnly(saved.liquidOnly);
        if (PRESETS.some((p) => p.key === saved.signalFilter)) setSignalFilter(saved.signalFilter);
        if (typeof saved.sortKey === "string") setSortKey(saved.sortKey);
        if (saved.sortDir === "asc" || saved.sortDir === "desc") setSortDir(saved.sortDir);
      }
    } catch {
      // ignore
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ liquidOnly, signalFilter, sortKey, sortDir }));
    } catch {
      // ignore
    }
  }, [hydrated, liquidOnly, signalFilter, sortKey, sortDir]);

  const summary = useMemo(() => {
    const pool = liquidOnly ? items.filter((i) => i.liquidity === "LIQUID") : items;
    return {
      pool: pool.length,
      leaders: pool.filter((i) => i.signal === "LEADER").length,
      entry: pool.filter((i) => i.breakout_20d || i.pullback_20dma).length,
      breakout: pool.filter((i) => i.breakout_20d).length,
      pullback: pool.filter((i) => i.pullback_20dma).length,
    };
  }, [items, liquidOnly]);

  const advancedActive = [year !== "all", bucket !== "all", freshness !== "all", minPct !== "", maxPct !== "", neverAbove].filter(Boolean).length;
  const anyFilter = query !== "" || advancedActive > 0 || signalFilter !== "all" || !liquidOnly;
  const clearFilters = () => {
    setQuery(""); setYear("all"); setBucket("all"); setFreshness("all");
    setMinPct(""); setMaxPct(""); setNeverAbove(false); setSignalFilter("all"); setLiquidOnly(true);
  };

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
      if (liquidOnly && item.liquidity !== "LIQUID") return false;
      if (signalFilter === "leaders" && item.signal !== "LEADER") return false;
      if (signalFilter === "entry" && !(item.breakout_20d || item.pullback_20dma)) return false;
      if (signalFilter === "breakout" && !item.breakout_20d) return false;
      if (signalFilter === "pullback" && !item.pullback_20dma) return false;
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
  }, [items, query, year, bucket, freshness, liquidOnly, signalFilter, minPct, maxPct, neverAbove, sortKey, sortDir]);

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

  const loadReview = async (): Promise<void> => {
    try {
      const response = await fetch(`${API}/api/market-data/ipo/review`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      setReview({ discard: payload.discard ?? [], keep_count: payload.keep_count ?? 0 });
      setMessage(`List review: keep ${payload.keep_count}, suggest discard ${payload.discard_count} (nothing deleted)`);
    } catch (error) {
      setMessage(`List review failed: ${error instanceof Error ? error.message : error}`);
    }
  };

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
    if (autoRemove && !window.confirm("Permanently delete illiquid IPOs from the watchlist, categories and price history? Run the dry-run first if unsure.")) return;
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
          <Navigation active="/ipo" />
        </div>
      </header>

      <details className="ipo-maint">
        <summary><Wrench size={13} /> Maintenance · IPO detection &amp; liquidity screen{status?.running ? " · scanner running" : ""}</summary>
        <section className="auto-scan">
        <span className="auto-title"><Rocket size={14} /> Automation</span>
        {status?.running ? (
          <button className="test-button stop" type="button" onClick={() => runScanner("stop")} disabled={scanning} title="Stop the automatic IPO detection scanner"><Square size={13} /> Stop IPO scan</button>
        ) : (
          <>
            <button className="test-button" type="button" onClick={() => runScanner("start")} disabled={scanning} title="Start automatic IPO detection (runs on a schedule)"><Play size={13} /> Start IPO scan</button>
            <button className="test-button" type="button" onClick={() => runScanner("run-once")} disabled={scanning} title="Run IPO detection once immediately (bhavcopy scan)"><ScanLine size={13} /> Scan now</button>
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
      </details>

      <section className="metrics ipo-metrics" aria-label="IPO summary">
        {([
          ["all", "Tracked" + (liquidOnly ? " (liquid)" : ""), summary.pool, ""],
          ["leaders", "Leaders", summary.leaders, "bullish"],
          ["entry", "Entry cues", summary.entry, "confirmed"],
          ["breakout", "Breakouts", summary.breakout, "confirmed"],
          ["pullback", "Pullbacks", summary.pullback, "confirmed"],
        ] as [SignalFilter, string, number, string][]).map(([key, label, value, tone]) => (
          <button key={key} type="button" className={`metric ipo-tile ${tone}${signalFilter === key ? " selected" : ""}`} onClick={() => setSignalFilter(key)} title={PRESETS.find((p) => p.key === key)?.hint}>
            <span>{label}</span>
            <strong>{value}</strong>
          </button>
        ))}
      </section>

      <section className="panel filter-bar ipo-filters">
        <input
          className="filter-input"
          type="search"
          placeholder="Search symbol..."
          aria-label="Search symbol"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="filters" role="group" aria-label="Setup preset">
          {PRESETS.map((p) => (
            <button key={p.key} type="button" className={signalFilter === p.key ? "active" : ""} onClick={() => setSignalFilter(p.key)} title={p.hint}>{p.label}</button>
          ))}
        </div>
        <label className="filter-label filter-check" title="Only IPOs whose 60-day median traded value is above the liquid threshold (Rs 1 cr/day)">
          <input type="checkbox" checked={liquidOnly} onChange={(e) => setLiquidOnly(e.target.checked)} />
          Liquid only
        </label>
        <details className="ipo-more">
          <summary><SlidersHorizontal size={13} /> More filters{advancedActive ? ` (${advancedActive})` : ""}</summary>
          <div className="ipo-more-body">
            <select className="filter-input" aria-label="Listing year" value={year} onChange={(e) => setYear(e.target.value)}>
              <option value="all">All listing years</option>
              {years.map((y) => (<option key={y} value={y}>{y}</option>))}
            </select>
            <select className="filter-input" aria-label="Performance vs listing" value={bucket} onChange={(e) => setBucket(e.target.value as PerfBucket)}>
              <option value="all">All performance</option>
              <option value="gainers">Gainers (&gt; 0%)</option>
              <option value="losers">Losers (&lt; 0%)</option>
              <option value="flat">Flat (within 1%)</option>
            </select>
            <select className="filter-input" aria-label="Listing age" value={freshness} onChange={(e) => setFreshness(e.target.value as Freshness)}>
              <option value="all">All ages</option>
              <option value="15d">Fresh: last 15 days</option>
              <option value="1m">Last 1 month</option>
              <option value="3m">Last 3 months</option>
            </select>
            <label className="filter-label">
              Min %
              <input className="filter-input filter-num" type="number" placeholder="-" value={minPct} onChange={(e) => setMinPct(e.target.value)} />
            </label>
            <label className="filter-label">
              Max %
              <input className="filter-input filter-num" type="number" placeholder="-" value={maxPct} onChange={(e) => setMaxPct(e.target.value)} />
            </label>
            <label className="filter-label filter-check" title="Since-listing high never went above the listing price">
              <input type="checkbox" checked={neverAbove} onChange={(e) => setNeverAbove(e.target.checked)} />
              Never above listing
            </label>
          </div>
        </details>
        <span className="ipo-filter-spacer" />
        {anyFilter && (
          <button className="test-button ipo-ghost" type="button" onClick={clearFilters} title="Reset all filters to defaults">Reset</button>
        )}
        <button className="test-button ipo-ghost" type="button" onClick={() => void loadReview()} title="Suggest which tracked IPOs to discard (non-liquid, stale, non-EQ). Deletes nothing.">
          <SearchX size={14} /> Review list
        </button>
      </section>

      {review && (
        <section className="panel strategy-panel">
          <div className="panel-heading">
            <span>Suggested discards · {review.discard.length} (keeping {review.keep_count})</span>
            <div className="panel-heading-actions">
              <button className="test-button" type="button" onClick={() => setReview(null)}>Close</button>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Symbol</th><th>Listed</th><th>Avg value (cr/day)</th><th>Why</th></tr></thead>
              <tbody>
                {review.discard.map((item) => (
                  <tr key={item.symbol}>
                    <td><strong>{item.symbol}</strong></td>
                    <td>{item.listing_date}</td>
                    <td className="number">{number(item.avg_value_cr_60d)}</td>
                    <td className="muted" style={{ whiteSpace: "normal" }}>{item.reasons.join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

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
                    <tr key={item.symbol} style={{ backgroundColor: item.decision === "REMOVE" ? "#fdf4f1" : item.decision === "WATCH" ? "#fef9ee" : item.decision === "KEEP" ? "#f3faf7" : "transparent" }}>
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
          <span>IPO setups · trend, entry cues &amp; since-listing range</span>
          <div className="panel-heading-actions">
            <small>{filtered.length} of {items.length} shown</small>
          </div>
        </div>
        <div className="table-wrap">
          {filtered.length > 0 ? (
            <table className="ipo-table">
              <thead>
                <tr>
                  {([
                    ["symbol", "Symbol", "Click a symbol to open its chart"],
                    ["strength_score", "Trend", "6-point score: above 20-DMA, 20>50-DMA, within 10% of high, 20d return > 0, volume rising, above listing price"],
                    [null, "Entry cue", "Breakout: close above prior 20-bar high. Pullback: trend intact, within 3% above the 20-DMA"],
                    ["ret_20d", "20d", "Return over the last 20 sessions"],
                    ["pct_from_high", "From high", "Distance from the since-listing high"],
                    ["pct_vs_listing", "vs listing", "Close vs listing-day open"],
                    ["current_price", "Price", "Latest close"],
                    ["avg_value_cr_60d", "Value cr/day", "60-day average traded value"],
                    ["listing_date", "Listed", ""],
                    ["listing_price", "Listing px", ""],
                    ["high_since_listing", "High since", ""],
                    ["low_since_listing", "Low since", ""],
                  ] as [SortKey | null, string, string][]).map(([key, label, tip]) => (
                    <th
                      key={label}
                      className={key ? "sortable" : undefined}
                      title={tip || undefined}
                      aria-sort={key && key === sortKey ? (sortDir === "asc" ? "ascending" : "descending") : undefined}
                      onClick={key ? () => toggleSort(key) : undefined}
                    >
                      {label}{key && key === sortKey ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => {
                  const lp = item.listing_price;
                  const hiPct = lp && item.high_since_listing != null
                    ? ((item.high_since_listing - lp) / lp) * 100 : null;
                  const loPct = lp && item.low_since_listing != null
                    ? ((item.low_since_listing - lp) / lp) * 100 : null;
                  const signed = (v: number | null | undefined, suffix = "%") =>
                    v == null ? "—" : `${v > 0 ? "+" : ""}${v}${suffix}`;
                  return (
                  <tr key={item.symbol} className={item.signal === "LEADER" ? "ipo-row-leader" : undefined}>
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
                        <strong>{item.symbol.replace(/^NSE:/, "")}</strong>
                      </a>
                    </td>
                    <td>
                      <span className={`badge ${item.signal === "LEADER" ? "bias-bull" : item.signal === "WEAK" ? "bias-bear" : "bias-neutral"}`}>
                        {item.signal}{item.strength_score != null ? ` ${item.strength_score}/6` : ""}
                      </span>
                    </td>
                    <td>
                      {item.breakout_20d ? <span className="badge bias-bull">Breakout</span> : null}
                      {item.pullback_20dma ? <span className="badge bias-neutral ipo-cue-pullback">Pullback</span> : null}
                      {!item.breakout_20d && !item.pullback_20dma ? <span className="muted">—</span> : null}
                    </td>
                    <td className={`number ${pctClass(item.ret_20d)}`}>{signed(item.ret_20d)}</td>
                    <td className="number">{item.pct_from_high == null ? "—" : `${item.pct_from_high}%`}</td>
                    <td className="number">
                      <span className={`badge ${pctClass(item.pct_vs_listing)}`}>{signed(item.pct_vs_listing)}</span>
                    </td>
                    <td className="number">{number(item.current_price)}</td>
                    <td className="number" title={item.liquidity}>
                      <span className={`ipo-dot ipo-dot-${item.liquidity === "N/A" ? "na" : item.liquidity.toLowerCase()}`} aria-label={item.liquidity} />
                      {number(item.avg_value_cr_60d)}
                    </td>
                    <td>{item.listing_date}</td>
                    <td className="number">{number(item.listing_price)}</td>
                    <td className="number">
                      {number(item.high_since_listing)}
                      {hiPct != null ? <small className="muted"> ({signed(Number(hiPct.toFixed(1)))})</small> : null}
                    </td>
                    <td className="number">
                      {number(item.low_since_listing)}
                      {loPct != null ? <small className="muted"> ({signed(Number(loPct.toFixed(1)))})</small> : null}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="empty">
              <SearchX size={20} />
              {items.length === 0 ? (
                <>No IPOs tracked yet.<br />Open Maintenance and use “Scan now” to detect newly-listed NSE stocks, or backfill to pull their history.</>
              ) : (
                <>No IPOs match these filters.<br /><button className="test-button" type="button" onClick={clearFilters}>Reset filters</button></>
              )}
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
