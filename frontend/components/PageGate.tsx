"use client";

// Renders a page only when the current role may open it (admin, or a guest page
// allowed in Settings -> Guest access). The page's own effects/fetches do not run
// otherwise. The API enforces the same rules; this only avoids a broken page.

import type { ReactNode } from "react";
import Navigation from "./Navigation";
import { canOpen, useAuth, type GuestPage } from "./auth";

type Active = "/" | "/alerts" | "/watchlist" | "/ipo" | "/backtest" | "/status" | "/settings";

export default function PageGate({ page, active, title, children }: { page: GuestPage | "settings"; active: Active; title: string; children: ReactNode }) {
  const auth = useAuth();
  if (auth && canOpen(auth, page)) return <>{children}</>;
  return (
    <main className="shell">
      <header className="topbar">
        <div className="top-title">
          <p className="kicker">Market Structure Monitor</p>
          <h1>{title}</h1>
        </div>
        <div className="top-actions"><Navigation active={active} /></div>
      </header>
      {auth && (
        <section className="panel" style={{ padding: 24, textAlign: "center", color: "var(--muted)", fontSize: 13 }}>
          This page is for the admin only. Use the 🔒 at the top to sign in.
        </section>
      )}
    </main>
  );
}
