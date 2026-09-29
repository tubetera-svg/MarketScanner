"use client";

import { useCallback, useEffect, useState } from "react";
import { Zap } from "lucide-react";
import { CONDITION_LABEL, type PriceAlertStatus } from "./PriceAlertPanel";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Every price alert (all symbols) with Pause/Resume/Re-arm/Delete, plus the
 * triggered feed, shown on the Alerts page (app/alerts). Alerts are
 * created from the chart popup; delivery is components/PriceAlertNotifier.
 */
export default function PriceAlertList({ onOpenChart }: { onOpenChart: (symbol: string) => void }) {
  const [status, setStatus] = useState<PriceAlertStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notifyPermission, setNotifyPermission] = useState<NotificationPermission | "unsupported">("unsupported");

  const load = useCallback(() => {
    fetch(`${API}/api/price-alerts`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data: PriceAlertStatus) => setStatus(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    if (typeof Notification !== "undefined") setNotifyPermission(Notification.permission);
    const id = window.setInterval(load, 15000);
    return () => window.clearInterval(id);
  }, [load]);

  const change = async (id: string, init: RequestInit) => {
    setBusy(true);
    try {
      await fetch(`${API}/api/price-alerts/${id}`, { ...init, headers: { "Content-Type": "application/json" } });
    } catch {
      // the next poll shows the real state
    } finally {
      setBusy(false);
      load();
    }
  };

  if (!status) return null;

  return (
    <section className="panel silver-bullet-results" id="price-alerts" style={{ marginBottom: 16 }}>
      <div className="panel-heading">
        <span>Price alerts</span>
        <small>
          {status.running ? `${status.alerts.filter((alert) => alert.status === "active").length} active · ${status.triggered_session_count} triggered today · checked every ${status.interval_minutes} min` : "watcher off (Settings)"}
          {status.last_check_at ? ` · last ${new Date(status.last_check_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
        </small>
        {notifyPermission === "default" ? (
          <button className="test-button button-secondary" type="button" onClick={() => void Notification.requestPermission().then(setNotifyPermission)}>
            Enable desktop notifications
          </button>
        ) : null}
      </div>
      {status.last_error && <p className="date-note" title={status.last_error}>Last check had errors: {status.last_error.slice(0, 140)}</p>}
      {status.alerts.length > 0 ? (
        <div className="price-alert-table-wrap">
          <table className="price-alert-table">
            <thead>
              <tr><th>Symbol</th><th>Condition</th><th>Level</th><th>Last</th><th>Fires</th><th>Status</th><th>Last triggered</th><th>Expires</th><th /></tr>
            </thead>
            <tbody>
              {status.alerts
                .slice()
                .sort((a, b) => Number(b.status === "active") - Number(a.status === "active") || a.symbol.localeCompare(b.symbol))
                .map((alert) => (
                  <tr key={alert.id} className={alert.status}>
                    <td>
                      <button type="button" className="chart-link-btn" onClick={() => onOpenChart(alert.symbol)}>{alert.symbol}</button>
                      {alert.note ? <small className="muted"> {alert.note}</small> : null}
                    </td>
                    <td>{CONDITION_LABEL[alert.condition]}</td>
                    <td>{alert.level}</td>
                    <td>{alert.last_price ?? "-"}</td>
                    <td>{alert.trigger === "once" ? "Once" : alert.trigger === "once_per_day" ? "Once per day (cut-off)" : `Every cross${alert.cooldown_min ? ` (${alert.cooldown_min}m)` : ""}`}</td>
                    <td><span className="price-alert-status">{alert.status}{alert.trigger_count ? ` ×${alert.trigger_count}` : ""}</span>
                      {alert.triggered_this_session ? <span className="price-alert-today" title="Triggered in the current daily bar (per the market's cut-off)">today</span> : null}</td>
                    <td>{alert.last_triggered_at ? new Date(alert.last_triggered_at).toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-"}</td>
                    <td>{alert.expires_at ? new Date(alert.expires_at).toLocaleDateString([], { month: "short", day: "2-digit" }) : "Never"}</td>
                    <td className="price-alert-actions">
                      {alert.status === "active" || alert.status === "paused" ? (
                        <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void change(alert.id, { method: "PUT", body: JSON.stringify({ status: alert.status === "active" ? "paused" : "active" }) })}>
                          {alert.status === "active" ? "Pause" : "Resume"}
                        </button>
                      ) : (
                        <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void change(alert.id, { method: "PUT", body: JSON.stringify(alert.status === "expired" ? { status: "active", expires_at: null } : { status: "active" }) })}>
                          Re-arm
                        </button>
                      )}
                      <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void change(alert.id, { method: "DELETE" })}>Delete</button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="date-note">No price alerts yet. Open any chart, click Alert (or Alt+click a price) to add one.</p>
      )}
      {status.events.length > 0 ? (
        <>
          <p className="kicker" style={{ margin: "10px 0 6px" }}>Triggered</p>
          <div className="tracker-list">
            {status.events.slice().reverse().map((event) => (
              <button key={event.id} type="button" className="signal-chip price-alert-chip" onClick={() => onOpenChart(event.symbol)}>
                <Zap size={12} />
                <strong>{event.symbol}</strong>
                <small>{CONDITION_LABEL[event.condition]} {event.level} — {new Date(event.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}{event.note ? ` — ${event.note}` : ""}</small>
              </button>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}
