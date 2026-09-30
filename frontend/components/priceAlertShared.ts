// Shared price-alert types, labels and helpers (chart popup, Alerts page, notifier).
// Mirrors api/price_alerts.py.

export type AlertCondition =
  | "crosses_above" | "crosses_below" | "crosses"
  | "enters_zone" | "exits_zone"
  | "closes_above" | "closes_below" | "closes_inside";
export type AlertTimeframe = "5m" | "15m" | "1h" | "4h" | "1D";
export type AlertWindow = "always" | "nse_hours" | "ny_killzones" | "silver_bullet";

export type PriceAlert = {
  id: string;
  symbol: string;
  level: number;
  /** Other zone edge (zone conditions only). */
  level2?: number | null;
  condition: AlertCondition;
  timeframe?: AlertTimeframe | null;
  window?: AlertWindow | null;
  trigger: "once" | "once_per_day" | "every_check";
  cooldown_min: number;
  expires_at: string | null;
  snoozed_until?: string | null;
  note: string;
  status: "active" | "paused" | "triggered" | "expired";
  last_price: number | null;
  last_triggered_at: string | null;
  last_trigger_price?: number | null;
  trigger_count: number;
  /** Fired within the current daily bar (per Settings > Daily bar cut-offs). */
  triggered_this_session?: boolean;
  /** % from the last price to the nearest level (0 inside a zone). */
  distance_pct?: number | null;
};

export type AlertOutcome = { price: number; pct: number | null } | null;

export type PriceAlertEvent = {
  id: string;
  alert_id: string;
  ts: string;
  symbol: string;
  condition: AlertCondition;
  level: number;
  level2?: number | null;
  timeframe?: AlertTimeframe | null;
  trigger?: PriceAlert["trigger"];
  price: number | null;
  note: string;
  eod_at?: string;
  /** Price move after the trigger: +15m, +1h, end of market day (missing = pending). */
  outcomes?: Partial<Record<"15m" | "1h" | "eod", AlertOutcome>>;
};

export type PriceAlertStatus = {
  running: boolean;
  interval_minutes: number;
  near_pct?: number;
  push_enabled?: boolean;
  push_channels?: string[];
  last_push_error?: string | null;
  last_check_at: string | null;
  last_error: string | null;
  alerts: PriceAlert[];
  triggered_session_count: number;
  events: PriceAlertEvent[];
};

export const CONDITION_LABEL: Record<AlertCondition, string> = {
  crosses: "crosses",
  crosses_above: "crosses above",
  crosses_below: "crosses below",
  enters_zone: "enters zone",
  exits_zone: "exits zone",
  closes_above: "closes above",
  closes_below: "closes below",
  closes_inside: "closes inside zone",
};


export const isZoneCondition = (condition: AlertCondition) => condition === "enters_zone" || condition === "exits_zone" || condition === "closes_inside";
export const isCloseCondition = (condition: AlertCondition) => condition.startsWith("closes_");

export const TRIGGER_LABEL: Record<PriceAlert["trigger"], string> = {
  once: "Once",
  once_per_day: "Once per day",
  every_check: "Every cross",
};

export const WINDOW_LABEL: Record<AlertWindow, string> = {
  always: "Any time",
  nse_hours: "NSE hours (09:15–15:30 IST)",
  ny_killzones: "ICT kill zones (NY)",
  silver_bullet: "AM Silver Bullet (10–11 NY)",
};



/** "enters zone 100 – 102 · 1h close" style summary. */
export const describeAlert = (alert: Pick<PriceAlert, "condition" | "level" | "level2" | "timeframe">) => {
  const zone = isZoneCondition(alert.condition) && alert.level2 != null;
  const low = zone ? Math.min(alert.level, alert.level2 as number) : alert.level;
  const high = zone ? Math.max(alert.level, alert.level2 as number) : alert.level;
  // Keep the precision the level was set with (e.g. 552.694).
  const text = (value: number) => value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  const levels = zone ? `${text(low)} – ${text(high)}` : text(alert.level);
  const tf = isCloseCondition(alert.condition) && alert.timeframe ? ` · ${alert.timeframe} close` : "";
  return `${CONDITION_LABEL[alert.condition] ?? alert.condition} ${levels}${tf}`;
};

/** Backend symbol key: bare symbols are NSE. */
export const alertSymbol = (symbol: string) => {
  const value = symbol.trim().toUpperCase();
  return value.includes(":") ? value : `NSE:${value}`;
};
