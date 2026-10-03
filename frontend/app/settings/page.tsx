"use client";

import Navigation from "../../components/Navigation";

// Settings: one place to enable/disable strategies and automations, tune
// intervals / look-back days, and show/hide strategies and pages in the UI.
// Grouped into tabs: Scanning, Data, Alerts & push, Display, or All at once.
// Automation and UI settings are persisted by PUT /api/settings; strategy
// toggles use their existing endpoint and apply immediately.

import { Fragment, useCallback, useEffect, useState } from "react";
import { previewSound, setSoundSettings, soundLabel, type AlertSoundKind, type SoundSettings } from "../../components/alertSound";
import { fetchAppSettings } from "../../components/appSettings";
import { infoSummary, renderInfoBody } from "../../components/InfoBody";
import { Info } from "lucide-react";
import { formatDateTime, formatDayDateTime, formatTradingDate, IST, marketToday, setDisplayTimezone, useDisplayTimezone, weekdayOf, zoneLabel } from "../../components/time";
import { apiFetch, refreshAuth, replaceToken, useAuth, type GuestAccess, type GuestPage } from "../../components/auth";
import PageGate from "../../components/PageGate";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Strategy = { name: string; label: string; group: string; enabled: boolean; runnable: boolean; description?: string | null };
type Automation = {
  silver_bullet_auto: { enabled: boolean; push: boolean };
  ipo_scanner: { enabled: boolean; interval_minutes: number; lookback_days: number };
  data_auto_sync: { enabled: boolean; lookback_days: number; interval_hours: number };
  ltf_confirmation: { enabled: boolean; interval_minutes: number; push: boolean };
  price_alerts: { enabled: boolean; interval_minutes: number; near_pct: number; push: boolean };
};
type StrategyParams = { ltf_timeframe: string; propulsion_mean_threshold: string };
type DataCutoffs = { nse: string; commodities: string; crypto: string; gift_nifty: string };
type Settings = { automation: Automation; strategy: StrategyParams; data_cutoffs: DataCutoffs; news: { currencies: string[] }; ui: { hidden_strategies: string[]; hidden_pages: string[]; display_timezone: string }; sounds: SoundSettings; access: GuestAccess };

const SOUND_ROWS: { kind: AlertSoundKind; title: string; hint: string }[] = [
  { kind: "ltf", title: "LTF trigger", hint: "An intraday confirmation setup triggers" },
  { kind: "silver_bullet", title: "Silver Bullet", hint: "A new Silver Bullet signal appears" },
  { kind: "price_alert", title: "Price alert", hint: "A chart-popup price alert fires (any page)" },
  { kind: "news_event", title: "News event", hint: "Shortly before a high-impact news or EIA inventory release (scanner page open)" },
];

// Timezone and day are fixed per market; only the time is configurable.
const CUTOFF_ROWS: { key: keyof DataCutoffs; title: string; hint: string }[] = [
  { key: "nse", title: "NSE", hint: "IST, same trading day — bhavcopy published (default 17:00). Also gates when NSE daily analysis runs." },
  { key: "gift_nifty", title: "GIFT Nifty (NSEIX)", hint: "IST, next day — evening session ends 02:45 (default 03:00)" },
  { key: "commodities", title: "Commodities / Forex", hint: "New York time, same day — daily rollover (default 17:00)" },
  { key: "crypto", title: "Crypto", hint: "UTC, next day — UTC day close (default 00:00 = 05:30 IST)" },
];
type Payload = {
  settings: Settings;
  strategies: Strategy[];
  hideable_pages: string[];
  strategy_choices: Record<keyof StrategyParams, string[]>;
  news_currencies: string[];
  sound_choices: string[];
  display_timezones: { value: string; label: string }[];
  nse_holidays?: { items: { date: string; name: string }[]; source: "nse" | "builtin"; fetched_at: string | null; error: string | null; url: string };
  status: {
    silver_bullet: { auto_armed: boolean; last_push_error?: string | null };
    ipo_scanner: { running: boolean; last_ran_at: string | null; last_error: string | null };
    data_auto_sync: { running: boolean; last_run_at: string | null; last_error: string | null };
    ltf_confirmation: { running: boolean; last_check_at: string | null; last_error: string | null; last_push_error?: string | null };
    price_alerts: { running: boolean; last_check_at: string | null; last_error: string | null; push_channels?: string[]; last_push_error?: string | null };
  };
};

const TABS = [
  { id: "scanning", label: "Scanning" },
  { id: "data", label: "Data" },
  { id: "alerts", label: "Alerts & push" },
  { id: "display", label: "Display" },
  { id: "access", label: "Access" },
  { id: "all", label: "All" },
] as const;
type TabId = (typeof TABS)[number]["id"];

