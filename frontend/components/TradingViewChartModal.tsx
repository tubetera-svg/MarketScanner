"use client";

import { useEffect, useState } from "react";

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

/** Build a TradingView widgetembed URL. Shared across the app. */
export const tradingViewWidgetUrl = (
  link: string | null | undefined,
  symbol: string,
  timeframe = "D",
  indicators: string[] = [],
) => {
  let chartSymbol = symbol;
  try {
    const parsed = link ? new URL(link) : null;
    chartSymbol = parsed?.searchParams.get("symbol") || symbol;
  } catch {
    // Fall back to the result symbol when a provider URL is malformed.
  }
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

const INDICATORS: Array<[string, string]> = [
  ["RSI@tv-basicstudies", "RSI"],
  ["MACD@tv-basicstudies", "MACD"],
  ["MAExp@tv-basicstudies", "EMA"],
  ["VWAP@tv-basicstudies", "VWAP"],
];

/**
 * Shared TradingView chart popup. Render it unconditionally and pass the
 * currently selected symbol (or null to hide). Remount per symbol via `key`
 * at the call site to reset timeframe/indicator selections.
 */
export default function TradingViewChartModal({
  chart,
  onClose,
}: {
  chart: ChartTarget | null;
  onClose: () => void;
}) {
  const [timeframe, setTimeframe] = useState("D");
  const [indicators, setIndicators] = useState<string[]>([]);
  const [resolved, setResolved] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [lookupFailed, setLookupFailed] = useState(false);

  const symbol = chart?.symbol ?? null;
  const sourceLink = chart?.sourceLink ?? null;
  const preset = chart?.tvSymbol ?? null;

  // Widget symbol: use the resolved TradingView symbol when we have one,
  // otherwise the raw app symbol exactly like the old inline popup did —
  // the widget itself is the source of truth for whether a symbol exists.
  const chartSymbol = sourceLink
    ? symbol ?? ""
    : preset ?? resolved ?? symbol ?? "";

  useEffect(() => {
    if (!symbol) return;
    // A provider link already carries a valid TradingView symbol.
    if (sourceLink || preset) {
      setResolved(null);
      setResolving(false);
      setLookupFailed(false);
      return;
    }
    let live = true;
    setResolving(true);
    setResolved(null);
    setLookupFailed(false);
    resolveTvSymbol(symbol)
      .then((value) => {
        if (!live) return;
        setResolved(value.tvSymbol);
        setLookupFailed(value.failed);
      })
      .finally(() => {
        if (live) setResolving(false);
      });
    return () => {
      live = false;
    };
  }, [symbol, sourceLink, preset]);

  // Advisory only: negative lookup (not a failure) shows a hint above the
  // widget, never instead of it — restoring the old always-render behaviour.
  const unlistedHint = !sourceLink && !preset && !resolving && !lookupFailed && resolved === null;
  const src = tradingViewWidgetUrl(sourceLink, chartSymbol, timeframe, indicators);

  if (!chart) return null;

  return (
    <div className="chart-modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="chart-modal" role="dialog" aria-modal="true" aria-label={`${chart.symbol} TradingView chart`}>
        <div className="chart-modal-header">
          <strong>{chart.symbol}</strong>
          {chartSymbol !== chart.symbol ? (
            <span className="muted chart-symbol-note">· {chartSymbol}</span>
          ) : null}
          <a
            className="chart-modal-open"
            href={tvChartUrl(chartSymbol)}
            target="_blank"
            rel="noreferrer"
            title={`Open ${chartSymbol} on TradingView`}
          >
            Open ↗
          </a>
          <button type="button" className="chart-modal-close" aria-label="Close chart" title="Close chart" onClick={onClose}>×</button>
        </div>
        <div className="chart-modal-controls">
          <label>
            Timeframe
            <select value={timeframe} onChange={(event) => setTimeframe(event.target.value)}>
              <option value="1">1m</option>
              <option value="5">5m</option>
              <option value="15">15m</option>
              <option value="60">1h</option>
              <option value="240">4h</option>
              <option value="D">1D</option>
              <option value="W">1W</option>
            </select>
          </label>
          <span className="chart-control-label">Indicators</span>
          {INDICATORS.map(([value, label]) => (
            <label key={value} className="chart-indicator">
              <input
                type="checkbox"
                checked={indicators.includes(value)}
                onChange={(event) =>
                  setIndicators((current) =>
                    event.target.checked
                      ? [...current, value]
                      : current.filter((indicator) => indicator !== value),
                  )
                }
              />
              {label}
            </label>
          ))}
        </div>
        {resolving ? (
          <div className="chart-frame chart-frame-loading">
            Resolving {chart.symbol} on TradingView…
          </div>
        ) : (
          <>
          {unlistedHint ? (
          <div className="chart-unlisted-hint">
            <span>
              <strong>{chart.symbol}</strong> may not be on TradingView (no NSE/BSE feed found).
            </span>
            <a
              href={`https://www.tradingview.com/symbols/?q=${encodeURIComponent(chart.symbol.split(":").pop() ?? "")}`}
              target="_blank"
              rel="noreferrer"
            >
              Search TradingView ↗
            </a>
            <a
              href={`https://www.google.com/search?q=${encodeURIComponent(`${chart.symbol.split(":").pop()} NSE share price`)}`}
              target="_blank"
              rel="noreferrer"
            >
              Search the web ↗
            </a>
          </div>
          ) : null}
          <iframe
            key={src}
            title={`${chart.symbol} live TradingView chart`}
            src={src}
            className="chart-frame"
            allowFullScreen
          />
          </>
        )}
      </section>
    </div>
  );
}
