"use client";

import { useEffect, useState } from "react";
import { Activity, Database, History, Rocket, Settings } from "lucide-react";

type NavigationProps = {
  active: "/" | "/watchlist" | "/ipo" | "/backtest" | "/settings";
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
  return (
    <>
      <a className={`top-link${active === "/" ? " active" : ""}`} href="/" aria-current={active === "/" ? "page" : undefined}><Activity size={13} /> Scanner</a>
      {!hidden.includes("watchlist") && <a className={`top-link${active === "/watchlist" ? " active" : ""}`} href="/watchlist" aria-current={active === "/watchlist" ? "page" : undefined}><Database size={13} /> Database</a>}
      {!hidden.includes("ipo") && <a className={`top-link${active === "/ipo" ? " active" : ""}`} href="/ipo" aria-current={active === "/ipo" ? "page" : undefined}><Rocket size={13} /> IPO</a>}
      {!hidden.includes("backtest") && <a className={`top-link${active === "/backtest" ? " active" : ""}`} href="/backtest" aria-current={active === "/backtest" ? "page" : undefined}><History size={13} /> Backtest</a>}
      <a className={`top-link${active === "/settings" ? " active" : ""}`} href="/settings" aria-current={active === "/settings" ? "page" : undefined}><Settings size={13} /> Settings</a>
    </>
  );
}
