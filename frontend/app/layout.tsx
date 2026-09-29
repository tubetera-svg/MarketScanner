import type { Metadata } from "next";
import "./globals.css";
import PriceAlertNotifier from "../components/PriceAlertNotifier";

export const metadata: Metadata = {
  title: "QuantLens",
  description: "Adaptive market structure monitor",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // PriceAlertNotifier: price-alert toasts/sound on every page.
  return <html lang="en"><body>{children}<PriceAlertNotifier /></body></html>;
}
