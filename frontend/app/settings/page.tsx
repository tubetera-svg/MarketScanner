"use client";

import Navigation from "../../components/Navigation";

// Settings: one place to enable/disable strategies and automations, tune
// intervals / look-back days, and show/hide strategies and pages in the UI.
// Automation and UI settings are persisted by PUT /api/settings; strategy
// toggles use their existing endpoint and apply immediately.

import { useCallback, useEffect, useState } from "react";
import { previewSound, setSoundSettings, soundLabel, type AlertSoundKind, type SoundSettings } from "../../components/alertSound";
import { fetchAppSettings } from "../../components/appSettings";

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
type Settings = { automation: Automation; strategy: StrategyParams; data_cutoffs: DataCutoffs; news: { currencies: string[] }; ui: { hidden_strategies: string[]; hidden_pages: string[] }; sounds: SoundSettings };

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
  status: {
    silver_bullet: { auto_armed: boolean; last_push_error?: string | null };
    ipo_scanner: { running: boolean; last_ran_at: string | null; last_error: string | null };
    data_auto_sync: { running: boolean; last_run_at: string | null; last_error: string | null };
    ltf_confirmation: { running: boolean; last_check_at: string | null; last_error: string | null; last_push_error?: string | null };
    price_alerts: { running: boolean; last_check_at: string | null; last_error: string | null; push_channels?: string[]; last_push_error?: string | null };
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

export default function SettingsPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [message, setMessage] = useState("Loading…");

  const apply = (payload: Payload, note: string) => {
    setData(payload);
    setSoundSettings(payload.settings.sounds); // alerts on this page pick it up at once
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
            <Row title="Silver Bullet: push" hint={`Send new live signals (10:00–11:00 New York) to Telegram/ntfy — same channels as price-alert push${status.silver_bullet.last_push_error ? ` · last send failed: ${status.silver_bullet.last_push_error}` : ""}`}>
              <Switch label="Silver Bullet push" on={auto.silver_bullet_auto.push} onChange={(v) => patchAuto("silver_bullet_auto", { push: v })} />
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
            <Row title="Price alerts" hint={`How often chart-popup price alerts are checked (completed 5m bars, open markets only; 5 min minimum) — ${stateNote(status.price_alerts.running, status.price_alerts.last_check_at, status.price_alerts.last_error)}`}>
              <NumberField value={auto.price_alerts.interval_minutes} min={5} max={240} unit="min" onCommit={(v) => patchAuto("price_alerts", { interval_minutes: v })} />
              <Switch label="Price alerts" on={auto.price_alerts.enabled} onChange={(v) => patchAuto("price_alerts", { enabled: v })} />
            </Row>
            <Row title="Price alerts: near-level checks" hint="Check a symbol every 5 minutes while price is within this % of one of its alert levels (0 = always use the interval above)">
              <NumberField value={auto.price_alerts.near_pct} min={0} max={10} step={0.1} unit="%" onCommit={(v) => patchAuto("price_alerts", { near_pct: v })} />
            </Row>
            <Row
              title="Price alerts: push"
              hint={`Also send fired alerts to your phone. Channels come from environment variables on the API machine (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and/or NTFY_TOPIC) — configured: ${status.price_alerts.push_channels?.length ? status.price_alerts.push_channels.join(", ") : "none"}${status.price_alerts.last_push_error ? ` · last send failed: ${status.price_alerts.last_push_error}` : ""}`}
            >
              <button
                type="button"
                className="chart-tool-btn"
                disabled={!status.price_alerts.push_channels?.length}
                title="Send a test message to the configured channels"
                onClick={async () => {
                  try {
                    const response = await fetch(`${API}/api/price-alerts/test-push`, { method: "POST" });
                    const body = await response.json().catch(() => null);
                    setMessage(response.ok ? (body?.errors?.length ? `Test push failed: ${body.errors.join("; ")}` : `Test push sent (${body?.channels?.join(", ")})`) : `Test push: ${body?.detail ?? response.status}`);
                  } catch (error) {
                    setMessage(`Test push failed: ${error instanceof Error ? error.message : error}`);
                  }
                }}
              >
                Send test
              </button>
              <Switch label="Price alert push" on={auto.price_alerts.push} onChange={(v) => patchAuto("price_alerts", { push: v })} />
            </Row>
            <details style={{ fontSize: 11, color: "var(--muted)", padding: "10px 0" }}>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>How to set up Telegram</summary>
              <ol style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.6 }}>
                <li>In Telegram, message @BotFather → <code>/newbot</code> → copy the bot token.</li>
                <li>Open your new bot, tap Start and send it any message.</li>
                <li>Visit <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code> and copy <code>chat.id</code> (empty result = message the bot again; or ask @userinfobot for your ID).</li>
                <li>On the API machine run <code>setx TELEGRAM_BOT_TOKEN &quot;…&quot;</code> and <code>setx TELEGRAM_CHAT_ID &quot;…&quot;</code>.</li>
                <li>Restart the scanner (stop/start launchers), then check that &quot;configured&quot; shows telegram and press Send test.</li>
              </ol>
            </details>
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Intraday confirmations</span><small>watcher · timeframe</small></div>
            <Row title="Intraday confirmation watcher" hint={`Arms daily setups after each market's close, then waits for an intraday CISD in the next session — ${stateNote(status.ltf_confirmation.running, status.ltf_confirmation.last_check_at, status.ltf_confirmation.last_error)}`}>
              <NumberField value={auto.ltf_confirmation.interval_minutes} min={1} max={60} unit="min" onCommit={(v) => patchAuto("ltf_confirmation", { interval_minutes: v })} />
              <Switch label="Intraday confirmation watcher" on={auto.ltf_confirmation.enabled} onChange={(v) => patchAuto("ltf_confirmation", { enabled: v })} />
            </Row>
            <Row title="Intraday confirmations: push" hint={`Send setups that trigger (same events as the scanner-page sound) to Telegram/ntfy — same channels as price-alert push${status.ltf_confirmation.last_push_error ? ` · last send failed: ${status.ltf_confirmation.last_push_error}` : ""}`}>
              <Switch label="Intraday confirmation push" on={auto.ltf_confirmation.push} onChange={(v) => patchAuto("ltf_confirmation", { push: v })} />
            </Row>
            <Row title="Intraday confirmation timeframe" hint="Bars the watcher uses to confirm a daily setup with a change in the state of delivery (CISD)">
              <Choice label="Intraday confirmation timeframe" value={data.settings.strategy.ltf_timeframe} options={data.strategy_choices.ltf_timeframe}
                onChange={(v) => save({ strategy: { ltf_timeframe: v } })} />
            </Row>
          </section>

          <section className="panel" style={{ padding: 16 }}>
            <div className="panel-heading"><span>Strategy parameters</span><small>applies to the next scan</small></div>
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
            <div className="panel-heading"><span>Alert sounds</span><small>played in the browser · header 🔊 = temporary mute</small></div>
            <Row title="Alert sounds" hint="Master switch and volume for every alert sound. Toasts and desktop notifications are not affected.">
              <VolumeField value={data.settings.sounds.volume} onCommit={(v) => save({ sounds: { volume: v } })} />
              <Switch label="Alert sounds" on={data.settings.sounds.enabled} onChange={(v) => save({ sounds: { enabled: v } })} />
            </Row>
            {SOUND_ROWS.map(({ kind, title, hint }) => {
              const choice = data.settings.sounds[kind];
              return (
                <Row key={kind} title={title} hint={hint}>
                  {kind === "news_event" && (
                    <NumberField value={data.settings.sounds.news_event.lead_minutes} min={1} max={60} unit="min before"
                      onCommit={(v) => save({ sounds: { news_event: { lead_minutes: v } } })} />
                  )}
                  {kind === "news_event" && (
                    <NumberField value={data.settings.sounds.news_event.repeat} min={1} max={3} unit="× play"
                      onCommit={(v) => save({ sounds: { news_event: { repeat: v } } })} />
                  )}
                  <select aria-label={`${title} sound`} value={choice.sound} onChange={(e) => save({ sounds: { [kind]: { sound: e.target.value } } })}
                    style={{ height: 26, border: "1px solid var(--line)", borderRadius: 4, padding: "0 6px", font: "12px 'DM Mono', monospace", background: "var(--bg, transparent)", color: "inherit" }}>
                    {data.sound_choices.map((id) => <option key={id} value={id}>{soundLabel(id)}</option>)}
                  </select>
                  <button type="button" className="chart-tool-btn" title="Play this sound" onClick={() => previewSound(choice.sound, data.settings.sounds.volume, kind, data.settings.sounds)}>▶ Test</button>
                  <Switch label={`${title} sound`} on={choice.enabled} onChange={(v) => save({ sounds: { [kind]: { enabled: v } } })} />
                </Row>
              );
            })}
            <Row title="Quiet hours" hint="No alert sounds in this IST window (may cross midnight, e.g. 23:00 → 07:00).">
              <TimeField label="Quiet hours start" value={data.settings.sounds.quiet_hours.start} onCommit={(v) => save({ sounds: { quiet_hours: { start: v } } })} />
              <span style={{ fontSize: 11, color: "var(--muted)" }}>to</span>
              <TimeField label="Quiet hours end" value={data.settings.sounds.quiet_hours.end} onCommit={(v) => save({ sounds: { quiet_hours: { end: v } } })} />
              <Switch label="Quiet hours" on={data.settings.sounds.quiet_hours.enabled} onChange={(v) => save({ sounds: { quiet_hours: { enabled: v } } })} />
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
