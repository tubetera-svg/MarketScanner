"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUpRight, Database, Info, Plus, RefreshCw, SearchX, Timer } from "lucide-react";

type WatchSymbol = { symbol: string; session: string; asset_class?: string; scope?: string };
type WatchScope = "All" | "Nifty indexes" | "Nifty 50" | "Nifty Bank" | "Nifty IT" | "Nifty Auto" | "Nifty Pharma" | "F&O" | "Crypto" | "Commodities" | "Forex";
type Setup = {
  symbol: string;
  session: string;
  state: string;
  tier: string;
  price: number;
  poi_price: number | null;
  poi_type: string | null;
  entry: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  risk_reward: number | null;
  liquidity_swept: string | null;
  trade_confirmed: boolean;
};
type DateNote = { requested: string; resolved: string; reason: string | null };
type ScheduleStatus = {
  running: boolean;
  scanning: boolean;
  interval_minutes: number | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  run_count: number;
};
type StrategyFlag = { name: string; label: string; group: string; enabled: boolean; runnable: boolean; description?: string | null };
type StrategyRow = {
  symbol: string;
  profile?: string | null;
  note?: string | null;
  tradingview_link?: string | null;
  state?: string | null;
  direction?: number | null;
  entry?: number | null;
  sl?: number | null;
  target?: number | null;
  rr?: number | null;
  track_mode?: string | null;
  flip_level?: number | null;
  signal_date?: string | null;
  daily_bias?: string | null;
  weekly_bias?: string | null;
  monthly_bias?: string | null;
};
type TrackedEvent = { ts: string; date: string; state: string; note: string | null };
type TrackedSetup = {
  symbol: string;
  profile: string;
  week: string;
  state: string;
  direction: number | null;
  entry: number | null;
  sl: number | null;
  target: number | null;
  rr: number | null;
  track_mode: string | null;
  first_seen: string;
  triggered_date: string | null;
  last_seen: string;
  events: TrackedEvent[];
};
type TrackerAlert = {
  symbol: string;
  profile: string;
  week: string | null;
  kind: string;
  state: string;
  direction: number | null;
  entry: number | null;
  sl: number | null;
  target: number | null;
  rr: number | null;
};
type StrategyGroup = { strategy: string; label: string; total: number; bull_count: number; bear_count: number; bullish: StrategyRow[]; bearish: StrategyRow[] };
type StrategiesPayload = { strategies?: StrategyFlag[]; weekly_profiles_master_enabled?: boolean };

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const niftyIndexes = ["NSE:NIFTY", "NSE:BANKNIFTY", "NSE:FINNIFTY", "NSE:MIDCPNIFTY", "NSE:NIFTYNXT50", "NSE:INDIAVIX"];
const nifty50 = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ_AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"];
const sectorSymbols: Record<Exclude<WatchScope, "All" | "Nifty indexes" | "Nifty 50" | "Commodities" | "Forex" | "F&O" | "Crypto">, string[]> = {
  "Nifty Bank": ["AUBANK", "AXISBANK", "BANDHANBNK", "BANKBARODA", "CANBK", "FEDERALBNK", "HDFCBANK", "ICICIBANK", "INDUSINDBK", "KOTAKBANK", "PNB", "SBIN"],
  "Nifty IT": ["COFORGE", "HCLTECH", "INFY", "LTIM", "LTTS", "MPHASIS", "PERSISTENT", "TCS", "TECHM", "WIPRO"],
  "Nifty Auto": ["APOLLOTYRE", "ASHOKLEY", "BAJAJ_AUTO", "BHARATFORG", "EICHERMOT", "HEROMOTOCO", "M&M", "MARUTI", "TATAMOTORS", "TVSMOTOR"],
  "Nifty Pharma": ["ALKEM", "AUROPHARMA", "CIPLA", "DIVISLAB", "DRREDDY", "GLENMARK", "LUPIN", "SUNPHARMA", "TORNTPHARM", "ZYDUSLIFE"],
};

const WEEKLY_PROFILE_DAYS: Record<string, string> = {
  "classic_expansion_sweep": "Wed/Thu",
  "midweek_reversal_sweep": "Wed",
  "consolidation_reversal_sweep": "Thu",
  "intraweek_reversal_sweep": "Wed",
  "thursday_counter_sweep": "Thu",
  "tgif_setup_sweep": "Fri",
};

const baseSymbol = (symbol: string) => symbol.split(":").pop() ?? symbol;
const isCommodity = (symbol: string) => /(?:NATURALGAS|UKOIL|USOIL|XAUUSD|XAGUSD|COPPER|SILVER|GOLD)/i.test(symbol);
const matchesScope_check = (item: { symbol: string; session: string }, scope: WatchScope) => {
  const symbol = item.symbol.toUpperCase();
  const base = baseSymbol(symbol);
  if (scope === "All") return true;
  if (scope === "Forex") return item.session === "forex_24_5" && !isCommodity(symbol);
  if (scope === "Commodities") return item.session === "forex_24_5" && isCommodity(symbol);
  if (scope === "Crypto") return symbol.startsWith("CRYPTO:");
  if (scope === "F&O") return symbol.startsWith("NSE:");
  if (scope === "Nifty indexes") return niftyIndexes.includes(symbol);
  if (scope === "Nifty 50") return symbol.startsWith("NSE:") && nifty50.includes(base);
  return symbol.startsWith("NSE:") && sectorSymbols[scope].includes(base);
};

type Sentiment = "bull" | "bear" | "neutral";
const sentimentOf = (direction: number | null): Sentiment =>
  direction == null ? "neutral" : direction > 0 ? "bull" : "bear";
const SENTIMENT_LABEL: Record<Sentiment, string> = { bull: "BULL", bear: "BEAR", neutral: "—" };

const monthLabel = (ym: string) => {
  const [year, month] = ym.split("-").map(Number);
  if (!year || !month) return ym;
  return new Date(year, month - 1, 1).toLocaleDateString(undefined, { month: "short", year: "numeric" });
};

