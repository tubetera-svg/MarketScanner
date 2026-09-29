"use client";

import { useCallback, useEffect, useState } from "react";
import { formatPrice } from "./OhlcChart";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type PriceAlert = {
  id: string;
  symbol: string;
  level: number;
  condition: "crosses_above" | "crosses_below" | "crosses";
  trigger: "once" | "once_per_day" | "every_check";
  cooldown_min: number;
  expires_at: string | null;
  note: string;
  status: "active" | "paused" | "triggered" | "expired";
  last_price: number | null;
  last_triggered_at: string | null;
  trigger_count: number;
  /** Fired within the current daily bar (per Settings > Daily bar cut-offs). */
  triggered_this_session?: boolean;
};

export type PriceAlertEvent = {
  id: string;
  alert_id: string;
  ts: string;
  symbol: string;
  condition: PriceAlert["condition"];
  level: number;
  price: number | null;
  note: string;
};

export type PriceAlertStatus = {
  running: boolean;
  interval_minutes: number;
  last_check_at: string | null;
  last_error: string | null;
  alerts: PriceAlert[];
  triggered_session_count: number;
  events: PriceAlertEvent[];
};

export const CONDITION_LABEL: Record<PriceAlert["condition"], string> = {
  crosses: "crosses",
  crosses_above: "crosses above",
  crosses_below: "crosses below",
};

const TRIGGER_LABEL: Record<PriceAlert["trigger"], string> = {
  once: "Once",
  once_per_day: "Once per day (cut-off)",
  every_check: "Every cross",
};

const EXPIRY: [string, number | null][] = [
  ["Never", null],
  ["1 day", 1],
  ["1 week", 7],
  ["1 month", 30],
];

/** Backend symbol key: bare symbols are NSE. */
export const alertSymbol = (symbol: string) => {
  const value = symbol.trim().toUpperCase();
  return value.includes(":") ? value : `NSE:${value}`;
};

/**
 * Chart-popup alert form + this symbol's alerts. Evaluated server-side every
 * `interval_minutes` on completed 5m bars; delivery is in-app (scanner page).
 */
