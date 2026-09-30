"use client";

import { useEffect, useRef, useState } from "react";
import { Zap } from "lucide-react";
import TradingViewChartModal, { type ChartTarget } from "./TradingViewChartModal";
import { describeAlert, type PriceAlertEvent, type PriceAlertStatus } from "./PriceAlertPanel";
import { playAlertSound } from "./alertSound";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Mounted once in app/layout.tsx so price-alert triggers reach every page:
 * sound, a toast that opens the chart, and a desktop notification when the
 * browser allows it. Alerts are evaluated server-side (api/price_alerts.py).
 */
export default function PriceAlertNotifier() {
  const [toasts, setToasts] = useState<PriceAlertEvent[]>([]);
  const [chart, setChart] = useState<ChartTarget | null>(null);
  const announced = useRef<Set<string>>(new Set());
  const seeded = useRef(false);

  useEffect(() => {
    const poll = () => {
      fetch(`${API}/api/price-alerts`, { cache: "no-store" })
        .then((response) => response.json())
        .then((data: PriceAlertStatus) => {
          // The first poll only records past events, so a page load is silent.
          const fresh = data.events.filter((event) => !announced.current.has(event.id));
          fresh.forEach((event) => announced.current.add(event.id));
          const wasSeeded = seeded.current;
          seeded.current = true;
          if (!wasSeeded || !fresh.length) return;
          playAlertSound(true);
          setToasts((current) => [...current, ...fresh].slice(-5));
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            for (const event of fresh) {
              new Notification(`${event.symbol} ${describeAlert(event)}`, {
                body: `At ${event.price ?? "-"}${event.note ? ` · ${event.note}` : ""}`,
                tag: event.id,
              });
            }
          }
        })
        .catch(() => {});
    };
    poll();
    const id = window.setInterval(poll, 15000);
    return () => window.clearInterval(id);
  }, []);

  const dismiss = (id: string) => setToasts((current) => current.filter((item) => item.id !== id));

  // Toast actions: snooze a repeating alert, re-arm a one-shot one.
  const act = (event: PriceAlertEvent, body: Record<string, unknown>) => {
    dismiss(event.id);
    fetch(`${API}/api/price-alerts/${event.alert_id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => undefined);
  };

  return (
    <>
      {toasts.length > 0 && (
        <div className="price-alert-toasts" role="status" aria-live="assertive">
          {toasts.map((event) => (
            <div key={event.id} className="price-alert-toast">
              <div className="price-alert-toast-main">
                <button type="button" className="price-alert-toast-body" onClick={() => { setChart({ symbol: event.symbol, sourceLink: null, interval: "5m" }); dismiss(event.id); }} title="Open the 5m chart (the trigger is marked)">
                  <Zap size={14} />
                  <span>
                    <strong>{event.symbol}</strong> {describeAlert(event)}
                    <small>At {event.price ?? "-"}{event.note ? ` · ${event.note}` : ""}</small>
                  </span>
                </button>
                <div className="price-alert-toast-actions">
                  <button type="button" onClick={() => { setChart({ symbol: event.symbol, sourceLink: null, interval: "5m" }); dismiss(event.id); }}>Chart</button>
                  {event.trigger === "once" ? (
                    <button type="button" onClick={() => act(event, { status: "active" })} title="Watch this level again">Re-arm</button>
                  ) : (
                    <button type="button" onClick={() => act(event, { snooze_minutes: 30 })} title="Mute this alert for 30 minutes">Snooze 30m</button>
                  )}
                </div>
              </div>
              <button type="button" className="price-alert-toast-close" aria-label="Dismiss alert" onClick={() => dismiss(event.id)}>×</button>
            </div>
          ))}
        </div>
      )}
      {chart && <TradingViewChartModal key={chart.symbol} chart={chart} onClose={() => setChart(null)} />}
    </>
  );
}
