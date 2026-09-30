"use client";

import { useCallback, useEffect, useState } from "react";
import PriceAlertForm, { type AlertPayload } from "./PriceAlertForm";
import {
  TRIGGER_LABEL,
  WINDOW_LABEL,
  alertSymbol,
  describeAlert,
  type PriceAlert,
  type PriceAlertEvent,
  type PriceAlertStatus,
} from "./priceAlertShared";

export * from "./priceAlertShared";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/** POST/PUT a JSON body; returns an error message or null. */
export const saveAlert = async (url: string, method: "POST" | "PUT", body: unknown): Promise<string | null> => {
  try {
    const response = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (response.ok) return null;
    const payload = await response.json().catch(() => null);
    return typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`;
  } catch {
    return "Request failed";
  }
};

/**
 * Chart-popup alert form + this symbol's alerts (each editable in place).
 * Evaluated server-side on completed bars (see api/price_alerts.py); delivery
 * is PriceAlertNotifier.
 */
export default function PriceAlertPanel({
  symbol,
  lastPrice,
  tick,
  pickedLevel,
  onAlertsChange,
  refreshKey = 0,
}: {
  symbol: string;
  lastPrice: number | null;
  /** Price step used to snap levels. */
  tick?: number | null;
  /** Level (or zone) picked on the chart; the nonce refills the same price. */
  pickedLevel: { price: number; price2?: number | null; nonce: number } | null;
  onAlertsChange: (alerts: PriceAlert[], events: PriceAlertEvent[]) => void;
  /** Bump to reload now (e.g. after a line was dragged on the chart). */
  refreshKey?: number;
}) {
  const key = alertSymbol(symbol);
  const [status, setStatus] = useState<PriceAlertStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

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

  useEffect(() => {
    if (refreshKey) load();
  }, [refreshKey, load]);

  const mine = (status?.alerts ?? []).filter((alert) => alert.symbol === key);
  useEffect(() => {
    onAlertsChange(mine, (status?.events ?? []).filter((event) => event.symbol === key));
    // `mine` is derived from status; the callback identity is the caller's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, key]);

  const send = async (url: string, init: RequestInit) => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json" } });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        setError(typeof body?.detail === "string" ? body.detail : `HTTP ${response.status}`);
      }
      load();
    } catch {
      setError("Request failed");
    } finally {
      setBusy(false);
    }
  };

  const create = async (payload: AlertPayload) => {
    const failure = await saveAlert(`${API}/api/price-alerts`, "POST", { ...payload, symbol: key, reference_price: lastPrice });
    if (!failure) load();
    return failure;
  };

  const edit = async (id: string, payload: AlertPayload) => {
    const failure = await saveAlert(`${API}/api/price-alerts/${id}`, "PUT", payload);
    if (!failure) {
      setEditingId(null);
      load();
    }
    return failure;
  };

  return (
    <div className="chart-alerts">
      <PriceAlertForm tick={tick} lastPrice={lastPrice} pickedLevel={editingId ? null : pickedLevel} submitLabel="Create alert" onSubmit={create} />
      <div className="chart-alerts-meta muted">
        Alt+click picks a level · Alt+drag draws a zone · drag a line to move it · right-click for quick alerts · checked every {status?.interval_minutes ?? 15} min{status?.near_pct ? ` (every 5 min within ${status.near_pct}% of a level)` : ""}
        {tick ? ` · tick ${tick}` : ""}
        {status && !status.running ? <span className="warn"> · watcher is off</span> : null}
        {status?.last_error ? <span className="warn" title={status.last_error}> · last check had errors</span> : null}
        {error ? <span className="warn"> · {error}</span> : null}
      </div>
      {mine.length ? (
        <ul className="chart-alerts-list">
          {mine.map((alert) => (
            editingId === alert.id ? (
              <li key={alert.id} className="chart-alert-edit">
                <PriceAlertForm
                  key={alert.id}
                  initial={alert}
                  tick={tick}
                  lastPrice={lastPrice}
                  pickedLevel={pickedLevel}
                  title="Edit alert"
                  submitLabel="Save changes"
                  onSubmit={(payload) => edit(alert.id, payload)}
                  onCancel={() => setEditingId(null)}
                />
              </li>
            ) : (
              <li key={alert.id} className={`chart-alert-item ${alert.status}`}>
                <span>
                  {describeAlert(alert)} · {TRIGGER_LABEL[alert.trigger]}
                  {alert.trigger === "every_check" && alert.cooldown_min ? ` (${alert.cooldown_min}m cooldown)` : ""}
                  {alert.window && alert.window !== "always" ? ` · ${WINDOW_LABEL[alert.window]}` : ""}
                  {alert.note ? <span className="muted"> · {alert.note}</span> : null}
                </span>
                <span className="chart-alert-state">
                  {alert.snoozed_until && Date.parse(alert.snoozed_until) > Date.now() ? "snoozed" : alert.status}
                  {alert.trigger_count ? ` ×${alert.trigger_count}` : ""}
                </span>
                <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => setEditingId(alert.id)} title="Edit this alert (Alt+click / Alt+drag on the chart fills its levels)">
                  Edit
                </button>
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
            )
          ))}
        </ul>
      ) : null}
    </div>
  );
}
