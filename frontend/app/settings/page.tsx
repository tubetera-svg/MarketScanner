"use client";

import Navigation from "../../components/Navigation";

// Settings: one place to enable/disable strategies and automations, tune
// intervals / look-back days, and show/hide strategies and pages in the UI.
// Automation and UI settings are persisted by PUT /api/settings; strategy
// toggles use their existing endpoint and apply immediately.

import { useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Strategy = { name: string; label: string; group: string; enabled: boolean; runnable: boolean; description?: string | null };
type Automation = {
  silver_bullet_auto: { enabled: boolean };
  ipo_scanner: { enabled: boolean; interval_minutes: number; lookback_days: number };
  data_auto_sync: { enabled: boolean; lookback_days: number; interval_hours: number };
  ltf_confirmation: { enabled: boolean; interval_minutes: number };
};
type StrategyParams = { ltf_timeframe: string; propulsion_mean_threshold: string };
type DataCutoffs = { nse: string; commodities: string; crypto: string; gift_nifty: string };
type Settings = { automation: Automation; strategy: StrategyParams; data_cutoffs: DataCutoffs; news: { currencies: string[] }; ui: { hidden_strategies: string[]; hidden_pages: string[] } };

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
  status: {
    silver_bullet: { auto_armed: boolean };
    ipo_scanner: { running: boolean; last_ran_at: string | null; last_error: string | null };
    data_auto_sync: { running: boolean; last_run_at: string | null; last_error: string | null };
    ltf_confirmation: { running: boolean; last_check_at: string | null; last_error: string | null };
  };
};

const PAGE_LABELS: Record<string, string> = { watchlist: "Database", ipo: "IPO", backtest: "Backtest" };
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

function Row({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "10px 0", borderBottom: "1px solid var(--line)", flexWrap: "wrap" }}>
      <div style={{ minWidth: 200, flex: "1 1 220px" }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>{title}</div>
        {hint && <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>{hint}</div>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>{children}</div>
    </div>
  );
}

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
    const parsed = Math.max(min, Math.min(max, Math.round(Number(draft) / step) * step || value));
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

