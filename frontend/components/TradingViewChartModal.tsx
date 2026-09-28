"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import OhlcChart, {
  EMA_COLORS,
  VWAP_COLOR,
  aggregate,
  downloadSvgPng,
  formatPrice,
  formatVolume,
  type Bar,
  type Overlays,
} from "./OhlcChart";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type ChartTarget = {
  symbol: string;
  /** Provider URL (e.g. scanner result link). Null falls back to the raw symbol. */
  sourceLink?: string | null;
  /** Pre-resolved TradingView symbol; skips the resolution round-trip when set. */
  tvSymbol?: string | null;
};

/**
 * Resolve an app symbol (e.g. NSE:ACHYUT) to the TradingView symbol that
 * actually exists (NSE preferred, BSE fallback — many SME IPOs are BSE-only).
 * Returns null when TradingView has no NSE/BSE listing for it.
 */
export type TvResolution = {
  /** TradingView symbol, or null when TradingView carries no NSE/BSE listing. */
  tvSymbol: string | null;
  /** True when the lookup itself failed (offline/WAF), not "genuinely absent". */
  failed: boolean;
};

export const resolveTvSymbol = async (symbol: string): Promise<TvResolution> => {
  try {
    const response = await fetch(
      `${API}/api/market-data/tv-symbol?symbol=${encodeURIComponent(symbol)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return { tvSymbol: null, failed: true };
    const payload = await response.json();
    const items = payload?.items ?? {};
    // Backend keys are normalized to uppercase; match case-insensitively.
    const item = items[symbol] ?? items[String(symbol).toUpperCase()];
    if (!item) return { tvSymbol: null, failed: true };
    return { tvSymbol: item.tv_symbol ?? null, failed: Boolean(item.error) };
  } catch {
    return { tvSymbol: null, failed: true };
  }
};

/** TradingView chart URL for an already-resolved symbol. */
export const tvChartUrl = (symbol: string) =>
  `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}`;

const symbolFromLink = (link: string | null | undefined, fallback: string) => {
  if (!link) return fallback;
  try {
    return new URL(link).searchParams.get("symbol") || fallback;
  } catch {
    return fallback;
  }
};

/** Build a TradingView widgetembed URL. Shared across the app. */
export const tradingViewWidgetUrl = (
  link: string | null | undefined,
  symbol: string,
  timeframe = "D",
  indicators: string[] = [],
) => {
  const chartSymbol = symbolFromLink(link, symbol);
  const params = new URLSearchParams({
    symbol: chartSymbol,
    interval: timeframe,
    hidesidetoolbar: "0",
    symboledit: "1",
    saveimage: "1",
    toolbarbg: "#f1f3f6",
    studies: JSON.stringify(indicators),
    overrides: JSON.stringify({
      "mainSeriesProperties.candleStyle.upColor": "#16a34a",
      "mainSeriesProperties.candleStyle.downColor": "#000000",
      "mainSeriesProperties.candleStyle.borderUpColor": "#16a34a",
      "mainSeriesProperties.candleStyle.borderDownColor": "#000000",
      "mainSeriesProperties.candleStyle.wickUpColor": "#16a34a",
      "mainSeriesProperties.candleStyle.wickDownColor": "#000000",
    }),
    theme: "light",
    style: "1",
    timezone: "Etc/UTC",
    withdateranges: "1",
    hideideas: "1",
    hide_side_toolbar: "0",
    hide_volume: "1",
    locale: "en",
  });
  return `https://www.tradingview.com/widgetembed/?${params.toString()}`;
};

type Interval = "5m" | "15m" | "1h" | "4h" | "1d" | "1w" | "1M";

const INTERVALS: Array<[Interval, string]> = [
  ["5m", "5m"],
  ["15m", "15m"],
  ["1h", "1H"],
  ["4h", "4H"],
  ["1d", "1D"],
  ["1w", "1W"],
  ["1M", "1M"],
];
const INTRADAY = new Set<Interval>(["5m", "15m", "1h", "4h"]);
/** Bars requested per interval (history available to pan/zoom through). */
const BARS_FOR: Record<Interval, number> = { "5m": 1500, "15m": 1000, "1h": 1000, "4h": 800, "1d": 1500, "1w": 520, "1M": 240 };
/** Bars in view after a reset; null = fit everything. */
const VISIBLE_FOR: Record<Interval, number | null> = { "5m": 60, "15m": 50, "1h": 50, "4h": 45, "1d": 40, "1w": 40, "1M": 36 };
const REFRESH_MS = 60_000;

const OVERLAY_LABELS: Array<[keyof Overlays, string]> = [
  ["ema20", "EMA 20"],
  ["ema50", "EMA 50"],
  ["ema200", "EMA 200"],
  ["vwap", "VWAP"],
  ["volume", "Volume"],
  ["rsi", "RSI 14"],
  ["levels", "52W H/L"],
];

type Prefs = { overlays: Overlays; interval: Interval; autoRefresh: boolean };
const PREFS_KEY = "marketScanner.chartModal.v3";
const DEFAULT_PREFS: Prefs = {
  overlays: { ema20: true, ema50: true, ema200: false, vwap: true, volume: false, rsi: false, levels: true },
  interval: "1d",
  autoRefresh: true,
};

const readPrefs = (): Prefs => {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    if (!raw) return DEFAULT_PREFS;
    const parsed = JSON.parse(raw);
    return {
      overlays: { ...DEFAULT_PREFS.overlays, ...(parsed?.overlays ?? {}) },
      interval: INTERVALS.some(([key]) => key === parsed?.interval) ? parsed.interval : DEFAULT_PREFS.interval,
      autoRefresh: typeof parsed?.autoRefresh === "boolean" ? parsed.autoRefresh : DEFAULT_PREFS.autoRefresh,
    };
  } catch {
    return DEFAULT_PREFS;
  }
};

