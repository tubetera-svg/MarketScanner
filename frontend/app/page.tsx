"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import TradingViewChartModal, { type ChartLevel, type ChartTarget } from "../components/TradingViewChartModal";
import { useStatusFlash } from "../components/useStatusFlash";
import { getSoundSettings, playAlertSound } from "../components/alertSound";
import { fetchAppSettings } from "../components/appSettings";
import Navigation from "../components/Navigation";
import { FavoriteStar, useFavorites } from "../components/Favorites";
import { Activity, AlertTriangle, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUpRight, CheckCircle2, ChevronDown, ChevronRight, History, Info, Play, Plus, Radio, RefreshCw, SearchX, Settings2, Square, Timer, Zap } from "lucide-react";

type WatchSymbol = { symbol: string; session: string; asset_class?: string; scope?: string; index?: string; f_and_o?: string };
type WatchScope = "All" | "Nifty indexes" | "Nifty 50" | "Nifty Bank" | "Nifty IT" | "Nifty Auto" | "Nifty Pharma" | "F&O" | "Crypto" | "Commodities" | "Forex" | string;
type AutoSyncMarket = { symbols: number; last_synced_session: string | null; latest_final_session: string | null; attempts: number; last_result: string | null };
type AutoSyncStatus = {
  running: boolean;
  syncing: boolean;
  tick_minutes: number;
  interval_hours: number;
  lookback_days: number;
  last_run_at: string | null;
  last_error: string | null;
  run_count: number;
  markets: Record<string, AutoSyncMarket>;
};
type SilverBulletSignal = {
  id: string;
  symbol: string;
  direction: "bullish" | "bearish";
  signal_time: string;
  range_high: number;
  range_low: number;
  entry: number;
  stop_loss: number;
  target: number;
  note: string;
};
type LtfSetup = {
  key: string;
  symbol: string;
  strategy: string;
  direction: number;
  zone_low: number;
  zone_high: number;
  invalidation: number;
  signal_date: string;
  valid_until: string;
  market: string;
  state: "armed" | "triggered" | "invalidated" | "expired";
  entry: number | null;
  sl: number | null;
  target: number | null;
  triggered_at: string | null;
  note: string;
};
type LtfStatus = {
  running: boolean;
  timeframe: string;
  armed_count: number;
  last_check_at: string | null;
  last_error: string | null;
  setups: LtfSetup[];
};
type SilverBulletStatus = {
  running: boolean;
  symbols: string[];
  signals: SilverBulletSignal[];
  last_check_at: string | null;
  next_check_at: string | null;
  last_error: string | null;
  run_count: number;
  scan_date: string | null;
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
  tag?: string | null;
  swing_level?: number | null;
  protected_level?: number | null;
  triggered_level?: number | null;
  order_block_midpoint?: number | null;
  flip_level?: number | null;
  signal_date?: string | null;
  ctx_bias_d?: string | null;
  ctx_bias_w?: string | null;
  ctx_bias_m?: string | null;
  ctx_bias_confluence?: number | null;
  ctx_adr?: number | null;
  ctx_adr_used_pct?: number | null;
  ctx_opposing_wick_pct?: number | null;
  ctx_wick_class?: string | null;
  ctx_candle_type?: string | null;
  ctx_next_day_bias?: string | null;
  ctx_cont_streak?: number | null;
  ctx_phase_change?: boolean | null;
  ctx_prev_wick_mid?: number | null;
  ctx_prev_wick_status?: string | null;
  ctx_prev_eq?: number | null;
  ctx_prev_eq_status?: string | null;
  ctx_pdh?: number | null;
  ctx_pdl?: number | null;
  ctx_pwh?: number | null;
  ctx_pwl?: number | null;
  ctx_pmh?: number | null;
  ctx_pml?: number | null;
  ctx_draw_above?: number | null;
  ctx_draw_below?: number | null;
};
type StrategyGroup = { strategy: string; label: string; total: number; bull_count: number; bear_count: number; bullish: StrategyRow[]; bearish: StrategyRow[]; has_live_data: boolean };
type StrategiesPayload = { strategies?: StrategyFlag[]; weekly_profiles_master_enabled?: boolean };

