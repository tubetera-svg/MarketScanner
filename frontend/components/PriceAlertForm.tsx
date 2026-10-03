"use client";

import { useEffect, useState } from "react";
import { snapPrice } from "./OhlcChart";
import { formatDateTime, getDisplayTimezone, useDisplayTimezone } from "./time";
import {
  CONDITION_LABEL,
  TRIGGER_LABEL,
  WINDOW_LABEL,
  isCloseCondition,
  isZoneCondition,
  type AlertCondition,
  type AlertTimeframe,
  type AlertWindow,
  type PriceAlert,
} from "./priceAlertShared";

const CONDITION_GROUPS: [string, AlertCondition[]][] = [
  ["Line (any touch, 5m)", ["crosses", "crosses_above", "crosses_below"]],
  ["Zone (any touch, 5m)", ["enters_zone", "exits_zone"]],
  ["Bar close (confirmed)", ["closes_above", "closes_below", "closes_inside"]],
];
const TIMEFRAME_OPTIONS: AlertTimeframe[] = ["15m", "1h", "4h", "1D"];

/** Fields sent to POST/PUT /api/price-alerts (``expires_at`` omitted = keep). */
export type AlertPayload = {
  level: number;
  level2: number | null;
  condition: AlertCondition;
  timeframe: AlertTimeframe;
  trigger: PriceAlert["trigger"];
  cooldown_min: number;
  window: AlertWindow;
  expires_at?: string | null;
  note: string;
};

const priceText = (value: number | null | undefined, tick?: number | null) => (value == null ? "" : String(snapPrice(value, tick)));
const shortDate = (iso: string) => formatDateTime(iso, getDisplayTimezone());

/**
 * Alert fields as one form, used to create alerts (chart popup) and to edit
 * them (chart popup list and the Alerts page). ``onSubmit`` returns an error
 * message, or null on success.
 */
