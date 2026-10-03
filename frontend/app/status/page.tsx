"use client";

// Status (admin only): data freshness per source and watchlist symbol,
// automation / push health and the cached calendars (GET /api/health/details).
// Read-only: nothing here fetches market data or changes state.

import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import Navigation from "../../components/Navigation";
import PageGate from "../../components/PageGate";
import { apiFetch } from "../../components/auth";
import { formatDateTime, formatTradingDate, useDisplayTimezone, zoneLabel } from "../../components/time";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Status = "empty" | "stale" | "gaps" | "ok";
type SymbolRow = {
  symbol: string; source: string; market: string; scope: string;
  first_date: string | null; last_date: string | null; expected_last: string | null;
  behind: number; missing: number; missing_sample: string[]; no_data_days: number; status: Status;
};
type SourceRow = { source: string; rows: number; symbols: number; latest_date: string | null; no_data_markers: number; invalid_rows: number };
type CacheInfo = { fetched_at?: string | null; error?: string | null; retry_at?: string | null; source?: string | null; stale?: boolean; events?: number };
type Health = {
  checked_at: string;
  database: { backend: string; size_mb: number | null };
  sources: SourceRow[];
  freshness: { window_start: string; window_days: number; expected_last: Record<string, string | null>; counts: Record<Status, number>; symbols: SymbolRow[] };
  automation: Record<string, Record<string, unknown>>;
  caches: Record<string, CacheInfo>;
};

const STATUS_LABEL: Record<Status, string> = { empty: "No data", stale: "Stale", gaps: "Gaps", ok: "OK" };
const STATUS_HINT: Record<Status, string> = {
  empty: "No stored bars at all",
  stale: "Latest final session(s) not stored yet",
  gaps: "Expected sessions missing inside the window (no bar, no no-data marker)",
  ok: "Up to date, no gaps",
};
const AUTOMATION_LABEL: Record<string, string> = {
  silver_bullet: "Silver Bullet (NY AM)",
  ipo_scanner: "IPO scanner",
  data_auto_sync: "Data auto-sync",
  ltf_confirmation: "LTF confirmation",
  price_alerts: "Price alerts",
};
const CACHE_LABEL: Record<string, string> = {
  nse_holidays: "NSE holidays",
  nse_events: "NSE events (results / ex-dates)",
  news_calendar: "Economic calendar",
};

const text = (value: unknown): string => (value == null || value === "" ? "" : String(value));