export default function PriceAlertPanel({
  symbol,
  lastPrice,
  pickedLevel,
  onAlertsChange,
}: {
  symbol: string;
  lastPrice: number | null;
  /** Level picked by Alt+click on the chart (nonce forces refill of the same price). */
  pickedLevel: { price: number; nonce: number } | null;
  onAlertsChange: (alerts: PriceAlert[]) => void;
}) {
  const key = alertSymbol(symbol);
  const [status, setStatus] = useState<PriceAlertStatus | null>(null);
  const [level, setLevel] = useState(lastPrice != null ? formatPrice(lastPrice).replace(/,/g, "") : "");
  const [condition, setCondition] = useState<PriceAlert["condition"]>("crosses");
  const [trigger, setTrigger] = useState<PriceAlert["trigger"]>("once");
  const [cooldown, setCooldown] = useState(60);
  const [expiryDays, setExpiryDays] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    fetch(`${API}/api/price-alerts`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data: PriceAlertStatus) => setStatus(data))
      .catch(() => setError("Couldn't load alerts"));
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 30000);
    return () => window.clearInterval(id);
  }, [load]);

  const mine = (status?.alerts ?? []).filter((alert) => alert.symbol === key);
  useEffect(() => {
    onAlertsChange(mine);
    // `mine` is derived from status; the callback identity is the caller's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, key]);

  useEffect(() => {
    if (pickedLevel) setLevel(String(Number(pickedLevel.price.toPrecision(6))));
  }, [pickedLevel]);

  // Candles load after mount: prefill the level with the last close once.
  useEffect(() => {
    if (lastPrice != null) setLevel((current) => current || formatPrice(lastPrice).replace(/,/g, ""));
  }, [lastPrice]);

  const send = async (url: string, init: RequestInit) => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json" } });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        setError(typeof body?.detail === "string" ? body.detail : `HTTP ${response.status}`);
        return;
      }
      load();
    } catch {
      setError("Request failed");
    } finally {
      setBusy(false);
    }
  };

  const create = () => {
    const value = Number(level);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Enter a price level");
      return;
    }
    const expires = expiryDays ? new Date(Date.now() + expiryDays * 86400000).toISOString() : null;
    void send(`${API}/api/price-alerts`, {
      method: "POST",
      body: JSON.stringify({
        symbol: key,
        level: value,
        condition,
        trigger,
        cooldown_min: trigger === "every_check" ? cooldown : 0,
        expires_at: expires,
        note,
        reference_price: lastPrice,
      }),
    });
    setNote("");
  };

  return (
    <div className="chart-alerts">
      <div className="chart-alerts-form">
        <span className="chart-alerts-title">Alert when price</span>
        <select value={condition} onChange={(event) => setCondition(event.target.value as PriceAlert["condition"])} aria-label="Condition">
          {Object.entries(CONDITION_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <input className="chart-alerts-level" type="number" step="any" value={level} onChange={(event) => setLevel(event.target.value)} aria-label="Price level" />
        <select value={trigger} onChange={(event) => setTrigger(event.target.value as PriceAlert["trigger"])} aria-label="Trigger frequency">
          {Object.entries(TRIGGER_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        {trigger === "every_check" ? (
          <label title="Minimum minutes between two triggers">
            Cooldown
            <input className="chart-alerts-num" type="number" min={0} max={1440} value={cooldown} onChange={(event) => setCooldown(Number(event.target.value) || 0)} />
            min
          </label>
        ) : null}
        <label>
          Expires
          <select value={expiryDays ?? ""} onChange={(event) => setExpiryDays(event.target.value ? Number(event.target.value) : null)}>
            {EXPIRY.map(([label, days]) => <option key={label} value={days ?? ""}>{label}</option>)}
          </select>
        </label>
        <input className="chart-alerts-note" type="text" placeholder="Note (optional)" maxLength={200} value={note} onChange={(event) => setNote(event.target.value)} />
        <button type="button" className="chart-tool-btn primary" onClick={create} disabled={busy}>Create alert</button>
      </div>
      <div className="chart-alerts-meta muted">
        Alt+click the chart to pick a level · "day" rolls at this market's daily bar cut-off (Settings) · checked every {status?.interval_minutes ?? 15} min on completed 5m bars (Settings)
        {status && !status.running ? <span className="warn"> · watcher is off</span> : null}
        {status?.last_error ? <span className="warn" title={status.last_error}> · last check had errors</span> : null}
        {error ? <span className="warn"> · {error}</span> : null}
      </div>
      {mine.length ? (
        <ul className="chart-alerts-list">
          {mine.map((alert) => (
            <li key={alert.id} className={`chart-alert-item ${alert.status}`}>
              <span>
                {CONDITION_LABEL[alert.condition]} <strong>{formatPrice(alert.level)}</strong> · {TRIGGER_LABEL[alert.trigger]}
                {alert.trigger === "every_check" && alert.cooldown_min ? ` (${alert.cooldown_min}m cooldown)` : ""}
                {alert.note ? <span className="muted"> · {alert.note}</span> : null}
              </span>
              <span className="chart-alert-state">{alert.status}{alert.trigger_count ? ` ×${alert.trigger_count}` : ""}</span>
              {alert.status === "active" || alert.status === "paused" ? (
                <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void send(`${API}/api/price-alerts/${alert.id}`, { method: "PUT", body: JSON.stringify({ status: alert.status === "active" ? "paused" : "active" }) })}>
                  {alert.status === "active" ? "Pause" : "Resume"}
                </button>
              ) : (
                <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void send(`${API}/api/price-alerts/${alert.id}`, { method: "PUT", body: JSON.stringify(alert.status === "expired" ? { status: "active", expires_at: null } : { status: "active" }) })}>
                  Re-arm
                </button>
              )}
              <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void send(`${API}/api/price-alerts/${alert.id}`, { method: "DELETE" })}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