export default function SettingsPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [message, setMessage] = useState("Loading…");

  const apply = (payload: Payload, note: string) => {
    setData(payload);
    setMessage(note);
  };

  const load = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/settings`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      apply(await response.json(), "Loaded");
    } catch (error) {
      setMessage(`Could not load settings: ${error instanceof Error ? error.message : error}`);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (patch: unknown) => {
    try {
      const response = await fetch(`${API}/api/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      apply(await response.json(), "Saved & applied");
    } catch (error) {
      setMessage(`Save failed: ${error instanceof Error ? error.message : error}`);
    }
  };

  const patchAuto = <K extends keyof Automation>(key: K, values: Partial<Automation[K]>) => save({ automation: { [key]: values } });

  const setStrategy = async (name: string, enabled: boolean) => {
    try {
      const response = await fetch(`${API}/api/strategies/${name}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
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
  const stateNote = (running: boolean | undefined, last: string | null | undefined, err: string | null | undefined) =>
    `${running ? "running" : "stopped"}${last ? ` · last ${new Date(last).toLocaleString()}` : ""}${err ? ` · error: ${err}` : ""}`;
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
        <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 480px), 1fr))", alignItems: "start" }}>
          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Automation</span><small>intervals · look-back days</small></div>
            <Row title="Silver Bullet auto-schedule" hint={`Arms the AM window scan (10:00 New York) — ${status.silver_bullet.auto_armed ? "armed" : "off"}`}>
              <Switch label="Silver Bullet auto-schedule" on={auto.silver_bullet_auto.enabled} onChange={(v) => patchAuto("silver_bullet_auto", { enabled: v })} />
            </Row>
            <Row title="IPO scanner" hint={`Scans a trailing window once the NSE daily bar is ready — ${stateNote(status.ipo_scanner.running, status.ipo_scanner.last_ran_at, status.ipo_scanner.last_error)}`}>
              <NumberField value={auto.ipo_scanner.interval_minutes} min={1} max={1440} unit="min" onCommit={(v) => patchAuto("ipo_scanner", { interval_minutes: v })} />
              <NumberField value={auto.ipo_scanner.lookback_days} min={1} max={90} unit="days" onCommit={(v) => patchAuto("ipo_scanner", { lookback_days: v })} />
              <Switch label="IPO scanner" on={auto.ipo_scanner.enabled} onChange={(v) => patchAuto("ipo_scanner", { enabled: v })} />
            </Row>
            <Row title="Market-data auto-sync" hint={`Daily OHLC sync per market after its bar is final — ${stateNote(status.data_auto_sync.running, status.data_auto_sync.last_run_at, status.data_auto_sync.last_error)}`}>
              <NumberField value={auto.data_auto_sync.interval_hours} min={0.25} max={24} step={0.25} unit="h interval" onCommit={(v) => patchAuto("data_auto_sync", { interval_hours: v })} />
              <NumberField value={auto.data_auto_sync.lookback_days} min={1} max={120} unit="days" onCommit={(v) => patchAuto("data_auto_sync", { lookback_days: v })} />
              <Switch label="Market-data auto-sync" on={auto.data_auto_sync.enabled} onChange={(v) => patchAuto("data_auto_sync", { enabled: v })} />
            </Row>
            <Row title="Intraday confirmation watcher" hint={`Arms daily setups after each market's close, then waits for an intraday CISD in the next session — ${stateNote(status.ltf_confirmation.running, status.ltf_confirmation.last_check_at, status.ltf_confirmation.last_error)}`}>
              <NumberField value={auto.ltf_confirmation.interval_minutes} min={1} max={60} unit="min" onCommit={(v) => patchAuto("ltf_confirmation", { interval_minutes: v })} />
              <Switch label="Intraday confirmation watcher" on={auto.ltf_confirmation.enabled} onChange={(v) => patchAuto("ltf_confirmation", { enabled: v })} />
            </Row>
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Strategy parameters</span><small>applies to the next scan</small></div>
            <Row title="Intraday confirmation timeframe" hint="Bars the watcher uses to confirm a daily setup with a change in the state of delivery (CISD)">
              <Choice label="Intraday confirmation timeframe" value={data.settings.strategy.ltf_timeframe} options={data.strategy_choices.ltf_timeframe}
                onChange={(v) => save({ strategy: { ltf_timeframe: v } })} />
            </Row>
            <Row title="Propulsion block mean threshold" hint="Midpoint of the propulsion candle that a close must not cross (sets the invalidation and stop)">
              <Choice label="Propulsion block mean threshold" value={data.settings.strategy.propulsion_mean_threshold} options={data.strategy_choices.propulsion_mean_threshold}
                onChange={(v) => save({ strategy: { propulsion_mean_threshold: v } })} />
            </Row>
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Daily bar cut-offs</span><small>when a day&apos;s bar is final · data sync uses it</small></div>
            {CUTOFF_ROWS.map(({ key, title, hint }) => (
              <Row key={key} title={title} hint={hint}>
                <TimeField label={`${title} cut-off`} value={data.settings.data_cutoffs[key]} onCommit={(v) => save({ data_cutoffs: { [key]: v } })} />
              </Row>
            ))}
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>High-impact news</span><small>ForexFactory red events · fetched once per day</small></div>
            <Row title="Currencies" hint="Shown in the scanner's Events strip. None selected = all currencies.">
              {data.news_currencies.map((code) => {
                const current = data.settings.news.currencies;
                const on = current.includes(code);
                return (
                  <label key={code} style={{ display: "inline-flex", alignItems: "center", gap: 4, font: "12px 'DM Mono', monospace" }}>
                    <input type="checkbox" checked={on} onChange={() => save({ news: { currencies: on ? current.filter((c) => c !== code) : [...current, code] } })} />
                    {code}
                  </label>
                );
              })}
              {data.settings.news.currencies.length > 0 && <button type="button" onClick={() => save({ news: { currencies: [] } })} style={{ font: "11px 'DM Mono', monospace" }}>All</button>}
            </Row>
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Pages</span><small>show / hide in navigation</small></div>
            {data.hideable_pages.map((page) => (
              <Row key={page} title={PAGE_LABELS[page] ?? page}>
                <Switch label={`Show ${PAGE_LABELS[page] ?? page}`} on={!data.settings.ui.hidden_pages.includes(page)} onChange={(show) => toggleHidden("hidden_pages", page, !show)} />
              </Row>
            ))}
          </section>

          <section className="panel" style={{ padding: 16, gridColumn: "1 / -1" }}>
            <div className="panel-heading"><span>Strategies</span><small>enable = runs in scans · show = listed on the scanner page</small></div>
            {groups.map((group) => (
              <div key={group}>
                <div style={{ textTransform: "uppercase", color: "var(--muted)", font: "10px 'DM Mono', monospace", padding: "12px 0 2px" }}>{group}</div>
                {data.strategies.filter((item) => item.group === group).map((item) => (
                  <Row key={item.name} title={item.label} hint={item.runnable ? item.description ?? undefined : "Not runnable — disabled in all_strategy.py"}>
                    <span style={{ fontSize: 10, color: "var(--muted)" }}>Enabled</span>
                    <Switch label={`Enable ${item.label}`} on={item.enabled} disabled={!item.runnable} onChange={(v) => setStrategy(item.name, v)} />
                    <span style={{ fontSize: 10, color: "var(--muted)", marginLeft: 8 }}>Shown</span>
                    <Switch label={`Show ${item.label}`} on={!data.settings.ui.hidden_strategies.includes(item.name)} onChange={(show) => toggleHidden("hidden_strategies", item.name, !show)} />
                  </Row>
                ))}
              </div>
            ))}
          </section>
        </div>
      )}
    </main>
  );
}