const shiftIso = (iso: string, days: number) => {
  const date = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};

const detailText = (payload: unknown): string | null => {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  const error = (detail as { error?: unknown }).error;
  return typeof error === "string" ? error : JSON.stringify(detail);
};

type ChartPayload = {
  rows?: Bar[];
  source?: string;
  live?: boolean;
  tv_symbol?: string | null;
  interval_served?: string;
  notes?: string[];
};

type ChartState = {
  status: "loading" | "ready" | "empty" | "error";
  key: string;
  bars: Bar[];
  live: boolean;
  tvSymbol: string | null;
  source: string | null;
  notes: string[];
  message: string | null;
  updatedAt: Date | null;
};

const EMPTY_STATE: ChartState = {
  status: "loading",
  key: "",
  bars: [],
  live: false,
  tvSymbol: null,
  source: null,
  notes: [],
  message: null,
  updatedAt: null,
};

const toBars = (rows: Bar[] | undefined): Bar[] =>
  (rows ?? [])
    .map((row) => ({
      date: String(row.date),
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume) || 0,
    }))
    .filter((row) => [row.open, row.high, row.low, row.close].every(Number.isFinite))
    .sort((a, b) => a.date.localeCompare(b.date));

/**
 * Shared chart popup. Render it unconditionally and pass the currently
 * selected symbol (or null to hide). Remount per symbol via `key` at the
 * call site to reset selections.
 *
 * Draws its own candlestick chart fed by the backend
 * (/api/market-data/chart: live TradingView candles, local daily fallback)
 * for every market. TradingView's embeddable widget is not used: it refuses
 * NSE/BSE symbols ("This symbol is only available on TradingView").
 */
