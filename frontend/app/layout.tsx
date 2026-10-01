import type { Metadata } from "next";
import "./globals.css";
import PriceAlertNotifier from "../components/PriceAlertNotifier";
import AudioUnlockBanner from "../components/AudioUnlockBanner";

export const metadata: Metadata = {
  title: "QuantLens",
  description: "Adaptive market structure monitor",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // PriceAlertNotifier: price-alert toasts/sound on every page.
  // AudioUnlockBanner: hint while the browser still blocks alert sounds.
  return <html lang="en"><body>{children}<PriceAlertNotifier /><AudioUnlockBanner /></body></html>;
}
