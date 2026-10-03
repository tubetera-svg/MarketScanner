"use client";

// Admin sign-in / read-only guest (api/auth.py). The API decides the role:
// localhost is admin with no login; elsewhere a stored sign-in token makes the
// browser admin, otherwise it is a guest. `apiFetch` adds the token to every API
// call; `useAuth` gives pages the role and the guest rules (Settings -> Guest access).

import { useEffect, useSyncExternalStore } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const TOKEN_KEY = "ms.admin_token";

export type GuestPage = "scanner" | "alerts" | "ipo" | "watchlist" | "backtest";
export type GuestAccess = {
  guest_pages: GuestPage[];
  guest_max_symbols: number;
  guest_scan_cooldown_seconds: number;
  guest_past_dates: boolean;
  guest_extra_info: boolean;
  guest_banner: string;
  session_days: number;
};
export type AuthState = { role: "admin" | "guest"; local: boolean; password_set: boolean; access: GuestAccess };

// Storage can throw (private mode, blocked site data): treat as "no token".
const readToken = (): string | null => {
  try { return window.localStorage.getItem(TOKEN_KEY); } catch { return null; }
};
const writeToken = (token: string | null): void => {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch { /* the sign-in then lasts until the page reloads */ }
};
let memoryToken: string | null = null;
const currentToken = (): string | null => (typeof window === "undefined" ? null : readToken() ?? memoryToken);

/** fetch() for API calls: same arguments, plus the admin token when signed in. */
export const apiFetch = (input: string, init: RequestInit = {}): Promise<Response> => {
  const token = currentToken();
  if (!token) return fetch(input, init);
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
};

let state: AuthState | null = null;
let pending: Promise<void> | null = null;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((listener) => listener());

/** (Re)load the role from the API; shared by every caller on the page. */
export const refreshAuth = (): Promise<void> => {
  pending = apiFetch(`${API}/api/auth/me`, { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
    .then((next: AuthState) => { state = next; emit(); })
    .catch(() => { pending = null; });
  return pending;
};

/** Role + guest rules; null until loaded. Re-renders on sign-in/out. */
export const useAuth = (): AuthState | null => {
  useEffect(() => { if (!pending) void refreshAuth(); }, []);
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => state,
    () => null,
  );
};

/** True when the current role may do admin-only things (unknown yet = false). */
export const isAdmin = (auth: AuthState | null): boolean => auth?.role === "admin";

/** True when this role may open the page (Settings needs admin). */
export const canOpen = (auth: AuthState | null, page: GuestPage | "settings"): boolean =>
  !!auth && (auth.role === "admin" || (page !== "settings" && auth.access.guest_pages.includes(page)));

export const signIn = async (password: string): Promise<void> => {
  const response = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.detail ?? `Sign-in failed (HTTP ${response.status})`);
  memoryToken = body.token;
  writeToken(body.token);
  window.location.reload(); // every page and poller re-reads with admin rights
};

export const signOut = (): void => {
  memoryToken = null;
  writeToken(null);
  window.location.reload();
};

/** Keep the token after a password change from a signed-in browser. */
export const replaceToken = (token: string | null): void => {
  if (!token) return;
  memoryToken = token;
  writeToken(token);
};