export default function TradingViewChartModal({
  chart,
  onClose,
}: {
  chart: ChartTarget | null;
  onClose: () => void;
}) {
  const symbol = chart?.symbol ?? null;
  const sourceLink = chart?.sourceLink ?? null;
  const preset = chart?.tvSymbol ?? null;

  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const [prefs, setPrefs] = useState<Prefs>(DEFAULT_PREFS);
  const prefsLoaded = useRef(false);
  const [data, setData] = useState<ChartState>(EMPTY_STATE);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [resetNonce, setResetNonce] = useState(0);
  const [fitAll, setFitAll] = useState(false);
  const chartWrapRef = useRef<HTMLDivElement>(null);

  const interval = prefs.interval;
  const intraday = INTRADAY.has(interval);

  // "Open" link target: provider link, preset, or the symbol the backend
  // actually charted (e.g. BSE listing for an NSE app symbol).
  const chartSymbol = sourceLink
    ? symbolFromLink(sourceLink, symbol ?? "")
    : preset ?? data.tvSymbol ?? symbol ?? "";

  useEffect(() => {
    setPrefs(readPrefs());
    prefsLoaded.current = true;
  }, []);

  useEffect(() => {
    if (!prefsLoaded.current) return;
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      // storage unavailable: preferences just don't persist
    }
  }, [prefs]);

  // Candles. A changed symbol/interval shows the loader; a refresh of the
  // same series updates in place so zoom/pan survive.
  useEffect(() => {
    if (!symbol) return;
    const key = `${symbol}|${interval}`;
    let live = true;
    setData((current) => (current.key === key && current.bars.length ? current : { ...EMPTY_STATE, key }));
    const params = new URLSearchParams({ symbol, interval, bars: String(BARS_FOR[interval]) });
    fetch(`${API}/api/market-data/chart?${params.toString()}`, { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (!live) return;
        if (!response.ok) {
          setData((current) =>
            current.key === key && current.bars.length
              ? { ...current, notes: [detailText(payload) ?? `HTTP ${response.status}`] }
              : {
                  ...EMPTY_STATE,
                  key,
                  status: response.status === 404 ? "empty" : "error",
                  message: detailText(payload) ?? `HTTP ${response.status}`,
                },
          );
          return;
        }
        const body = (payload ?? {}) as ChartPayload;
        let bars = toBars(body.rows);
        // Local fallback serves daily bars; roll them up for 1W/1M.
        if (body.interval_served === "1d" && (interval === "1w" || interval === "1M")) {
          bars = aggregate(bars, interval === "1w" ? "W" : "M");
        }
        setData({
          status: bars.length ? "ready" : "empty",
          key,
          bars,
          live: Boolean(body.live),
          tvSymbol: body.tv_symbol ?? null,
          source: body.live ? `TradingView${body.tv_symbol && body.tv_symbol !== symbol ? ` (${body.tv_symbol})` : ""}` : `Local DB (${body.source ?? "daily"})`,
          notes: body.notes ?? [],
          message: bars.length ? null : "No candles returned.",
          updatedAt: new Date(),
        });
      })
      .catch((error) => {
        if (!live) return;
        const message = error instanceof Error ? error.message : "Request failed";
        setData((current) =>
          current.key === key && current.bars.length
            ? { ...current, notes: [message] }
            : { ...EMPTY_STATE, key, status: "error", message },
        );
      });
    return () => {
      live = false;
    };
  }, [symbol, interval, reloadNonce]);

  // Live auto-refresh for intraday TradingView candles.
  useEffect(() => {
    if (!intraday || !prefs.autoRefresh || !data.live) return;
    const timer = window.setInterval(() => setReloadNonce((n) => n + 1), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [intraday, prefs.autoRefresh, data.live]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const stats = useMemo(() => {
    const bars = data.bars;
    if (!bars.length) return null;
    const last = bars[bars.length - 1];
    const lastDay = last.date.slice(0, 10);
    // Change vs the previous session's close (intraday) or previous bar.
    let prevClose: number | null = null;
    if (intraday) {
      for (let i = bars.length - 1; i >= 0; i--) {
        if (bars[i].date.slice(0, 10) < lastDay) {
          prevClose = bars[i].close;
          break;
        }
      }
    } else if (bars.length > 1) {
      prevClose = bars[bars.length - 2].close;
    }
    const change = prevClose != null ? last.close - prevClose : 0;
    const changePct = prevClose ? (change / prevClose) * 100 : 0;
    const window52 = intraday
      ? bars.filter((bar) => bar.date.slice(0, 10) === lastDay)
      : bars.filter((bar) => bar.date >= shiftIso(last.date, -365));
    const high = Math.max(...window52.map((bar) => bar.high));
    const low = Math.min(...window52.map((bar) => bar.low));
    const recent = bars.slice(-21, -1);
    const avgVolume = recent.length ? recent.reduce((sum, bar) => sum + bar.volume, 0) / recent.length : 0;
    return {
      last,
      change,
      changePct,
      high,
      low,
      offHigh: high ? (last.close / high - 1) * 100 : 0,
      position: high > low ? (last.close - low) / (high - low) : 1,
      volumeRatio: avgVolume ? last.volume / avgVolume : null,
    };
  }, [data.bars, intraday]);

  const setOverlay = (key: keyof Overlays, value: boolean) =>
    setPrefs((current) => ({ ...current, overlays: { ...current.overlays, [key]: value } }));

  const setInterval_ = (value: Interval) => {
    setPrefs((current) => ({ ...current, interval: value }));
    setFitAll(false);
  };

  const snapshot = () => {
    const svg = chartWrapRef.current?.querySelector("svg");
    if (!svg || !symbol) return;
    const stamp = (data.bars[data.bars.length - 1]?.date ?? "").replace(/[^0-9]/g, "");
    void downloadSvgPng(svg, `${symbol.replace(/[^A-Za-z0-9_-]+/g, "_")}_${interval}_${stamp}.png`).catch(() => undefined);
  };

  const copySymbol = async () => {
    if (!symbol) return;
    try {
      await navigator.clipboard.writeText(symbol);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // clipboard blocked; ignore
    }
  };

  if (!chart) return null;

  const up = (stats?.change ?? 0) >= 0;
  const intervalLabel = INTERVALS.find(([key]) => key === interval)?.[1] ?? interval;

  return (
    <div className="chart-modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section
        className={`chart-modal${expanded ? " expanded" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={`${chart.symbol} chart`}
      >
        <div className="chart-modal-header">
          <div className="chart-modal-title">
            <strong>{chart.symbol}</strong>
            {stats ? (
              <span className="chart-head-quote">
                {formatPrice(stats.last.close)}
                <span className={up ? "chart-up" : "chart-down"}>
                  {up ? "+" : ""}
                  {formatPrice(stats.change)} ({up ? "+" : ""}
                  {stats.changePct.toFixed(2)}%)
                </span>
                {data.live && intraday ? <span className="chart-live-dot" title="Live TradingView candles">LIVE</span> : null}
              </span>
            ) : null}
          </div>
          <div className="chart-modal-actions">
            <button type="button" className="chart-tool-btn" onClick={copySymbol} title="Copy symbol">
              {copied ? "Copied" : "Copy"}
            </button>
            <button type="button" className="chart-tool-btn" onClick={() => setExpanded((value) => !value)} title={expanded ? "Restore size" : "Expand to full window"}>
              {expanded ? "Restore" : "Expand"}
            </button>
            <a
              className="chart-modal-open"
              href={tvChartUrl(chartSymbol)}
              target="_blank"
              rel="noreferrer"
              title={`Open ${chartSymbol} full chart on TradingView`}
            >
              Open ↗
            </a>
            <button type="button" className="chart-modal-close" aria-label="Close chart" title="Close chart (Esc)" onClick={onClose}>×</button>
          </div>
        </div>

        <>
            <div className="chart-modal-controls">
              <div className="chart-seg small" role="group" aria-label="Interval">
                {INTERVALS.map(([value, label]) => (
                  <button key={value} type="button" className={interval === value ? "active" : ""} onClick={() => setInterval_(value)}>
                    {label}
                  </button>
                ))}
              </div>
              {OVERLAY_LABELS.map(([key, label]) => {
                const disabled = (key === "vwap" && !intraday) || (key === "levels" && intraday);
                return (
                  <label key={key} className={`chart-indicator${disabled ? " disabled" : ""}`} title={disabled ? (key === "vwap" ? "Intraday only" : "Daily and higher only") : undefined}>
                    <input type="checkbox" checked={prefs.overlays[key]} disabled={disabled} onChange={(event) => setOverlay(key, event.target.checked)} />
                    {key in EMA_COLORS || key === "vwap" ? (
                      <span className="chart-swatch" style={{ background: key === "vwap" ? VWAP_COLOR : EMA_COLORS[key as keyof typeof EMA_COLORS] }} />
                    ) : null}
                    {label}
                  </label>
                );
              })}
              <div className="chart-control-actions">
                {intraday ? (
                  <label className="chart-indicator" title={`Refresh live candles every ${REFRESH_MS / 1000}s`}>
                    <input type="checkbox" checked={prefs.autoRefresh} onChange={(event) => setPrefs((current) => ({ ...current, autoRefresh: event.target.checked }))} />
                    Auto
                  </label>
                ) : null}
                <button type="button" className="chart-tool-btn" onClick={() => setReloadNonce((n) => n + 1)} title="Reload candles">
                  Refresh
                </button>
                <button type="button" className="chart-tool-btn" onClick={() => { setFitAll(false); setResetNonce((n) => n + 1); }} disabled={data.status !== "ready"} title="Reset zoom (or double-click the chart)">
                  Reset
                </button>
                <button type="button" className="chart-tool-btn" onClick={() => { setFitAll(true); setResetNonce((n) => n + 1); }} disabled={data.status !== "ready"} title="Show all loaded bars">
                  Fit
                </button>
                <button type="button" className="chart-tool-btn" onClick={snapshot} disabled={data.status !== "ready"} title="Download chart as PNG">
                  Snapshot
                </button>
              </div>
            </div>

            {stats && data.status === "ready" ? (
              <div className="chart-stats">
                <span className="chart-stat" title={data.notes.join(" ") || undefined}>
                  <em>Source</em>
                  {data.source}
                  {data.updatedAt ? <span className="muted"> · {data.updatedAt.toLocaleTimeString("en-IN", { hour12: false })}</span> : null}
                </span>
                <span className="chart-stat">
                  <em>Last bar</em>
                  {stats.last.date.replace("T", " ")}
                  {intraday ? <span className="muted"> IST</span> : null}
                </span>
                <span className="chart-stat">
                  <em>{intraday ? "Day range" : "52W range"}</em>
                  {formatPrice(stats.low)}
                  <span className="chart-range-bar" aria-hidden="true">
                    <span style={{ left: `${Math.round(stats.position * 100)}%` }} />
                  </span>
                  {formatPrice(stats.high)}
                </span>
                {!intraday ? (
                  <span className="chart-stat">
                    <em>From 52W high</em>
                    <span className={stats.offHigh >= -0.05 ? "chart-up" : "chart-down"}>{stats.offHigh.toFixed(1)}%</span>
                  </span>
                ) : null}
                <span className="chart-stat">
                  <em>Volume</em>
                  {formatVolume(stats.last.volume)}
                  {stats.volumeRatio != null ? (
                    <span className={stats.volumeRatio >= 1.5 ? "chart-up" : "muted"}> · {stats.volumeRatio.toFixed(1)}× 20-bar avg</span>
                  ) : null}
                </span>
                {data.notes.length && !data.live ? <span className="chart-stat warn">{data.notes[0]}</span> : null}
              </div>
            ) : null}

            <div className="chart-local" ref={chartWrapRef}>
              {data.status === "ready" ? (
                <OhlcChart
                  bars={data.bars}
                  overlays={prefs.overlays}
                  intraday={intraday}
                  monthly={interval === "1M"}
                  title={`${chart.symbol} · ${intervalLabel}`}
                  initialCount={fitAll ? null : VISIBLE_FOR[interval]}
                  resetKey={`${data.key}|${resetNonce}`}
                  levels={!intraday && stats ? { high: stats.high, low: stats.low } : null}
                />
              ) : data.status === "loading" ? (
                <div className="chart-frame chart-frame-loading">Loading {chart.symbol} {intervalLabel} candles…</div>
              ) : (
                <div className="chart-empty">
                  <strong>{data.status === "error" ? `Couldn't load ${chart.symbol}` : `No ${intervalLabel} data for ${chart.symbol}`}</strong>
                  {data.message ? <span className="muted">{data.message}</span> : null}
                  <div className="chart-empty-actions">
                    <button type="button" className="chart-tool-btn" onClick={() => setReloadNonce((n) => n + 1)}>
                      Retry
                    </button>
                    {intraday ? (
                      <button type="button" className="chart-tool-btn" onClick={() => setInterval_("1d")}>
                        Show daily
                      </button>
                    ) : null}
                    <a href={tvChartUrl(chartSymbol)} target="_blank" rel="noreferrer">
                      Open on TradingView ↗
                    </a>
                  </div>
                </div>
              )}
            </div>
            <div className="chart-footnote muted">
              Candles via backend (TradingView feed, local DB fallback) · times IST · scroll to zoom · drag to pan · ←/→ keys · double-click resets
            </div>
        </>
      </section>
    </div>
  );
}
