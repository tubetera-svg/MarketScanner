import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "IPO Tracker · QuantLens",
  description: "Track newly-listed NSE IPOs since launch: listing price vs current/high/low",
};

export default function IPOLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}