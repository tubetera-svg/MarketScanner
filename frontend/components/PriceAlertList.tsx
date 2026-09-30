"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpDown, ArrowUpRight, BellOff, BellRing, Pause, Pencil, Play, RefreshCw, RotateCcw, Square, Trash2, Zap } from "lucide-react";
import {
  WINDOW_LABEL,
  describeAlert,
  isZoneCondition,
  type AlertCondition,
  type AlertOutcome,
  type PriceAlert,
  type PriceAlertStatus,
} from "./priceAlertShared";
import PriceAlertForm, { type AlertPayload } from "./PriceAlertForm";
import { saveAlert } from "./PriceAlertPanel";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Filter = "all" | "active" | "triggered" | "inactive";

const CONDITION_ICON: Record<AlertCondition, typeof ArrowUpRight> = {
  crosses_above: ArrowUpRight,
  crosses_below: ArrowDownRight,
  crosses: ArrowUpDown,
  enters_zone: Square,
  exits_zone: Square,
  closes_above: ArrowUpRight,
  closes_below: ArrowDownRight,
  closes_inside: Square,
};

const firesLabel = (alert: PriceAlert) =>
  alert.trigger === "once"
    ? "Once"
    : alert.trigger === "once_per_day"
      ? "Once per day"
      : `Every cross${alert.cooldown_min ? ` · ${alert.cooldown_min}m cooldown` : ""}`;

// Levels keep the precision they were set with (e.g. 552.694 from Alt+click).
const levelText = (value: number) => value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dateTime = (iso: string) => new Date(iso).toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const snoozed = (alert: PriceAlert) => Boolean(alert.snoozed_until && Date.parse(alert.snoozed_until) > Date.now());

const OUTCOME_KEYS: ["15m" | "1h" | "eod", string][] = [["15m", "+15m"], ["1h", "+1h"], ["eod", "EOD"]];

function OutcomeChip({ label, outcome }: { label: string; outcome: AlertOutcome | undefined }) {
  if (outcome === undefined) return <span className="pa-outcome pending" title="Not due yet">{label} …</span>;
  if (outcome === null || outcome.pct == null) return <span className="pa-outcome" title="No data for this point">{label} —</span>;
  return (
    <span className={`pa-outcome ${outcome.pct >= 0 ? "up" : "down"}`} title={`Price ${levelText(outcome.price)}`}>
      {label} {outcome.pct >= 0 ? "+" : ""}{outcome.pct.toFixed(2)}%
    </span>
  );
}

/**
 * Every price alert (all symbols) with Pause/Resume/Re-arm/Snooze/Delete and
 * bulk actions, plus the saved trigger history with outcomes, shown on the
 * Alerts page (app/alerts). Alerts are created from the chart popup; delivery
 * is components/PriceAlertNotifier.
 */