const biasBadge = (bias: string | null | undefined, label: string) => {
  if (!bias || bias === "Neutral") return <span className="bias-badge bias-neutral">{label}</span>;
  if (bias === "Bullish") return <span className="bias-badge bias-bull">{label}</span>;
  if (bias === "Bearish") return <span className="bias-badge bias-bear">{label}</span>;
  return <span className="bias-badge bias-neutral">{label}</span>;
};

const intervalOptions = [
  { value: 1, label: "Every 1 minute" },
  { value: 3, label: "Every 3 minutes" },
  { value: 5, label: "Every 5 minutes" },
  { value: 15, label: "Every 15 minutes" },
  { value: 30, label: "Every 30 minutes" },
  { value: 60, label: "Every 1 hour" },
  { value: 120, label: "Every 2 hours" },
  { value: 240, label: "Every 4 hours" },
  { value: 720, label: "Every 12 hours" },
  { value: 1440, label: "Daily (24h)" },
];

const formatCountdown = (totalSeconds: number) => {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
};

// Commodity inventory reports (EIA weekly releases) shown as a "news tile" with
// the next release date/time converted to IST and a live countdown. Times are
// the official ET release windows; DST is handled via the America/New_York
// timezone offset so IST (UTC+5:30) is always correct.
const INVENTORY_REPORTS: {
  key: string;
  label: string;
  detail: string;
  weekdayET: number; // 0=Sun..6=Sat
  hourET: number;
  minuteET: number;
  url: string;
}[] = [
  {
    key: "crude",
    label: "Crude Oil Inventories",
    detail: "EIA Petroleum Status Report",
    weekdayET: 3, // Wednesday
    hourET: 10,
    minuteET: 30,
    url: "https://in.investing.com/economic-calendar/crude-oil-inventories-75",
  },
  {
    key: "natgas",
    label: "Natural Gas Storage",
    detail: "EIA Weekly Gas Storage Report",
    weekdayET: 4, // Thursday
    hourET: 10,
    minuteET: 30,
    url: "https://in.investing.com/economic-calendar/natural-gas-storage-386",
  },
];

// Milliseconds that `timeZone` is ahead of UTC for a given instant (accounts for DST).
const tzOffsetMs = (instant: Date, timeZone: string): number => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(instant);
  const map: Record<string, number> = {};
  for (const part of parts) if (part.type !== "literal") map[part.type] = Number(part.value);
  const asUTC = Date.UTC(map.year, map.month - 1, map.day, map.hour === 24 ? 0 : map.hour, map.minute, map.second);
  return asUTC - instant.getTime();
};

// Next UTC instant (Date) for a release that occurs at hourET:minuteET on weekdayET
// in America/New_York, strictly after `now`.
const nextReleaseInstantET = (now: Date, weekdayET: number, hourET: number, minuteET: number): Date => {
  for (let i = 0; i < 14; i++) {
    const candidate = new Date(now.getTime() + i * 86400000);
    const etWall = new Date(candidate.getTime() - tzOffsetMs(candidate, "America/New_York"));
    if (etWall.getUTCDay() !== weekdayET) continue;
    const wallMs = Date.UTC(etWall.getUTCFullYear(), etWall.getUTCMonth(), etWall.getUTCDate(), hourET, minuteET, 0);
    const instant = wallMs - tzOffsetMs(new Date(wallMs), "America/New_York");
    if (instant > now.getTime()) return new Date(instant);
  }
  return new Date(now.getTime() + 7 * 86400000);
};

const formatIST = (instant: Date): string =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(instant) + " IST";

type AudioContextWindow = Window & { webkitAudioContext?: typeof AudioContext };
let audioContext: AudioContext | null = null;

const playTones = (frequencies: number[], toneSeconds: number, gapSeconds: number) => {
  const Context = window.AudioContext ?? (window as AudioContextWindow).webkitAudioContext;
  if (!Context) return;
  audioContext = audioContext ?? new Context();
  if (audioContext.state === "suspended") void audioContext.resume();
  const startAt = audioContext.currentTime + 0.05;
  frequencies.forEach((frequency, index) => {
    const context = audioContext as AudioContext;
    const start = startAt + index * (toneSeconds + gapSeconds);
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.3, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + toneSeconds);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(start + toneSeconds);
  });
};

// Browser-side replacement for the scanner's terminal Ring04.wav alert.
const playAlertSound = (urgent: boolean) => {
  try {
    if (urgent) playTones([988, 1319, 988, 1319], 0.16, 0.07);
    else playTones([784, 1047], 0.4, 0.15);
  } catch {
    // Audio is best-effort; never break scanning over it.
  }
};

