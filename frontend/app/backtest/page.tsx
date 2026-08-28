"use client";

import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, ArrowDownRight, ArrowUpRight, Database, TrendingDown, TrendingUp } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

// Template tokens (keep charts consistent with globals.css palette)
const C_BLUE = "#356c9b";
const C_CORAL = "#d65a3a";
const C_MUTED = "#71808b";
const C_TEAL = "#287b79";

type StrategyFlag = { name: string; label: string; group: string; enabled: boolean; runnable: boolean };
type WatchSymbol = { symbol: string; session: string };

type Metrics = {
  sharpe: number | null;
  sortino: number | null;
  max_drawdown_pct: number;
  win_rate: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  cagr: number | null;
  num_trades: number;
};

type TradeRow = {
  symbol: string;
  strategy: string;
  side: number;
  entry_date: string;
  entry_price: number;
  exit_date: string;
  exit_price: number;
  exit_reason: string;
  qty: number;
  pnl: number;
  pnl_pct: number;
};

type Report = {
  strategy: string;
  config: Record<string, unknown>;
  equity_curve: { date: string; equity: number; drawdown_pct: number; exposure: number }[];
  trades: TradeRow[];
  metrics: Metrics;
  benchmark_curve: { date: string; equity: number }[] | null;
  warnings: string[];
};