function StatusPageContent() {
  const tz = useDisplayTimezone();
  const [data, setData] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Status | "problems" | "all">("problems");
  const [refetching, setRefetching] = useState<string | null>(null);
  const [refetchNote, setRefetchNote] = useState<string | null>(null);

  // Delete the source's stored bad bars and fetch those dates again (admin; may hit NSE/TradingView).
  const refetchBadBars = (source: string) => {
    setRefetching(source);
    setRefetchNote(null);
    apiFetch(`${API}/api/health/refetch-bad-bars?source=${encodeURIComponent(source)}`, { method: "POST" })
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`);
        const fixed = payload.fixed?.length ?? 0;
        const missing: string[] = payload.still_missing ?? [];
        setRefetchNote(
          `${source}: ${fixed} of ${payload.checked} bad bar(s) replaced`
          + (missing.length ? ` · still missing (gap): ${missing.slice(0, 5).join(", ")}${missing.length > 5 ? " …" : ""}` : "")
          + (payload.errors?.length ? ` · ${payload.errors[0]}` : "")
          + (payload.more ? " · more left, click again" : ""),
        );
        load();
      })
      .catch((exc: Error) => setRefetchNote(`${source}: ${exc.message || "re-fetch failed"}`))
      .finally(() => setRefetching(null));
  };

  const load = () => {
    setLoading(true);
    apiFetch(`${API}/api/health/details`)
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`);
        setData(payload as Health);
        setError(null);
      })
      .catch((exc: Error) => setError(exc.message || "API unreachable"))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const when = (value: unknown) => {
    const raw = text(value);
    return raw ? `${formatDateTime(raw, tz)} ${zoneLabel(tz, raw)}` : "—";
  };
  const rows = useMemo(() => {
    const all = data?.freshness.symbols ?? [];
    if (filter === "all") return all;
    if (filter === "problems") return all.filter((row) => row.status !== "ok");
    return all.filter((row) => row.status === filter);
  }, [data, filter]);

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Status</h1>
        </div>
        <div className="top-actions">
          <Navigation active="/status" />
        </div>
      </header>

      <section className="panel status-panel">
        <div className="panel-heading">
          <span>
            Data health {data ? <small className="muted">· checked {when(data.checked_at)} · DB {data.database.backend}{data.database.size_mb != null ? ` ${data.database.size_mb} MB` : ""}</small> : null}
          </span>
          <button type="button" className="test-button" onClick={load} disabled={loading} title="Re-check (reads the database only; fetches nothing)">
            <RefreshCw size={12} /> {loading ? "Checking…" : "Refresh"}
          </button>
        </div>
        {error && <p className="status-error">{error}</p>}
        {data && (
          <div className="status-grid">
            <div>
              <h3>Sources</h3>
              <table className="status-table">
                <thead><tr><th>Source</th><th>Rows</th><th>Symbols</th><th>Latest bar</th><th>No-data markers</th><th title="Stored bars failing the integrity check (high < low, open/close outside the range, zero/missing price). Never used by strategies.">Bad bars</th></tr></thead>
                <tbody>
                  {data.sources.map((row) => (
                    <tr key={row.source}>
                      <td>{row.source}</td><td>{row.rows.toLocaleString("en-IN")}</td><td>{row.symbols}</td>
                      <td>{row.latest_date ? formatTradingDate(row.latest_date) : "—"}</td><td>{row.no_data_markers}</td>
                      <td className={row.invalid_rows ? "status-bad" : undefined}>
                        {row.invalid_rows}{" "}
                        <button
                          type="button"
                          className="status-refetch"
                          onClick={() => refetchBadBars(row.source)}
                          disabled={!row.invalid_rows || refetching !== null}
                          title={row.invalid_rows ? `Delete these ${row.invalid_rows} bad bar(s) and fetch the dates again from ${row.source}` : "No bad bars to re-fetch"}
                        >
                          <RefreshCw size={10} className={refetching === row.source ? "spin" : undefined} /> {refetching === row.source ? "Fetching…" : "Re-fetch"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {refetchNote && <p className="status-note">{refetchNote}</p>}
              <p className="muted status-note">
                Latest final session per market (each market&apos;s own cut-off):{" "}
                {Object.entries(data.freshness.expected_last).map(([market, day]) => `${market} ${day ? formatTradingDate(day) : "—"}`).join(" · ")}
              </p>
            </div>
            <div>
              <h3>Automations</h3>
              <table className="status-table">
                <thead><tr><th>Job</th><th>State</th><th>Last run</th><th>Last error</th></tr></thead>
                <tbody>
                  {Object.entries(data.automation).map(([key, item]) => {
                    const running = Boolean(item.running ?? item.auto_armed);
                    const errorText = [text(item.last_error), item.last_push_error ? `push: ${text(item.last_push_error)}` : ""].filter(Boolean).join(" · ");
                    return (
                      <tr key={key}>
                        <td>{AUTOMATION_LABEL[key] ?? key}</td>
                        <td><span className={`status-pill ${running ? "ok" : "idle"}`}>{running ? (item.syncing ? "syncing" : "running") : "off"}</span></td>
                        <td>{when(item.last_check_at ?? item.last_run_at)}</td>
                        <td className={errorText ? "status-bad" : "muted"} title={errorText}>{errorText || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <h3>Calendars</h3>
              <table className="status-table">
                <thead><tr><th>Cache</th><th>Fetched</th><th>Items</th><th>Error / retry</th></tr></thead>
                <tbody>
                  {Object.entries(data.caches).map(([key, item]) => (
                    <tr key={key}>
                      <td>{CACHE_LABEL[key] ?? key}{item.source ? <small className="muted"> · {item.source}</small> : null}</td>
                      <td>{when(item.fetched_at)}</td>
                      <td>{item.events ?? "—"}</td>
                      <td className={item.error ? "status-bad" : "muted"} title={text(item.error)}>
                        {item.error ? `${item.error}${item.retry_at ? ` · retry ${when(item.retry_at)}` : ""}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>

      {data && (
        <section className="panel status-panel">
          <div className="panel-heading">
            <span>Watchlist freshness <small className="muted">· last {data.freshness.window_days} days from {formatTradingDate(data.freshness.window_start)}</small></span>
            <div className="panel-heading-actions">
              {(["problems", "empty", "stale", "gaps", "ok", "all"] as const).map((key) => (
                <button
                  key={key}
                  type="button"
                  className={`section-nav-item${filter === key ? " active" : ""}`}
                  onClick={() => setFilter(key)}
                  title={key === "problems" ? "Everything not OK" : key === "all" ? "All watchlist symbols" : STATUS_HINT[key]}
                >
                  {key === "problems" ? "Problems" : key === "all" ? "All" : STATUS_LABEL[key]}{" "}
                  {key === "problems"
                    ? data.freshness.counts.empty + data.freshness.counts.stale + data.freshness.counts.gaps
                    : key === "all" ? data.freshness.symbols.length : data.freshness.counts[key]}
                </button>
              ))}
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Symbol</th><th>Market</th><th>Scope</th><th>Last bar</th><th>Expected</th><th>Behind</th><th>Missing</th><th>No-data days</th><th>Status</th></tr></thead>
              <tbody>
                {rows.length === 0 && <tr><td colSpan={9} className="muted">Nothing to show.</td></tr>}
                {rows.map((row) => (
                  <tr key={row.symbol}>
                    <td><strong>{row.symbol}</strong></td>
                    <td>{row.market}</td>
                    <td>{row.scope}</td>
                    <td>{row.last_date ? formatTradingDate(row.last_date) : "—"}</td>
                    <td>{row.expected_last ? formatTradingDate(row.expected_last) : "—"}</td>
                    <td>{row.behind || ""}</td>
                    <td title={row.missing_sample.length ? `First missing: ${row.missing_sample.map(formatTradingDate).join(", ")}` : ""}>{row.missing || ""}</td>
                    <td>{row.no_data_days || ""}</td>
                    <td><span className={`status-pill ${row.status}`} title={STATUS_HINT[row.status]}>{STATUS_LABEL[row.status]}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  );
}

export default function StatusPage() {
  return <PageGate page="settings" active="/status" title="Status"><StatusPageContent /></PageGate>;
}
