export function friendlyTime(value: string | null | undefined, now = new Date()): string {
  const parsed = parseTime(value);
  if (!parsed) return value && value !== "never" ? value : "never";

  const deltaMs = now.getTime() - parsed.getTime();
  if (deltaMs >= 0 && deltaMs < 60_000) return "just now";
  if (deltaMs >= 60_000 && deltaMs < 3_600_000) {
    return `${Math.floor(deltaMs / 60_000)}m ago`;
  }
  if (deltaMs >= 3_600_000 && deltaMs < 86_400_000) {
    return `${Math.floor(deltaMs / 3_600_000)}h ago`;
  }

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDate(parsed, yesterday)) {
    return `yesterday ${timeOnly(parsed)}`;
  }

  const month = parsed.toLocaleString(undefined, { month: "short" });
  const day = parsed.getDate();
  if (parsed.getFullYear() === now.getFullYear()) {
    return `${month} ${day}, ${timeOnly(parsed)}`;
  }
  return `${month} ${day}, ${parsed.getFullYear()}`;
}

export function exactTime(value: string | null | undefined): string {
  const parsed = parseTime(value);
  if (!parsed) return value || "never";
  return parsed.toISOString().replace("T", " ").replace(".000Z", " UTC");
}

export function shortId(value: string | null | undefined): string {
  if (!value) return "";
  if (value.length <= 22) return value;
  return `${value.slice(0, 15)}...${value.slice(-4)}`;
}

export function plural(value: number, singular: string, pluralName = `${singular}s`): string {
  return `${value} ${value === 1 ? singular : pluralName}`;
}

export function titleCase(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function compactUrl(value: string): string {
  try {
    const url = new URL(value);
    return `${url.hostname}${url.pathname}`.replace(/\/$/, "");
  } catch {
    return value;
  }
}

function parseTime(value: string | null | undefined): Date | null {
  if (!value || value === "never") return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function sameDate(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function timeOnly(value: Date): string {
  return value.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
