// Formatting helpers for the subscription-usage panel. Kept out of the
// component file so it only exports components (react-refresh constraint).

// Compact token formatting (e.g. 142,200,916 -> "142.2M") so the tiles stay
// glanceable. A null count renders a real label so empty data never becomes a
// punctuation glyph.
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "No data";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${trimDecimal(value / 1000)}K`;
  if (value < 1_000_000_000) return `${trimDecimal(value / 1_000_000)}M`;
  return `${trimDecimal(value / 1_000_000_000)}B`;
}

// Minutes-to-reset as a friendly "Xh Ym" / "Ym" string. 0 means the window is
// rolling over right now.
export function formatReset(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "No reset";
  if (minutes <= 0) return "now";
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours <= 0) return `${mins}m`;
  if (hours >= 24) {
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    if (remHours === 0) return `${days}d`;
    return `${days}d ${remHours}h`;
  }
  if (mins === 0) return `${hours}h`;
  return `${hours}h ${mins}m`;
}

// One decimal place, but drop a trailing ".0" so round numbers read cleanly
// (1.0K -> "1K", 75.8K -> "75.8K").
function trimDecimal(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "");
}
