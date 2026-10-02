// One shared GET /api/price-alerts poller for every consumer on the page
// (nav badge, global notifier, Alerts page list, chart-popup panel) instead of
// each polling on its own timer. Mutations call refreshPriceAlerts() so every
// subscriber sees the new state at once.

import type { PriceAlertStatus } from "./priceAlertShared";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const POLL_MS = 15000;

type Listener = (status: PriceAlertStatus | null, failed: boolean) => void;

const listeners = new Set<Listener>();
let latest: PriceAlertStatus | null = null;
let timer: number | null = null;
let requested = 0; // sequence of started requests
let applied = 0;   // sequence of the newest response applied (drops out-of-order replies)
let inFlight = 0;

export const refreshPriceAlerts = (): Promise<void> => {
  const seq = ++requested;
  inFlight += 1;
  return fetch(`${API}/api/price-alerts`, { cache: "no-store" })
    .then((response) => response.json())
    .then((data: PriceAlertStatus) => {
      if (seq < applied) return;
      applied = seq;
      latest = data;
      listeners.forEach((listener) => listener(data, false));
    })
    .catch(() => {
      if (seq < applied) return;
      listeners.forEach((listener) => listener(latest, true));
    })
    .finally(() => { inFlight -= 1; });
};

/**
 * Subscribe to alert status: replays the last result, fetches now (unless a
 * request is already in flight) and polls while anyone listens.
 */
export const subscribePriceAlerts = (listener: Listener): (() => void) => {
  listeners.add(listener);
  if (latest) listener(latest, false);
  if (timer === null) timer = window.setInterval(() => { void refreshPriceAlerts(); }, POLL_MS);
  if (!inFlight) void refreshPriceAlerts();
  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
};
