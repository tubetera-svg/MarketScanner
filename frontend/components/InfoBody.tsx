import type { ReactNode } from "react";

// Renders strategy_info.txt markdown (bullets, nested bullets, `code`, a leading
// "Summary:" line) as readable info-popover content. Shared by the scanner and
// Settings pages.
const renderInline = (text: string): ReactNode[] =>
  text.split(/(`[^`]+`)/g).filter(Boolean).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") && part.length > 2 ? <code key={index}>{part.slice(1, -1)}</code> : part.replace(/\*\*/g, ""),
  );

const lines = (text: string) => text.split(String.fromCharCode(10)).filter((line) => line.trim());

export const infoSummary = (text: string): string | null => {
  const first = lines(text)[0]?.trim() ?? "";
  return first.startsWith("Summary:") ? first.slice("Summary:".length).trim() : null;
};

export const renderInfoBody = (text: string, { skipSummary = false } = {}): ReactNode => (
  <>
    {lines(text).map((line, index) => {
      if (index === 0 && line.startsWith("Summary:")) {
        return skipSummary ? null : <p key={index} className="info-summary">{renderInline(line.slice("Summary:".length).trim())}</p>;
      }
      const bullet = /^(\s*)[-*]\s+(.*)$/.exec(line);
      if (!bullet) return <p key={index}>{renderInline(line.trim())}</p>;
      return <div key={index} className={`info-li${bullet[1].length >= 2 ? " nested" : ""}`}>{renderInline(bullet[2])}</div>;
    })}
  </>
);
