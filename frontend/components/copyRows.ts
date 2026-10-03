// Copy result rows to the clipboard: tab-separated text (pastes into Excel /
// Google Sheets as columns) plus an HTML table (Word / email), and a
// TradingView-style symbol list. Client-side only; values are copied raw
// (numbers stay numbers), times should be formatted by the caller via time.ts.

export type Cell = string | number | boolean | null | undefined;
export type CopyTable = { header: string[]; rows: Cell[][] };

const cellText = (value: Cell): string =>
  value == null ? "" : String(value).replace(/[\t\r\n]+/g, " ").trim();

const escapeHtml = (text: string): string =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export const toTsv = ({ header, rows }: CopyTable): string =>
  [header, ...rows].map((row) => row.map(cellText).join("\t")).join("\n");

export const toHtml = ({ header, rows }: CopyTable): string =>
  "<table><thead><tr>" + header.map((h) => `<th>${escapeHtml(h)}</th>`).join("") + "</tr></thead><tbody>"
  + rows.map((row) => "<tr>" + row.map((value) => `<td>${escapeHtml(cellText(value))}</td>`).join("") + "</tr>").join("")
  + "</tbody></table>";

/**
 * Columns for heterogeneous result objects: ``lead`` first (when any row has a
 * value), then every other key with a value in at least one row, ``ctx_*``
 * context keys after those, and ``tail`` keys last. ``skip`` keys are dropped.
 */
export const autoColumns = (rows: Record<string, unknown>[], lead: string[], tail: string[] = [], skip: string[] = []): string[] => {
  const present = new Set<string>();
  const order: string[] = [];
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (value == null || value === "" || present.has(key)) continue;
      present.add(key);
      order.push(key);
    }
  }
  const fixed = new Set([...lead, ...tail, ...skip]);
  const rest = order.filter((key) => !fixed.has(key));
  return [
    ...lead.filter((key) => present.has(key)),
    ...rest.filter((key) => !key.startsWith("ctx_")),
    ...rest.filter((key) => key.startsWith("ctx_")),
    ...tail.filter((key) => present.has(key)),
  ];
};

/** TradingView symbol ("NSE:RELIANCE") from a chart link, else the symbol with an optional default exchange. */
export const tvSymbol = (symbol: string, link?: string | null, defaultExchange?: string): string => {
  if (link) {
    try {
      const fromLink = new URL(link).searchParams.get("symbol");
      if (fromLink) return fromLink.toUpperCase();
    } catch {
      // fall through to the bare symbol
    }
  }
  const value = symbol.trim().toUpperCase();
  return value.includes(":") || !defaultExchange ? value : `${defaultExchange}:${value}`;
};

/** Comma-separated, de-duplicated list (TradingView watchlist import / paste). */
export const symbolList = (symbols: string[]): string => Array.from(new Set(symbols.filter(Boolean))).join(",");

const legacyCopy = (text: string): boolean => {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } finally {
    area.remove();
  }
};

export const copyText = async (text: string): Promise<void> => {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // insecure context / permission denied: try the legacy path
  }
  if (!legacyCopy(text)) throw new Error("Clipboard is not available in this browser");
};

/** Copy a table as TSV + HTML (rich paste), falling back to plain text. */
export const copyTable = async (table: CopyTable): Promise<void> => {
  const tsv = toTsv(table);
  try {
    if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([tsv], { type: "text/plain" }),
        "text/html": new Blob([toHtml(table)], { type: "text/html" }),
      })]);
      return;
    }
  } catch {
    // rich clipboard unsupported or refused: plain text below
  }
  await copyText(tsv);
};