export default function PriceAlertForm({
  initial,
  tick,
  lastPrice,
  pickedLevel,
  submitLabel,
  title = "Alert when price",
  onSubmit,
  onCancel,
}: {
  /** Existing alert when editing; omitted when creating. */
  initial?: PriceAlert;
  tick?: number | null;
  lastPrice?: number | null;
  /** Level (or zone) picked on the chart; the nonce refills the same price. */
  pickedLevel?: { price: number; price2?: number | null; nonce: number } | null;
  submitLabel: string;
  title?: string;
  onSubmit: (payload: AlertPayload) => Promise<string | null>;
  onCancel?: () => void;
}) {
  useDisplayTimezone(); // re-render when the display zone changes
  const editing = Boolean(initial);
  const [level, setLevel] = useState(initial ? priceText(initial.level) : priceText(lastPrice, tick));
  const [level2, setLevel2] = useState(initial ? priceText(initial.level2) : "");
  const [condition, setCondition] = useState<AlertCondition>(initial?.condition ?? "crosses");
  const [timeframe, setTimeframe] = useState<AlertTimeframe>(initial?.timeframe && initial.timeframe !== "5m" ? initial.timeframe : "15m");
  const [trigger, setTrigger] = useState<PriceAlert["trigger"]>(initial?.trigger ?? "once");
  const [cooldown, setCooldown] = useState(initial?.cooldown_min || 60);
  const [windowName, setWindowName] = useState<AlertWindow>(initial?.window ?? "always");
  // "keep" leaves an existing expiry untouched when editing.
  const [expiry, setExpiry] = useState(initial?.expires_at ? "keep" : "");
  const [note, setNote] = useState(initial?.note ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const zone = isZoneCondition(condition);
  const closing = isCloseCondition(condition);

  useEffect(() => {
    if (!pickedLevel) return;
    if (pickedLevel.price2 != null) {
      // A drawn zone: switch to a zone condition unless one is already chosen.
      setLevel(priceText(Math.min(pickedLevel.price, pickedLevel.price2), tick));
      setLevel2(priceText(Math.max(pickedLevel.price, pickedLevel.price2), tick));
      setCondition((current) => (isZoneCondition(current) ? current : "enters_zone"));
    } else {
      setLevel(priceText(pickedLevel.price, tick));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickedLevel]);

  // Candles load after mount: prefill a new alert's level with the last close once.
  useEffect(() => {
    if (!editing && lastPrice != null) setLevel((current) => current || priceText(lastPrice, tick));
  }, [editing, lastPrice, tick]);

  const submit = async () => {
    const value = Number(level);
    const value2 = Number(level2);
    if (!level || !Number.isFinite(value) || value <= 0) {
      setError("Enter a price level");
      return;
    }
    if (zone && (!level2 || !Number.isFinite(value2) || value2 <= 0 || value2 === value)) {
      setError("Enter both zone edges (or Alt+drag on the chart)");
      return;
    }
    const payload: AlertPayload = {
      level: snapPrice(value, tick),
      level2: zone ? snapPrice(value2, tick) : null,
      condition,
      timeframe: closing ? timeframe : "5m",
      trigger,
      cooldown_min: trigger === "every_check" ? cooldown : 0,
      window: windowName,
      note,
    };
    if (expiry !== "keep") {
      payload.expires_at = !expiry ? null : expiry === "session_end" ? "session_end" : new Date(Date.now() + Number(expiry) * 86400000).toISOString();
    }
    setBusy(true);
    setError(null);
    const failure = await onSubmit(payload);
    setBusy(false);
    if (failure) setError(failure);
    else if (!editing) setNote("");
  };

  return (
    <div
      className={`chart-alerts-form${editing ? " editing" : ""}`}
      onKeyDown={(event) => {
        // Editing: Enter saves (from a field, not a button), Esc cancels.
        if (event.key === "Escape" && onCancel) {
          event.preventDefault();
          event.stopPropagation();
          onCancel();
        } else if (event.key === "Enter" && editing && !busy && (event.target as HTMLElement).tagName !== "BUTTON") {
          event.preventDefault();
          void submit();
        }
      }}
    >
      <span className="chart-alerts-title">{title}</span>
      <select value={condition} onChange={(event) => setCondition(event.target.value as AlertCondition)} aria-label="Condition">
        {CONDITION_GROUPS.map(([group, options]) => (
          <optgroup key={group} label={group}>
            {options.map((value) => <option key={value} value={value}>{CONDITION_LABEL[value]}</option>)}
          </optgroup>
        ))}
      </select>
      <input className="chart-alerts-level" type="number" step={tick ?? "any"} value={level} onChange={(event) => setLevel(event.target.value)} aria-label={zone ? "Zone edge" : "Price level"} autoFocus={editing} />
      {zone ? (
        <>
          <span className="muted">to</span>
          <input className="chart-alerts-level" type="number" step={tick ?? "any"} value={level2} onChange={(event) => setLevel2(event.target.value)} aria-label="Other zone edge" placeholder="other edge" />
        </>
      ) : null}
      {closing ? (
        <label title="Timeframe whose completed bar close is checked (1D closes at the market's daily cut-off)">
          on
          <select value={timeframe} onChange={(event) => setTimeframe(event.target.value as AlertTimeframe)}>
            {TIMEFRAME_OPTIONS.map((value) => <option key={value} value={value}>{value} close</option>)}
          </select>
        </label>
      ) : null}
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
      <label title="Only fire inside this window (ignored for 1D closes)">
        When
        <select value={windowName} onChange={(event) => setWindowName(event.target.value as AlertWindow)}>
          {Object.entries(WINDOW_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>
        Expires
        <select value={expiry} onChange={(event) => setExpiry(event.target.value)}>
          {initial?.expires_at ? <option value="keep">Keep ({shortDate(initial.expires_at)})</option> : null}
          <option value="">Never</option>
          <option value="session_end">End of day (cut-off)</option>
          <option value="1">1 day</option>
          <option value="7">1 week</option>
          <option value="30">1 month</option>
        </select>
      </label>
      <input className="chart-alerts-note" type="text" placeholder="Note (optional)" maxLength={200} value={note} onChange={(event) => setNote(event.target.value)} />
      <button type="button" className="chart-tool-btn primary" onClick={() => void submit()} disabled={busy} title={editing ? "Save (Enter)" : undefined}>{submitLabel}</button>
      {onCancel ? <button type="button" className="chart-tool-btn" onClick={onCancel} disabled={busy} title="Cancel (Esc)">Cancel</button> : null}
      {error ? <span className="chart-alerts-error">{error}</span> : null}
    </div>
  );
}
