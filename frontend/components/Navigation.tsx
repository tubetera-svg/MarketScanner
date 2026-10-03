"use client";

import { useEffect, useState } from "react";
import { Activity, Bell, Database, History, Rocket, Settings } from "lucide-react";
import SoundToggle from "./SoundToggle";
import AdminMenu from "./AdminMenu";
import { canOpen, useAuth } from "./auth";
import { fetchAppSettings } from "./appSettings";
import { subscribePriceAlerts } from "./priceAlertFeed";

type NavigationProps = {
  active: "/" | "/alerts" | "/watchlist" | "/ipo" | "/backtest" | "/settings";
};

export default function Navigation({ active }: NavigationProps) {
  const auth = useAuth();
  // Guests only see the pages Settings -> Guest access opens to them (Settings never).
  const guestHides = (page: "scanner" | "alerts" | "ipo" | "watchlist" | "backtest" | "settings") => !!auth && !canOpen(auth, page);
  const [hidden, setHidden] = useState<string[]>([]);
  useEffect(() => {
    fetchAppSettings<{ settings?: { ui?: { hidden_pages?: string[] } } }>()
      .then((data: { settings?: { ui?: { hidden_pages?: string[] } } }) => setHidden(data.settings?.ui?.hidden_pages ?? []))
      .catch(() => {});
  }, []);
  // Price alerts triggered in the current daily bar (per-market cut-off) for the Alerts link badge.
  const [triggeredAlerts, setTriggeredAlerts] = useState(0);
  useEffect(() => subscribePriceAlerts((data) => {
    if (data) setTriggeredAlerts(data.triggered_session_count ?? 0);
  }), []);
  return (
    <>
      {!guestHides("scanner") && <a className={`top-link${active === "/" ? " active" : ""}`} href="/" aria-current={active === "/" ? "page" : undefined}><Activity size={13} /> Scanner</a>}
      {!guestHides("alerts") && <a className={`top-link${active === "/alerts" ? " active" : ""}`} href="/alerts" aria-current={active === "/alerts" ? "page" : undefined} title={`${triggeredAlerts} price alert${triggeredAlerts === 1 ? "" : "s"} triggered today (resets at each market's daily bar cut-off)`}>
        <Bell size={13} /> Alerts{triggeredAlerts > 0 ? <span className="top-link-badge">{triggeredAlerts}</span> : null}
      </a>}
      {!hidden.includes("watchlist") && !guestHides("watchlist") && <a className={`top-link${active === "/watchlist" ? " active" : ""}`} href="/watchlist" aria-current={active === "/watchlist" ? "page" : undefined}><Database size={13} /> Database</a>}
      {!hidden.includes("ipo") && !guestHides("ipo") && <a className={`top-link${active === "/ipo" ? " active" : ""}`} href="/ipo" aria-current={active === "/ipo" ? "page" : undefined}><Rocket size={13} /> IPO</a>}
      {!hidden.includes("backtest") && !guestHides("backtest") && <a className={`top-link${active === "/backtest" ? " active" : ""}`} href="/backtest" aria-current={active === "/backtest" ? "page" : undefined}><History size={13} /> Backtest</a>}
      {!guestHides("settings") && <a className={`top-link${active === "/settings" ? " active" : ""}`} href="/settings" aria-current={active === "/settings" ? "page" : undefined}><Settings size={13} /> Settings</a>}
      <SoundToggle />
      <AdminMenu />
    </>
  );
}
