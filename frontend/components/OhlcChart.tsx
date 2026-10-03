"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { addDays, useDisplayTimezone, wallLabel, weekdayOf } from "./time";

/**
 * Lightweight SVG candlestick chart (no external charting dependency). Fed by
 * /api/market-data/chart: live TradingView candles, which include NSE/BSE
 * symbols the TradingView embed widget refuses, or stored daily OHLC.
 */

/** `date` is YYYY-MM-DD (trading day), or YYYY-MM-DDTHH:MM wall time in the display zone for intraday bars. */
export type Bar = { date: string; open: number; high: number; low: number; close: number; volume: number };
export type Overlays = {
  ema20: boolean;
  ema50: boolean;
  ema200: boolean;
  vwap: boolean;
  volume: boolean;
  rsi: boolean;
  levels: boolean;
};

// Literal colours (not CSS vars) so PNG snapshots render identically.
const UP = "#16a34a";
const DOWN = "#000000";
const TEXT_DOWN = "#d65a3a";
const GRID = "#eef2f3";
const AXIS = "#71808b";
const INK = "#15232d";
const RSI_COLOR = "#287b79";
const ALERT_COLOR = "#e07b00";
const GUIDE_COLOR = "#2f6f9e";
export const EMA_COLORS = { ema20: "#356c9b", ema50: "#c08a2e", ema200: "#7c3aed" } as const;
export const VWAP_COLOR = "#db2777";
const FONT = "'DM Mono', ui-monospace, monospace";

const AXIS_W = 70;
const DATE_H = 22;
const TOP = 26;
const RIGHT_PAD = 3;
const MIN_BARS = 10;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// ---- maths ---------------------------------------------------------------

export const ema = (values: number[], period: number): Array<number | null> => {
  const out: Array<number | null> = [];
  const k = 2 / (period + 1);
  let prev: number | null = null;
  let sum = 0;
  values.forEach((value, index) => {
    if (index < period - 1) {
      sum += value;
      out.push(null);
    } else if (index === period - 1) {
      sum += value;
      prev = sum / period;
      out.push(prev);
    } else {
      prev = value * k + (prev as number) * (1 - k);
      out.push(prev);
    }
  });
  return out;
};

/** Wilder RSI. */
export const rsi = (values: number[], period = 14): Array<number | null> => {
  const out: Array<number | null> = values.map(() => null);
  if (values.length <= period) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const delta = values[i] - values[i - 1];
    if (delta >= 0) gain += delta;
    else loss -= delta;
  }
  gain /= period;
  loss /= period;
  const value = () => (loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
  out[period] = value();
  for (let i = period + 1; i < values.length; i++) {
    const delta = values[i] - values[i - 1];
    gain = (gain * (period - 1) + Math.max(delta, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-delta, 0)) / period;
    out[i] = value();
  }
  return out;
};

/** Session VWAP, reset each calendar day of the bar labels (display zone). */
export const vwap = (bars: Bar[]): Array<number | null> => {
  let day = "";
  let pv = 0;
  let vol = 0;
  return bars.map((bar) => {
    const key = bar.date.slice(0, 10);
    if (key !== day) {
      day = key;
      pv = 0;
      vol = 0;
    }
    const volume = bar.volume || 0;
    pv += ((bar.high + bar.low + bar.close) / 3) * volume;
    vol += volume;
    return vol > 0 ? pv / vol : null;
  });
};

// Monday of the bar's week (pure calendar arithmetic on the trading date).
const weekKey = (iso: string) => addDays(iso, -((weekdayOf(iso) + 6) % 7));

/** Roll daily bars up to weekly (Mon-start) or monthly bars. */
export const aggregate = (bars: Bar[], timeframe: "D" | "W" | "M"): Bar[] => {
  if (timeframe === "D") return bars;
  const out: Bar[] = [];
  let key = "";
  for (const bar of bars) {
    const next = timeframe === "M" ? bar.date.slice(0, 7) : weekKey(bar.date);
    const last = out[out.length - 1];
    if (!last || next !== key) {
      out.push({ ...bar });
      key = next;
    } else {
      last.high = Math.max(last.high, bar.high);
      last.low = Math.min(last.low, bar.low);
      last.close = bar.close;
      last.volume += bar.volume;
    }
  }
  return out;
};

