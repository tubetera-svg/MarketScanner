"use client";

// Alerts: every price alert across symbols (created from any chart popup's
// Alert button) and the recently triggered feed.

import { useState } from "react";
import Navigation from "../../components/Navigation";
import PriceAlertList from "../../components/PriceAlertList";
import TradingViewChartModal, { type ChartTarget } from "../../components/TradingViewChartModal";
import PageGate from "../../components/PageGate";

function AlertsPageContent() {
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

      <PriceAlertList onOpenChart={(symbol, interval) => setChart({ symbol, sourceLink: null, interval })} />

      {chart && <TradingViewChartModal key={chart.symbol} chart={chart} onClose={() => setChart(null)} />}
    </main>
  );
}

export default function AlertsPage() {
  return <PageGate page="alerts" active="/alerts" title="Alerts"><AlertsPageContent /></PageGate>;
}
