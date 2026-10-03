"use client";

// "Copy" (table as TSV + HTML) and optional "Symbols" (TradingView list)
// buttons for result panels. Rows are built lazily on click. Safe inside a
// <summary>: clicks don't toggle the parent <details>.

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Check, Copy, ListPlus } from "lucide-react";
import { copyTable, copyText, symbolList, type CopyTable } from "./copyRows";

type Props = {
  table: () => CopyTable;
  symbols?: () => string[];
  /** Icon-only buttons (group headers). */
  compact?: boolean;
  /** What is being copied, for tooltips ("all strategy results"). */
  what?: string;
};

export default function CopyResultsButton({ table, symbols, compact = false, what = "these results" }: Props) {
  const [status, setStatus] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const flash = (text: string) => {
    setStatus(text);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setStatus(null), 2000);
  };
  const guard = (event: MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };
  const onCopyTable = async (event: MouseEvent) => {
    guard(event);
    const data = table();
    if (data.rows.length === 0) return flash("Nothing to copy");
    try {
      await copyTable(data);
      flash(`Copied ${data.rows.length} rows × ${data.header.length} cols`);
    } catch {
      flash("Copy failed");
    }
  };
  const onCopySymbols = async (event: MouseEvent) => {
    guard(event);
    const list = symbolList(symbols ? symbols() : []);
    if (!list) return flash("Nothing to copy");
    try {
      await copyText(list);
      flash(`Copied ${list.split(",").length} symbols`);
    } catch {
      flash("Copy failed");
    }
  };

  return (
    <span className={`copy-results${compact ? " compact" : ""}`}>
      <button type="button" className="button-secondary" onClick={onCopyTable} title={`Copy ${what} as a table (paste into Excel / Sheets)`} aria-label={`Copy ${what} as a table`}>
        {status ? <Check size={12} /> : <Copy size={12} />}{!compact && " Copy"}
      </button>
      {symbols && (
        <button type="button" className="button-secondary" onClick={onCopySymbols} title={`Copy symbols of ${what} as a TradingView list (NSE:ABC,NSE:XYZ)`} aria-label={`Copy symbols of ${what}`}>
          <ListPlus size={12} />{!compact && " Symbols"}
        </button>
      )}
      <span className="copy-results-status" role="status" aria-live="polite">{status ?? ""}</span>
    </span>
  );
}