export default function BacktestPage() {
  const [catalog, setCatalog] = useState<StrategyFlag[]>([]);
  const [watchlist, setWatchlist] = useState<WatchSymbol[]>([]);
  const [customSymbols, setCustomSymbols] = useState<string[]>([]);
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [symbolQuery, setSymbolQuery] = useState("");
  const [newSymbol, setNewSymbol] = useState("");

  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [initialCapital, setInitialCapital] = useState<number>(100000);
  const [holdDays, setHoldDays] = useState<number>(5);
  const [benchmark, setBenchmark] = useState<string>("");

  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, Report> | null>(null);

  useEffect(() => {
    fetch(`${API}/api/strategies`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setCatalog(d.strategies ?? []))
      .catch(() => {});
    fetch(`${API}/api/watchlist`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setWatchlist(d.symbols ?? []))
      .catch(() => {});
  }, []);

  const symbolOptions = useMemo(
    () => Array.from(new Set([...watchlist.map((w) => w.symbol), ...customSymbols])).sort(),
    [watchlist, customSymbols]
  );
  const visibleSymbols = useMemo(
    () => symbolOptions.filter((s) => s.toLowerCase().includes(symbolQuery.toLowerCase())),
    [symbolOptions, symbolQuery]
  );

  const groupedStrategies = useMemo(() => {
    const groups: Record<string, StrategyFlag[]> = {};
    for (const s of catalog) (groups[s.group] ??= []).push(s);
    return groups;
  }, [catalog]);

  const toggleStrategy = (name: string) =>
    setSelectedStrategies((prev) => (prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]));
  const setGroupStrategies = (names: string[], on: boolean) =>
    setSelectedStrategies((prev) =>
      on ? Array.from(new Set([...prev, ...names])) : prev.filter((n) => !names.includes(n))
    );
  const toggleSymbol = (sym: string) =>
    setSelectedSymbols((prev) => (prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym]));

  const addCustomSymbol = () => {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym) return;
    setCustomSymbols((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
    setSelectedSymbols((prev) => (prev.includes(sym) ? prev : [...prev, sym]));
    setNewSymbol("");
  };

  const runBacktest = async () => {
    setError(null);
    if (!selectedStrategies.length || !selectedSymbols.length || !startDate || !endDate) {
      setError("Select at least one strategy, one symbol, and a date range.");
      return;
    }
    setRunning(true);
    try {
      const resp = await fetch(`${API}/api/backtest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: selectedSymbols,
          strategies: selectedStrategies,
          start_date: startDate,
          end_date: endDate,
          initial_capital: initialCapital,
          hold_days: holdDays,
          benchmark_symbol: benchmark || null,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) setError(data.detail ?? "Backtest failed.");
      else setReports(data.reports);
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };

  const fmt = (v: number | null, pct = false, dp = 2) =>
    v === null || v === undefined ? "—" : pct ? `${(v * 100).toFixed(dp)}%` : v.toFixed(dp);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Backtesting Engine</p>
          <h1>Strategy Replay</h1>
        </div>
        <div className="top-actions">
          <a className="top-link" href="/"><Activity size={12} /> Scanner</a>
          <a className="top-link" href="/watchlist"><Database size={12} /> Database</a>
        </div>
      </header>

      <div className="workspace">
        {/* ---------------- Controls ---------------- */}
        <aside className="controls panel">
          <div className="panel-heading"><span>Configuration</span></div>

          <div className="watch-filter" style={{ marginTop: 12 }}>
            <label className="filter-label">Start date</label>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            <label className="filter-label">End date</label>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            <label className="filter-label">Initial capital</label>
            <input type="number" value={initialCapital} onChange={(e) => setInitialCapital(Number(e.target.value))} />
            <label className="filter-label">Hold days (core strategies)</label>
            <input type="number" value={holdDays} onChange={(e) => setHoldDays(Number(e.target.value))} />
            <label className="filter-label">Benchmark symbol (optional)</label>
            <input type="text" placeholder="NSE:NIFTY" value={benchmark} onChange={(e) => setBenchmark(e.target.value)} />
          </div>

          <div style={{ marginTop: 14 }}>
            <div className="panel-heading">
              <span>Strategies</span>
              {catalog.length > 0 && (() => {
                const allOn = selectedStrategies.length === catalog.length;
                const partial = selectedStrategies.length > 0 && !allOn;
                return (
                  <button className="toggle-text" title={allOn ? "Clear all strategies" : "Select all strategies"} onClick={() => setSelectedStrategies(allOn ? [] : catalog.map((s) => s.name))}>
                    {allOn ? "Clear" : "Select all"}
                    {partial && <span className="day-marker" style={{ marginLeft: 4 }}>…</span>}
                  </button>
                );
              })()}
            </div>
            {Object.entries(groupedStrategies).map(([group, items]) => {
              const names = items.map((s) => s.name);
              const selectedCount = names.filter((n) => selectedStrategies.includes(n)).length;
              const allOn = selectedCount === names.length;
              const partial = selectedCount > 0 && !allOn;
              return (
                <div key={group} style={{ marginTop: 10 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <span className="filter-label" style={{ margin: 0 }}>{group}</span>
                    <button
                      className="toggle-text"
                      title={allOn ? `Clear ${group}` : `Select all ${group}`}
                      onClick={() => setGroupStrategies(names, !allOn)}
                    >
                      {allOn ? "Clear" : "Select all"}
                      {partial && <span className="day-marker" style={{ marginLeft: 4 }}>…</span>}
                    </button>
                  </div>
                  <div className="filters">
                    {items.map((s) => (
                      <button
                        key={s.name}
                        className={selectedStrategies.includes(s.name) ? "active" : ""}
                        disabled={!s.runnable}
                        onClick={() => toggleStrategy(s.name)}
                        title={s.label}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>

          <div style={{ marginTop: 16 }}>
            <div className="panel-heading">
              <span>Symbols <small>{selectedSymbols.length}/{symbolOptions.length}</small></span>
              <div className="panel-heading-actions">
                <button className="toggle-text" title="Select visible" onClick={() => setSelectedSymbols((p) => Array.from(new Set([...p, ...visibleSymbols])))}>Sel</button>
                <button className="toggle-text" title="Clear" onClick={() => setSelectedSymbols([])}>∅</button>
              </div>
            </div>

            <input
              className="manage-search"
              style={{ marginTop: 10 }}
              placeholder="Search symbols…"
              value={symbolQuery}
              onChange={(e) => setSymbolQuery(e.target.value)}
            />
            <div className="add-watchlist" style={{ gridTemplateColumns: "1fr auto" }}>
              <input
                placeholder="Add symbol, e.g. NSE:INFY"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addCustomSymbol()}
              />
              <button type="button" onClick={addCustomSymbol}>Add</button>
            </div>

            <div className="check-list">
              {visibleSymbols.map((sym) => (
                <label key={sym} className="check-row">
                  <input type="checkbox" checked={selectedSymbols.includes(sym)} onChange={() => toggleSymbol(sym)} />
                  <span>{sym}</span>
                </label>
              ))}
              {visibleSymbols.length === 0 && <div className="small-empty">No symbols match.</div>}
            </div>
          </div>

          <button className="scan-now" style={{ marginTop: 14, width: "100%", justifyContent: "center" }} onClick={runBacktest} disabled={running}>
            {running ? "Running…" : "Run Backtest"}
          </button>
          {error && <p className="sync-note" style={{ marginTop: 8 }}><strong>Error:</strong> {error}</p>}
        </aside>

        {/* ---------------- Results ---------------- */}
        <main className="main-content">
          {!reports && (
            <div className="empty">
              <Activity size={28} />
              <span>Pick strategies + symbols and a date range, then Run Backtest.</span>
            </div>
          )}
          {reports && Object.entries(reports).map(([key, rep]) => (
            <ReportView key={key} label={key} report={rep} fmt={fmt} />
          ))}
        </main>
      </div>
    </div>
  );
}

function ReportView({ label, report, fmt }: { label: string; report: Report; fmt: (v: number | null, pct?: boolean, dp?: number) => string }) {
  const m = report.metrics;
  const hasBench = !!report.benchmark_curve;
  const combo = report.equity_curve.map((e, i) => ({
    date: e.date,
    strategy: e.equity,
    benchmark: hasBench ? report.benchmark_curve?.[i]?.equity : undefined,
  }));
  const drawdown = report.equity_curve.map((e) => ({ date: e.date, dd: -(e.drawdown_pct * 100) }));

  return (
    <div className="panel" style={{ padding: 16, marginBottom: 16 }}>
      <div className="table-head">
        <h3 style={{ textTransform: "capitalize" }}>{label.replace(/_/g, " ")}</h3>
        <span className="badge">{report.trades.length} trades</span>
      </div>
      {report.warnings?.length > 0 && <div className="date-note">{report.warnings.join(" ")}</div>}

      <div className="metrics">
        <Metric icon={<TrendingUp size={16} />} label="Sharpe" value={fmt(m.sharpe)} />
        <Metric icon={<TrendingDown size={16} />} label="Max Drawdown" value={fmt(m.max_drawdown_pct, true)} tone="bear" />
        <Metric label="Win Rate" value={fmt(m.win_rate, true)} />
        <Metric label="Profit Factor" value={fmt(m.profit_factor)} />
        <Metric label="CAGR" value={fmt(m.cagr, true)} />
        <Metric label="Trades" value={String(m.num_trades)} />
        <Metric label="Expectancy" value={fmt(m.expectancy)} />
      </div>

      <h3 style={{ fontSize: 14, margin: "8px 0" }}>Equity Curve</h3>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={combo}>
          <CartesianGrid stroke="#edf0ef" />
          <XAxis dataKey="date" tick={{ fontSize: 10, fill: C_MUTED }} minTickGap={40} />
          <YAxis tick={{ fontSize: 10, fill: C_MUTED }} />
          <Tooltip />
          <Legend />
          <Line type="monotone" dataKey="strategy" stroke={C_BLUE} dot={false} name="Strategy" />
          {hasBench && <Line type="monotone" dataKey="benchmark" stroke={C_MUTED} dot={false} name="Benchmark" strokeDasharray="4 4" />}
        </LineChart>
      </ResponsiveContainer>

      <h3 style={{ fontSize: 14, margin: "12px 0 0" }}>Drawdown</h3>
      <ResponsiveContainer width="100%" height={150}>
        <AreaChart data={drawdown}>
          <CartesianGrid stroke="#edf0ef" />
          <XAxis dataKey="date" tick={{ fontSize: 10, fill: C_MUTED }} minTickGap={40} />
          <YAxis tick={{ fontSize: 10, fill: C_MUTED }} />
          <Tooltip />
          <Area type="monotone" dataKey="dd" stroke={C_CORAL} fill="rgba(214,90,58,0.18)" name="Drawdown %" />
        </AreaChart>
      </ResponsiveContainer>

      <h3 style={{ fontSize: 14, margin: "12px 0 0" }}>Trades</h3>
      <div className="table-wrap" style={{ maxHeight: 280 }}>
        <table>
          <thead>
            <tr>
              <th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>P&amp;L</th><th>%</th>
            </tr>
          </thead>
          <tbody>
            {report.trades.map((t, i) => (
              <tr key={i}>
                <td>{t.symbol}</td>
                <td>{t.side > 0 ? "L" : "S"}</td>
                <td>{t.entry_date}</td>
                <td>{t.exit_date}</td>
                <td>{t.exit_reason}</td>
                <td style={{ color: t.pnl >= 0 ? C_TEAL : C_CORAL, fontFamily: "'DM Mono', monospace" }}>{t.pnl.toFixed(2)}</td>
                <td style={{ color: t.pnl >= 0 ? C_TEAL : C_CORAL, fontFamily: "'DM Mono', monospace" }}>{(t.pnl_pct * 100).toFixed(2)}%</td>
              </tr>
            ))}
            {report.trades.length === 0 && (
              <tr><td colSpan={7} className="small-empty">No trades generated for this configuration.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Metric({ icon, label, value, tone }: { icon?: ReactNode; label: string; value: string; tone?: "bull" | "bear" }) {
  return (
    <div className={`metric ${tone === "bear" ? "bearish" : tone === "bull" ? "bullish" : "confirmed"}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {icon}
    </div>
  );
}
