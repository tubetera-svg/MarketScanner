// Shared GET /api/settings: callers within a few seconds of each other (page
// load: alert sounds, navigation, page) reuse one request instead of each
// fetching. Rejects on network/HTTP errors; a failed request is not reused.

import { apiFetch } from "./auth";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const REUSE_MS = 5000;

let cached: { at: number; promise: Promise<unknown> } | null = null;

export const fetchAppSettings = <T = unknown>(): Promise<T> => {
  if (cached && Date.now() - cached.at < REUSE_MS) return cached.promise as Promise<T>;
  const promise = apiFetch(`${API}/api/settings`, { cache: "no-store" }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
  const entry = { at: Date.now(), promise };
  cached = entry;
  promise.catch(() => { if (cached === entry) cached = null; });
  return promise as Promise<T>;
};