// ---- formatting ----------------------------------------------------------

const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a);

/** Price step implied by the bars (e.g. 0.05 for most NSE stocks); null when unknown. */
export const inferTick = (bars: Bar[]): number | null => {
  const values = bars.slice(-300).flatMap((bar) => [bar.open, bar.high, bar.low, bar.close]).filter((v) => Number.isFinite(v) && v > 0);
  if (!values.length) return null;
  let decimals = 0;
  for (const value of values) decimals = Math.max(decimals, (value.toFixed(6).replace(/0+$/, "").split(".")[1] ?? "").length);
  const scale = 10 ** Math.min(decimals, 6);
  let step = 0;
  for (const value of values) {
    step = gcd(step, Math.round(value * scale));
    if (step === 1) break;
  }
  return step ? step / scale : null;
};

/** Round ``price`` to ``tick`` (or 6 significant digits without one). */
export const snapPrice = (price: number, tick?: number | null) => {
  if (!tick) return Number(price.toPrecision(6));
  const decimals = (tick.toString().split(".")[1] ?? "").length;
  return Number((Math.round(price / tick) * tick).toFixed(decimals));
};

export const formatPrice = (value: number) => {
  const abs = Math.abs(value);
  const digits = abs < 1 ? 4 : abs < 10 ? 3 : 2;
  return value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
export const formatVolume = (value: number) => compact.format(value);

const niceStep = (range: number, target: number) => {
  const raw = range / Math.max(1, target);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * magnitude;
};

const dayLabel = (iso: string) => `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`;

/** Axis label; `prev` is the previous label's bar date (null for the first). */
const dateLabel = (iso: string, prev: string | null, intraday: boolean, monthly: boolean) => {
  if (intraday) return prev && prev.slice(0, 10) === iso.slice(0, 10) ? iso.slice(11, 16) : dayLabel(iso);
  const monthYear = `${MONTHS[Number(iso.slice(5, 7)) - 1]} '${iso.slice(2, 4)}`;
  if (monthly || (prev && prev.slice(0, 4) !== iso.slice(0, 4))) return monthYear;
  return dayLabel(iso);
};

// ---- view window ---------------------------------------------------------

type View = { start: number; count: number };

const clampView = (start: number, count: number, len: number): View => {
  const safeCount = Math.min(len, Math.max(Math.min(MIN_BARS, len), Math.round(count)));
  const safeStart = Math.min(Math.max(0, Math.round(start)), Math.max(0, len - safeCount));
  return { start: safeStart, count: safeCount };
};

/** Rasterise the chart SVG to a PNG download. */
export async function downloadSvgPng(svg: SVGSVGElement, filename: string) {
  const { width, height } = svg.getBoundingClientRect();
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(width));
  clone.setAttribute("height", String(height));
  const xml = new XMLSerializer().serializeToString(clone);
  const url = URL.createObjectURL(new Blob([xml], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("snapshot render failed"));
      image.src = url;
    });
    const scale = window.devicePixelRatio || 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(image, 0, 0, width, height);
    const link = document.createElement("a");
    link.download = filename;
    link.href = canvas.toDataURL("image/png");
    link.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

// ---- component -----------------------------------------------------------

