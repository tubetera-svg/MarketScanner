import { Activity, Database, Rocket } from "lucide-react";

type NavigationProps = {
  active: "/" | "/watchlist" | "/ipo" | "/backtest";
};

export default function Navigation({ active }: NavigationProps) {
  return (
    <>
      <a className={`top-link${active === "/" ? " active" : ""}`} href="/" aria-current={active === "/" ? "page" : undefined}><Activity size={13} /> Scanner</a>
      <a className={`top-link${active === "/watchlist" ? " active" : ""}`} href="/watchlist" aria-current={active === "/watchlist" ? "page" : undefined}><Database size={13} /> Database</a>
      <a className={`top-link${active === "/ipo" ? " active" : ""}`} href="/ipo" aria-current={active === "/ipo" ? "page" : undefined}><Rocket size={13} /> IPO</a>
      <a className={`top-link${active === "/backtest" ? " active" : ""}`} href="/backtest" aria-current={active === "/backtest" ? "page" : undefined}><Activity size={13} /> Backtest</a>
    </>
  );
}