// Settings -> Access: pages a read-only guest may open (the API enforces the same list).
const GUEST_PAGE_ROWS: { page: GuestPage; title: string; hint: string }[] = [
  { page: "scanner", title: "Scanner", hint: "View and run limited scans; no sync, strategy or watchlist changes" },
  { page: "alerts", title: "Alerts", hint: "See your price alerts; no add / edit / delete" },
  { page: "ipo", title: "IPO", hint: "View only; no IPO scan or review" },
  { page: "watchlist", title: "Database", hint: "Browse stored OHLC; edits stay admin only" },
  { page: "backtest", title: "Backtest", hint: "Run backtests within the guest scan limits" },
];

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const PAGE_LABELS: Record<string, string> ={ watchlist: "Database", ipo: "IPO", backtest: "Backtest" };
const CHOICE_LABELS: Record<string, string> = {
  "1h": "1 hour", "15m": "15 minutes",
  range: "Full range (high + low) / 2", body: "Body (open + close) / 2",
};

function Choice({ value, options, label, onChange }: { value: string; options: string[]; label: string; onChange: (next: string) => void }) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      style={{ height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace", background: "var(--bg, transparent)", color: "inherit" }}>
      {options.map((option) => <option key={option} value={option}>{CHOICE_LABELS[option] ?? option}</option>)}
    </select>
  );
}

// `tip` = hover-only detail (long strategy descriptions); `status` = live state line under the hint.
function Row({ title, hint, tip, status, children }: { title: string; hint?: string; tip?: string; status?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "4px 12px", padding: "7px 0", borderBottom: "1px solid var(--line)", flexWrap: "wrap" }}>
      <div style={{ minWidth: 0, flex: "1 1 200px" }}>
        <div title={tip} style={{ fontSize: 12, fontWeight: 600, cursor: tip ? "help" : undefined }}>{title}</div>
        {hint && <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 1, lineHeight: 1.35 }}>{hint}</div>}
        {status}
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 6, flexWrap: "wrap", marginLeft: "auto" }}>{children}</div>
    </div>
  );
}

