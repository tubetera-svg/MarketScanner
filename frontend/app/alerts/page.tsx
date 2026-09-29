"use client";

// Alerts: every price alert across symbols (created from any chart popup's
// Alert button) and the recently triggered feed.

import { useState } from "react";
import Navigation from "../../components/Navigation";
import PriceAlertList from "../../components/PriceAlertList";
import TradingViewChartModal, { type ChartTarget } from "../../components/TradingViewChartModal";

export default function AlertsPage() {
  const [chart, setChart] = useState<ChartTarget | null>(null);

  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>Alerts</h1>
        </div>
        <div className="top-actions">
          <Navigation active="/alerts" />
        </div>
      </header>

      <PriceAlertList onOpenChart={(symbol) => setChart({ symbol, sourceLink: null })} />

      {chart && <TradingViewChartModal key={chart.symbol} chart={chart} onClose={() => setChart(null)} />}
    </main>
  );
}
