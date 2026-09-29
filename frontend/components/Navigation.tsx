"use client";

import { useEffect, useState } from "react";
import { Activity, Bell, Database, History, Rocket, Settings } from "lucide-react";

type NavigationProps = {
  active: "/" | "/alerts" | "/watchlist" | "/ipo" | "/backtest" | "/settings";
};

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export default function Navigation({ active }: NavigationProps) {
  const [hidden, setHidden] = useState<string[]>([]);
  useEffect(() => {
    fetch(`${API}/api/settings`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data: { settings?: { ui?: { hidden_pages?: string[] } } }) => setHidden(data.settings?.ui?.hidden_pages ?? []))
      .catch(() => {});
  }, []);
  // Price alerts triggered in the current daily bar (per-market cut-off) for the Alerts link badge.
  const [triggeredAlerts, setTriggeredAlerts] = useState(0);
  useEffect(() => {
    const load = () => {
      fetch(`${API}/api/price-alerts`, { cache: "no-store" })
        .then((response) => response.json())
        .then((data: { triggered_session_count?: number }) => setTriggeredAlerts(data.triggered_session_count ?? 0))
        .catch(() => {});
    };
    load();
    const id = window.setInterval(load, 30000);
    return () => window.clearInterval(id);
  }, []);
  return (
    <>
      <a className={`top-link${active === "/" ? " active" : ""}`} href="/" aria-current={active === "/" ? "page" : undefined}><Activity size={13} /> Scanner</a>
      <a className={`top-link${active === "/alerts" ? " active" : ""}`} href="/alerts" aria-current={active === "/alerts" ? "page" : undefined} title={`${triggeredAlerts} price alert${triggeredAlerts === 1 ? "" : "s"} triggered today (resets at each market's daily bar cut-off)`}>
        <Bell size={13} /> Alerts{triggeredAlerts > 0 ? <span className="top-link-badge">{triggeredAlerts}</span> : null}
      </a>
      {!hidden.includes("watchlist") && <a className={`top-link${active === "/watchlist" ? " active" : ""}`} href="/watchlist" aria-current={active === "/watchlist" ? "page" : undefined}><Database size={13} /> Database</a>}
      {!hidden.includes("ipo") && <a className={`top-link${active === "/ipo" ? " active" : ""}`} href="/ipo" aria-current={active === "/ipo" ? "page" : undefined}><Rocket size={13} /> IPO</a>}
      {!hidden.includes("backtest") && <a className={`top-link${active === "/backtest" ? " active" : ""}`} href="/backtest" aria-current={active === "/backtest" ? "page" : undefined}><History size={13} /> Backtest</a>}
      <a className={`top-link${active === "/settings" ? " active" : ""}`} href="/settings" aria-current={active === "/settings" ? "page" : undefined}><Settings size={13} /> Settings</a>
    </>
  );
}