// One-line run state: dot + running/stopped · last run, then any error (full text on hover).
function Status({ running, last, error, tz }: { running?: boolean; last?: string | null; error?: string | null; tz: string }) {
  return (
    <div style={{ font: "10px 'DM Mono', monospace", color: "var(--muted)", marginTop: 3, display: "flex", gap: 6, alignItems: "center", minWidth: 0 }}>
      {running !== undefined && <span style={{ width: 6, height: 6, borderRadius: 3, flex: "none", background: running ? "var(--teal)" : "var(--line)" }} />}
      {running !== undefined && <span style={{ flex: "none" }}>{running ? "running" : "stopped"}</span>}
      {last && <span style={{ flex: "none", whiteSpace: "nowrap" }}>· last {formatDateTime(last, tz)} {zoneLabel(tz, last)}</span>}
      {error && <span title={error} style={{ color: "var(--coral-ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: "1 1 0", minWidth: 0 }}>· {error}</span>}
    </div>
  );
}

// Masonry-style section: panels flow down two columns without row gaps.
const SECTION: React.CSSProperties = { padding: 12, breakInside: "avoid", marginBottom: 12 };
const SUB: React.CSSProperties = { textTransform: "uppercase", color: "var(--muted)", font: "10px 'DM Mono', monospace" };

function Switch({ on, onChange, label, disabled }: { on: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} className="toggle-text" onClick={() => onChange(!on)}
      style={on ? { borderColor: "var(--teal)", color: "var(--teal-ink)", background: "var(--teal-soft)" } : undefined}>
      {on ? "ON" : "OFF"}
    </button>
  );
}

function NumberField({ value, min, max, unit, step = 1, onCommit }: { value: number; min: number; max: number; unit: string; step?: number; onCommit: (next: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const parsed = Number(Math.max(min, Math.min(max, Math.round(Number(draft) / step) * step || value)).toFixed(6));
    setDraft(String(parsed));
    if (parsed !== value) onCommit(parsed);
  };
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--muted)" }}>
      <input type="number" min={min} max={max} step={step} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && commit()}
        style={{ width: 64, height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace" }} />
      {unit}
    </label>
  );
}

// Text input that saves on blur / Enter.
function TextField({ value, label, onCommit }: { value: string; label: string; onCommit: (next: string) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => { if (draft.trim() !== value) onCommit(draft.trim()); };
  return (
    <input type="text" aria-label={label} value={draft} maxLength={200} onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
      style={{ width: 180, height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace" }} />
  );
}

function TimeField({ value, label, onCommit }: { value: string; label: string; onCommit: (next: string) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (/^\d{2}:\d{2}$/.test(draft) && draft !== value) onCommit(draft);
    else setDraft(value);
  };
  return (
    <input type="time" aria-label={label} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()}
      style={{ height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace" }} />
  );
}

// Range slider that saves on release instead of on every step.
function VolumeField({ value, onCommit }: { value: number; onCommit: (next: number) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => { if (draft !== value) onCommit(draft); };
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--muted)" }}>
      <input type="range" aria-label="Alert volume" min={0} max={100} step={5} value={draft} onChange={(e) => setDraft(Number(e.target.value))}
        onPointerUp={commit} onKeyUp={commit} onBlur={commit} style={{ width: 140 }} />
      <span style={{ font: "12px 'DM Mono', monospace", minWidth: 32 }}>{draft}%</span>
    </label>
  );
}

function SettingsPageContent() {
  const displayTz = useDisplayTimezone();
  const authState = useAuth();
  const [data, setData] = useState<Payload | null>(null);
  const [message, setMessage] = useState("Loading…");
  const [tab, setTab] = useState<TabId>("scanning");
  const [openInfo, setOpenInfo] = useState<string | null>(null); // strategy whose ⓘ panel is open

  // The URL hash (#alerts, …) keeps the open tab on reload and makes it linkable.
  useEffect(() => {
    const fromHash = () => {
      const id = window.location.hash.slice(1);
      if (TABS.some((item) => item.id === id)) setTab(id as TabId);
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, []);
  const selectTab = (id: TabId) => {
    setTab(id);
    window.history.replaceState(null, "", `#${id}`);
  };
  const shows = (id: TabId) => tab === id || tab === "all";

  const apply = (payload: Payload, note: string) => {
    setData(payload);
    setSoundSettings(payload.settings.sounds); // alerts on this page pick it up at once
    setDisplayTimezone(payload.settings.ui.display_timezone); // every page re-renders its times
    setMessage(note);
  };

  const load = useCallback(async () => {
    try {
      apply(await fetchAppSettings<Payload>(), "Loaded");
    } catch (error) {
      setMessage(`Could not load settings: ${error instanceof Error ? error.message : error}`);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (patch: unknown) => {
    try {
      const response = await apiFetch(`${API}/api/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      apply(await response.json(), "Saved & applied");
    } catch (error) {
      setMessage(`Save failed: ${error instanceof Error ? error.message : error}`);
    }
  };

  const patchAuto = <K extends keyof Automation>(key: K, values: Partial<Automation[K]>) => save({ automation: { [key]: values } });

  const setStrategy = async (name: string, enabled: boolean) => {
    try {
      const response = await apiFetch(`${API}/api/strategies/${name}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
      if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? `HTTP ${response.status}`);
      await load();
      setMessage("Saved & applied");
    } catch (error) {
      setMessage(`Strategy update failed: ${error instanceof Error ? error.message : error}`);
    }
  };

  const toggleHidden = (field: "hidden_strategies" | "hidden_pages", key: string, hide: boolean) => {
    if (!data) return;
    const current = data.settings.ui[field];
    save({ ui: { [field]: hide ? [...current, key] : current.filter((item) => item !== key) } });
  };

  const auto = data?.settings.automation;
  const status = data?.status;
  const groups = data ? Array.from(new Set(data.strategies.map((item) => item.group))) : [];

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Settings</h1>
        </div>
        <div className="top-actions">
          <div className="status"><span className="pulse" />{message}</div>
          <Navigation active="/settings" />
        </div>
      </header>

      {data && auto && status && (
        <>
          <nav role="tablist" aria-label="Settings sections" style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
            {TABS.map(({ id, label }) => (
              <button key={id} type="button" role="tab" aria-selected={tab === id} className={`chart-tool-btn${tab === id ? " active" : ""}`} onClick={() => selectTab(id)}>
                {label}
              </button>
            ))}
          </nav>

          <div role="tabpanel" style={{ columns: "2 440px", columnGap: 12 }}>
            {shows("scanning") && (
              <>
                <section className="panel" style={{ ...SECTION, columnSpan: "all" }}>
                  <div className="panel-heading"><span>Strategies</span><small>run = included in scans · show = listed on scanner</small></div>
                  {/* One mini-table per group (side by side when wide): name | Run | Show, headers once. */}
                  <div style={{ display: "grid", gap: "4px 32px", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))", alignItems: "start" }}>
                    {groups.map((group) => {
                      const items = data.strategies.filter((item) => item.group === group);
                      const cell: React.CSSProperties = { width: 52, display: "flex", justifyContent: "center", flex: "none" };
                      return (
                        <div key={group}>
                          <div style={{ ...SUB, display: "flex", alignItems: "center", padding: "10px 0 6px", borderBottom: "1px solid var(--line)" }}>
                            <span style={{ flex: 1 }}>{group} <span style={{ opacity: 0.7 }}>· {items.filter((item) => item.enabled).length}/{items.length} on</span></span>
                            <span style={cell}>Run</span>
                            <span style={cell}>Show</span>
                          </div>
                          {items.map((item) => {
                            const open = openInfo === item.name;
                            const summary = item.description ? infoSummary(item.description) : null;
                            return (
                              <div key={item.name} style={{ borderBottom: "1px solid var(--line)" }}>
                                <div style={{ display: "flex", alignItems: "center", padding: "5px 0" }}>
                                  <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 4 }}>
                                    <div style={{ minWidth: 0 }}>
                                      <div style={{ fontSize: 12, fontWeight: 600, color: item.enabled ? undefined : "var(--muted)" }}>{item.label}</div>
                                      {!item.runnable && <Status error="not runnable — disabled in all_strategy.py" tz={displayTz} />}
                                    </div>
                                    {item.description && (
                                      <button type="button" className="info-btn" aria-label={`About ${item.label}`} aria-expanded={open} title={summary ?? "About this strategy"}
                                        onClick={() => setOpenInfo(open ? null : item.name)}>
                                        <Info size={13} />
                                      </button>
                                    )}
                                  </div>
                                  <span style={cell}><Switch label={`Enable ${item.label}`} on={item.enabled} disabled={!item.runnable} onChange={(v) => setStrategy(item.name, v)} /></span>
                                  <span style={cell}><Switch label={`Show ${item.label}`} on={!data.settings.ui.hidden_strategies.includes(item.name)} onChange={(show) => toggleHidden("hidden_strategies", item.name, !show)} /></span>
                                </div>
                                {open && item.description && (
                                  <div className="info-panel">
                                    {summary && <p className="info-summary">{summary}</p>}
                                    <details open={!summary}>
                                      <summary>Exact rules</summary>
                                      <div className="info-details">{renderInfoBody(item.description, { skipSummary: true })}</div>
                                    </details>
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Scheduled scans</span><small>background</small></div>
                  <Row title="Silver Bullet auto-schedule" hint="Arms the AM window scan (10:00 New York)"
                    status={<Status running={status.silver_bullet.auto_armed} tz={displayTz} />}>
                    <Switch label="Silver Bullet auto-schedule" on={auto.silver_bullet_auto.enabled} onChange={(v) => patchAuto("silver_bullet_auto", { enabled: v })} />
                  </Row>
                  <Row title="IPO scanner" hint="Scans a trailing window once the NSE daily bar is ready"
                    status={<Status running={status.ipo_scanner.running} last={status.ipo_scanner.last_ran_at} error={status.ipo_scanner.last_error} tz={displayTz} />}>
                    <NumberField value={auto.ipo_scanner.interval_minutes} min={1} max={1440} unit="min" onCommit={(v) => patchAuto("ipo_scanner", { interval_minutes: v })} />
                    <NumberField value={auto.ipo_scanner.lookback_days} min={1} max={90} unit="days" onCommit={(v) => patchAuto("ipo_scanner", { lookback_days: v })} />
                    <Switch label="IPO scanner" on={auto.ipo_scanner.enabled} onChange={(v) => patchAuto("ipo_scanner", { enabled: v })} />
                  </Row>
                  <Row title="Intraday confirmation watcher" hint="Arms daily setups after each close, then waits for an intraday CISD next session"
                    status={<Status running={status.ltf_confirmation.running} last={status.ltf_confirmation.last_check_at} error={status.ltf_confirmation.last_error} tz={displayTz} />}>
                    <NumberField value={auto.ltf_confirmation.interval_minutes} min={1} max={60} unit="min" onCommit={(v) => patchAuto("ltf_confirmation", { interval_minutes: v })} />
                    <Switch label="Intraday confirmation watcher" on={auto.ltf_confirmation.enabled} onChange={(v) => patchAuto("ltf_confirmation", { enabled: v })} />
                  </Row>
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Strategy parameters</span><small>next scan</small></div>
                  <Row title="Intraday confirmation timeframe" hint="Bars used to confirm a daily setup with a CISD">
                    <Choice label="Intraday confirmation timeframe" value={data.settings.strategy.ltf_timeframe} options={data.strategy_choices.ltf_timeframe}
                      onChange={(v) => save({ strategy: { ltf_timeframe: v } })} />
                  </Row>
                  <Row title="Propulsion block mean" hint="Candle midpoint a close must not cross (sets invalidation and stop)">
                    <Choice label="Propulsion block mean threshold" value={data.settings.strategy.propulsion_mean_threshold} options={data.strategy_choices.propulsion_mean_threshold}
                      onChange={(v) => save({ strategy: { propulsion_mean_threshold: v } })} />
                  </Row>
                </section>
              </>
            )}

            {shows("data") && (
              <>
                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Market data</span><small>daily OHLC</small></div>
                  <Row title="Auto-sync" hint="Syncs each market after its daily bar is final"
                    status={<Status running={status.data_auto_sync.running} last={status.data_auto_sync.last_run_at} error={status.data_auto_sync.last_error} tz={displayTz} />}>
                    <NumberField value={auto.data_auto_sync.interval_hours} min={0.25} max={24} step={0.25} unit="h" onCommit={(v) => patchAuto("data_auto_sync", { interval_hours: v })} />
                    <NumberField value={auto.data_auto_sync.lookback_days} min={1} max={120} unit="days" onCommit={(v) => patchAuto("data_auto_sync", { lookback_days: v })} />
                    <Switch label="Market-data auto-sync" on={auto.data_auto_sync.enabled} onChange={(v) => patchAuto("data_auto_sync", { enabled: v })} />
                  </Row>
                  <div style={{ ...SUB, padding: "10px 0 0" }}>Daily bar cut-offs · when a day&apos;s bar is final</div>
                  {CUTOFF_ROWS.map(({ key, title, hint }) => (
                    <Row key={key} title={title} hint={hint}>
                      <TimeField label={`${title} cut-off`} value={data.settings.data_cutoffs[key]} onCommit={(v) => save({ data_cutoffs: { [key]: v } })} />
                    </Row>
                  ))}
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>NSE holidays</span><small>info · capital market segment</small></div>
                  {(() => {
                    const info = data.nse_holidays;
                    const today = marketToday(IST);
                    const items = (info?.items ?? []).filter((item) => item.date >= `${today.slice(0, 4)}-01-01`);
                    const next = items.find((item) => item.date >= today && weekdayOf(item.date) % 6 !== 0);
                    return (
                      <>
                        <div style={{ fontSize: 11, color: "var(--muted)", padding: "6px 0" }}>
                          NSE stocks, ETFs, IPOs and indices only (not GIFT Nifty, forex, commodities or crypto); sync, scans and LTF confirmation skip these days.{" "}
                          Source: {info?.source === "nse" ? "NSE" : "built-in list (NSE not reached yet)"}
                          {info?.fetched_at ? `, updated ${formatDateTime(info.fetched_at, displayTz)} ${zoneLabel(displayTz, info.fetched_at)}` : ""}
                          {" · "}refreshed once a day ·{" "}
                          <a href={info?.url ?? "https://www.nseindia.com/resources/exchange-communication-holidays"} target="_blank" rel="noreferrer">NSE holiday calendar ↗</a>
                        </div>
                        {items.length === 0 ? (
                          <div style={{ fontSize: 11, color: "var(--muted)" }}>No holidays listed.</div>
                        ) : (
                          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 12, rowGap: 2, font: "11px 'DM Mono', monospace" }}>
                            {items.map((item) => {
                              const weekend = weekdayOf(item.date) % 6 === 0;
                              const style = { opacity: item.date < today || weekend ? 0.45 : 1, fontWeight: item.date === next?.date ? 600 : 400 };
                              return (
                                <Fragment key={item.date}>
                                  <span style={style} title={item.date === next?.date ? "Next NSE holiday" : undefined}>{WEEKDAYS[weekdayOf(item.date)]} {formatTradingDate(item.date)}</span>
                                  <span style={style}>{item.name}{weekend ? " (weekend)" : ""}</span>
                                </Fragment>
                              );
                            })}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>High-impact news</span><small>ForexFactory · daily</small></div>
                  <Row title="Currencies" hint="Shown in the scanner's Events strip. None selected = all.">
                    {data.news_currencies.map((code) => {
                      const current = data.settings.news.currencies;
                      const on = current.includes(code);
                      return (
                        <label key={code} style={{ display: "inline-flex", alignItems: "center", gap: 3, font: "11px 'DM Mono', monospace" }}>
                          <input type="checkbox" checked={on} onChange={() => save({ news: { currencies: on ? current.filter((c) => c !== code) : [...current, code] } })} />
                          {code}
                        </label>
                      );
                    })}
                    {data.settings.news.currencies.length > 0 && <button type="button" className="chart-tool-btn" onClick={() => save({ news: { currencies: [] } })}>All</button>}
                  </Row>
                </section>
              </>
            )}

            {shows("alerts") && (
              <>
                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Price alerts</span><small>background checks</small></div>
                  <Row title="Check interval" hint="Completed 5m bars, open markets only (5 min minimum)"
                    status={<Status running={status.price_alerts.running} last={status.price_alerts.last_check_at} error={status.price_alerts.last_error} tz={displayTz} />}>
                    <NumberField value={auto.price_alerts.interval_minutes} min={5} max={240} unit="min" onCommit={(v) => patchAuto("price_alerts", { interval_minutes: v })} />
                    <Switch label="Price alerts" on={auto.price_alerts.enabled} onChange={(v) => patchAuto("price_alerts", { enabled: v })} />
                  </Row>
                  <Row title="Near-level checks" hint="Check every 5 min while price is within this % of a level (0 = off)">
                    <NumberField value={auto.price_alerts.near_pct} min={0} max={10} step={0.1} unit="%" onCommit={(v) => patchAuto("price_alerts", { near_pct: v })} />
                  </Row>
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Push notifications</span><small>Telegram / ntfy</small></div>
                  <Row title="Channels" hint="Set by env vars on the API machine: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or NTFY_TOPIC"
                    status={<Status last={null} error={status.price_alerts.push_channels?.length ? null : "none configured"} tz={displayTz} />}>
                    {status.price_alerts.push_channels?.map((channel) => <span key={channel} style={{ ...SUB, color: "var(--teal-ink)" }}>{channel}</span>)}
                    <button
                      type="button"
                      className="chart-tool-btn"
                      disabled={!status.price_alerts.push_channels?.length}
                      title="Send a test message to the configured channels"
                      onClick={async () => {
                        try {
                          const response = await apiFetch(`${API}/api/price-alerts/test-push`, { method: "POST" });
                          const body = await response.json().catch(() => null);
                          setMessage(response.ok ? (body?.errors?.length ? `Test push failed: ${body.errors.join("; ")}` : `Test push sent (${body?.channels?.join(", ")})`) : `Test push: ${body?.detail ?? response.status}`);
                        } catch (error) {
                          setMessage(`Test push failed: ${error instanceof Error ? error.message : error}`);
                        }
                      }}
                    >
                      Send test
                    </button>
                  </Row>
                  <Row title="Price alerts" hint="Fired price alerts"
                    status={status.price_alerts.last_push_error ? <Status error={`last send failed: ${status.price_alerts.last_push_error}`} tz={displayTz} /> : undefined}>
                    <Switch label="Price alert push" on={auto.price_alerts.push} onChange={(v) => patchAuto("price_alerts", { push: v })} />
                  </Row>
                  <Row title="Silver Bullet" hint="New live signals (10:00–11:00 New York)"
                    status={status.silver_bullet.last_push_error ? <Status error={`last send failed: ${status.silver_bullet.last_push_error}`} tz={displayTz} /> : undefined}>
                    <Switch label="Silver Bullet push" on={auto.silver_bullet_auto.push} onChange={(v) => patchAuto("silver_bullet_auto", { push: v })} />
                  </Row>
                  <Row title="Intraday confirmations" hint="Setups that trigger (same events as the scanner sound)"
                    status={status.ltf_confirmation.last_push_error ? <Status error={`last send failed: ${status.ltf_confirmation.last_push_error}`} tz={displayTz} /> : undefined}>
                    <Switch label="Intraday confirmation push" on={auto.ltf_confirmation.push} onChange={(v) => patchAuto("ltf_confirmation", { push: v })} />
                  </Row>
                  <details style={{ fontSize: 11, color: "var(--muted)", paddingTop: 8 }}>
                    <summary style={{ cursor: "pointer", fontWeight: 600 }}>How to set up Telegram</summary>
                    <ol style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.6 }}>
                      <li>In Telegram, message @BotFather → <code>/newbot</code> → copy the bot token.</li>
                      <li>Open your new bot, tap Start and send it any message.</li>
                      <li>Visit <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code> and copy <code>chat.id</code> (empty result = message the bot again; or ask @userinfobot for your ID).</li>
                      <li>Add <code>TELEGRAM_BOT_TOKEN</code> and <code>TELEGRAM_CHAT_ID</code> where the API (backend) runs, not the frontend:
                        <ul style={{ margin: "2px 0", paddingLeft: 16 }}>
                          <li><b>Local:</b> in PowerShell run <code>setx TELEGRAM_BOT_TOKEN &quot;…&quot;</code> and <code>setx TELEGRAM_CHAT_ID &quot;…&quot;</code>, close all terminals, then restart via the stop/start launchers.</li>
                          <li><b>Online (Render):</b> API service → Environment → add both variables → Save (the service redeploys). Nothing is needed on Vercel.</li>
                        </ul>
                      </li>
                      <li>Check that Channels above shows telegram, then press Send test.</li>
                    </ol>
                  </details>
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Alert sounds</span><small>browser · 🔊 in header = mute</small></div>
                  <Row title="Master volume" hint="Applies to every alert sound; toasts and desktop notifications are unaffected">
                    <VolumeField value={data.settings.sounds.volume} onCommit={(v) => save({ sounds: { volume: v } })} />
                    <Switch label="Alert sounds" on={data.settings.sounds.enabled} onChange={(v) => save({ sounds: { enabled: v } })} />
                  </Row>
                  {SOUND_ROWS.map(({ kind, title, hint }) => {
                    const choice = data.settings.sounds[kind];
                    return (
                      <Row key={kind} title={title} hint={hint}>
                        <select aria-label={`${title} sound`} value={choice.sound} onChange={(e) => save({ sounds: { [kind]: { sound: e.target.value } } })}
                          style={{ height: 26, width: 150, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace", background: "var(--bg, transparent)", color: "inherit" }}>
                          {data.sound_choices.map((id) => <option key={id} value={id}>{soundLabel(id)}</option>)}
                        </select>
                        <button type="button" className="chart-tool-btn" title="Play this sound" onClick={() => previewSound(choice.sound, data.settings.sounds.volume, kind, data.settings.sounds)}>▶</button>
                        <Switch label={`${title} sound`} on={choice.enabled} onChange={(v) => save({ sounds: { [kind]: { enabled: v } } })} />
                      </Row>
                    );
                  })}
                  <Row title="News event timing" hint="How early, and how many times, the news sound plays">
                    <NumberField value={data.settings.sounds.news_event.lead_minutes} min={1} max={60} unit="min before"
                      onCommit={(v) => save({ sounds: { news_event: { lead_minutes: v } } })} />
                    <NumberField value={data.settings.sounds.news_event.repeat} min={1} max={3} unit="× play"
                      onCommit={(v) => save({ sounds: { news_event: { repeat: v } } })} />
                  </Row>
                  <Row title="Quiet hours" hint={`No sounds in this window (${zoneLabel(displayTz)}; may cross midnight)`}>
                    <TimeField label="Quiet hours start" value={data.settings.sounds.quiet_hours.start} onCommit={(v) => save({ sounds: { quiet_hours: { start: v } } })} />
                    <span style={{ fontSize: 11, color: "var(--muted)" }}>to</span>
                    <TimeField label="Quiet hours end" value={data.settings.sounds.quiet_hours.end} onCommit={(v) => save({ sounds: { quiet_hours: { end: v } } })} />
                    <Switch label="Quiet hours" on={data.settings.sounds.quiet_hours.enabled} onChange={(v) => save({ sounds: { quiet_hours: { enabled: v } } })} />
                  </Row>
                </section>
              </>
            )}

            {shows("display") && (
              <section className="panel" style={SECTION}>
                <div className="panel-heading"><span>Display</span><small>every page · push messages</small></div>
                <Row title="Display timezone" hint={`Times in the app and push messages; markets keep their own zones. Now ${formatDayDateTime(Date.now(), displayTz)} ${zoneLabel(displayTz)}`}>
                  <select aria-label="Display timezone" value={data.settings.ui.display_timezone} onChange={(e) => save({ ui: { display_timezone: e.target.value } })}
                    style={{ height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace", background: "var(--bg, transparent)", color: "inherit" }}>
                    {data.display_timezones.map((zone) => <option key={zone.value} value={zone.value}>{zone.label}</option>)}
                  </select>
                </Row>
                <div style={{ ...SUB, padding: "10px 0 0" }}>Pages in navigation</div>
                {data.hideable_pages.map((page) => (
                  <Row key={page} title={PAGE_LABELS[page] ?? page}>
                    <Switch label={`Show ${PAGE_LABELS[page] ?? page}`} on={!data.settings.ui.hidden_pages.includes(page)} onChange={(show) => toggleHidden("hidden_pages", page, !show)} />
                  </Row>
                ))}
              </section>
            )}

            {shows("access") && (
              <>
                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Guest access</span><small>visitors who are not signed in · read-only</small></div>
                  <div style={{ ...SUB, padding: "10px 0 0" }}>Pages guests can open</div>
                  {GUEST_PAGE_ROWS.map(({ page, title, hint }) => {
                    const pages = data.settings.access.guest_pages;
                    const on = pages.includes(page);
                    return (
                      <Row key={page} title={title} hint={hint}>
                        <Switch label={`Guests can open ${title}`} on={on} onChange={(v) => save({ access: { guest_pages: v ? [...pages, page] : pages.filter((p) => p !== page) } })} />
                      </Row>
                    );
                  })}
                  <Row title="Settings" hint="Always admin only">
                    <Switch label="Guests can open Settings" on={false} disabled onChange={() => undefined} />
                  </Row>
                  <div style={{ ...SUB, padding: "10px 0 0" }}>Guest scans</div>
                  <Row title="Max symbols per scan" hint="A full-watchlist scan stays admin only; your own scan stops a running guest scan">
                    <NumberField value={data.settings.access.guest_max_symbols} min={1} max={500} unit="symbols" onCommit={(v) => save({ access: { guest_max_symbols: v } })} />
                  </Row>
                  <Row title="Cooldown" hint="Per visitor (IP) between scans">
                    <NumberField value={data.settings.access.guest_scan_cooldown_seconds} min={0} max={3600} unit="s" onCommit={(v) => save({ access: { guest_scan_cooldown_seconds: v } })} />
                  </Row>
                  <Row title="Past-date scans" hint="Allow the date picker; off = today only">
                    <Switch label="Guests can scan past dates" on={data.settings.access.guest_past_dates} onChange={(v) => save({ access: { guest_past_dates: v } })} />
                  </Row>
                  <Row title="Extra info columns" hint="Daily context and bias columns make scans heavier">
                    <Switch label="Guests get extra info" on={data.settings.access.guest_extra_info} onChange={(v) => save({ access: { guest_extra_info: v } })} />
                  </Row>
                  <Row title="Banner" hint="Shown next to the 🔒 for guests (empty = none)">
                    <TextField label="Guest banner" value={data.settings.access.guest_banner} onCommit={(v) => save({ access: { guest_banner: v } })} />
                  </Row>
                </section>

                <section className="panel" style={SECTION}>
                  <div className="panel-heading"><span>Admin sign-in</span><small>stored as a hash in the database</small></div>
                  <Row title="Status" hint={authState?.local ? "You are on localhost: admin without signing in" : "Signed in as admin"}
                    status={<Status error={authState && !authState.password_set ? "no password set - the hosted site is read-only for everyone" : null} tz={displayTz} />}>
                    <span style={{ ...SUB, color: authState?.password_set ? "var(--teal-ink)" : "var(--coral-ink)" }}>{authState?.password_set ? "password set" : "not set"}</span>
                  </Row>
                  <Row title={authState?.password_set ? "Change password" : "Set password"} hint="At least 8 characters. Changing it signs every other device out.">
                    <PasswordField onSaved={(note) => setMessage(note)} />
                  </Row>
                  <Row title="Stay signed in" hint="How long a sign-in lasts on a device">
                    <NumberField value={data.settings.access.session_days} min={1} max={365} unit="days" onCommit={(v) => save({ access: { session_days: v } })} />
                  </Row>
                </section>
              </>
            )}
          </div>
        </>
      )}
    </main>
  );
}

// Sets / changes the admin password (PUT /api/auth/password, admin only).
function PasswordField({ onSaved }: { onSaved: (note: string) => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await apiFetch(`${API}/api/auth/password`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : Array.isArray(body?.detail) ? "Password must be at least 8 characters" : `HTTP ${response.status}`);
      replaceToken(body?.token ?? null);
      setPassword("");
      await refreshAuth();
      onSaved("Admin password saved");
    } catch (error) {
      onSaved(`Password not saved: ${error instanceof Error ? error.message : error}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} style={{ display: "flex", gap: 6 }}>
      <input type="password" aria-label="New admin password" autoComplete="new-password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)}
        style={{ width: 150, height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace" }} />
      <button type="submit" className="chart-tool-btn" disabled={busy || password.length < 8}>Save</button>
    </form>
  );
}

export default function SettingsPage() {
  return <PageGate page="settings" active="/settings" title="Settings"><SettingsPageContent /></PageGate>;
}