export default function Home() {
  const [watchlist, setWatchlist] = useState<WatchSymbol[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [setups, setSetups] = useState<Setup[]>([]);
  const [scannedAt, setScannedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("Loading watchlist...");
  const [watchScope, setWatchScope] = useState<WatchScope>("Commodities");
  const [watchQuery, setWatchQuery] = useState("");
  const [newSymbol, setNewSymbol] = useState("");
  const [watchlistMessage, setWatchlistMessage] = useState("");
  const [showAddSymbol, setShowAddSymbol] = useState(false);
  const [anchorDate, setAnchorDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [dateNote, setDateNote] = useState<DateNote | null>(null);
  const [syncSummary, setSyncSummary] = useState<{
    anchor_date: string;
    lookback_days: number;
    results: { symbol: string; source: string; notes?: string[]; fetched_new?: number; rows?: number }[];
    synced: number;
    failed: number;
    gated: boolean;
  } | null>(null);
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null);
  const [intervalMinutes, setIntervalMinutes] = useState("15");
  const [countdown, setCountdown] = useState(0);
  const announcedScanRef = useRef<string | null>(null);
  const [markets, setMarkets] = useState<{ nse: boolean; forex_commodities: boolean } | null>(null);
  const [strategies, setStrategies] = useState<StrategyFlag[]>([]);
  const [weeklyMasterOn, setWeeklyMasterOn] = useState(true);
  const [strategyAnchorDate, setStrategyAnchorDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [strategyScanning, setStrategyScanning] = useState(false);
  const [strategyGroups, setStrategyGroups] = useState<StrategyGroup[]>([]);
  const [strategyDateNote, setStrategyDateNote] = useState<string | null>(null);
  const [trackerSetups, setTrackerSetups] = useState<TrackedSetup[]>([]);
  const [trackerAlerts, setTrackerAlerts] = useState<TrackerAlert[]>([]);
  const [trackerWatchlistOnly, setTrackerWatchlistOnly] = useState(true);
  const [trackerGroupBy, setTrackerGroupBy] = useState<"none" | "symbol" | "week" | "month">("none");
  const [inventoryNow, setInventoryNow] = useState(() => Date.now());

  const loadTracker = async (symbols?: string[]) => {
    try {
      const params = symbols && symbols.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : "";
      const response = await fetch(`${API}/api/weekly-profile-tracker${params}`, { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      setTrackerSetups(data.setups ?? []);
    } catch {
      /* best-effort */
    }
  };

  const loadResults = async () => {
    const response = await fetch(`${API}/api/results`, { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load scanner results");
    const data = await response.json();
    setSetups(data.results ?? []);
    setScannedAt(data.scanned_at ?? null);
    if (data.requested_date && data.resolved_date) setDateNote({ requested: data.requested_date, resolved: data.resolved_date, reason: data.resolution_reason });
  };

  const syncData = async () => {
    setLoading(true);
    setMessage("Synchronizing market data...");
    try {
      const response = await fetch(`${API}/api/market-data/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: selected,
          anchor_date: anchorDate,
          gate_market_hours: true,
          use_aliases: true,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Data sync failed");
      const results: { symbol: string; source: string; notes?: string[]; fetched_new?: number; rows?: number }[] = data.results ?? [];
      const synced = results.filter((row) => !row.notes?.some((note: string) => note.startsWith("sync failed"))).length;
      const failed = results.filter((row) => row.notes?.some((note: string) => note.startsWith("sync failed"))).length;
      const gated = results.some((row) => row.notes?.some((note: string) => note.includes("market still open") || note.includes("last completed session")));
      setSyncSummary({ anchor_date: data.anchor_date, lookback_days: data.lookback_days, results, synced, failed, gated });
      setMessage(
        `Sync complete — ${synced} ok, ${failed} failed` +
        `${gated ? " (some deferred: market still open)" : ""} · ${data.lookback_days}d window`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Data sync failed");
    } finally {
      setLoading(false);
    }
  };

  const refreshSchedule = async () => {
    const response = await fetch(`${API}/api/schedule`, { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load schedule");
    setSchedule(await response.json());
  };

  const startSchedule = async (minutes: number) => {
    try {
      const response = await fetch(`${API}/api/schedule/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interval_minutes: minutes, symbols: selected }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not start auto-scan");
      setSchedule(data);
      setMessage(`Auto-scan on — ${intervalOptions.find((option) => option.value === data.interval_minutes)?.label.toLowerCase() ?? `${data.interval_minutes} min`}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start auto-scan");
    }
  };

  const stopSchedule = async () => {
    try {
      const response = await fetch(`${API}/api/schedule/stop`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not stop auto-scan");
      setSchedule(data);
      setMessage("Auto-scan stopped");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not stop auto-scan");
    }
  };

  const changeInterval = (value: string) => {
    setIntervalMinutes(value);
    if (schedule?.running) startSchedule(Number(value));
  };

  const applyStrategies = (data: StrategiesPayload) => {
    if (data.strategies) setStrategies(data.strategies);
    if (typeof data.weekly_profiles_master_enabled === "boolean") setWeeklyMasterOn(data.weekly_profiles_master_enabled);
  };

  const toggleStrategy = async (flag: StrategyFlag) => {
    const nextEnabled = !flag.enabled;
    setStrategies((current) => current.map((item) => (item.name === flag.name ? { ...item, enabled: nextEnabled } : item)));
    try {
      const response = await fetch(`${API}/api/strategies/${flag.name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: nextEnabled }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not update strategy flag");
      applyStrategies(data);
      setMessage(`${flag.label} ${nextEnabled ? "ON" : "OFF"}`);
    } catch (error) {
      setStrategies((current) => current.map((item) => (item.name === flag.name ? { ...item, enabled: flag.enabled } : item)));
      setMessage(error instanceof Error ? error.message : "Could not update strategy flag");
    }
  };

  const setGroupStrategies = async (group: string, on: boolean) => {
    const names = strategies.filter((f) => f.group === group).map((f) => f.name);
    setStrategies((current) => current.map((item) => (names.includes(item.name) ? { ...item, enabled: on } : item)));
    try {
      for (const name of names) {
        const response = await fetch(`${API}/api/strategies/${name}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: on }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail ?? "Could not update strategy flag");
        applyStrategies(data);
      }
      setMessage(`${on ? "Enabled" : "Disabled"} ${names.length} ${group} strategies`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not update strategy flags");
    }
  };

  const runStrategyScan = async () => {
    setStrategyScanning(true);
    setMessage("Running strategy profiles...");
    try {
      const response = await fetch(`${API}/api/strategy-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: selected, anchor_date: strategyAnchorDate }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Strategy scan failed");
      const groups: StrategyGroup[] = data.results ?? [];
      setStrategyGroups(groups);
      const resolvedDate: string | undefined = data.resolved_date;
      if (resolvedDate) setStrategyAnchorDate(resolvedDate);
      const bulls = groups.reduce((sum, group) => sum + group.bull_count, 0);
      const bears = groups.reduce((sum, group) => sum + group.bear_count, 0);
      const alerts: TrackerAlert[] = data.tracker_alerts ?? [];
      setTrackerAlerts(alerts);
      const trig = alerts.filter((a) => a.kind === "triggered").length;
      const exits = alerts.filter((a) => a.kind === "closed_sl" || a.kind === "closed_target").length;
      setStrategyDateNote(`Testing date ${data.resolved_date ?? strategyAnchorDate}${data.resolution_reason ? ` (${data.resolution_reason})` : ""} · ${groups.length} strategies · ${bulls} bull / ${bears} bear matches`);
      setMessage(`Strategy scan complete - ${bulls} bullish, ${bears} bearish${trig ? ` - ${trig} new trigger(s)` : ""}${exits ? ` - ${exits} exit(s)` : ""}`);
      await loadTracker(selected);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Strategy scan failed");
    } finally {
      setStrategyScanning(false);
    }
  };

  const shiftStrategyDate = (days: number) => {
    if (strategyScanning) return;
    const current = new Date(strategyAnchorDate);
    if (Number.isNaN(current.getTime())) return;
    current.setUTCDate(current.getUTCDate() + days);
    const next = current.toISOString().slice(0, 10);
    setStrategyAnchorDate(next);
    setMessage(`Testing date shifted to ${next} (click Run scan to apply)`);
  };

  useEffect(() => {
    fetch(`${API}/api/strategies`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data: StrategiesPayload) => applyStrategies(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!trackerWatchlistOnly) loadTracker().catch(() => {});
  }, []);

  // Tick every second so the commodity inventory-report countdown stays live.
  useEffect(() => {
    const id = window.setInterval(() => setInventoryNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Compute the next release instant for a report, preferring a cached value
  // from localStorage when it is still valid for the current calendar day. The
  // cache is keyed by report key and stores {iso, dayKey}; we trust it for the
  // rest of the UTC day it was computed, then recompute the next day. This
  // avoids re-running the timezone math on every page reload while keeping the
  // result fresh (the value changes weekly anyway).
  const cachedReleaseInstant = (report: typeof INVENTORY_REPORTS[number]): Date => {
    if (typeof window === "undefined") return nextReleaseInstantET(new Date(), report.weekdayET, report.hourET, report.minuteET);
    const now = new Date();
    const todayKey = `${now.getUTCFullYear()}-${now.getUTCMonth()}-${now.getUTCDate()}`;
    try {
      const raw = window.localStorage.getItem("inventoryReleaseCache");
      if (raw) {
        const parsed = JSON.parse(raw) as Record<string, { iso: string; dayKey: string }>;
        const entry = parsed[report.key];
        if (entry && entry.dayKey === todayKey) {
          const ts = Date.parse(entry.iso);
          if (!Number.isNaN(ts) && ts > now.getTime()) return new Date(ts);
        }
      }
    } catch {
      // ignore cache read errors (private mode, quota, malformed JSON)
    }
    const fresh = nextReleaseInstantET(now, report.weekdayET, report.hourET, report.minuteET);
    try {
      const existing = window.localStorage.getItem("inventoryReleaseCache");
      const store: Record<string, { iso: string; dayKey: string }> = existing ? JSON.parse(existing) : {};
      store[report.key] = { iso: fresh.toISOString(), dayKey: todayKey };
      window.localStorage.setItem("inventoryReleaseCache", JSON.stringify(store));
    } catch {
      // ignore cache write errors
    }
    return fresh;
  };

  const inventoryReports = INVENTORY_REPORTS.map((report) => {
    const instant = cachedReleaseInstant(report);
    const seconds = Math.max(0, Math.round((instant.getTime() - inventoryNow) / 1000));
    // Highlight when the release is within 24h so it grabs attention.
    const soon = seconds <= 24 * 3600;
    const sameDay = instant.toDateString() === new Date(inventoryNow).toDateString();
    return { ...report, ist: formatIST(instant), soon, sameDay };
  });

  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/watchlist`).then((response) => response.json()),
      loadResults(),
      fetch(`${API}/api/schedule`, { cache: "no-store" }).then((response) => response.json()).catch(() => null),
    ]).then(([watchData, , scheduleData]) => {
      const symbols = watchData.symbols ?? [];
      setWatchlist(symbols);
      const initialScope = "Commodities" as WatchScope;
      const initialSymbols = symbols.filter((item: WatchSymbol) => matchesScope_check(item, initialScope)).map((item: WatchSymbol) => item.symbol);
      setSelected(initialSymbols);
      loadTracker(initialSymbols).catch(() => {});
      if (scheduleData) setSchedule(scheduleData);
      setMessage("Ready to scan");
    }).catch(() => setMessage("API unavailable. Start FastAPI on port 8000."));
  }, []);

  useEffect(() => {
    const loadMarkets = () => {
      fetch(`${API}/api/markets`, { cache: "no-store" }).then((response) => response.json()).then(setMarkets).catch(() => {});
    };
    loadMarkets();
    const id = window.setInterval(loadMarkets, 60000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!schedule?.running) {
      setCountdown(0);
      return;
    }
    if (!schedule.next_run_at) {
      // Scheduler started but its first slot isn't published yet (initial
      // scan still running) — poll gently until next_run_at appears.
      const id = window.setInterval(() => {
        Promise.allSettled([refreshSchedule(), loadResults()]);
      }, 2000);
      return () => window.clearInterval(id);
    }
    const target = new Date(schedule.next_run_at).getTime();
    let polling = false;
    const tick = () => {
      setCountdown(Math.max(0, Math.round((target - Date.now()) / 1000)));
      if (polling) return;
      polling = true;
      // Countdown expired: poll until the backend reports the next slot.
      // The fresh next_run_at restarts this countdown and loadResults() brings
      // in the new timestamp + setups, so long-running scans stay in sync.
      Promise.allSettled([refreshSchedule(), loadResults()]).finally(() => {
        polling = false;
      });
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule?.running, schedule?.next_run_at]);

  // Ring the UI alert when a fresh scan reports Tier A activity — mirrors the
  // old terminal winsound alert (double-chirp for Tier A + liquidity event).
  useEffect(() => {
    if (!scannedAt) return;
    if (announcedScanRef.current === null) {
      announcedScanRef.current = scannedAt;
      return;
    }
    if (scannedAt === announcedScanRef.current) return;
    announcedScanRef.current = scannedAt;
    const hasTierA = setups.some((setup) => setup.tier === "A");
    if (!hasTierA) return;
    const liquidityEvent = setups.some((setup) => setup.tier === "A" && setup.state.includes("liquidity"));
    playAlertSound(liquidityEvent);
  }, [scannedAt, setups]);

  const runScan = async () => {
    setLoading(true);
    setMessage("Scanning selected symbols...");
    try {
      const response = await fetch(`${API}/api/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: selected }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Scan failed");
      setSetups(data.results ?? []);
      setScannedAt(data.scanned_at ?? null);
      setMessage("Scan complete");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scan failed");
    } finally {
      setLoading(false);
    }
  };

  const addToWatchlist = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setWatchlistMessage("");
    try {
      const response = await fetch(`${API}/api/watchlist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: newSymbol }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not add symbol");
      const symbols = (data.symbols ?? []) as WatchSymbol[];
      setWatchlist(symbols);
      const added = symbols.find((entry) => entry.symbol === newSymbol.trim().toUpperCase());
      setSelected((current) => [...current, newSymbol.trim().toUpperCase()]);
      setNewSymbol("");
      setWatchlistMessage(added?.scope ? `Added · ${added.scope}` : "Added");
    } catch (error) {
      setWatchlistMessage(error instanceof Error ? error.message : "Could not add symbol");
    }
  };

  const filteredWatchlist = watchlist.filter((item) => matchesScope_check(item, watchScope) && item.symbol.toLowerCase().includes(watchQuery.toLowerCase()));
  const allVisibleSelected = filteredWatchlist.length > 0 && filteredWatchlist.every((item) => selected.includes(item.symbol));

  const groupKeyOf = (setup: TrackedSetup): string => {
    if (trackerGroupBy === "symbol") return setup.symbol;
    if (trackerGroupBy === "week") return setup.week;
    if (trackerGroupBy === "month") return setup.week.slice(0, 7);
    return "";
  };
  const trackerGroups = useMemo(() => {
    if (trackerGroupBy === "none" || trackerSetups.length === 0) return null;
    const map = new Map<string, { key: string; items: TrackedSetup[] }>();
    for (const setup of trackerSetups) {
      const key = groupKeyOf(setup);
      const bucket = map.get(key);
      if (bucket) bucket.items.push(setup);
      else map.set(key, { key, items: [setup] });
    }
    return Array.from(map.values())
      .map((group) => ({
        ...group,
        label: trackerGroupBy === "month" ? monthLabel(group.key) : group.key,
        bull: group.items.filter((item) => sentimentOf(item.direction) === "bull").length,
        bear: group.items.filter((item) => sentimentOf(item.direction) === "bear").length,
      }))
      .sort((a, b) =>
        trackerGroupBy === "week"
          ? b.key.localeCompare(a.key)
          : a.key.localeCompare(b.key),
      );
  }, [trackerSetups, trackerGroupBy]);

  const renderTrackerRow = (setup: TrackedSetup) => {
    const sentiment = sentimentOf(setup.direction);
    return (
      <div key={`${setup.symbol}|${setup.profile}|${setup.week}`} className={`tracker-row ${setup.state} sentiment-${sentiment}`}>
        <strong>{setup.symbol}</strong>
        <span className="profile">{setup.profile}</span>
        <span className={`badge ${sentiment === "bull" ? "bullish" : sentiment === "bear" ? "bearish" : "neutral"}`}>{SENTIMENT_LABEL[sentiment]}</span>
        <span className={`signal-state ${setup.state}`}>{setup.state}</span>
        {setup.entry != null && (
          <small>
            E {setup.entry}{setup.sl != null ? ` · SL ${setup.sl}` : ""}{setup.target != null ? ` · T ${setup.target}` : ""}{setup.rr != null ? ` · R:R ${setup.rr}` : ""}
          </small>
        )}
        {setup.track_mode && <span className="signal-track">{setup.track_mode === "live" ? "LIVE" : "EOD"}</span>}
        <small className="week">
          {setup.state === "triggered"
            ? (setup.triggered_date ?? setup.events?.find((e) => e.state === "triggered")?.date ?? setup.last_seen)
            : setup.last_seen}
          <span className="week-of"> · wk {setup.week}</span>
        </small>
      </div>
    );
  };

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Quant Lens</h1>
        </div>
        <div className="top-actions">
          <a className="top-link" href="/watchlist"><Database size={12} /> Database</a>
          <a className="top-link" href="/backtest"><Activity size={12} /> Backtest</a>

          {markets && (
            <>
              <span className={`market-chip ${markets.nse ? "open" : "closed"}`}>NSE {markets.nse ? "OPEN" : "CLOSED"}</span>
              <span className={`market-chip ${markets.forex_commodities ? "open" : "closed"}`}>FX · CMDTY {markets.forex_commodities ? "OPEN" : "CLOSED"}</span>
            </>
          )}
          <div className="status"><span className="pulse" />{message}</div>
        </div>
      </header>

      <div className="workspace">
        <aside className="controls panel">
          <div className="panel-heading"><span>Watchlist</span><div className="panel-heading-actions"><small>{selected.length}/{watchlist.length}</small><button className="add-toggle" type="button" aria-label="Add symbol to watchlist" title="Add symbol to watchlist" aria-expanded={showAddSymbol} onClick={() => { setShowAddSymbol((current) => !current); setWatchlistMessage(""); }}><Plus size={15} /></button></div></div>{showAddSymbol && <form className="add-watchlist" onSubmit={addToWatchlist}><input autoFocus aria-label="Add symbol to watchlist" placeholder="Add symbol, e.g. NSE:INFY" value={newSymbol} onChange={(event) => setNewSymbol(event.target.value)} /><button type="submit">Add</button>{watchlistMessage && <small className={watchlistMessage === "Added" ? "add-success" : "add-error"}>{watchlistMessage}</small>}</form>}<div className="watch-filter"><select aria-label="Filter watchlist" value={watchScope} onChange={(event) => { const nextScope = event.target.value as WatchScope; setWatchScope(nextScope); setSelected(watchlist.filter((item) => matchesScope_check(item, nextScope)).map((item) => item.symbol)); }}><option>All</option><option>Nifty indexes</option><option>Nifty 50</option><option>Nifty Bank</option><option>Nifty IT</option><option>Nifty Auto</option><option>Nifty Pharma</option><option>F&amp;O</option><option>Crypto</option><option>Commodities</option><option>Forex</option></select><input aria-label="Search watchlist" placeholder="Search symbol" value={watchQuery} onChange={(event) => setWatchQuery(event.target.value)} /><button type="button" aria-pressed={allVisibleSelected} onClick={() => setSelected((current) => { if (allVisibleSelected) { const visible = new Set(filteredWatchlist.map((item) => item.symbol)); return current.filter((symbol) => !visible.has(symbol)); } return Array.from(new Set([...current, ...filteredWatchlist.map((item) => item.symbol)])); })}>{allVisibleSelected ? "Unselect visible" : "Select visible"}</button></div><div className="check-list">{filteredWatchlist.map((item) => <label key={item.symbol} className="check-row"><input type="checkbox" checked={selected.includes(item.symbol)} onChange={() => setSelected((current) => current.includes(item.symbol) ? current.filter((symbol) => symbol !== item.symbol) : [...current, item.symbol])} /><span>{item.symbol}</span><small>{item.session === "crypto_24_7" ? "CRYPTO" : item.session === "forex_24_5" ? (isCommodity(item.symbol) ? "CMDTY" : "FX") : "NSE"}</small>{item.scope ? <span className="scope-tag">{item.scope}</span> : null}</label>)}{filteredWatchlist.length === 0 && <p className="filter-empty">No symbols in this filter.</p>}</div></aside>
        <main className="main-content">
<section className="scan-controls" style={{ justifyContent: "space-between" }}>
        <section className="auto-scan">
        <span className="auto-title"><Timer size={14} /> Auto-scan</span>
        <select id="auto-interval" aria-label="Auto-scan interval" value={intervalMinutes} onChange={(event) => changeInterval(event.target.value)}>
          {intervalOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        {schedule?.running ? (
          <>
            <button className="test-button stop" type="button" onClick={stopSchedule}>Stop auto-scan</button>
            <span className="auto-live"><span className="pulse" />{schedule.scanning ? "SCAN IN PROGRESS…" : `RUNNING · NEXT IN ${formatCountdown(countdown)}`}</span>
          </>
        ) : (
          <button className="test-button" type="button" onClick={() => startSchedule(Number(intervalMinutes))}>Start auto-scan</button>
        )}
        <button className="scan-now" type="button" onClick={runScan} disabled={loading || selected.length === 0}>
          <RefreshCw size={14} className={loading ? "spin" : undefined} />
          {loading ? "Scanning…" : "Run scan"}
        </button>
        {schedule?.running && <small className="auto-meta">{schedule.run_count} auto-scans this session{schedule.last_run_at ? ` · last at ${new Date(schedule.last_run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : ""}{selected.length > 0 ? ` · ${selected.length} selected symbols` : " · full watchlist"}</small>}
      </section>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end", flex: "1 1 320px", minWidth: 0 }}>
        <section className="date-test"><label htmlFor="anchor-date">Sync anchor date</label><input id="anchor-date" type="date" value={anchorDate} onChange={(event) => setAnchorDate(event.target.value)} /><button className="test-button" onClick={syncData} disabled={loading || selected.length === 0}>Sync</button></section>
        {dateNote && <p className="date-note">Testing date: {dateNote.requested}{dateNote.reason ? ` was unavailable (${dateNote.reason}); using ${dateNote.resolved}.` : ` using ${dateNote.resolved}.`}</p>}
        {syncSummary && (
          <div className="history-results">
            <p className="kicker">Sync summary · {syncSummary.anchor_date} · {syncSummary.lookback_days}d window</p>
            <span>{syncSummary.synced} synced{syncSummary.failed > 0 ? ` · ${syncSummary.failed} failed` : ""}{syncSummary.gated ? " · some deferred (market open)" : ""}</span>
            {syncSummary.results.filter((row) => row.notes?.some((note: string) => note.startsWith("sync failed") || note.includes("alias"))).map((row) => (
              <span key={row.symbol} className="sync-note"><strong>{row.symbol}</strong> {row.notes?.join("; ")}</span>
            ))}
          </div>
        )}
      </div>
      </section>
      <section className="panel strategy-panel">
        <div className="panel-heading">
          <span>Strategy profiles</span>
          <div className="panel-heading-actions">
            <small>{strategies.filter((flag) => flag.enabled).length}/{strategies.length} ON</small>
            <button className="test-button" type="button" onClick={runStrategyScan} disabled={strategyScanning || selected.length === 0}>
              <RefreshCw size={14} className={strategyScanning ? "spin" : undefined} />
              {strategyScanning ? "Scanning…" : "Run strategies"}
            </button>
          </div>
        </div>
          <div className="inventory-horizontal">
            {inventoryReports.map((report) => (
              <a
                key={report.key}
                href={report.url}
                target="_blank"
                rel="noreferrer"
                className={`inventory-chip${report.soon ? " soon" : ""}`}
                title={`Next ${report.label} release — ${report.url}`}
              >
                <span className="inventory-label">{report.label}</span>
                <span className="inventory-ist">{report.ist}</span>
                {report.soon && <span className={`inventory-flag${report.sameDay ? " today" : ""}`}>{report.sameDay ? "TODAY" : "SOON"}</span>}
              </a>
            ))}
          </div>

        {!weeklyMasterOn && <p className="date-note">WEEKLY_PROFILES_ENABLED is off in all_strategy.py — weekly profile chips stay locked until the master switch is turned on there.</p>}
        <div className="strategy-groups">
          <div className="strategy-group">
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <span className="filter-label" style={{ margin: 0 }}>Core</span>
              {(() => {
                const names = strategies.filter((f) => f.group === "Core").map((f) => f.name);
                const count = names.filter((n) => strategies.find((f) => f.name === n)?.enabled).length;
                const allOn = count === names.length;
                const partial = count > 0 && !allOn;
                return (
                  <button className="toggle-text" title={allOn ? "Turn all Core OFF" : "Turn all Core ON"} onClick={() => setGroupStrategies("Core", !allOn)}>
                    {allOn ? "Clear" : "Select all"}
                    {partial && <span className="day-marker" style={{ marginLeft: 4 }}>…</span>}
                  </button>
                );
              })()}
            </div>
            <div className="filters">
              {strategies.filter((flag) => flag.group === "Core").map((flag) => (
                <span key={flag.name} className="strategy-chip-wrap">
                  <button type="button" title={flag.name} className={flag.enabled ? "active" : ""} onClick={() => toggleStrategy(flag)}>
                    {flag.label}
                  </button>
                  {flag.description && (
                    <span className="info-trigger" aria-label={`Info: ${flag.label}`}>
                      <Info size={12} />
                      <span className="info-tooltip">{flag.description}</span>
                    </span>
                  )}
                </span>
              ))}
            </div>
          </div>
          <div className="strategy-group">
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
              <span className="filter-label" style={{ margin: 0 }}>Weekly profiles</span>
              {(() => {
                const names = strategies.filter((f) => f.group === "Weekly profiles").map((f) => f.name);
                const count = names.filter((n) => strategies.find((f) => f.name === n)?.enabled).length;
                const allOn = count === names.length;
                const partial = count > 0 && !allOn;
                return (
                  <button className="toggle-text" title={allOn ? "Turn all Weekly profiles OFF" : "Turn all Weekly profiles ON"} onClick={() => setGroupStrategies("Weekly profiles", !allOn)}>
                    {allOn ? "Clear" : "Select all"}
                    {partial && <span className="day-marker" style={{ marginLeft: 4 }}>…</span>}
                  </button>
                );
              })()}
            </div>
            <div className="filters">
              {strategies.filter((flag) => flag.group === "Weekly profiles").map((flag) => (
                <span key={flag.name} className="strategy-chip-wrap">
                  <button type="button" title={flag.runnable ? flag.name : `${flag.name} — blocked by the master switch`} disabled={!flag.runnable} className={flag.enabled ? "active" : ""} onClick={() => toggleStrategy(flag)}>
                    {flag.label}
                    {flag.group === "Weekly profiles" && WEEKLY_PROFILE_DAYS[flag.name] && (
                      <span className="day-marker" aria-label={`Requires weekdays: ${WEEKLY_PROFILE_DAYS[flag.name]}`}>{WEEKLY_PROFILE_DAYS[flag.name]}</span>
                    )}
                  </button>
                  {flag.description && (
                    <span className="info-trigger" aria-label={`Info: ${flag.label}`}>
                      <Info size={12} />
                      <span className="info-tooltip">{flag.description}</span>
                    </span>
                  )}
                </span>
              ))}
            </div>
          </div>
        </div>
        <small className="auto-meta">Click a chip to turn that strategy ON/OFF (saved to strategy_flags.json). Runs over {selected.length} selected watchlist symbols.</small>
        <div className="strategy-date">
          <label htmlFor="strategy-date">Testing date</label>
          <button
            className="date-arrow"
            type="button"
            aria-label="Previous day"
            onClick={() => shiftStrategyDate(-1)}
            disabled={strategyScanning}
          >
            <ArrowLeft size={14} />
          </button>
          <input id="strategy-date" type="date" value={strategyAnchorDate} onChange={(event) => setStrategyAnchorDate(event.target.value)} />
          <button
            className="date-arrow"
            type="button"
            aria-label="Next day"
            onClick={() => shiftStrategyDate(1)}
            disabled={strategyScanning}
          >
            <ArrowRight size={14} />
          </button>
        </div>
        {strategyDateNote && <p className="date-note">{strategyDateNote}</p>}
        {strategyGroups.length > 0 && (
          <div className="strategy-results">
            {strategyGroups.map((group) => (
              <details key={group.strategy} className="strategy-result" open>
                <summary>{group.label} <span style={{marginLeft: 'auto', display: 'inline-flex', gap: 6}}><span className="badge bullish">{group.bull_count} BULL</span><span className="badge bearish">{group.bear_count} BEAR</span></span></summary>
                {group.bull_count + group.bear_count > 0 ? (
                  <div className="signal-list">
                    {group.bullish.map((row) => (
                      <a key={`bull-${row.symbol}`} href={row.tradingview_link ?? "#"} target="_blank" rel="noreferrer" className="signal-chip bull">
                        <ArrowUpRight size={12} />
                        <strong>{row.symbol}</strong>
                        {(row.daily_bias || row.weekly_bias || row.monthly_bias) && (
                          <span className="bias-badges">
                            {biasBadge(row.monthly_bias, "M")}
                            {biasBadge(row.weekly_bias, "W")}
                            {biasBadge(row.daily_bias, "D")}
                          </span>
                        )}
                        {row.state && <span className={`signal-state ${row.state}`}>{row.state}</span>}
                        {row.entry != null && (
                          <small>
                            E {row.entry}{row.sl != null ? ` · SL ${row.sl}` : ""}{row.target != null ? ` · T ${row.target}` : ""}{row.rr != null ? ` · R:R ${row.rr}` : ""}
                          </small>
                        )}
                        {row.entry == null && row.note && (
                          <span className="note-tooltip-wrap">
                            <span className="note-tooltip-text">{row.note}</span>
                            <span className="note-tooltip-content">{row.note}</span>
                          </span>
                        )}
                        {(row.flip_level != null || row.signal_date) && (
                          <small>
                            {row.flip_level != null && `Lvl ${row.flip_level}`}
                            {row.signal_date ? ` · ${row.signal_date}` : ""}
                          </small>
                        )}
                        {row.track_mode && <span className="signal-track">{row.track_mode === "live" ? "LIVE" : "EOD"}</span>}
                      </a>
                    ))}
                    {group.bearish.map((row) => (
                      <a key={`bear-${row.symbol}`} href={row.tradingview_link ?? "#"} target="_blank" rel="noreferrer" className="signal-chip bear">
                        <ArrowDownRight size={12} />
                        <strong>{row.symbol}</strong>
                        {(row.daily_bias || row.weekly_bias || row.monthly_bias) && (
                          <span className="bias-badges">
                            {biasBadge(row.monthly_bias, "M")}
                            {biasBadge(row.weekly_bias, "W")}
                            {biasBadge(row.daily_bias, "D")}
                          </span>
                        )}
                        {row.state && <span className={`signal-state ${row.state}`}>{row.state}</span>}
                        {row.entry != null && (
                          <small>
                            E {row.entry}{row.sl != null ? ` · SL ${row.sl}` : ""}{row.target != null ? ` · T ${row.target}` : ""}{row.rr != null ? ` · R:R ${row.rr}` : ""}
                          </small>
                        )}
                        {row.entry == null && row.note && (
                          <span className="note-tooltip-wrap">
                            <span className="note-tooltip-text">{row.note}</span>
                            <span className="note-tooltip-content">{row.note}</span>
                          </span>
                        )}
                        {(row.flip_level != null || row.signal_date) && (
                          <small>
                            {row.flip_level != null && `Lvl ${row.flip_level}`}
                            {row.signal_date ? ` · ${row.signal_date}` : ""}
                          </small>
                        )}
                        {row.track_mode && <span className="signal-track">{row.track_mode === "live" ? "LIVE" : "EOD"}</span>}
                      </a>
                    ))}
                  </div>
                ) : (
                  <div className="empty small-empty"><SearchX size={14} /> No matches for this strategy.</div>
                )}
              </details>
            ))}
          </div>
        )}
      </section>
      <section className="panel tracker-panel">
        <p className="kicker">Cross-scan setup tracker</p>
        <h3>
          Weekly-profile setups
          <span className="tracker-info" aria-label="How to read the tracker">
            <Info size={13} />
            <span className="info-tooltip">
              Each row is a weekly-profile setup followed across scans.{"\n"}
              State: armed = forming (no trigger yet) · triggered = signal confirmed (in the trade) · closed_sl / closed_target = stop or target hit · invalidated = thesis failed · expired = week ended without triggering.{"\n"}
              E = planned entry (≈ confirmation close) · SL = invalidation extreme + buffer · T = opposite liquidity pool · R:R = target ÷ risk.{"\n"}
              LIVE = trackable intraday · EOD = NSE, confirm only after close.
            </span>
          </span>
        </h3>
        <div className="tracker-head-actions">
          <small className="auto-meta">
            {trackerSetups.filter((s) => s.state === "armed" || s.state === "triggered").length} active
            {" · "}
            {trackerSetups.length} tracked
          </small>
          <div className="tracker-groupby" role="group" aria-label="Group tracker results by">
            <span className="filter-label">Group</span>
            <div className="filters">
              {(["none", "symbol", "week", "month"] as const).map((option) => (
                <button key={option} type="button" className={trackerGroupBy === option ? "active" : ""} onClick={() => setTrackerGroupBy(option)}>
                  {option === "none" ? "Off" : option === "symbol" ? "Symbol" : option === "week" ? "Week" : "Month"}
                </button>
              ))}
            </div>
          </div>
          <button type="button" className="test-button" onClick={() => { const next = !trackerWatchlistOnly; setTrackerWatchlistOnly(next); loadTracker(next ? selected : []); }}>
            {trackerWatchlistOnly ? "Watchlist only" : "All setups"}
          </button>
        </div>
        {trackerAlerts.length > 0 && (
          <div className="tracker-alerts">
            {trackerAlerts.map((a, i) => (
              <span key={i} className={`alert ${a.kind}`}>
                {a.symbol} {a.kind.replaceAll("_", " ")}
                {a.direction ? ` (${a.direction > 0 ? "long" : "short"})` : ""}
              </span>
            ))}
          </div>
        )}
        <div className="tracker-list">
          {trackerSetups.length === 0 ? (
            <div className="empty small-empty"><SearchX size={14} /> No tracked setups yet — run a weekly-profile scan.</div>
          ) : trackerGroupBy === "none" ? (
            trackerSetups.map(renderTrackerRow)
          ) : (
            trackerGroups!.map((group) => (
              <div key={group.key} className="tracker-group">
                <div className="tracker-group-head">
                  <span className="tracker-group-label">{group.label}</span>
                  <span className="tracker-group-count">{group.items.length}</span>
                  {group.bull > 0 && <span className="badge bullish">{group.bull} BULL</span>}
                  {group.bear > 0 && <span className="badge bearish">{group.bear} BEAR</span>}
                </div>
                <div className="tracker-group-items">
                  {group.items.map(renderTrackerRow)}
                </div>
              </div>
            ))
          )}
        </div>
        </section>
        </main>
      </div>
      <footer>Rule-based analysis only. Validate signals before taking any trade.</footer>
    </main>
  );
}