export default function OhlcChart({
  bars,
  overlays,
  intraday,
  monthly = false,
  title,
  initialCount,
  resetKey,
  levels,
  alertLines,
  guideLines,
  markers,
  tick,
  onAltClick,
  onZoneDraw,
  onAlertMove,
  onContextMenu,
}: {
  bars: Bar[];
  overlays: Overlays;
  /** Bars carry display-zone wall times; enables VWAP and time-of-day labels. */
  intraday: boolean;
  monthly?: boolean;
  title: string;
  /** Bars visible after a reset (latest bars); null = all. */
  initialCount: number | null;
  /** Changing this resets zoom/pan. New bars alone never reset the view. */
  resetKey: string;
  /** 52-week high/low from daily data. */
  levels?: { high: number; low: number } | null;
  /** Price alerts to draw; inactive ones are faded. */
  /** Price alerts to draw (``price2`` = zone); inactive ones are faded. */
  alertLines?: { id: string; price: number; price2?: number | null; active: boolean; draggable?: boolean }[];
  /** Reference levels (e.g. a scanner setup's entry/SL/target), drawn dotted. */
  guideLines?: { price: number; label: string }[];
  /** Fired alerts: ``ts`` = ISO time of the bar where it happened. */
  markers?: { id: string; ts: string; price: number; label: string }[];
  /** Price step for snapping picked/dragged levels. */
  tick?: number | null;
  /** Alt+click inside the price pane reports the price under the cursor. */
  onAltClick?: (price: number) => void;
  /** Alt+drag inside the price pane reports the band drawn. */
  onZoneDraw?: (low: number, high: number) => void;
  /** A draggable alert line (or one zone edge) was dropped at ``price``. */
  onAlertMove?: (id: string, edge: "level" | "level2", price: number) => void;
  /** Right-click inside the price pane. */
  onContextMenu?: (price: number, clientX: number, clientY: number) => void;
}) {
  const displayTz = useDisplayTimezone();
  const wrapRef = useRef<HTMLDivElement>(null);
  const clipId = `ohlc-clip-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [size, setSize] = useState({ w: 0, h: 0 });
  const resetView = (total: number): View => clampView(total - (initialCount ?? total), initialCount ?? total, total);
  const [view, setView] = useState<View>(() => resetView(bars.length));
  const [hover, setHover] = useState<{ i: number; y: number } | null>(null);
  const drag = useRef<{ x: number; start: number } | null>(null);
  const geom = useRef({ step: 0, len: 0 });
  const prevLen = useRef(bars.length);

  const len = bars.length;
  // Alert line being dragged (preview price) and whether the cursor is on one.
  const [alertDrag, setAlertDrag] = useState<{ id: string; edge: "level" | "level2"; price: number; from: number } | null>(null);
  // Alt+drag zone being drawn (prices) and where the drag started (px).
  const [zoneDraw, setZoneDraw] = useState<{ from: number; to: number; y0: number } | null>(null);
  const [overAlert, setOverAlert] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height }),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setView(resetView(len));
    setHover(null);
    prevLen.current = len;
    // Only an explicit reset (resetKey) re-frames the chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  // Live refresh appended bars: keep the right edge pinned if it was showing
  // the latest bar, otherwise leave the user's pan position alone.
  useEffect(() => {
    const before = prevLen.current;
    prevLen.current = len;
    if (len <= before) return;
    setView((current) =>
      current.start + current.count >= before ? clampView(current.start + (len - before), current.count, len) : current,
    );
  }, [len]);

  // Wheel zoom anchored at the cursor (native listener: React's is passive).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (event: WheelEvent) => {
      const { step, len: total } = geom.current;
      if (!step || !total) return;
      event.preventDefault();
      const mx = event.clientX - el.getBoundingClientRect().left;
      setView((current) => {
        const offset = Math.min(Math.max(mx / step, 0), current.count);
        const frac = offset / current.count;
        const count = current.count * (event.deltaY > 0 ? 1.15 : 1 / 1.15);
        return clampView(current.start + offset - frac * count, count, total);
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const series = useMemo(() => {
    const closes = bars.map((bar) => bar.close);
    return {
      ema20: ema(closes, 20),
      ema50: ema(closes, 50),
      ema200: ema(closes, 200),
      vwap: intraday ? vwap(bars) : bars.map(() => null),
      rsi: rsi(closes, 14),
    };
  }, [bars, intraday]);

  const v = clampView(view.start, view.count, len);
  const plotW = Math.max(0, size.w - AXIS_W);
  const plotH = Math.max(0, size.h - DATE_H);
  const rsiH = overlays.rsi ? Math.round(plotH * 0.24) : 0;
  const priceH = plotH - rsiH;
  const step = plotW / (v.count + RIGHT_PAD);
  geom.current = { step, len };

  const end = Math.min(len, v.start + v.count);
  const xc = (i: number) => (i - v.start + 0.5) * step;

  let lo = Infinity;
  let hi = -Infinity;
  let maxVol = 0;
  for (let i = v.start; i < end; i++) {
    lo = Math.min(lo, bars[i].low);
    hi = Math.max(hi, bars[i].high);
    maxVol = Math.max(maxVol, bars[i].volume || 0);
  }
  if (!Number.isFinite(lo)) {
    lo = 0;
    hi = 1;
  }
  if (hi === lo) {
    hi += Math.abs(hi) * 0.01 || 1;
    lo -= Math.abs(lo) * 0.01 || 1;
  }
  const pad = (hi - lo) * 0.05;
  lo -= pad;
  hi += pad;
  const volSpace = overlays.volume ? (priceH - TOP) * 0.18 : 0;
  const yTop = TOP;
  const yBot = priceH - 6 - volSpace;
  const y = (price: number) => yBot - ((price - lo) / (hi - lo)) * (yBot - yTop);
  const priceAt = (py: number) => lo + ((yBot - py) / (yBot - yTop)) * (hi - lo);
  const clampY = (py: number) => Math.min(yBot, Math.max(yTop, py));
  const rsiTop = priceH + 8;
  const rsiBot = plotH - 4;
  const ry = (value: number) => rsiBot - (value / 100) * (rsiBot - rsiTop);

  const scene = useMemo(() => {
    if (plotW <= 0 || plotH <= 0 || !len) return null;
    const bodyW = Math.max(1, Math.min(step * 0.7, 18));
    const tickStep = niceStep(hi - lo, Math.max(2, Math.floor((yBot - yTop) / 48)));
    const ticks: number[] = [];
    for (let t = Math.ceil(lo / tickStep) * tickStep; t <= hi; t += tickStep) ticks.push(t);

    const labelEvery = Math.max(1, Math.ceil(90 / Math.max(step, 0.0001)));
    const dateTicks: Array<{ i: number; label: string }> = [];
    let prevLabel: string | null = null;
    for (let i = v.start; i < end; i++) {
      if (i % labelEvery !== 0) continue;
      dateTicks.push({ i, label: dateLabel(bars[i].date, prevLabel, intraday, monthly) });
      prevLabel = bars[i].date;
    }

    const linePath = (values: Array<number | null>, map: (value: number) => number) => {
      let path = "";
      let pen = false;
      for (let i = v.start; i < end; i++) {
        const value = values[i];
        if (value == null) {
          pen = false;
          continue;
        }
        path += `${pen ? "L" : "M"}${xc(i).toFixed(1)} ${map(value).toFixed(1)}`;
        pen = true;
      }
      return path;
    };

    const candles = [];
    const volumes = [];
    for (let i = v.start; i < end; i++) {
      const bar = bars[i];
      const up = bar.close >= bar.open;
      const color = up ? UP : DOWN;
      const x = xc(i);
      const top = y(Math.max(bar.open, bar.close));
      const bottom = y(Math.min(bar.open, bar.close));
      candles.push(
        <g key={i}>
          <line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} stroke={color} strokeWidth={1} />
          <rect x={x - bodyW / 2} y={top} width={bodyW} height={Math.max(1, bottom - top)} fill={color} />
        </g>,
      );
      if (overlays.volume && maxVol > 0) {
        const h = ((bar.volume || 0) / maxVol) * volSpace;
        volumes.push(
          <rect key={i} x={x - bodyW / 2} y={priceH - 2 - h} width={bodyW} height={h} fill={color} opacity={0.28} />,
        );
      }
    }

    const last = bars[len - 1];
    const lastUp = len < 2 || last.close >= bars[len - 2].close;
    const lastY = y(last.close);
    const levelLines =
      overlays.levels && levels
        ? ([
            ["52W H", levels.high],
            ["52W L", levels.low],
          ] as const).filter(([, value]) => value >= lo && value <= hi)
        : [];

    return (
      <>
        {/* grid + price axis */}
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={0} x2={plotW} y1={y(tick)} y2={y(tick)} stroke={GRID} />
            <text x={plotW + 6} y={y(tick) + 3.5} fontSize={10} fill={AXIS} fontFamily={FONT}>
              {formatPrice(tick)}
            </text>
          </g>
        ))}
        {dateTicks.map(({ i, label }) => (
          <g key={i}>
            <line x1={xc(i)} x2={xc(i)} y1={TOP - 6} y2={plotH} stroke={GRID} />
            <text x={xc(i)} y={plotH + 15} fontSize={10} fill={AXIS} fontFamily={FONT} textAnchor="middle">
              {label}
            </text>
          </g>
        ))}
        <line x1={plotW} x2={plotW} y1={0} y2={plotH} stroke="#dce4e7" />
        <line x1={0} x2={plotW} y1={plotH} y2={plotH} stroke="#dce4e7" />

        <g clipPath={`url(#${clipId}-price)`}>
          {volumes}
          {candles}
          {(["ema200", "ema50", "ema20"] as const).map((key) =>
            overlays[key] ? (
              <path key={key} d={linePath(series[key], y)} fill="none" stroke={EMA_COLORS[key]} strokeWidth={1.4} />
            ) : null,
          )}
          {intraday && overlays.vwap ? (
            <path d={linePath(series.vwap, y)} fill="none" stroke={VWAP_COLOR} strokeWidth={1.4} strokeDasharray="5 2" />
          ) : null}
          {levelLines.map(([label, value]) => (
            <g key={label}>
              <line x1={0} x2={plotW} y1={y(value)} y2={y(value)} stroke={AXIS} strokeDasharray="2 4" />
              <text x={plotW - 4} y={y(value) - 4} fontSize={9.5} fill={AXIS} fontFamily={FONT} textAnchor="end">
                {label} {formatPrice(value)}
              </text>
            </g>
          ))}
          {(guideLines ?? [])
            .filter(({ price }) => price >= lo && price <= hi)
            .map(({ price, label }, index) => (
              <g key={`guide-${index}`}>
                <line x1={0} x2={plotW} y1={y(price)} y2={y(price)} stroke={GUIDE_COLOR} strokeDasharray="1 3" opacity={0.8} />
                <text x={plotW - 4} y={y(price) - 4} fontSize={9.5} fill={GUIDE_COLOR} fontFamily={FONT} textAnchor="end">
                  {label} {formatPrice(price)}
                </text>
              </g>
            ))}
          {zoneDraw ? (
            <rect x={0} width={plotW} y={clampY(y(Math.max(zoneDraw.from, zoneDraw.to)))} height={Math.abs(clampY(y(zoneDraw.from)) - clampY(y(zoneDraw.to)))} fill={ALERT_COLOR} opacity={0.14} stroke={ALERT_COLOR} strokeDasharray="4 3" />
          ) : null}
          {(alertLines ?? []).map((line) => {
            const dragging = alertDrag?.id === line.id;
            const price = dragging && alertDrag.edge === "level" ? alertDrag.price : line.price;
            const price2 = line.price2 == null ? null : dragging && alertDrag.edge === "level2" ? alertDrag.price : line.price2;
            const top = Math.max(price, price2 ?? price);
            const bottom = Math.min(price, price2 ?? price);
            if (top < lo || bottom > hi) return null;
            const edges = price2 == null ? [price] : [price, price2];
            // Line labels sit above the line on the left; zone labels inside
            // the band on the right, so a nearby line alert stays readable.
            const labelY = price2 != null ? clampY(y(top)) + 11 : clampY(y(top)) - 4 < yTop ? clampY(y(top)) + 11 : clampY(y(top)) - 4;
            return (
              <g key={`alert-${line.id}`} opacity={line.active || dragging ? 1 : 0.4}>
                {price2 != null ? (
                  <rect x={0} width={plotW} y={clampY(y(top))} height={Math.max(0, clampY(y(bottom)) - clampY(y(top)))} fill={ALERT_COLOR} opacity={0.08} />
                ) : null}
                {dragging ? (
                  <line x1={0} x2={plotW} y1={y(alertDrag.from)} y2={y(alertDrag.from)} stroke={ALERT_COLOR} strokeDasharray="2 4" opacity={0.45} />
                ) : null}
                {edges
                  .filter((edge) => edge >= lo && edge <= hi)
                  .map((edge, index) => (
                    <line key={index} x1={0} x2={plotW} y1={y(edge)} y2={y(edge)} stroke={ALERT_COLOR} strokeDasharray={dragging ? undefined : "6 3"} strokeWidth={dragging ? 1.5 : 1} />
                  ))}
                <text x={price2 != null ? plotW - 4 : 4} y={labelY} fontSize={9.5} fill={ALERT_COLOR} fontFamily={FONT} textAnchor={price2 != null ? "end" : "start"}>
                  {price2 != null ? `ZONE ${formatPrice(bottom)} – ${formatPrice(top)}` : `ALERT ${formatPrice(price)}`}
                </text>
              </g>
            );
          })}
          {(markers ?? []).map((marker) => {
            // Bar containing the trigger (intraday bars are labelled by display-zone open time).
            const label = wallLabel(Date.parse(marker.ts) - 60000, displayTz);
            const key = intraday ? label : label.slice(0, 10);
            let index = -1;
            for (let i = len - 1; i >= 0; i--) {
              if (bars[i].date <= key) {
                index = i;
                break;
              }
            }
            if (index < v.start || index >= end || marker.price < lo || marker.price > hi) return null;
            return (
              <g key={`marker-${marker.id}`}>
                <title>{marker.label}</title>
                <circle cx={xc(index)} cy={y(marker.price)} r={4.5} fill={ALERT_COLOR} stroke="#fff" strokeWidth={1.5} />
              </g>
            );
          })}
          {lastY >= yTop && lastY <= yBot ? (
            <line x1={0} x2={plotW} y1={lastY} y2={lastY} stroke={lastUp ? UP : DOWN} strokeDasharray="3 3" opacity={0.7} />
          ) : null}
        </g>
        {lastY >= yTop && lastY <= yBot ? (
          <g>
            <rect x={plotW + 1} y={lastY - 8} width={AXIS_W - 2} height={16} rx={2} fill={lastUp ? UP : DOWN} />
            <text x={plotW + 6} y={lastY + 3.5} fontSize={10} fill="#fff" fontFamily={FONT}>
              {formatPrice(last.close)}
            </text>
          </g>
        ) : null}

        {overlays.rsi ? (
          <g>
            <line x1={0} x2={plotW + AXIS_W} y1={priceH + 2} y2={priceH + 2} stroke="#dce4e7" />
            <rect x={0} y={ry(70)} width={plotW} height={ry(30) - ry(70)} fill={RSI_COLOR} opacity={0.06} />
            {[70, 50, 30].map((level) => (
              <g key={level}>
                <line x1={0} x2={plotW} y1={ry(level)} y2={ry(level)} stroke={AXIS} strokeDasharray={level === 50 ? "1 4" : "3 3"} opacity={0.6} />
                <text x={plotW + 6} y={ry(level) + 3.5} fontSize={10} fill={AXIS} fontFamily={FONT}>
                  {level}
                </text>
              </g>
            ))}
            <g clipPath={`url(#${clipId}-rsi)`}>
              <path d={linePath(series.rsi, ry)} fill="none" stroke={RSI_COLOR} strokeWidth={1.3} />
            </g>
            <text x={6} y={rsiTop + 10} fontSize={10} fill={AXIS} fontFamily={FONT}>
              RSI 14
            </text>
          </g>
        ) : null}
      </>
    );
    // y/xc/ry are derived from the listed values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, series, overlays, levels, alertLines, alertDrag, guideLines, markers, zoneDraw, intraday, monthly, v.start, v.count, plotW, plotH, priceH, len, clipId, displayTz]);

  // ---- interaction --------------------------------------------------------

  const localPoint = (event: { clientX: number; clientY: number }) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    return { x: event.clientX - (rect?.left ?? 0), y: event.clientY - (rect?.top ?? 0) };
  };

  /** Draggable alert line (or zone edge) within 5px of ``py`` (price pane only). */
  const alertAt = (px: number, py: number) => {
    if (!onAlertMove || px > plotW || py < yTop || py > yBot) return null;
    let best: { id: string; edge: "level" | "level2"; price: number; gap: number } | null = null;
    for (const line of alertLines ?? []) {
      if (!line.draggable) continue;
      const edges: Array<["level" | "level2", number]> = [["level", line.price]];
      if (line.price2 != null) edges.push(["level2", line.price2]);
      for (const [edge, price] of edges) {
        if (price < lo || price > hi) continue;
        const gap = Math.abs(y(price) - py);
        if (gap <= 5 && (!best || gap < best.gap)) best = { id: line.id, edge, price, gap };
      }
    }
    return best;
  };
  const clampPrice = (py: number) => snapPrice(priceAt(Math.min(yBot, Math.max(yTop, py))), tick);
  const capture = (event: React.PointerEvent<SVGSVGElement>) => {
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // pointer already released (e.g. synthetic events): dragging still works inside the chart
    }
  };
  const inPricePane = (point: { x: number; y: number }) => point.x <= plotW && point.y >= yTop && point.y <= yBot;

  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!len || !step) return;
    const point = localPoint(event);
    if (zoneDraw) {
      setZoneDraw({ ...zoneDraw, to: clampPrice(point.y) });
    } else if (alertDrag) {
      setAlertDrag({ ...alertDrag, price: clampPrice(point.y) });
    } else if (!drag.current) {
      setOverAlert(Boolean(alertAt(point.x, point.y)));
    }
    if (drag.current) {
      const { x, start } = drag.current;
      setView((current) => clampView(start - (event.clientX - x) / step, current.count, len));
    }
    if (point.x > plotW || point.y > plotH) {
      setHover(null);
      return;
    }
    const i = Math.min(end - 1, Math.max(v.start, v.start + Math.floor(point.x / step)));
    setHover({ i, y: point.y });
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    const shift = Math.max(1, Math.round(v.count / 10));
    if (event.key === "ArrowLeft") setView(clampView(v.start - shift, v.count, len));
    else if (event.key === "ArrowRight") setView(clampView(v.start + shift, v.count, len));
    else if (event.key === "+" || event.key === "=") setView(clampView(v.start + v.count * 0.1, v.count * 0.8, len));
    else if (event.key === "-") setView(clampView(v.start - v.count * 0.125, v.count * 1.25, len));
    else return;
    event.preventDefault();
  };

  // ---- legend + crosshair -------------------------------------------------

  const focus = hover ? hover.i : len - 1;
  const bar = bars[focus];
  const prev = focus > 0 ? bars[focus - 1] : null;
  const change = bar && prev ? bar.close - prev.close : 0;
  const changePct = bar && prev && prev.close ? (change / prev.close) * 100 : 0;
  const tone = change >= 0 ? UP : TEXT_DOWN;

  return (
    <div
      ref={wrapRef}
      className="ohlc-chart"
      tabIndex={0}
      onKeyDown={onKeyDown}
      aria-label={`${title} candlestick chart. Scroll to zoom, drag to pan, drag an alert line to move it, Alt+drag to draw an alert zone, right-click for alert options, arrow keys to move, double-click to reset.`}
    >
      {size.w > 0 && size.h > 0 ? (
        <svg
          width={size.w}
          height={size.h}
          className="ohlc-svg"
          onPointerDown={(event) => {
            if (event.button === 2) return; // right-click: context menu
            const point = localPoint(event);
            if (event.altKey && (onAltClick || onZoneDraw)) {
              // Alt+click picks a level; Alt+drag draws a zone.
              if (inPricePane(point)) {
                const price = clampPrice(point.y);
                setZoneDraw({ from: price, to: price, y0: point.y });
                capture(event);
              }
              return;
            }
            const grabbed = alertAt(point.x, point.y);
            if (grabbed) {
              // Drag the alert line instead of panning; saved on release.
              setAlertDrag({ id: grabbed.id, edge: grabbed.edge, price: grabbed.price, from: grabbed.price });
            } else {
              drag.current = { x: event.clientX, start: v.start };
            }
            capture(event);
          }}
          onPointerMove={onPointerMove}
          onPointerUp={(event) => {
            drag.current = null;
            if (zoneDraw) {
              const moved = Math.abs(localPoint(event).y - zoneDraw.y0) >= 4;
              if (moved && onZoneDraw && zoneDraw.from !== zoneDraw.to) onZoneDraw(Math.min(zoneDraw.from, zoneDraw.to), Math.max(zoneDraw.from, zoneDraw.to));
              else onAltClick?.(zoneDraw.from);
              setZoneDraw(null);
            }
            if (alertDrag) {
              if (alertDrag.price !== alertDrag.from) onAlertMove?.(alertDrag.id, alertDrag.edge, alertDrag.price);
              setAlertDrag(null);
            }
          }}
          onPointerCancel={() => {
            drag.current = null;
            setAlertDrag(null);
            setZoneDraw(null);
          }}
          onContextMenu={(event) => {
            if (!onContextMenu) return;
            const point = localPoint(event);
            if (!inPricePane(point)) return;
            event.preventDefault();
            onContextMenu(clampPrice(point.y), event.clientX, event.clientY);
          }}
          onPointerLeave={() => {
            if (!drag.current && !alertDrag) setHover(null);
            setOverAlert(false);
          }}
          style={alertDrag || overAlert ? { cursor: "ns-resize" } : undefined}
          onDoubleClick={() => setView(resetView(len))}
        >
          <defs>
            <clipPath id={`${clipId}-price`}>
              <rect x={0} y={0} width={plotW} height={priceH} />
            </clipPath>
            <clipPath id={`${clipId}-rsi`}>
              <rect x={0} y={rsiTop - 2} width={plotW} height={Math.max(0, rsiBot - rsiTop + 4)} />
            </clipPath>
          </defs>
          <rect x={0} y={0} width={size.w} height={size.h} fill="#ffffff" />
          {scene}

          {hover && bar ? (
            <g pointerEvents="none">
              <line x1={xc(hover.i)} x2={xc(hover.i)} y1={0} y2={plotH} stroke={INK} strokeDasharray="3 3" opacity={0.45} />
              {hover.y < priceH ? (
                <>
                  <line x1={0} x2={plotW} y1={hover.y} y2={hover.y} stroke={INK} strokeDasharray="3 3" opacity={0.45} />
                  <rect x={plotW + 1} y={hover.y - 8} width={AXIS_W - 2} height={16} rx={2} fill={INK} />
                  <text x={plotW + 6} y={hover.y + 3.5} fontSize={10} fill="#fff" fontFamily={FONT}>
                    {formatPrice(priceAt(hover.y))}
                  </text>
                </>
              ) : null}
              <rect x={Math.min(Math.max(0, xc(hover.i) - 56), plotW - 112)} y={plotH + 2} width={112} height={18} rx={2} fill={INK} />
              <text
                x={Math.min(Math.max(56, xc(hover.i)), plotW - 56)}
                y={plotH + 15}
                fontSize={10}
                fill="#fff"
                fontFamily={FONT}
                textAnchor="middle"
              >
                {bar.date.replace("T", " ")}
              </text>
            </g>
          ) : null}

          {bar ? (
            <text x={8} y={16} fontSize={11} fontFamily={FONT} fill={AXIS} pointerEvents="none">
              <tspan fill={INK} fontWeight={600}>
                {title}
              </tspan>
              <tspan dx={10}>O </tspan>
              <tspan fill={tone}>{formatPrice(bar.open)}</tspan>
              <tspan dx={6}>H </tspan>
              <tspan fill={tone}>{formatPrice(bar.high)}</tspan>
              <tspan dx={6}>L </tspan>
              <tspan fill={tone}>{formatPrice(bar.low)}</tspan>
              <tspan dx={6}>C </tspan>
              <tspan fill={tone}>{formatPrice(bar.close)}</tspan>
              <tspan dx={6} fill={tone}>
                {change >= 0 ? "+" : ""}
                {formatPrice(change)} ({change >= 0 ? "+" : ""}
                {changePct.toFixed(2)}%)
              </tspan>
              {overlays.volume ? <tspan dx={8}>Vol {formatVolume(bar.volume || 0)}</tspan> : null}
              {(["ema20", "ema50", "ema200"] as const).map((key) => {
                const value = series[key][focus];
                return overlays[key] && value != null ? (
                  <tspan key={key} dx={8} fill={EMA_COLORS[key]}>
                    {key.replace("ema", "EMA")} {formatPrice(value)}
                  </tspan>
                ) : null;
              })}
              {intraday && overlays.vwap && series.vwap[focus] != null ? (
                <tspan dx={8} fill={VWAP_COLOR}>
                  VWAP {formatPrice(series.vwap[focus] as number)}
                </tspan>
              ) : null}
            </text>
          ) : null}
          {overlays.rsi && bar && series.rsi[focus] != null ? (
            <text x={56} y={rsiTop + 10} fontSize={10} fill={RSI_COLOR} fontFamily={FONT} pointerEvents="none">
              {(series.rsi[focus] as number).toFixed(1)}
            </text>
          ) : null}
        </svg>
      ) : null}
    </div>
  );
}
