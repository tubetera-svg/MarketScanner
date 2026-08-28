import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Watchlist · QuantLens",
  description: "Read-only browser for the local SQLite market-data store",
};

export default function WatchlistLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