// Renders strategy_info.txt markdown (bullets, nested bullets, `code`) as readable tooltip content.
const renderInline = (text: string): ReactNode[] =>
  text.split(/(`[^`]+`)/g).filter(Boolean).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") && part.length > 2 ? <code key={index}>{part.slice(1, -1)}</code> : part.replace(/\*\*/g, ""),
  );
const renderInfoBody = (text: string): ReactNode => (
  <>
    {text.split(String.fromCharCode(10)).filter((line) => line.trim()).map((line, index) => {
      const bullet = /^(\s*)[-*]\s+(.*)$/.exec(line);
      if (!bullet) return <p key={index}>{renderInline(line.trim())}</p>;
      return <div key={index} className={`info-li${bullet[1].length >= 2 ? " nested" : ""}`}>{renderInline(bullet[2])}</div>;
    })}
  </>
);
const nyIsWeekend = () => ["Sat", "Sun"].includes(new Date().toLocaleDateString("en-US", { timeZone: "America/New_York", weekday: "short" }));
const localDate = (offsetDays = 0) => {
  // Local calendar date (not UTC): toISOString() lagged a day in IST before 05:30.
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
};

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const SYNC_BATCH_SIZE = 500;
type SyncResultRow = { symbol: string; source: string; notes?: string[]; fetched_new?: number; count?: number; missing_dates?: string[] };
const syncFailed = (row: SyncResultRow) => row.notes?.some((note) => note.startsWith("sync failed")) ?? false;
const groupSyncIssues = (results: SyncResultRow[]) => {
  const groups = new Map<string, string[]>();
  const add = (reason: string, symbol: string) => {
    const list = groups.get(reason) ?? [];
    list.push(symbol);
    groups.set(reason, list);
  };
  for (const row of results) {
    const symbolPattern = new RegExp(row.symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    const failedNotes = row.notes?.filter((note) => note.startsWith("sync failed")) ?? [];
    if (failedNotes.length > 0) {
      failedNotes.forEach((note) => add(note.replace(symbolPattern, "<sym>").slice(0, 160), row.symbol));
    } else if ((row.missing_dates?.length ?? 0) > 0) {
      add("missing dates", row.symbol);
    } else if (row.notes?.some((note) => note.includes("alias"))) {
      add("alias used", row.symbol);
    }
  }
  return Array.from(groups, ([reason, symbols]) => ({ reason, symbols })).sort((a, b) => b.symbols.length - a.symbols.length);
};
const apiErrorMessage = (detail: unknown, fallback: string) => {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        const error = item as { msg?: unknown; loc?: unknown };
        const location = Array.isArray(error.loc) ? error.loc.filter((part) => part !== "body").join(".") : "";
        const message = typeof error.msg === "string" ? error.msg : JSON.stringify(item);
        return location ? `${location}: ${message}` : message;
      }
      return String(item);
    }).filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  if (detail && typeof detail === "object") {
    const error = detail as { error?: unknown; message?: unknown };
    if (typeof error.error === "string" && error.error.trim()) return error.error;
    if (typeof error.message === "string" && error.message.trim()) return error.message;
    return JSON.stringify(detail);
  }
  return fallback;
};
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
const isCommodity = (symbol: string) => /(?:COMEX|NYMEX|CBOT|MCX|NATURALGAS|NATGAS|UKOIL|USOIL|XAUUSD|XAGUSD|COPPER|SILVER|GOLD|CRUDE|PLATINUM|PALLADIUM|WHEAT|CORN|SOYBEAN|COCOA|COFFEE|SUGAR|COTTON)/i.test(symbol);
// Saved "index" classification (comma-separated, e.g. "NIFTY 50, NIFTY BANK") also
// places a symbol in the matching Nifty pill, so the hard-coded lists can't drift.
const inSavedIndex = (item: { index?: string }, scope: WatchScope) =>
  (item.index ?? "").split(",").some((value) => value.trim().toUpperCase() === scope.toUpperCase());
const matchesScope_check = (item: { symbol: string; session: string; scope?: string; index?: string; f_and_o?: string }, scope: WatchScope, favorites?: Set<string>) => {
  const symbol = item.symbol.toUpperCase();
  const base = baseSymbol(symbol);
  if (scope === "All") return true;
  // Starred symbols (config/favorites.json), shared with the Watchlist and IPO pages.
  if (scope === "Favorites") return favorites?.has(symbol) ?? false;
  // F&O pill also covers F&O stocks kept under another scope (e.g. IPO).
  if (scope === "F&O" && item.f_and_o === "F&O") return true;
  if (scope === "Forex" || scope === "Commodities" || scope === "Crypto" || scope === "F&O" || scope === "Equity" || scope === "IPO" || item.scope === scope) return item.scope === scope;
  if (scope === "Nifty indexes") return niftyIndexes.includes(symbol) || symbol.startsWith("NSEIX:");
  if (inSavedIndex(item, scope)) return true;
  if (scope === "Nifty 50") return symbol.startsWith("NSE:") && nifty50.includes(base);
  return symbol.startsWith("NSE:") && (sectorSymbols[scope as keyof typeof sectorSymbols] ?? []).includes(base);
};
const matchesScopes_check = (item: { symbol: string; session: string; scope?: string; index?: string; f_and_o?: string }, scopes: WatchScope[], favorites?: Set<string>) =>
  scopes.length === 0 || scopes.includes("All") || scopes.some((scope) => matchesScope_check(item, scope, favorites));

type ExtraInfoFlags = { adr: boolean; wick: boolean; bias: boolean };
const EXTRA_INFO_OPTIONS: { key: keyof ExtraInfoFlags; label: string; help: string }[] = [
  {
    key: "adr",
    label: "ADR",
    help: "ADR % = signal bar's range as % of the average daily range of the prior 20 bars. Near/above 100%: the day already made a typical move, less room to expand. Low %: room to run. Hover a result for the ADR value.",
  },
  {
    key: "wick",
    label: "Wick",
    help: "Opposing wick as % of the signal bar's range (lower wick for bullish rows, upper for bearish). Small (<=25%): clean close, supports expansion. Large (>=50%): strong rejection, wait for the next candle. wick50: after a sweep-and-reject bar, whether price respected or closed through that wick's midpoint (closing through weakens the reversal).",
  },
  {
    key: "bias",
    label: "M/W/D bias",
    help: "Monthly / weekly / daily bias from closed bars only (an unfinished week or month is skipped, e.g. mid-week reads W-1 vs W-2). Bullish: closed above the previous bar's high, or swept its low and closed back inside. Green = bullish, red = bearish, grey = neutral. Strongest when all three agree with the signal.",
  },
];

const biasBadge = (bias: string | null | undefined, label: string) => {
  if (bias === "Bullish") return <span className="bias-badge bias-bull">{label}</span>;
  if (bias === "Bearish") return <span className="bias-badge bias-bear">{label}</span>;
  return <span className="bias-badge bias-neutral">{label}</span>;
};

// Commodity inventory reports (EIA weekly releases) shown as a "news tile" with
// the next release date/time converted to IST and a live countdown. Times are
// the official ET release windows; DST is handled via the America/New_York
// timezone offset so IST (UTC+5:30) is always correct.
const INVENTORY_REPORTS: {
  key: string;
  label: string;
  short: string; // compact chip label
  detail: string;
  weekdayET: number; // 0=Sun..6=Sat
  hourET: number;
  minuteET: number;
  url: string;
}[] = [
  {
    key: "crude",
    label: "Crude Oil Inventories",
    short: "Crude",
    detail: "EIA Petroleum Status Report",
    weekdayET: 3, // Wednesday
    hourET: 10,
    minuteET: 30,
    url: "https://in.investing.com/economic-calendar/crude-oil-inventories-75",
  },
  {
    key: "natgas",
    label: "Natural Gas Storage",
    short: "NatGas",
    detail: "EIA Weekly Gas Storage Report",
    weekdayET: 4, // Thursday
    hourET: 10,
    minuteET: 30,
    url: "https://in.investing.com/economic-calendar/natural-gas-storage-386",
  },
];

// ForexFactory high-impact news (GET /api/news/high-impact).
type NewsEvent = { title: string; currency: string; time_utc: string; forecast: string; previous: string };
type NewsFeed = { events: NewsEvent[]; currencies: string[]; fetched_at: string | null; stale: boolean; error: string | null };
const FF_CALENDAR_URL = "https://www.forexfactory.com/calendar";

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
    const etWall = new Date(candidate.getTime() + tzOffsetMs(candidate, "America/New_York"));
    if (etWall.getUTCDay() !== weekdayET) continue;
    const wallMs = Date.UTC(etWall.getUTCFullYear(), etWall.getUTCMonth(), etWall.getUTCDate(), hourET, minuteET, 0);
    const instant = wallMs - tzOffsetMs(new Date(wallMs), "America/New_York");
    if (instant > now.getTime()) return new Date(instant);
  }
  return new Date(now.getTime() + 7 * 86400000);
};

// Compact chip time: countdown within 24h ("in 3h 12m"), else IST "Wed 20:00".
const formatChipTime = (instant: Date, now: number): string => {
  const minutes = Math.max(0, Math.round((instant.getTime() - now) / 60000));
  if (minutes < 24 * 60) return minutes >= 60 ? `in ${Math.floor(minutes / 60)}h ${minutes % 60}m` : `in ${minutes}m`;
  return new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }).format(instant);
};
const formatISTParts = (instant: Date, opts: Intl.DateTimeFormatOptions): string =>
  new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour12: false, ...opts }).format(instant);

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

// Scanner setup levels handed to the chart popup (guides + "Watch setup"
// alerts). Read-only: strategy output is never changed here.
const setupLevels = (pairs: [string, number | null | undefined][]): ChartLevel[] => {
  const seen = new Set<number>();
  return pairs.flatMap(([label, price]) => {
    if (typeof price !== "number" || !Number.isFinite(price) || price <= 0 || seen.has(price)) return [];
    seen.add(price);
    return [{ label, price }];
  });
};

export default function Home() {
  const [watchlist, setWatchlist] = useState<WatchSymbol[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("Loading watchlist...");
  const [watchScopes, setWatchScopes] = useState<WatchScope[]>(["Commodities"]);
  const { favorites, isFavorite, toggle: toggleFavorite } = useFavorites();
  const [watchQuery, setWatchQuery] = useState("");
  const [newSymbol, setNewSymbol] = useState("");
  const [watchlistMessage, setWatchlistMessage] = useState("");
  const [showAddSymbol, setShowAddSymbol] = useState(false);
  const [syncStartDate, setSyncStartDate] = useState(() => localDate(-13));
  const [anchorDate, setAnchorDate] = useState(() => localDate());
  const [syncSummary, setSyncSummary] = useState<{
    anchor_date: string;
    start_date?: string | null;
    end_date?: string | null;
    lookback_days: number;
    results: SyncResultRow[];
    synced: number;
    failed: number;
    gated: boolean;
    fetchedNew: number;
    incomplete: number;
  } | null>(null);
  const [autoSync, setAutoSync] = useState<AutoSyncStatus | null>(null);
    const [silverBullet, setSilverBullet] = useState<SilverBulletStatus | null>(null);
  const [silverBulletLoading, setSilverBulletLoading] = useState(false);
  const [ltf, setLtf] = useState<LtfStatus | null>(null);
  const [ltfChecking, setLtfChecking] = useState(false);
  const announcedLtfRef = useRef<Set<string>>(new Set());
  const ltfSeededRef = useRef(false);
    const announcedSilverBulletRef = useRef<Set<string>>(new Set());
  const [markets, setMarkets] = useState<{ nse: boolean; forex_commodities: boolean } | null>(null);
  const [strategies, setStrategies] = useState<StrategyFlag[]>([]);
  const [weeklyMasterOn, setWeeklyMasterOn] = useState(true);
  const [protectedSwingTimeframe, setProtectedSwingTimeframe] = useState("daily");
  const [strategyAnchorDate, setStrategyAnchorDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [strategyScanning, setStrategyScanning] = useState(false);
  const [strategyGroups, setStrategyGroups] = useState<StrategyGroup[]>([]);
  const [strategyDateNote, setStrategyDateNote] = useState<string | null>(null);
  const [dateTransition, setDateTransition] = useState(false);
  const statusFlash = useStatusFlash(message);
  const [autoRunOnDateChange, setAutoRunOnDateChange] = useState(true);
  const [includeSilverBulletTests, setIncludeSilverBulletTests] = useState(false);
  // Extra info on strategy results. The server computes it only when at least
  // one is on; each checkbox then just shows/hides its part.
  const [extraInfo, setExtraInfo] = useState<ExtraInfoFlags>({ adr: true, wick: true, bias: true });
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem("strategyExtraInfo");
      if (raw === "0") setExtraInfo({ adr: false, wick: false, bias: false });
      else if (raw && raw !== "1") setExtraInfo((prev) => ({ ...prev, ...JSON.parse(raw) }));
    } catch {}
  }, []);
  const toggleExtraInfo = (key: keyof ExtraInfoFlags, value: boolean) => {
    setExtraInfo((prev) => {
      const next = { ...prev, [key]: value };
      try {
        window.localStorage.setItem("strategyExtraInfo", JSON.stringify(next));
      } catch {}
      return next;
    });
  };
  const includeExtraInfo = extraInfo.adr || extraInfo.wick || extraInfo.bias;
  const [strategyResultsGroupBy, setStrategyResultsGroupBy] = useState<"strategy" | "symbol">("strategy");
  const [activeSection, setActiveSection] = useState<"scan" | "alerts" | "strategies">("scan");
  const [scanProgress, setScanProgress] = useState<string | null>(null);
  const sectionRefs = useRef<Record<string, HTMLElement | null>>({
    scan: null,
    alerts: null,
    strategies: null,
  });

  // Keep the sticky section nav docked under the topbar even when the header wraps.
  useEffect(() => {
    const header = document.querySelector<HTMLElement>(".topbar");
    if (!header || typeof ResizeObserver === "undefined") return;
    const sync = () => document.documentElement.style.setProperty("--topbar-h", `${header.offsetHeight}px`);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(header);
    return () => observer.disconnect();
  }, []);

  const scrollToSection = (section: "scan" | "alerts" | "strategies") => {
    setActiveSection(section);
    sectionRefs.current[section]?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && entry.intersectionRatio > 0.3) {
            const id = entry.target.id;
            if (id in sectionRefs.current) {
              setActiveSection(id as "scan" | "alerts" | "strategies");
            }
          }
        }
      },
      { rootMargin: "-80px 0px -60% 0px", threshold: [0, 0.3, 0.5, 1] }
    );
    Object.values(sectionRefs.current).forEach((el) => el && observer.observe(el));
    return () => observer.disconnect();
  }, []);

  const [inventoryNow, setInventoryNow] = useState(() => Date.now());
  const [newsFeed, setNewsFeed] = useState<NewsFeed | null>(null);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsOpen, setNewsOpen] = useState<{ top: number; right: number } | null>(null);
  const [chart, setChart] = useState<ChartTarget | null>(null);
  const [activeTooltip, setActiveTooltip] = useState<string | null>(null);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [commandPaletteQuery, setCommandPaletteQuery] = useState("");
  const [commandPaletteActiveIndex, setCommandPaletteActiveIndex] = useState(0);
  const commandPaletteRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!activeTooltip) return;
    const onDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      const trigger = target.closest('.info-trigger');
      const tooltip = target.closest('.info-tooltip');
      if (!trigger && !tooltip) {
        setActiveTooltip(null);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [activeTooltip]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "k") {
        event.preventDefault();
        setCommandPaletteOpen((open) => !open);
        setCommandPaletteQuery("");
        setCommandPaletteActiveIndex(0);
      }
      if (event.key === "Escape") {
        setCommandPaletteOpen(false);
        setActiveTooltip(null);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (commandPaletteOpen) {
      document.body.style.overflow = "hidden";
      setTimeout(() => commandPaletteRef.current?.querySelector("input")?.focus(), 0);
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [commandPaletteOpen]);
  const openTradingViewChart = (event: React.MouseEvent<HTMLAnchorElement>, row: StrategyRow) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !row.tradingview_link) return;
    event.preventDefault();
    setChart({
      symbol: row.symbol,
      sourceLink: row.tradingview_link,
      levels: setupLevels([
        ["Entry", row.entry], ["SL", row.sl], ["Target", row.target], ["Swing", row.swing_level],
        ["Protected", row.protected_level], ["Triggered", row.triggered_level], ["OB mid", row.order_block_midpoint], ["Flip", row.flip_level],
      ]),
    });
  };

  const syncData = async () => {
    if (syncStartDate && anchorDate && syncStartDate > anchorDate) {
      setMessage("Sync start date must be on or before the end date");
      return;
    }
    setLoading(true);
    setMessage("Synchronizing market data...");
    try {
      const results: SyncResultRow[] = [];
      let summaryData: { anchor_date: string; start_date?: string | null; end_date?: string | null; lookback_days: number } | null = null;
      for (let batchStart = 0; batchStart < selected.length; batchStart += SYNC_BATCH_SIZE) {
        const symbols = selected.slice(batchStart, batchStart + SYNC_BATCH_SIZE);
        setMessage(`Synchronizing market data... ${Math.min(batchStart + symbols.length, selected.length)}/${selected.length}`);
        const response = await fetch(`${API}/api/market-data/sync`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbols,
            anchor_date: anchorDate,
            start_date: syncStartDate || undefined,
            end_date: anchorDate || undefined,
            gate_market_hours: true,
            use_aliases: true,
          }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(apiErrorMessage(data.detail, "Data sync failed"));
        summaryData ??= data;
        results.push(...(data.results ?? []));
      }
      const failed = results.filter(syncFailed).length;
      const synced = results.length - failed;
      const gated = results.some((row) => row.notes?.some((note: string) => note.includes("last completed session") || note.startsWith("Nothing to sync yet")));
      const fetchedNew = results.reduce((total, row) => total + (row.fetched_new ?? 0), 0);
      const incomplete = results.filter((row) => !syncFailed(row) && (row.missing_dates?.length ?? 0) > 0).length;
      setSyncSummary({ ...summaryData!, results, synced, failed, gated, fetchedNew, incomplete });
      const range = `${summaryData!.start_date ?? `${summaryData!.lookback_days}d lookback`} → ${summaryData!.end_date ?? summaryData!.anchor_date}`;
      setMessage(
        `Sync complete (${range}) — ${synced} ok, ${failed} failed, ${fetchedNew} new bars` +
        `${gated ? " (latest bar deferred until market cut-off)" : ""}`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Data sync failed");
    } finally {
      setLoading(false);
    }
  };

  const autoSyncTitle = autoSync
    ? [
        `Auto-sync ${autoSync.running ? "on" : "off"} — checks every ${autoSync.interval_hours} h (set in Settings); each market syncs once its daily bar is final (NSE 17:00 IST, forex/commodities 17:00 New York, crypto 00:00 UTC = 05:30 IST).`,
        ...Object.entries(autoSync.markets).map(([source, market]) =>
          `${source}: last synced ${market.last_synced_session ?? "—"}, latest final ${market.latest_final_session ?? "—"}${market.last_result ? ` (${market.last_result})` : ""}`),
        ...(autoSync.last_error ? [`Error: ${autoSync.last_error}`] : []),
      ].join("\n")
    : "Auto-sync status unavailable";

  const refreshAutoSync = async () => {
    const response = await fetch(`${API}/api/market-data/auto-sync`, { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load auto-sync status");
    const data: AutoSyncStatus = await response.json();
    setAutoSync(data);
    return data;
  };

  const toggleAutoSync = async () => {
    const enable = !autoSync?.running;
    try {
      // Persisted in app settings so it re-arms on API boot without a page open.
      const response = await fetch(`${API}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ automation: { data_auto_sync: { enabled: enable } } }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(apiErrorMessage(data.detail, "Could not change auto-sync"));
      setAutoSync(data.status.data_auto_sync);
      setMessage(enable ? "Auto-sync on — NSE after bhavcopy (17:00 IST), forex/commodities after 17:00 New York, crypto after 00:00 UTC" : "Auto-sync stopped");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not change auto-sync");
    }
  };

  const startSilverBullet = async () => {
    if (silverBulletLoading) return;
    setSilverBulletLoading(true);
    try {
      const commodities = selected.filter((symbol) => isCommodity(symbol));
      const response = await fetch(`${API}/api/silver-bullet/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: commodities }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not start Silver Bullet scanner");
      setSilverBullet(data);
      setMessage(commodities.length ? "AM Silver Bullet live scanner started" : "Select commodity symbols first");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start Silver Bullet scanner");
    } finally {
      setSilverBulletLoading(false);
    }
  };

  const stopSilverBullet = async () => {
    try {
      const response = await fetch(`${API}/api/silver-bullet/stop`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not stop Silver Bullet scanner");
      setSilverBullet(data);
      setMessage("AM Silver Bullet scanner stopped");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not stop Silver Bullet scanner");
    }
  };

  const testSilverBullet = async (dateOverride = strategyAnchorDate) => {
    if (silverBulletLoading) return;
    setSilverBulletLoading(true);
    try {
      const commodities = selected.filter((symbol) => isCommodity(symbol));
      const response = await fetch(`${API}/api/silver-bullet/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ anchor_date: dateOverride, symbols: commodities }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not test Silver Bullet date");
      setSilverBullet(data);
      const hits = Array.isArray(data?.signals) ? data.signals.length : 0;
      setMessage(
        hits
          ? `✓ Silver Bullet test complete for ${dateOverride} — ${hits} setup${hits === 1 ? "" : "s"} locked in`
          : `✓ Silver Bullet test complete for ${dateOverride} — no setups, market stayed quiet`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not test Silver Bullet date");
    } finally {
      setSilverBulletLoading(false);
    }
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

  const runStrategyScan = async (dateOverride = strategyAnchorDate) => {
    setStrategyScanning(true);
    const enabledStrategies = strategies.filter((f) => f.enabled && f.runnable).length;
    setScanProgress(`Scanning ${selected.length} symbol${selected.length !== 1 ? "s" : ""} across ${enabledStrategies} strateg${enabledStrategies !== 1 ? "ies" : "y"}...`);
    setMessage("Running strategy profiles...");
    try {
      const response = await fetch(`${API}/api/strategy-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: selected, anchor_date: dateOverride, timeframe: protectedSwingTimeframe, include_context: includeExtraInfo, include_bias: extraInfo.bias }),
      });
      const data = await response.json();
      if (response.status === 499) {
        setScanProgress(null);
        setMessage("Strategy scan stopped");
        return;
      }
      if (!response.ok) throw new Error(data.detail ?? "Strategy scan failed");
      const groups: StrategyGroup[] = data.results ?? [];
      setStrategyGroups(groups);
      const resolvedDate: string | undefined = data.resolved_date;
      if (resolvedDate) setStrategyAnchorDate(resolvedDate);
      const bulls = groups.reduce((sum, group) => sum + group.bull_count, 0);
      const bears = groups.reduce((sum, group) => sum + group.bear_count, 0);
      setStrategyDateNote(`Testing date ${data.resolved_date ?? dateOverride}${data.resolution_reason ? ` (${data.resolution_reason})` : ""} — ${groups.length} strategies — ${bulls} bull / ${bears} bear matches`);
      setMessage(`Strategy scan complete - ${bulls} bullish, ${bears} bearish`);
      setScanProgress(null);
    } catch (error) {
      setScanProgress(null);
      setMessage(error instanceof Error ? error.message : "Strategy scan failed");
    } finally {
      setStrategyScanning(false);
    }
  };

  const stopStrategyScan = async () => {
    setScanProgress("Stopping...");
    try {
      await fetch(`${API}/api/strategy-scan/cancel`, { method: "POST" });
    } catch {
      setMessage("Could not reach API to stop the scan");
    }
  };

  // While a scan runs: warn before reload/close, and if the user leaves anyway
  // tell the API to stop the scan instead of letting it finish in the background.
  useEffect(() => {
    if (!strategyScanning) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const cancelOnLeave = () => {
      fetch(`${API}/api/strategy-scan/cancel`, { method: "POST", keepalive: true }).catch(() => undefined);
    };
    window.addEventListener("beforeunload", warn);
    window.addEventListener("pagehide", cancelOnLeave);
    return () => {
      window.removeEventListener("beforeunload", warn);
      window.removeEventListener("pagehide", cancelOnLeave);
    };
  }, [strategyScanning]);

  const shiftStrategyDate = (days: number) => {
    if (strategyScanning) return;
    const current = new Date(strategyAnchorDate);
    if (Number.isNaN(current.getTime())) return;
    current.setUTCDate(current.getUTCDate() + days);
    const hasCrypto = selected.some((sym) => {
      const item = watchlist.find((w) => w.symbol === sym);
      return item?.asset_class === "crypto";
    });
    if (!hasCrypto) {
      while (current.getUTCDay() === 0 || current.getUTCDay() === 6) {
        current.setUTCDate(current.getUTCDate() + (days > 0 ? 1 : -1));
      }
    }
    const next = current.toISOString().slice(0, 10);
    if (next > localDate()) return;
    setDateTransition(true);
    window.setTimeout(() => setDateTransition(false), 520);
    setStrategyAnchorDate(next);
    if (autoRunOnDateChange) {
      runStrategyScan(next);
      if (includeSilverBulletTests && next < localDate()) testSilverBullet(next);
    } else {
      setMessage(`Testing date shifted to ${next} (click Run scan to apply)`);
    }
  };

  useEffect(() => {
    const onShortcutKeyDown = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.key.toLowerCase() === "r") {
        event.preventDefault();
        if (!event.repeat && !strategyScanning && selected.length > 0) runStrategyScan();
      } else if (event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (!event.repeat && strategyScanning) stopStrategyScan();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        shiftStrategyDate(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        shiftStrategyDate(1);
      }
    };
    document.addEventListener("keydown", onShortcutKeyDown);
    return () => document.removeEventListener("keydown", onShortcutKeyDown);
  });

  const handleStrategyDateChange = (next: string) => {
    if (!next || next > localDate()) return;
    setDateTransition(true);
    window.setTimeout(() => setDateTransition(false), 520);
    setStrategyAnchorDate(next);
    if (autoRunOnDateChange) {
      runStrategyScan(next);
      if (includeSilverBulletTests && next < localDate()) testSilverBullet(next);
    }
  };

  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/strategies`, { cache: "no-store" }).then((response) => response.json()),
      fetchAppSettings<{ settings?: { ui?: { hidden_strategies?: string[] } } }>().catch(() => null),
    ])
      .then(([data, cfg]: [StrategiesPayload, { settings?: { ui?: { hidden_strategies?: string[] } } } | null]) => {
        const hidden = new Set(cfg?.settings?.ui?.hidden_strategies ?? []);
        applyStrategies({ ...data, strategies: data.strategies?.filter((item) => !hidden.has(item.name)) });
      })
      .catch(() => {});
  }, []);

  // Countdown chips show whole minutes, so only publish a new "now" when the
  // wall-clock minute changes; returning the previous value skips the re-render.
  useEffect(() => {
    const id = window.setInterval(() => {
      setInventoryNow((prev) => {
        const now = Date.now();
        return Math.floor(now / 60000) === Math.floor(prev / 60000) ? prev : now;
      });
    }, 1000);
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

  // ForexFactory high-impact ("red") news. The API caches the feed once per IST
  // day; the refresh button forces a live fetch.
  const loadHighImpactNews = (refresh = false) => {
    setNewsLoading(true);
    fetch(`${API}/api/news/high-impact${refresh ? "?refresh=true" : ""}`)
      .then((response) => response.json())
      .then((data: NewsFeed) => setNewsFeed(data))
      .catch(() => setNewsFeed((current) => current ? { ...current, stale: true, error: "API unreachable" } : null))
      .finally(() => setNewsLoading(false));
  };
  useEffect(() => { loadHighImpactNews(); }, []);

  // Sound shortly before each high-impact news / EIA release (Settings →
  // Alert sounds → News event). Past events are never announced.
  const newsFeedRef = useRef<NewsFeed | null>(null);
  newsFeedRef.current = newsFeed;
  const announcedNewsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const check = () => {
      const { enabled, lead_minutes } = getSoundSettings().news_event;
      if (!enabled) return;
      const now = Date.now();
      const events = [
        ...(newsFeedRef.current?.events ?? []).map((event) => ({ key: `${event.currency}|${event.title}|${event.time_utc}`, at: Date.parse(event.time_utc) })),
        ...INVENTORY_REPORTS.map((report) => {
          const at = nextReleaseInstantET(new Date(now), report.weekdayET, report.hourET, report.minuteET).getTime();
          return { key: `EIA|${report.key}|${at}`, at };
        }),
      ];
      const due = events.filter((event) => event.at > now && event.at - now <= lead_minutes * 60_000 && !announcedNewsRef.current.has(event.key));
      due.forEach((event) => announcedNewsRef.current.add(event.key));
      if (due.length) playAlertSound("news_event");
    };
    check();
    const id = window.setInterval(check, 30000);
    return () => window.clearInterval(id);
  }, []);

  const upcomingNews = (newsFeed?.events ?? [])
    .map((event) => ({ ...event, instant: new Date(event.time_utc) }))
    .filter((event) => event.instant.getTime() > inventoryNow);
  // Same currency + same release time = one chip ("AUD CPI m/m +2").
  const nextNewsGroup = upcomingNews.length
    ? upcomingNews.filter((e) => e.time_utc === upcomingNews[0].time_utc && e.currency === upcomingNews[0].currency)
    : [];
  const nextNews = nextNewsGroup.length ? {
    label: `${nextNewsGroup[0].currency} ${nextNewsGroup[0].title}${nextNewsGroup.length > 1 ? ` +${nextNewsGroup.length - 1}` : ""}`,
    time: formatChipTime(nextNewsGroup[0].instant, inventoryNow),
    soon: nextNewsGroup[0].instant.getTime() - inventoryNow <= 24 * 3600 * 1000,
    tooltip: nextNewsGroup.map((e) => `${formatIST(e.instant)}  ${e.currency}  ${e.title}`).join("\n"),
  } : null;
  const newsByDay = upcomingNews.reduce<{ day: string; events: typeof upcomingNews }[]>((days, event) => {
    const day = formatISTParts(event.instant, { weekday: "short", day: "2-digit", month: "short" });
    const last = days[days.length - 1];
    if (last && last.day === day) last.events.push(event); else days.push({ day, events: [event] });
    return days;
  }, []);

  // News popover opens on hover of the News pill and stays open while the
  // pointer is over the popover; a short delay bridges the gap between them.
  const newsCloseTimer = useRef<number | null>(null);
  const openNews = (anchor: HTMLElement) => {
    if (newsCloseTimer.current) window.clearTimeout(newsCloseTimer.current);
    const rect = anchor.getBoundingClientRect();
    setNewsOpen({ top: rect.bottom + 6, right: document.documentElement.clientWidth - rect.right });
  };
  const keepNewsOpen = () => { if (newsCloseTimer.current) window.clearTimeout(newsCloseTimer.current); };
  const closeNewsSoon = () => {
    if (newsCloseTimer.current) window.clearTimeout(newsCloseTimer.current);
    newsCloseTimer.current = window.setTimeout(() => setNewsOpen(null), 200);
  };
  useEffect(() => {
    if (!newsOpen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setNewsOpen(null); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [newsOpen]);

  const inventoryReports = useMemo(() => INVENTORY_REPORTS.map((report) => {
    const instant = cachedReleaseInstant(report);
    const seconds = Math.max(0, Math.round((instant.getTime() - inventoryNow) / 1000));
    // Highlight when the release is within 24h so it grabs attention.
    const soon = seconds <= 24 * 3600;
    const sameDay = instant.toDateString() === new Date(inventoryNow).toDateString();
    return { ...report, ist: formatIST(instant), time: formatChipTime(instant, inventoryNow), soon, sameDay };
  }), [inventoryNow]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/watchlist`).then((response) => response.json()),
    ]).then(([watchData]) => {
      const symbols = watchData.symbols ?? [];
      setWatchlist(symbols);
      const initialScopes: WatchScope[] = ["Commodities"];
      const initialSymbols = symbols.filter((item: WatchSymbol) => matchesScopes_check(item, initialScopes)).map((item: WatchSymbol) => item.symbol);
      setSelected(initialSymbols);
      setMessage("Ready to scan");
    }).catch(() => setMessage("API unavailable. Start FastAPI on port 8000."));
  }, []);

  // Auto-sync runs server-side (armed on API boot from app settings); poll its status.
  useEffect(() => {
    refreshAutoSync().catch(() => {});
    const id = window.setInterval(() => { refreshAutoSync().catch(() => {}); }, 60000);
    return () => window.clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const loadMarkets = () => {
      fetch(`${API}/api/markets`, { cache: "no-store" }).then((response) => response.json()).then(setMarkets).catch(() => {});
    };
    loadMarkets();
    const id = window.setInterval(loadMarkets, 60000);
    return () => window.clearInterval(id);
  }, []);

  // Poll the AM Silver Bullet status while the page is open. The scan is armed
  // server-side inside 10:00-11:00 New York, so it can start or restart without
  // this tab noticing: polling only while `running` hid alerts until a reload.
  useEffect(() => {
    const poll = () => {
      fetch(`${API}/api/silver-bullet`, { cache: "no-store" })
        .then((response) => response.json())
        .then((data: SilverBulletStatus) => setSilverBullet(data))
        .catch(() => {});
    };
    poll();
    const id = window.setInterval(poll, 15000);
    return () => window.clearInterval(id);
  }, []);

  // Intraday (LTF) confirmation watcher: armed daily setups and their CISD
  // triggers. Runs server-side (Settings -> Intraday confirmation watcher).
  useEffect(() => {
    const poll = () => {
      fetch(`${API}/api/ltf-confirmation`, { cache: "no-store" })
        .then((response) => response.json())
        .then((data: LtfStatus) => setLtf(data))
        .catch(() => {});
    };
    poll();
    const id = window.setInterval(poll, 30000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!ltf) return;
    // The first poll only records what already triggered, so a page load is silent.
    const seeded = ltfSeededRef.current;
    ltfSeededRef.current = true;
    const fresh = ltf.setups.filter((setup) => setup.state === "triggered" && !announcedLtfRef.current.has(setup.key));
    fresh.forEach((setup) => announcedLtfRef.current.add(setup.key));
    // One sound per poll, however many setups triggered.
    if (seeded && fresh.length) playAlertSound("ltf");
  }, [ltf]);

  // Live triggers stay visible; armed setups and triggers whose window has
  // ended are collapsed so a large watchlist doesn't flood the panel.
  const ltfGroups = useMemo(() => {
    // valid_until is a market session date (ltf_confirmation.session_date):
    // NSE = IST calendar day; forex/commodities = NY day rolling at 17:00.
    const now = Date.now();
    const isoDay = (ms: number, timeZone: string) => new Intl.DateTimeFormat("en-CA", { timeZone }).format(ms);
    const sessionToday: Record<string, string> = {
      NSE: isoDay(now, "Asia/Kolkata"),
      FOREX: isoDay(now + 7 * 3600_000, "America/New_York"),
    };
    const windowOpen = (setup: LtfSetup) => setup.valid_until >= (sessionToday[setup.market] ?? sessionToday.FOREX);
    const byTrigger = (a: LtfSetup, b: LtfSetup) => (b.triggered_at ?? "").localeCompare(a.triggered_at ?? "");
    const triggered = (ltf?.setups ?? []).filter((setup) => setup.state === "triggered").sort(byTrigger);
    return {
      live: triggered.filter(windowOpen),
      earlier: triggered.filter((setup) => !windowOpen(setup)),
      armed: (ltf?.setups ?? []).filter((setup) => setup.state === "armed").sort((a, b) => a.symbol.localeCompare(b.symbol)),
    };
  }, [ltf]);

  // Whole chip opens the in-app chart popup (like the strategy profile chips);
  // modified/middle click still opens TradingView in a tab.
  const ltfChip = (setup: LtfSetup) => (
    <a
      key={setup.key}
      href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(setup.symbol)}`}
      rel="noreferrer"
      className={`signal-chip ${setup.direction > 0 ? "bull" : "bear"}`}
      title={setup.note}
      onClick={(event) => {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        setChart({
          symbol: setup.symbol,
          sourceLink: null,
          levels: [
            ...(setup.zone_low > 0 && setup.zone_high > 0 ? [{ label: "LTF zone", price: setup.zone_low, price2: setup.zone_high }] : []),
            ...setupLevels([["Invalidation", setup.invalidation], ["Entry", setup.entry], ["SL", setup.sl], ["Target", setup.target]]),
          ],
        });
      }}
    >
      {setup.direction > 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
      <strong>{setup.symbol}</strong>
      <small>
        {setup.strategy.replace(/_/g, " ")} · {setup.state === "triggered"
          ? `TRIGGERED ${setup.entry ?? ""} · SL ${setup.sl ?? ""}${setup.target != null ? ` · T ${setup.target}` : ""}`
          : `zone ${setup.zone_low}–${setup.zone_high} · until ${setup.valid_until}`}
      </small>
    </a>
  );

  const checkLtfNow = async () => {
    setLtfChecking(true);
    try {
      const response = await fetch(`${API}/api/ltf-confirmation/check`, { method: "POST" });
      if (response.ok) setLtf(await response.json());
    } catch {
      // status poll will retry
    } finally {
      setLtfChecking(false);
    }
  };

  useEffect(() => {
    const fresh = (silverBullet?.signals ?? []).filter((signal) => !announcedSilverBulletRef.current.has(signal.id));
    fresh.forEach((signal) => announcedSilverBulletRef.current.add(signal.id));
    if (fresh.length) playAlertSound("silver_bullet");
  }, [silverBullet?.signals]);

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
      // Only auto-select when the new symbol is visible under the active scope filter.
      if (added && matchesScopes_check(added, watchScopes, favorites)) {
        setSelected((current) => (current.includes(added.symbol) ? current : [...current, added.symbol]));
      }
      setNewSymbol("");
      setWatchlistMessage(added?.scope ? `Added — ${added.scope}` : "Added");
    } catch (error) {
      setWatchlistMessage(error instanceof Error ? error.message : "Could not add symbol");
    }
  };

  const filteredWatchlist = watchlist.filter((item) => matchesScopes_check(item, watchScopes, favorites) && item.symbol.toLowerCase().includes(watchQuery.toLowerCase()));
  const visibleWatchSymbols = new Set(filteredWatchlist.map((item) => item.symbol));
  const hiddenSelectedCount = selected.filter((symbol) => !visibleWatchSymbols.has(symbol)).length;


  const strategySymbolGroups = useMemo(() => {
    const map = new Map<string, { symbol: string; bull: number; bear: number; items: { strategy: string; label: string; side: "bull" | "bear"; row: StrategyRow }[] }>();
    for (const group of strategyGroups) {
      for (const [side, rows] of [["bull", group.bullish], ["bear", group.bearish]] as const) {
        for (const row of rows) {
          let bucket = map.get(row.symbol);
          if (!bucket) {
            bucket = { symbol: row.symbol, bull: 0, bear: 0, items: [] };
            map.set(row.symbol, bucket);
          }
          bucket[side] += 1;
          bucket.items.push({ strategy: group.strategy, label: group.label, side, row });
        }
      }
    }
    return Array.from(map.values()).sort((a, b) => b.items.length - a.items.length || a.symbol.localeCompare(b.symbol));
  }, [strategyGroups]);
  const renderSignalChip = (row: StrategyRow, side: "bull" | "bear", strategy: string, key: string, heading: string) => (
    <a key={key} href={row.tradingview_link ?? "#"} rel="noreferrer" className={`signal-chip ${side}`} onClick={(event) => openTradingViewChart(event, row)}>
      {side === "bull" ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
      <strong>{heading}</strong>
      {extraInfo.bias && (row.ctx_bias_d || row.ctx_bias_w || row.ctx_bias_m) && (
        <span className="bias-badges" title={`MTF bias (closed bars) M ${row.ctx_bias_m} / W ${row.ctx_bias_w} / D ${row.ctx_bias_d} — confluence ${row.ctx_bias_confluence ?? 0}`}>
          {biasBadge(row.ctx_bias_m, "M")}
          {biasBadge(row.ctx_bias_w, "W")}
          {biasBadge(row.ctx_bias_d, "D")}
        </span>
      )}
      {row.state && <span className={`signal-state ${row.state}`}>{row.state}</span>}
      {strategy !== "protected_swings" && row.entry != null && (
        (() => {
          const detail = "E " + row.entry + (row.sl != null ? ` — SL ${row.sl}` : "") + (row.target != null ? ` — T ${row.target}` : "") + (row.rr != null ? ` — R:R ${row.rr}` : "") + (row.tag ? ` — ${row.tag}` : "");
          return <small title={detail}>{detail}</small>;
        })()
      )}
      {strategy === "propulsion_blocks" && row.triggered_level != null && (
        <small title="PB is the propulsion candle open; OB mid is the order-block midpoint">
          PB={row.triggered_level.toFixed(2)}{row.order_block_midpoint != null ? ` — OB mid ${row.order_block_midpoint.toFixed(2)}` : ""}
        </small>
      )}
      {strategy === "protected_swings" && row.tag && (
        <small title={row.note ?? row.tag}>
          {row.tag === "fvg_based" ? "fvg" : "sweep"}={row.swing_level?.toFixed(2) ?? "-"}
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
          {row.signal_date ? `${row.flip_level != null ? " — " : ""}${row.signal_date}` : ""}
        </small>
      )}
      {(extraInfo.adr || extraInfo.wick) && row.ctx_candle_type && (() => {
        const lvl = (label: string, value?: number | null) => (value != null ? `${label} ${value}` : null);
        const parts = [
          extraInfo.adr && row.ctx_adr_used_pct != null ? `ADR ${Math.round(row.ctx_adr_used_pct)}%` : null,
          extraInfo.wick && row.ctx_opposing_wick_pct != null ? `wick ${Math.round(row.ctx_opposing_wick_pct)}% ${row.ctx_wick_class}` : null,
          row.ctx_candle_type.replace(/_/g, " "),
          row.ctx_cont_streak ? `streak ${row.ctx_cont_streak > 0 ? "+" : ""}${row.ctx_cont_streak}${row.ctx_phase_change ? " (phase change?)" : ""}` : null,
          extraInfo.wick && row.ctx_prev_wick_status ? `wick50 ${row.ctx_prev_wick_status.replace(/_/g, " ")}` : null,
          row.ctx_prev_eq_status ? `EQ ${row.ctx_prev_eq_status.replace(/_/g, " ")}` : null,
        ].filter(Boolean);
        const detail = [
          extraInfo.adr ? lvl("ADR", row.ctx_adr) : null, `next-day bias ${row.ctx_next_day_bias}`,
          extraInfo.wick ? lvl("wick 50%", row.ctx_prev_wick_mid) : null, lvl("prev EQ", row.ctx_prev_eq),
          lvl("PDH", row.ctx_pdh), lvl("PDL", row.ctx_pdl), lvl("PWH", row.ctx_pwh), lvl("PWL", row.ctx_pwl),
          lvl("PMH", row.ctx_pmh), lvl("PML", row.ctx_pml),
          lvl("draw above", row.ctx_draw_above), lvl("draw below", row.ctx_draw_below),
        ].filter(Boolean).join(" — ");
        return <small title={detail}>{parts.join(" · ")}</small>;
      })()}
      {row.track_mode && <span className="signal-track">{row.track_mode === "live" ? "LIVE" : "EOD"}</span>}
    </a>
  );

  const commandPaletteItems = useMemo(() => {
    const items: { id: string; label: string; category: string; action: () => void; keywords: string[] }[] = [];

    for (const item of watchlist) {
      items.push({
        id: `symbol:${item.symbol}`,
        label: item.symbol,
        category: "Watchlist",
        action: () => setChart({ symbol: item.symbol, sourceLink: null }),
        keywords: [item.symbol.toLowerCase(), item.session?.toLowerCase() ?? "", item.scope?.toLowerCase() ?? ""],
      });
    }

    for (const flag of strategies) {
      items.push({
        id: `strategy:${flag.name}`,
        label: flag.label,
        category: "Strategy",
        action: () => {
          const el = document.querySelector(`.strategy-chip-wrap button[title="${flag.name}"]`) as HTMLElement | null;
          el?.scrollIntoView({ behavior: "smooth", block: "center" });
          el?.focus();
        },
        keywords: [flag.label.toLowerCase(), flag.name.toLowerCase(), flag.group.toLowerCase()],
      });
    }

    items.push(
      { id: "action:strategies", label: "Run strategies", category: "Action", action: () => { if (!strategyScanning && selected.length > 0) runStrategyScan(); }, keywords: ["run", "strategies", "profiles"] },
      { id: "action:live", label: "Start live scan", category: "Action", action: () => { if (!silverBulletLoading) startSilverBullet(); }, keywords: ["start", "live", "silver", "bullet"] },
      { id: "action:sync", label: "Sync market data", category: "Action", action: () => { if (!loading && selected.length > 0) syncData(); }, keywords: ["sync", "market", "data"] }
    );

    return items;
  }, [watchlist, strategies, loading, selected.length, strategyScanning, silverBulletLoading]);

  const filteredCommandPaletteItems = useMemo(() => {
    if (!commandPaletteQuery.trim()) return commandPaletteItems;
    const query = commandPaletteQuery.toLowerCase().split(/\s+/).filter(Boolean);
    return commandPaletteItems.filter((item) =>
      query.every((q) =>
        item.label.toLowerCase().includes(q) ||
        item.category.toLowerCase().includes(q) ||
        item.keywords.some((k) => k.includes(q))
      )
    );
  }, [commandPaletteItems, commandPaletteQuery]);

  const handleCommandPaletteSelect = (item: typeof commandPaletteItems[0]) => {
    item.action();
    setCommandPaletteOpen(false);
    setCommandPaletteQuery("");
  };

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Quant Lens</h1>
        </div>
        <div className="top-actions">
          <Navigation active="/" />

          {markets && (
            <>
              <span className={`market-chip ${markets.nse ? "open" : "closed"}`}>NSE {markets.nse ? "OPEN" : "CLOSED"}</span>
              <span className={`market-chip ${markets.forex_commodities ? "open" : "closed"}`}>FX — CMDTY {markets.forex_commodities ? "OPEN" : "CLOSED"}</span>
            </>
          )}
          <div className={`status${statusFlash ? " status-flash" : ""}`}><span className="pulse" />{message}</div>
        </div>
      </header>

      <nav className="section-nav" role="navigation" aria-label="Section navigation">
        <div className="section-nav-inner">
          <button type="button" className={`section-nav-item${activeSection === "scan" ? " active" : ""}`} onClick={() => scrollToSection("scan")}><Activity size={13} /> Scan</button>
          <button type="button" className={`section-nav-item${activeSection === "alerts" ? " active" : ""}`} onClick={() => scrollToSection("alerts")}><Zap size={13} /> Alerts</button>
          <button type="button" className={`section-nav-item${activeSection === "strategies" ? " active" : ""}`} onClick={() => scrollToSection("strategies")}><Settings2 size={13} /> Strategies</button>
          <div className="section-nav-aside">
            <span className="section-nav-hint">Ctrl+K / ⌘K Command palette</span>
            <div className="inventory-horizontal" aria-label="Upcoming events">
              <span className="inventory-heading">Events</span>
              {inventoryReports.map((report) => (
                <a
                  key={report.key}
                  href={report.url}
                  target="_blank"
                  rel="noreferrer"
                  className={`inventory-chip${report.soon ? " soon" : ""}`}
                  title={`${report.label} — ${report.ist} IST`}
                >
                  <span className="inventory-label">{report.short}</span>
                  <span className="inventory-ist">{report.time}</span>
                </a>
              ))}
              {nextNews && (
                <a href={FF_CALENDAR_URL} target="_blank" rel="noreferrer" className={`inventory-chip news${nextNews.soon ? " soon" : ""}`} title={nextNews.tooltip}>
                  <span className="inventory-label news-label">{nextNews.label}</span>
                  <span className="inventory-ist">{nextNews.time}</span>
                </a>
              )}
              <span
                tabIndex={0}
                className={`news-toggle${newsFeed?.stale ? " stale" : ""}`}
                aria-expanded={!!newsOpen}
                aria-label="High-impact news this week"
                onMouseEnter={(event) => openNews(event.currentTarget)}
                onMouseLeave={closeNewsSoon}
                onFocus={(event) => openNews(event.currentTarget)}
                onBlur={closeNewsSoon}
              >
                News <span className="news-count">{upcomingNews.length}</span> <ChevronDown size={10} />
              </span>
            </div>
            {/* Portal to <body>: .section-nav's backdrop-filter would re-anchor position:fixed. */}
            {newsOpen && createPortal(
              <div className="news-popover" role="dialog" aria-label="High-impact news" style={{ top: newsOpen.top, right: newsOpen.right }} onMouseEnter={keepNewsOpen} onMouseLeave={closeNewsSoon} onFocus={keepNewsOpen} onBlur={closeNewsSoon}>
                <div className="news-popover-head">
                  <span>High-impact · {newsFeed?.currencies.length ? newsFeed.currencies.join(", ") : "all currencies"}</span>
                  <button type="button" className="news-refresh" onClick={() => loadHighImpactNews(true)} disabled={newsLoading} aria-label="Fetch live high-impact news" title="Fetch live from ForexFactory">
                    <RefreshCw size={11} className={newsLoading ? "spin" : undefined} />
                  </button>
                </div>
                {newsByDay.length === 0 && <p className="news-empty">No upcoming high-impact events this week.</p>}
                {newsByDay.map(({ day, events }) => (
                  <div key={day} className="news-day">
                    <div className="news-day-label">{day}</div>
                    {events.map((event) => (
                      <div key={`${event.currency}-${event.time_utc}-${event.title}`} className="news-row">
                        <span className="news-time">{formatISTParts(event.instant, { hour: "2-digit", minute: "2-digit" })}</span>
                        <span className="news-ccy">{event.currency}</span>
                        <span className="news-title" title={event.title}>{event.title}</span>
                        {(event.forecast || event.previous) && <span className="news-fp">{event.forecast || "–"} / {event.previous || "–"}</span>}
                      </div>
                    ))}
                  </div>
                ))}
                <div className="news-popover-foot">
                  <span>{newsFeed?.fetched_at ? `Fetched ${formatIST(new Date(newsFeed.fetched_at))} IST` : "Not fetched"}{newsFeed?.stale ? ` · stale (${newsFeed.error ?? "cached"})` : ""}</span>
                  <a href={FF_CALENDAR_URL} target="_blank" rel="noreferrer">ForexFactory ↗</a>
                </div>
              </div>
            , document.body)}
          </div>
        </div>
      </nav>

      <div className="workspace">
        <aside className="controls panel">
          <div className="panel-heading"><span>Watchlist</span><div className="panel-heading-actions"><small title={hiddenSelectedCount ? `${hiddenSelectedCount} selected symbol(s) are hidden by the current search` : undefined}>{selected.length}/{watchlist.length}{hiddenSelectedCount ? ` (${hiddenSelectedCount} hidden)` : ""}</small><button className="add-toggle" type="button" aria-label="Add symbol to watchlist" title="Add symbol to watchlist" aria-expanded={showAddSymbol} onClick={() => { setShowAddSymbol((current) => !current); setWatchlistMessage(""); }}><Plus size={15} /></button></div></div>{showAddSymbol && <form className="add-watchlist" onSubmit={addToWatchlist}><input autoFocus aria-label="Add symbol to watchlist" placeholder="Add symbol, e.g. NSE:INFY" value={newSymbol} onChange={(event) => setNewSymbol(event.target.value)} /><button type="submit">Add</button>{watchlistMessage && <small className={watchlistMessage.startsWith("Added") ? "add-success" : "add-error"}>{watchlistMessage}</small>}</form>}<div className="watch-filter"><div className="watch-pills" role="group" aria-label="Filter watchlist"><button type="button" className={`watch-pill${watchScopes.includes("All") ? " active" : ""}`} onClick={() => { setWatchScopes(["All"]); setSelected(watchlist.map((item) => item.symbol)); }}>All</button>{(["Favorites","IPO","Nifty indexes","Nifty 50","Nifty Bank","Nifty IT","Nifty Auto","Nifty Pharma","F&O","Equity","Crypto","Commodities","Forex"] as WatchScope[]).map((opt) => (<button key={opt} type="button" className={`watch-pill${watchScopes.includes(opt) ? " active" : ""}`} onClick={() => { const newScopes = watchScopes.includes(opt) ? watchScopes.filter((s) => s !== opt) : [...watchScopes.filter((s) => s !== "All"), opt]; setWatchScopes(newScopes); const newFiltered = watchlist.filter((item) => matchesScopes_check(item, newScopes, favorites) && item.symbol.toLowerCase().includes(watchQuery.toLowerCase())); setSelected(newFiltered.map((item) => item.symbol)); }}>{opt === "Favorites" ? "★ Favorites" : opt}</button>))}</div><input aria-label="Search watchlist" placeholder="Search symbol" value={watchQuery} onChange={(event) => setWatchQuery(event.target.value)} /></div><div className="check-list">{filteredWatchlist.map((item) => <label key={item.symbol} className="check-row"><input type="checkbox" value={item.symbol} checked={selected.includes(item.symbol)} onChange={() => setSelected((current) => current.includes(item.symbol) ? current.filter((symbol) => symbol !== item.symbol) : [...current, item.symbol])} /><span className="check-symbol">{item.symbol}<FavoriteStar symbol={item.symbol} active={isFavorite(item.symbol)} onToggle={() => void toggleFavorite(item.symbol)} /></span><small>{item.session === "crypto_24_7" ? "CRYPTO" : item.session === "forex_24_5" ? (isCommodity(item.symbol) ? "CMDTY" : "FX") : item.symbol.toUpperCase().startsWith("NSEIX:") ? "NSEIX" : "NSE"}</small>{item.scope ? <span className="scope-tag">{item.scope}</span> : null}</label>)}{filteredWatchlist.length === 0 && <p className="filter-empty">No symbols in this filter.</p>}</div></aside>
        <main className="main-content">
<section className="scan-controls" style={{ justifyContent: "space-between" }} id="scan" ref={(el) => { sectionRefs.current.scan = el; }}>
        <section className="auto-scan">
        <section className="date-test"><label htmlFor="sync-start-date">Sync range</label><input id="sync-start-date" aria-label="Sync start date" type="date" value={syncStartDate} max={anchorDate || localDate()} onChange={(event) => setSyncStartDate(event.target.value)} /><span aria-hidden="true">to</span><input id="anchor-date" aria-label="Sync end date" type="date" value={anchorDate} min={syncStartDate || undefined} max={localDate()} onChange={(event) => setAnchorDate(event.target.value)} /><button className="test-button button-secondary" onClick={syncData} disabled={loading || selected.length === 0 || (!!syncStartDate && syncStartDate > anchorDate)}>Sync</button><button className={`test-button ${autoSync?.running ? "" : "button-secondary"}`} onClick={toggleAutoSync} aria-pressed={!!autoSync?.running} title={autoSyncTitle}>{autoSync?.syncing ? "Auto: syncing…" : autoSync?.running ? "Auto: on" : "Auto: off"}</button></section>
      </section>
              <section className="auto-scan" aria-label="AM Silver Bullet live scanner">
                <span className="auto-title info-title"><Timer size={14} /> AM Silver Bullet
                  <span className="info-trigger" aria-label="Info: AM Silver Bullet" role="button" tabIndex={0} onClick={() => setActiveTooltip(activeTooltip === "silver_bullet" ? null : "silver_bullet")} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTooltip(activeTooltip === "silver_bullet" ? null : "silver_bullet"); } }}>
                    <Info size={12} />
                    <span className={`info-tooltip rich${activeTooltip === "silver_bullet" ? " open" : ""}`}><strong className="info-heading">AM Silver Bullet</strong>{renderInfoBody([
                      "- Commodities only, 5-minute bars, New York time (10:00–11:00 NY = 19:30–20:30 IST in US summer, 20:30–21:30 IST in winter).",
                      "- Range = high and low of the 09:00–10:00 NY hour.",
                      "- Between 10:00 and 11:00 NY, price sweeps the range low (bullish) or range high (bearish).",
                      "- Confirms on the first FVG whose middle candle is the sweep bar or later, with the third candle closing back inside the range.",
                      "- Entry = FVG edge nearest price; stop-loss = swing extreme since the sweep; target = opposite side of the range.",
                      "- No setup if one bar sweeps both sides, or the target side is hit before confirmation.",
                      "- Uses closed bars only, so a shown signal never changes; one setup per symbol per session.",
                    ].join(String.fromCharCode(10)))}</span>
                  </span>
                </span>
                {silverBullet?.running ? (
                  <>
                    <button className="test-button stop button-secondary" type="button" onClick={stopSilverBullet}><Square size={12} /> Stop live scan</button>
                    <span className="auto-live"><span className="pulse" />{silverBullet.signals.length ? `${silverBullet.signals.length} alert(s)` : "WATCHING 10:00—11:00 NY"}</span>
                  </>
                ) : (
                  <>
                    <button className="test-button button-primary" type="button" onClick={startSilverBullet} disabled={silverBulletLoading}>
                      {silverBulletLoading ? <RefreshCw size={12} className="spin" /> : <Play size={12} />}
                      {silverBulletLoading ? "Loading—" : "Start live scan"}
                    </button>
                    <button className="test-button button-secondary" type="button" onClick={() => testSilverBullet()} disabled={silverBulletLoading}>
                      {silverBulletLoading ? <RefreshCw size={12} className="spin" /> : <CheckCircle2 size={12} />}
                      {silverBulletLoading ? "Loading—" : `Test ${strategyAnchorDate}`}
                    </button>
                  </>
                )}
                {!silverBullet?.running && nyIsWeekend() && <small className="auto-meta silver-bullet-note">Weekend — commodities scan resumes Monday 10:00 NY</small>}
                {silverBullet?.last_error && (() => {
                  const issues = silverBullet.last_error.split("; ").filter(Boolean);
                  return <small className="silver-bullet-failure" title={silverBullet.last_error}><AlertTriangle size={12} />{issues.length} symbol{issues.length === 1 ? "" : "s"} could not be fetched — <span>{issues[0]}</span>{issues.length > 1 ? ` (+${issues.length - 1} more)` : ""}</small>;
                })()}
                {silverBullet?.scan_date && !silverBullet.running && <small className="auto-meta">Showing {silverBullet.scan_date}</small>}
              </section>
            {silverBulletLoading || silverBullet?.scan_date ? (
              <section className={`panel silver-bullet-results${dateTransition ? " date-refresh" : ""}`} id="alerts" ref={(el) => { sectionRefs.current.alerts = el; }} style={{ marginBottom: 16 }}>
                <div className="panel-heading"><span>AM Silver Bullet alerts</span><small>New York session — commodities only</small></div>
                {silverBulletLoading ? (
                  <div className="silver-bullet-loading" role="status" aria-live="polite">
                    <RefreshCw size={15} className="spin" />
                    <span>Loading Silver Bullet results—</span>
                  </div>
                ) : silverBullet?.signals.length ? (
                  <div className="tracker-list">
                    {silverBullet.signals.slice().reverse().map((signal) => (
                      <button
                        key={signal.id}
                        type="button"
                        className={`signal-chip signal-chip-button ${signal.direction === "bullish" ? "bull" : "bear"}`}
                        title="Open chart with this setup's levels (Watch setup adds alerts)"
                        onClick={() => setChart({
                          symbol: signal.symbol,
                          sourceLink: null,
                          interval: "5m",
                          levels: [
                            ...(signal.range_low > 0 && signal.range_high > 0 ? [{ label: "SB range", price: signal.range_low, price2: signal.range_high }] : []),
                            ...setupLevels([["Entry", signal.entry], ["SL", signal.stop_loss], ["Target", signal.target]]),
                          ],
                        })}
                      >
                        {signal.direction === "bullish" ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
                        <strong>{signal.symbol}</strong>
                        <small>Trigger={signal.entry} — Time={new Date(signal.signal_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} NY</small>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="date-note">No valid Silver Bullet setup for {silverBullet?.scan_date}.</p>
                )}
              </section>
            ) : null}
            {ltf && (ltf.running || ltf.setups.length > 0) ? (
              <section className="panel silver-bullet-results" id="ltf-confirmation" style={{ marginBottom: 16 }}>
                <div className="panel-heading">
                  <span>Intraday confirmations</span>
                  <small>
                    {ltf.running ? `watching ${ltf.armed_count} armed · ${ltf.timeframe} CISD` : "watcher off (Settings)"}
                    {ltf.last_check_at ? ` · checked ${new Date(ltf.last_check_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
                  </small>
                  <button className="test-button button-secondary" type="button" onClick={checkLtfNow} disabled={ltfChecking}>
                    {ltfChecking ? <RefreshCw size={12} className="spin" /> : <RefreshCw size={12} />} Check now
                  </button>
                </div>
                {ltf.last_error && <p className="date-note" title={ltf.last_error}>Last check had errors: {ltf.last_error.slice(0, 140)}</p>}
                {ltfGroups.live.length > 0 && (
                  <div className="tracker-list">{ltfGroups.live.map(ltfChip)}</div>
                )}
                {ltfGroups.armed.length > 0 && (
                  <details className="sync-note">
                    <summary><strong>{ltfGroups.armed.length}</strong> armed · awaiting {ltf.timeframe} CISD</summary>
                    <div className="tracker-list">{ltfGroups.armed.map(ltfChip)}</div>
                  </details>
                )}
                {ltfGroups.earlier.length > 0 && (
                  <details className="sync-note">
                    <summary><strong>{ltfGroups.earlier.length}</strong> earlier triggers (window ended)</summary>
                    <div className="tracker-list">{ltfGroups.earlier.map(ltfChip)}</div>
                  </details>
                )}
                {ltf.setups.length === 0 && (
                  <p className="date-note">No armed setups yet — they are added after each market's daily close.</p>
                )}
              </section>
            ) : null}
      <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%", marginTop: 8 }}>
        {syncSummary && (
          <div className="history-results">
            <p className="kicker">Sync summary — {syncSummary.start_date ?? "lookback"} to {syncSummary.end_date ?? syncSummary.anchor_date}</p>
            <span>{syncSummary.synced} synced — {syncSummary.fetchedNew} new bars{syncSummary.failed > 0 ? ` — ${syncSummary.failed} failed` : ""}{syncSummary.incomplete > 0 ? ` — ${syncSummary.incomplete} with missing dates` : ""}{syncSummary.gated ? " — latest bar deferred until market cut-off" : ""}</span>
            {groupSyncIssues(syncSummary.results).map((group) => (
              <details key={group.reason} className="sync-note">
                <summary><strong>{group.symbols.length}×</strong> {group.reason}</summary>
                <span>{group.symbols.join(", ")}</span>
              </details>
            ))}
          </div>
        )}
      </div>
      </section>      <details className="panel strategy-panel" id="strategies" open ref={(el) => { sectionRefs.current.strategies = el; }}>
  <summary className="panel-heading">
    <span className="strategy-panel-title">
      <ChevronRight size={14} className="strategy-panel-status-icon" aria-hidden="true" />
      <span>Strategy profiles</span>
    </span>
    <div className="panel-heading-actions">
      <small>{strategies.filter((flag) => flag.enabled).length}/{strategies.length} ON</small>
      <button className="test-button button-primary" type="button" title="Run strategies (Alt+R)" onClick={() => runStrategyScan()} disabled={strategyScanning || selected.length === 0}>
        <RefreshCw size={14} className={strategyScanning ? "spin" : undefined} />
        {strategyScanning ? (scanProgress ? scanProgress : "Scanning—") : "Run strategies"}
      </button>
      {strategyScanning && (
        <button className="test-button stop" type="button" title="Stop the running strategy scan (Alt+S)" onClick={stopStrategyScan}>
          <Square size={14} />
          Stop
        </button>
      )}
      {strategyScanning && scanProgress && <span className="scan-progress">{scanProgress}</span>}
    </div>
  </summary>
          <div className="protected-swings-bar">
            <button
              type="button"
              className={`ps-chip${strategies.find((f: StrategyFlag) => f.name === "protected_swings")?.enabled ? " active" : ""}`}
              onClick={() => {
                const current = strategies.find((f: StrategyFlag) => f.name === "protected_swings");
                if (current) toggleStrategy(current);
              }}
              title="Toggle Protected Swings strategy"
            >
              Protected Swings{strategies.find((f: StrategyFlag) => f.name === "protected_swings")?.enabled ? " ✓" : ""}
            </button>
           <span className="info-trigger" aria-label="Info: Protected Swings" role="button" tabIndex={0} onClick={() => setActiveTooltip(activeTooltip === "protected_swings" ? null : "protected_swings")} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTooltip(activeTooltip === "protected_swings" ? null : "protected_swings"); } }}>
             <Info size={12} />
             <span className={`info-tooltip${activeTooltip === "protected_swings" ? " open" : ""}`}>Tracks confirmed protected swing highs and lows. A signal appears only on the confirmation session; anticipated swings do not trigger trades.</span>
           </span>
           <select
             id="protected-swing-timeframe"
             value={protectedSwingTimeframe}
             onChange={(event) => setProtectedSwingTimeframe(event.target.value)}
             disabled={strategies.find((f: StrategyFlag) => f.name === "protected_swings")?.enabled !== true}
             title={strategies.find((f: StrategyFlag) => f.name === "protected_swings")?.enabled !== true ? "Enable Protected Swings first" : "Protected swing timeframe"}
           >
             <option value="daily">Daily</option>
             <option value="weekly">Weekly</option>
             <option value="15m">15m</option>
             <option value="1h">1h</option>
             <option value="4h">4h</option>
           </select>
         </div>

        {!weeklyMasterOn && <p className="date-note">WEEKLY_PROFILES_ENABLED is off in all_strategy.py — weekly profile chips stay locked until the master switch is turned on there.</p>}
        <div className="strategy-legend" aria-label="Data mode legend">
          <span className="mode-badge live"><Radio size={10} /> Live</span>
          <span className="mode-badge hist"><History size={10} /> Historic</span>
          <small>Shown after a run: live intraday tracking vs end-of-day / historical confirmation.</small>
        </div>
        <div className="strategy-groups">
          {(["Core", "Weekly profiles"] as const).map((groupName) => {
            const groupFlags = strategies.filter((f) => f.group === groupName && f.name !== "protected_swings");
            const count = groupFlags.filter((f) => f.enabled).length;
            const allOn = groupFlags.length > 0 && count === groupFlags.length;
            const partial = count > 0 && !allOn;
            return (
              <div key={groupName} className="strategy-group">
                <div className="strategy-group-head">
                  <span className="filter-label">{groupName}</span>
                  <span className="strategy-group-count">{count}/{groupFlags.length}</span>
                  <button className="toggle-text" title={allOn ? `Turn all ${groupName} OFF` : `Turn all ${groupName} ON`} onClick={() => setGroupStrategies(groupName, !allOn)}>
                    {allOn ? "Clear" : "Select all"}
                    {partial && <span className="day-marker" style={{ marginLeft: 4 }}>—</span>}
                  </button>
                </div>
                <div className="filters strategy-chips">
                  {groupFlags.map((flag) => {
                    const result = strategyGroups.find((g) => g.strategy === flag.name);
                    const matchCount = result ? result.bull_count + result.bear_count : 0;
                    const mode = !result || result.total === 0 ? null : result.has_live_data ? "live" : "hist";
                    const isWeekly = groupName === "Weekly profiles";
                    return (
                      <span key={flag.name} className="strategy-chip-wrap">
                        <button type="button" title={isWeekly && !flag.runnable ? `${flag.name} — blocked by the master switch` : flag.name} disabled={isWeekly && !flag.runnable} className={flag.enabled ? "active" : ""} onClick={() => toggleStrategy(flag)}>
                          {flag.label}
                          {isWeekly && WEEKLY_PROFILE_DAYS[flag.name] && (
                            <span className="day-marker" aria-label={`Requires weekdays: ${WEEKLY_PROFILE_DAYS[flag.name]}`}>{WEEKLY_PROFILE_DAYS[flag.name]}</span>
                          )}
                          {mode === "live" && <span className="mode-icon live" title="Live data" aria-label="Live data"><Radio size={11} /></span>}
                          {mode === "hist" && <span className="mode-icon hist" title="Historic data" aria-label="Historic data"><History size={11} /></span>}
                        </button>
                        {matchCount > 0 && <span className="strategy-chip-badge">{matchCount}</span>}
                        {flag.description && (
                          <span className="info-trigger" aria-label={`Info: ${flag.label}`} role="button" tabIndex={0} onClick={() => setActiveTooltip(activeTooltip === flag.name ? null : flag.name)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTooltip(activeTooltip === flag.name ? null : flag.name); } }}>
                            <Info size={12} />
                            <span className={`info-tooltip rich${activeTooltip === flag.name ? " open" : ""}`}><strong className="info-heading">{flag.label}</strong>{renderInfoBody(flag.description)}</span>
                          </span>
                        )}
                      </span>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
        <small className="auto-meta">Click a chip to turn that strategy ON/OFF (saved to strategy_flags.json). Runs over {selected.length} selected watchlist symbols.</small>
        <div className="strategy-date">
          <label htmlFor="strategy-date">Testing date</label>
          <button
            className="date-arrow"
            type="button"
            aria-label="Previous day"
            title="Previous testing day (Alt+Left)"
            onClick={() => shiftStrategyDate(-1)}
            disabled={strategyScanning}
          >
            <ArrowLeft size={14} />
          </button>
          <input id="strategy-date" type="date" value={strategyAnchorDate} max={localDate()} onChange={(event) => handleStrategyDateChange(event.target.value)} />
          <button
            className="date-arrow"
            type="button"
            aria-label="Next day"
            title="Next testing day (Alt+Right)"
            onClick={() => shiftStrategyDate(1)}
            disabled={strategyScanning || strategyAnchorDate >= localDate()}
          >
            <ArrowRight size={14} />
          </button>
          <label className="strategy-date-auto">
            <input
              type="checkbox"
              checked={autoRunOnDateChange}
              onChange={(event) => setAutoRunOnDateChange(event.target.checked)}
            />
            Auto-run
          </label>
          <label className="strategy-date-auto" title="Include AM Silver Bullet when testing historical dates">
            <input
              type="checkbox"
              checked={includeSilverBulletTests}
              onChange={(event) => setIncludeSilverBulletTests(event.target.checked)}
            />
            AM SB
          </label>
          {EXTRA_INFO_OPTIONS.map((option) => (
            <label key={option.key} className="strategy-date-auto" title={`${option.help}

Unticking M/W/D bias skips its calculation; unticking all three skips all extra info. Ticking one back on applies on the next run.`}>
              <input
                type="checkbox"
                checked={extraInfo[option.key]}
                onChange={(event) => toggleExtraInfo(option.key, event.target.checked)}
              />
              {option.label}
            </label>
          ))}
        </div>
        {strategyDateNote && <p className="date-note">{strategyDateNote}</p>}
        {strategyGroups.length > 0 && (
          <div className="tracker-groupby" role="group" aria-label="Group strategy results by" style={{ marginBottom: 8 }}>
            <span className="filter-label">Group</span>
            <div className="filters">
              {(["strategy", "symbol"] as const).map((option) => (
                <button key={option} type="button" className={`${strategyResultsGroupBy === option ? "active" : ""} button-secondary`} onClick={() => setStrategyResultsGroupBy(option)}>
                  {option === "strategy" ? "Strategy" : "Symbol"}
                </button>
              ))}
            </div>
          </div>
        )}
        {strategyGroups.length > 0 && strategyResultsGroupBy === "strategy" && (
          <div className="strategy-results">
            {strategyGroups.map((group) => (
              <details key={group.strategy} className="strategy-result" open>
                <summary>{group.label}{group.total > 0 && (group.has_live_data ? <span className="mode-badge live" style={{ marginLeft: 8 }}><Radio size={10} /> Live</span> : <span className="mode-badge hist" style={{ marginLeft: 8 }}><History size={10} /> Historic</span>)} <span style={{marginLeft: 'auto', display: 'inline-flex', gap: 6}}><span className="badge bullish">{group.bull_count} BULL</span><span className="badge bearish">{group.bear_count} BEAR</span></span></summary>
                {group.bull_count + group.bear_count > 0 ? (
                  <div className="signal-list">
                    {group.bullish.map((row) => renderSignalChip(row, "bull", group.strategy, `bull-${row.symbol}`, row.symbol))}
                    {group.bearish.map((row) => renderSignalChip(row, "bear", group.strategy, `bear-${row.symbol}`, row.symbol))}
                  </div>
                ) : (
                  <div className="empty small-empty"><SearchX size={14} /> No matches for this strategy.</div>
                )}
              </details>
            ))}
          </div>
        )}
        {strategyGroups.length > 0 && strategyResultsGroupBy === "symbol" && (
          strategySymbolGroups.length > 0 ? (
            <div className="strategy-results">
              {strategySymbolGroups.map((group) => (
                <details key={group.symbol} className="strategy-result" open>
                  <summary>{group.symbol} <span style={{marginLeft: 'auto', display: 'inline-flex', gap: 6}}><span className="badge bullish">{group.bull} BULL</span><span className="badge bearish">{group.bear} BEAR</span></span></summary>
                  <div className="signal-list">
                    {group.items.map((item) => renderSignalChip(item.row, item.side, item.strategy, `${item.strategy}-${item.side}-${group.symbol}`, item.label))}
                  </div>
                </details>
              ))}
            </div>
          ) : (
            <div className="empty small-empty"><SearchX size={14} /> No matches for any strategy.</div>
          )
        )}
</details>
        {chart && (
          <TradingViewChartModal
            key={chart.symbol}
            chart={chart}
            onClose={() => setChart(null)}
          />
        )}
        {commandPaletteOpen && (
          <div className="command-palette-backdrop" onClick={() => { setCommandPaletteOpen(false); setCommandPaletteQuery(""); }}>
            <div className="command-palette" ref={commandPaletteRef} onClick={(e) => e.stopPropagation()}>
              <div className="command-palette-header">
                <label htmlFor="command-palette-input" className="command-palette-label">
                  <SearchX size={14} />
                  <span>Command</span>
                  <kbd className="command-palette-kbd">Ctrl+K / ⌘K</kbd>
                </label>
                <input
                  id="command-palette-input"
                  type="text"
                  className="command-palette-input"
                  value={commandPaletteQuery}
                  onChange={(e) => {
                    setCommandPaletteQuery(e.target.value);
                    setCommandPaletteActiveIndex(0);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setCommandPaletteActiveIndex((i) => Math.min(i + 1, filteredCommandPaletteItems.length - 1));
                    } else if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setCommandPaletteActiveIndex((i) => Math.max(i - 1, 0));
                    } else if (e.key === "Enter") {
                      e.preventDefault();
                      if (filteredCommandPaletteItems[commandPaletteActiveIndex]) {
                        handleCommandPaletteSelect(filteredCommandPaletteItems[commandPaletteActiveIndex]);
                      }
                    }
                  }}
                  placeholder="Search symbols, strategies, actions..."
                  autoFocus
                />
              </div>
              <div className="command-palette-results" role="listbox">
                {filteredCommandPaletteItems.length === 0 ? (
                  <div className="command-palette-empty">No matches for "{commandPaletteQuery}"</div>
                ) : (
                  filteredCommandPaletteItems.map((item, idx) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`command-palette-item${idx === commandPaletteActiveIndex ? " active" : ""}`}
                      role="option"
                      aria-selected={idx === commandPaletteActiveIndex}
                      onClick={() => handleCommandPaletteSelect(item)}
                    >
                      <span className="command-palette-item-label">{item.label}</span>
                      <span className="command-palette-item-category">{item.category}</span>
                    </button>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
        </main>
      </div>
      <footer>Rule-based analysis only. Validate signals before taking any trade.</footer>
    </main>
  );
}



