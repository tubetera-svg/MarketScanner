"use client";

// Time contract (AGENTS.md "Time & Timezone Contract") — the ONLY place the UI
// formats instants or derives calendar dates. Never call toLocale*String,
// toISOString().slice(...), or Date#getHours/getDate/... on dates elsewhere.
//
// * Instants from the API are ISO-8601 with an offset (UTC). A string with no
//   offset is the legacy IST wall-time convention and is read as IST.
// * Trading-day dates ("YYYY-MM-DD") are never converted between zones.
// * Every displayed time uses the Settings > Display timezone (default IST).
// * Market logic (sessions, cut-offs, "today" for a market) uses fixed market
//   zones — see marketToday(); it never follows the display setting.

import { useEffect, useSyncExternalStore } from "react";
import { fetchAppSettings } from "./appSettings";

export const IST = "Asia/Kolkata";
export const NEW_YORK = "America/New_York";
export const UTC = "UTC";
export const BROWSER = "browser";
export const DEFAULT_DISPLAY_TIMEZONE = IST;

type Instant = string | number | Date;

// ------------------------------------------------------------ display zone store

const STORAGE_KEY = "display_timezone";
let current: string = DEFAULT_DISPLAY_TIMEZONE;
let loaded = false;
const listeners = new Set<() => void>();

try {
  const saved = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
  if (saved) current = saved;
} catch {
  /* storage unavailable: keep the default */
}

const notify = () => listeners.forEach((listener) => listener());

export const getDisplayTimezone = (): string => current;

/** Apply a new display zone everywhere in this tab (and other open tabs). */
export const setDisplayTimezone = (zone: string | null | undefined): void => {
  const next = zone || DEFAULT_DISPLAY_TIMEZONE;
  if (next === current) return;
  current = next;
  try { window.localStorage.setItem(STORAGE_KEY, next); } catch { /* ignore */ }
  notify();
};

export const subscribeDisplayTimezone = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY && event.newValue) setDisplayTimezone(event.newValue);
  });
}

/** Load the server setting once per page load (cheap: shared settings request). */
export const loadDisplayTimezone = (): void => {
  if (loaded) return;
  loaded = true;
  fetchAppSettings<{ settings?: { ui?: { display_timezone?: string } } }>()
    .then((payload) => setDisplayTimezone(payload.settings?.ui?.display_timezone))
    .catch(() => { loaded = false; });
};

/** Current display zone; re-renders the caller when Settings changes it. */
export const useDisplayTimezone = (): string => {
  useEffect(loadDisplayTimezone, []);
  return useSyncExternalStore(subscribeDisplayTimezone, getDisplayTimezone, () => DEFAULT_DISPLAY_TIMEZONE);
};

// ------------------------------------------------------------------ instants

const intlZone = (zone: string): string | undefined => (zone === BROWSER ? undefined : zone);

/** API value -> Date. Offset-less ISO strings are legacy IST wall time. */
export const toInstant = (value: Instant): Date => {
  if (value instanceof Date) return value;
  if (typeof value === "number") return new Date(value);
  const text = value.trim();
  const naiveDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text);
  return new Date(naiveDateTime ? `${text.replace(" ", "T")}+05:30` : text);
};

const format = (value: Instant, zone: string, opts: Intl.DateTimeFormatOptions): string => {
  const instant = toInstant(value);
  if (Number.isNaN(instant.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-GB", { timeZone: intlZone(zone), hour12: false, ...opts }).format(instant);
};

/** Short zone name for labels: "IST", "EDT", "BST", "UTC", ... */
export const zoneLabel = (zone: string, at: Instant = Date.now()): string => {
  if (zone === IST) return "IST";
  const part = new Intl.DateTimeFormat("en-US", { timeZone: intlZone(zone), timeZoneName: "short" })
    .formatToParts(toInstant(at))
    .find((item) => item.type === "timeZoneName");
  return part?.value ?? zone;
};

/** "14:30" */
export const formatTime = (value: Instant, zone: string): string =>
  format(value, zone, { hour: "2-digit", minute: "2-digit" });

/** "03 Oct 14:30" */
export const formatDateTime = (value: Instant, zone: string): string =>
  format(value, zone, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

/** "Fri 03 Oct, 14:30" */
export const formatDayDateTime = (value: Instant, zone: string): string =>
  format(value, zone, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

/** Any other display format, always in the given zone. */
export const formatInZone = (value: Instant, zone: string, opts: Intl.DateTimeFormatOptions): string =>
  format(value, zone, opts);

/** Milliseconds ``zone`` is ahead of UTC at ``instant`` (DST-aware). */
export const zoneOffsetMs = (value: Instant, zone: string): number => {
  const instant = toInstant(value);
  const map: Record<string, number> = {};
  for (const part of new Intl.DateTimeFormat("en-US", {
    timeZone: intlZone(zone), hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(instant)) if (part.type !== "literal") map[part.type] = Number(part.value);
  const asUTC = Date.UTC(map.year, map.month - 1, map.day, map.hour === 24 ? 0 : map.hour, map.minute, map.second);
  return asUTC - Math.floor(instant.getTime() / 1000) * 1000;
};

/** Wall-clock label "YYYY-MM-DDTHH:MM" of an instant in ``zone`` (chart axes). */
export const wallLabel = (value: Instant, zone: string): string => {
  const instant = toInstant(value);
  return new Date(instant.getTime() + zoneOffsetMs(instant, zone)).toISOString().slice(0, 16);
};

/** Next instant (ms) at hour:minute wall time in ``zone``, strictly after now. */
export const nextWallTime = (hour: number, minute: number, zone: string, now: number = Date.now()): number => {
  for (let day = 0; day < 3; day++) {
    const wall = new Date(now + zoneOffsetMs(now, zone) + day * 86_400_000);
    const wallMs = Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate(), hour, minute);
    const instant = wallMs - zoneOffsetMs(wallMs - zoneOffsetMs(wallMs, zone), zone);
    if (instant > now) return instant;
  }
  return now + 86_400_000;
};

// ------------------------------------------------------- calendar (trading) dates

/** Calendar date "YYYY-MM-DD" of an instant as seen in ``zone``. */
export const calendarDateIn = (value: Instant, zone: string): string =>
  new Intl.DateTimeFormat("en-CA", { timeZone: intlZone(zone) }).format(toInstant(value));

/** Today's date "YYYY-MM-DD" in a *market* zone (default IST/NSE), not the display zone. */
export const marketToday = (zone: string = IST, now: Instant = Date.now()): string => calendarDateIn(now, zone);

/** "YYYY-MM-DD" +/- whole days (pure calendar arithmetic, zone-free). */
export const addDays = (isoDate: string, days: number): string => {
  const date = new Date(`${isoDate.slice(0, 10)}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};

/** Day of week (0 = Sunday) of a "YYYY-MM-DD" date. */
export const weekdayOf = (isoDate: string): number => new Date(`${isoDate.slice(0, 10)}T00:00:00Z`).getUTCDay();

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-20" -> "20-Oct-2026" (NSE circular style; trading date, never zone-converted). */
export const formatTradingDate = (isoDate: string): string => {
  const [year, month, day] = isoDate.slice(0, 10).split("-");
  return `${day}-${MONTHS[Number(month) - 1] ?? month}-${year}`;
};

/** Whole calendar days from ``fromDate`` to ``toDate`` ("YYYY-MM-DD"). */
export const daysBetween = (fromDate: string, toDate: string): number =>
  Math.round((Date.parse(`${toDate.slice(0, 10)}T00:00:00Z`) - Date.parse(`${fromDate.slice(0, 10)}T00:00:00Z`)) / 86_400_000);