export default function PriceAlertList({ onOpenChart }: { onOpenChart: (symbol: string, interval?: "5m") => void }) {
  const [status, setStatus] = useState<PriceAlertStatus | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [notifyPermission, setNotifyPermission] = useState<NotificationPermission | "unsupported">("unsupported");

  const load = useCallback(() => {
    fetch(`${API}/api/price-alerts`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data: PriceAlertStatus) => {
        setStatus(data);
        setLoadError(false);
      })
      .catch(() => setLoadError(true));
  }, []);

  useEffect(() => {
    load();
    if (typeof Notification !== "undefined") setNotifyPermission(Notification.permission);
    const id = window.setInterval(load, 15000);
    return () => window.clearInterval(id);
  }, [load]);

  const request = async (url: string, init: RequestInit) => {
    setBusy(true);
    try {
      await fetch(url, { ...init, headers: { "Content-Type": "application/json" } });
    } catch {
      // the next poll shows the real state
    } finally {
      setBusy(false);
      load();
    }
  };
  const change = (id: string, init: RequestInit) => request(`${API}/api/price-alerts/${id}`, init);

  const saveEdit = async (id: string, payload: AlertPayload) => {
    const failure = await saveAlert(`${API}/api/price-alerts/${id}`, "PUT", payload);
    if (!failure) {
      setEditingId(null);
      load();
    }
    return failure;
  };

  const bulk = async (action: "pause" | "resume" | "delete") => {
    const ids = [...selected];
    if (!ids.length) return;
    if (action === "delete" && !window.confirm(`Delete ${ids.length} alert${ids.length === 1 ? "" : "s"}?`)) return;
    await request(`${API}/api/price-alerts/bulk`, { method: "POST", body: JSON.stringify({ ids, action }) });
    setSelected(new Set());
  };

  const checkNow = async () => {
    setChecking(true);
    try {
      const response = await fetch(`${API}/api/price-alerts/check`, { method: "POST" });
      if (response.ok) setStatus((await response.json()) as PriceAlertStatus);
    } catch {
      // keep the last status
    } finally {
      setChecking(false);
    }
  };

  const alerts = useMemo(() => status?.alerts ?? [], [status]);
  const counts = useMemo(
    () => ({
      all: alerts.length,
      active: alerts.filter((alert) => alert.status === "active").length,
      triggered: alerts.filter((alert) => alert.triggered_this_session || alert.status === "triggered").length,
      inactive: alerts.filter((alert) => alert.status === "paused" || alert.status === "expired").length,
    }),
    [alerts],
  );
  const rows = useMemo(() => {
    const order: Record<PriceAlert["status"], number> = { active: 0, triggered: 1, paused: 2, expired: 3 };
    return alerts
      .filter((alert) =>
        filter === "all" ? true
          : filter === "active" ? alert.status === "active"
            : filter === "triggered" ? alert.triggered_this_session || alert.status === "triggered"
              : alert.status === "paused" || alert.status === "expired",
      )
      .sort((a, b) => Number(Boolean(b.triggered_this_session)) - Number(Boolean(a.triggered_this_session)) || order[a.status] - order[b.status] || a.symbol.localeCompare(b.symbol));
  }, [alerts, filter]);

  // Drop selections that are no longer listed (deleted / filtered out).
  const visibleIds = useMemo(() => new Set(rows.map((row) => row.id)), [rows]);
  const picked = [...selected].filter((id) => visibleIds.has(id));
  const allPicked = rows.length > 0 && picked.length === rows.length;
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!status) {
    return (
      <section className="panel pa-panel">
        <p className="pa-empty">{loadError ? "Couldn't reach the API for price alerts." : "Loading price alerts…"}</p>
      </section>
    );
  }

  const events = status.events.slice().reverse();
  const channels = status.push_channels ?? [];

  return (
    <div className="pa-page">
      <div className="pa-summary">
        <div className="pa-stat">
          <span>Active</span>
          <strong>{counts.active}</strong>
          <small>of {counts.all} alert{counts.all === 1 ? "" : "s"}</small>
        </div>
        <div className={`pa-stat${status.triggered_session_count ? " hot" : ""}`}>
          <span>Triggered today</span>
          <strong>{status.triggered_session_count}</strong>
          <small>resets at each market&apos;s cut-off</small>
        </div>
        <div className="pa-stat">
          <span>Paused / expired</span>
          <strong>{counts.inactive}</strong>
          <small>not being checked</small>
        </div>
        <div className="pa-stat pa-watcher">
          <span>Watcher</span>
          <strong className={status.running ? "on" : "off"}>
            <i aria-hidden="true" />
            {status.running ? `Every ${status.interval_minutes} min` : "Off"}
          </strong>
          <small>
            {status.last_check_at ? `last check ${time(status.last_check_at)}` : "not checked yet"}
            {status.running && status.near_pct ? ` · 5 min within ${status.near_pct}%` : ""}
            {!status.running ? " · turn on in Settings" : ""}
          </small>
          <small title={status.last_push_error ?? undefined}>
            Push: {status.push_enabled ? (channels.length ? channels.join(" + ") : "on, no channel set") : "off"}
            {status.last_push_error ? " · last send failed" : ""}
          </small>
          <div className="pa-watcher-actions">
            <button type="button" className="chart-tool-btn" onClick={() => void checkNow()} disabled={checking} title="Check all active alerts now (open markets only)">
              <RefreshCw size={11} className={checking ? "spin" : undefined} /> Check now
            </button>
            {notifyPermission === "default" ? (
              <button type="button" className="chart-tool-btn" onClick={() => void Notification.requestPermission().then(setNotifyPermission)}>
                <BellRing size={11} /> Desktop alerts
              </button>
            ) : null}
          </div>
        </div>
      </div>

      {status.last_error && <p className="date-note" title={status.last_error}>Last check had errors: {status.last_error.slice(0, 160)}</p>}

      <section className="panel pa-panel">
        <div className="pa-panel-head">
          <h2>Price alerts</h2>
          {picked.length ? (
            <div className="pa-bulk" role="group" aria-label="Bulk actions">
              <span>{picked.length} selected</span>
              <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void bulk("pause")}><Pause size={11} /> Pause</button>
              <button type="button" className="chart-tool-btn" disabled={busy} onClick={() => void bulk("resume")}><Play size={11} /> Resume / re-arm</button>
              <button type="button" className="chart-tool-btn danger" disabled={busy} onClick={() => void bulk("delete")}><Trash2 size={11} /> Delete</button>
              <button type="button" className="chart-link-btn" onClick={() => setSelected(new Set())}>Clear</button>
            </div>
          ) : (
            <div className="chart-seg small pa-filter" role="group" aria-label="Filter alerts">
              {([["all", "All"], ["active", "Active"], ["triggered", "Triggered"], ["inactive", "Paused / expired"]] as [Filter, string][]).map(([key, label]) => (
                <button key={key} type="button" className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>
                  {label} <em>{counts[key]}</em>
                </button>
              ))}
            </div>
          )}
        </div>

        {alerts.length === 0 ? (
          <div className="pa-empty">
            <BellRing size={22} />
            <strong>No price alerts yet</strong>
            <span>Open any chart and click <b>Alert</b>, right-click a price for quick alerts, or Alt+drag to draw a zone.</span>
          </div>
        ) : rows.length === 0 ? (
          <p className="pa-empty">Nothing in this view.</p>
        ) : (
          <div className="pa-table-wrap">
            <table className="pa-table">
              <thead>
                <tr>
                  <th className="pa-check">
                    <input type="checkbox" aria-label="Select all shown" checked={allPicked} onChange={() => setSelected(allPicked ? new Set() : new Set(rows.map((row) => row.id)))} />
                  </th>
                  <th>Symbol</th><th>Condition</th><th className="num">Last · distance</th><th>Fires</th><th>Status</th><th>Last triggered</th><th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {rows.map((alert) => {
                  const Icon = CONDITION_ICON[alert.condition] ?? ArrowUpDown;
                  const distance = alert.distance_pct;
                  const upward = alert.last_price != null && !isZoneCondition(alert.condition) ? alert.level >= alert.last_price : null;
                  const muted = snoozed(alert);
                  return (
                    <Fragment key={alert.id}>
                    <tr className={`pa-row ${alert.status}${alert.triggered_this_session ? " today" : ""}${selected.has(alert.id) ? " selected" : ""}`}>
                      <td className="pa-check">
                        <input type="checkbox" aria-label={`Select ${alert.symbol} alert`} checked={selected.has(alert.id)} onChange={() => toggle(alert.id)} />
                      </td>
                      <td>
                        <button type="button" className="pa-symbol" onClick={() => onOpenChart(alert.symbol)} title="Open chart">
                          {alert.symbol.split(":").slice(1).join(":") || alert.symbol}
                          <em>{alert.symbol.split(":")[0]}</em>
                        </button>
                        {alert.note ? <small className="pa-note">{alert.note}</small> : null}
                      </td>
                      <td>
                        <span className={`pa-cond ${alert.condition}`}><Icon size={12} /> {describeAlert(alert)}</span>
                        {alert.window && alert.window !== "always" ? <small className="pa-sub">{WINDOW_LABEL[alert.window]}</small> : null}
                      </td>
                      <td className="num">
                        {alert.last_price != null ? levelText(alert.last_price) : "—"}
                        {distance != null && alert.status === "active" ? (
                          <small className={`pa-distance ${upward === false ? "down" : "up"}`} title="Move needed from the last price to reach the level">
                            {distance === 0 ? "in zone" : `${upward === null ? "" : upward ? "▲ " : "▼ "}${distance.toFixed(2)}%`}
                          </small>
                        ) : null}
                      </td>
                      <td>
                        {firesLabel(alert)}
                        {alert.expires_at ? <small className="pa-sub">expires {dateTime(alert.expires_at)}</small> : null}
                      </td>
                      <td>
                        <span className={`pa-pill ${alert.status}`}>{alert.status}</span>
                        {muted ? <span className="pa-pill snoozed" title={`Until ${dateTime(alert.snoozed_until as string)}`}>snoozed</span> : null}
                        {alert.triggered_this_session ? <span className="pa-pill today">today</span> : null}
                      </td>
                      <td>
                        {alert.last_triggered_at ? dateTime(alert.last_triggered_at) : <span className="muted">—</span>}
                        {alert.trigger_count > 1 ? <small className="pa-sub">{alert.trigger_count} times</small> : null}
                      </td>
                      <td className="pa-actions">
                        {alert.status === "active" && alert.trigger !== "once" ? (
                          <button type="button" className="pa-icon-btn" disabled={busy} title={muted ? "Unsnooze" : "Snooze 1 hour"} aria-label={muted ? "Unsnooze alert" : "Snooze alert for 1 hour"}
                            onClick={() => void change(alert.id, { method: "PUT", body: JSON.stringify({ snooze_minutes: muted ? 0 : 60 }) })}>
                            {muted ? <BellRing size={13} /> : <BellOff size={13} />}
                          </button>
                        ) : null}
                        {alert.status === "active" || alert.status === "paused" ? (
                          <button type="button" className="pa-icon-btn" disabled={busy} title={alert.status === "active" ? "Pause" : "Resume"} aria-label={alert.status === "active" ? "Pause alert" : "Resume alert"}
                            onClick={() => void change(alert.id, { method: "PUT", body: JSON.stringify({ status: alert.status === "active" ? "paused" : "active" }) })}>
                            {alert.status === "active" ? <Pause size={13} /> : <Play size={13} />}
                          </button>
                        ) : (
                          <button type="button" className="pa-icon-btn" disabled={busy} title="Re-arm" aria-label="Re-arm alert"
                            onClick={() => void change(alert.id, { method: "PUT", body: JSON.stringify(alert.status === "expired" ? { status: "active", expires_at: null } : { status: "active" }) })}>
                            <RotateCcw size={13} />
                          </button>
                        )}
                        <button type="button" className={`pa-icon-btn${editingId === alert.id ? " on" : ""}`} disabled={busy} title="Edit" aria-label="Edit alert" aria-expanded={editingId === alert.id}
                          onClick={() => setEditingId((current) => (current === alert.id ? null : alert.id))}>
                          <Pencil size={13} />
                        </button>
                        <button type="button" className="pa-icon-btn danger" disabled={busy} title="Delete" aria-label="Delete alert"
                          onClick={() => { if (window.confirm(`Delete the ${alert.symbol} ${levelText(alert.level)} alert?`)) void change(alert.id, { method: "DELETE" }); }}>
                          <Trash2 size={13} />
                        </button>
                      </td>
                    </tr>
                    {editingId === alert.id ? (
                      <tr className="pa-edit-row">
                        <td colSpan={8}>
                          {/* Zero-width wrapper: the form wraps to the table width instead of widening it. */}
                          <div className="pa-edit-wrap">
                          <PriceAlertForm
                            initial={alert}
                            lastPrice={alert.last_price}
                            title={`Edit ${alert.symbol}`}
                            submitLabel="Save changes"
                            onSubmit={(payload) => saveEdit(alert.id, payload)}
                            onCancel={() => setEditingId(null)}
                          />
                          </div>
                        </td>
                      </tr>
                    ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel pa-panel">
        <div className="pa-panel-head">
          <h2>Trigger history</h2>
          <small className="pa-caption">saved · price move after +15m, +1h and at the day&apos;s cut-off</small>
        </div>
        {events.length ? (
          <ol className="pa-timeline">
            {events.map((event) => {
              const Icon = CONDITION_ICON[event.condition] ?? ArrowUpDown;
              return (
                <li key={event.id}>
                  <button type="button" onClick={() => onOpenChart(event.symbol, "5m")} title="Open the 5m chart (the trigger is marked)">
                    <time>{dateTime(event.ts)}</time>
                    <span className="pa-dot"><Zap size={11} /></span>
                    <span className="pa-event">
                      <strong>{event.symbol}</strong>
                      <span className={`pa-cond ${event.condition}`}><Icon size={12} /> {describeAlert(event)}</span>
                      {event.price != null ? <small>at {levelText(event.price)}</small> : null}
                      {event.note ? <small>· {event.note}</small> : null}
                      <span className="pa-outcomes">
                        {OUTCOME_KEYS.map(([key, label]) => <OutcomeChip key={key} label={label} outcome={event.outcomes?.[key]} />)}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="pa-empty">No triggers yet.</p>
        )}
      </section>
    </div>
  );
}
