import type { FiringRecord, ReliabilitySignal, Snapshot } from "../types";

// Where a feed row leads when the operator clicks it: an agent's latest run,
// or the Lessons review queue for memory suggestions.
export type FeedTarget =
  | { type: "agent"; codename: string }
  | { type: "memory" };

// A single entry in the in-app notification center. This is the surface that
// replaces macOS banners: every firing and every governor "needs you" signal
// becomes one chronological item the operator can read here.
export type FeedItem = {
  id: string;
  kind: "firing" | "needs-you";
  tone: "ok" | "warn" | "error" | "info";
  codename: string | null;
  title: string;
  detail: string;
  // ISO timestamp used for ordering; may be null for signals without a time.
  at: string | null;
  href?: string;
  target?: FeedTarget;
};

// Governor action tokens (ask_human, retry_later) read like log noise when
// shown raw; the feed leads with this operator phrasing instead.
const ACTION_LABELS: Record<string, string> = {
  ask_human: "needs your decision",
  inspect: "worth a look",
  retry_later: "backing off, will retry",
  review_candidate: "lesson suggestion ready",
  memory_promotion: "lesson suggestion ready",
};

const MEMORY_ACTIONS = new Set(["review_candidate", "memory_promotion"]);

function humanizeAction(action: string): string {
  return ACTION_LABELS[action] || action.replace(/_/g, " ");
}

const SEEN_KEY = "alfred-desktop.notifications.seen";
const MAX_FEED = 60;

/**
 * Build the chronological activity feed from the snapshot the client already
 * polls: recent firings plus the governor's "needs you" reliability signals
 * (actions, stale workers, failure patterns). Newest first.
 */
export function buildFeed(snapshot: Snapshot | null): FeedItem[] {
  if (!snapshot) {
    return [];
  }
  const items: FeedItem[] = [];

  for (const firing of snapshot.firings) {
    items.push(firingToItem(firing));
  }

  const reliability = snapshot.actions;
  for (const [index, signal] of (reliability?.actions || []).entries()) {
    items.push(signalToItem(signal, `action-${index}`, "warn"));
  }
  for (const [index, signal] of (reliability?.failure_patterns || []).entries()) {
    items.push(signalToItem(signal, `failure-${index}`, "error"));
  }
  for (const [index, signal] of (reliability?.stale_workers || []).entries()) {
    items.push(signalToItem(signal, `stale-${index}`, "warn"));
  }

  return items.sort(byNewestFirst).slice(0, MAX_FEED);
}

function firingToItem(firing: FiringRecord): FeedItem {
  const tone: FeedItem["tone"] =
    firing.status === "error" ? "error" : firing.status === "running" ? "warn" : "ok";
  return {
    id: `firing:${firing.firing_id}`,
    kind: "firing",
    tone,
    codename: firing.codename,
    title: `${firing.codename} ${firingVerb(firing.status)}`,
    detail: firing.summary || "No summary recorded.",
    at: firing.ended_at || firing.started_at,
    target: { type: "agent", codename: firing.codename },
  };
}

function firingVerb(status: string): string {
  if (status === "error") return "hit an error";
  if (status === "running") return "is running";
  if (status === "ok") return "finished";
  return "fired";
}

function signalToItem(
  signal: ReliabilitySignal,
  idSuffix: string,
  tone: FeedItem["tone"],
): FeedItem {
  // Signals name their agent inconsistently across kinds; take any of them.
  const codename = signal.codename || signal.agent || signal.target || null;
  const action = signal.action || "";
  const title =
    signal.title ||
    (action
      ? codename
        ? `${codename} · ${humanizeAction(action)}`
        : humanizeAction(action)
      : codename
        ? `${codename} needs you`
        : "Agents need you");
  let detail =
    signal.message || signal.summary || signal.reason || "Open the local source before changing state.";
  // The server appends the raw action token to its message ("...: ask_human");
  // the title already says it in plain words, so drop the duplicate tail.
  if (action && /^[a-z0-9_]+$/i.test(action)) {
    const stripped = detail.replace(new RegExp(`[\\s:·-]*${action}\\s*$`), "").trim();
    if (stripped) detail = stripped;
  }
  const isMemory = MEMORY_ACTIONS.has(action) || signal.kind === "memory_promotion";
  return {
    id: `needs-you:${idSuffix}:${codename || ""}:${stableHash(title + detail)}`,
    kind: "needs-you",
    tone,
    codename,
    title,
    detail,
    // Governor signals carry no timestamp; they sort to the top as "now".
    at: null,
    target: isMemory ? { type: "memory" } : codename ? { type: "agent", codename } : undefined,
  };
}

function byNewestFirst(a: FeedItem, b: FeedItem): number {
  const at = a.at ? Date.parse(a.at) : Number.POSITIVE_INFINITY;
  const bt = b.at ? Date.parse(b.at) : Number.POSITIVE_INFINITY;
  const aSafe = Number.isNaN(at) ? 0 : at;
  const bSafe = Number.isNaN(bt) ? 0 : bt;
  return bSafe - aSafe;
}

// ---------------------------------------------------------------------------
// Seen / unseen tracking
// ---------------------------------------------------------------------------

export function loadSeenIds(): Set<string> {
  try {
    const raw = window.localStorage.getItem(SEEN_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed.filter((id) => typeof id === "string")) : new Set();
  } catch {
    return new Set();
  }
}

/**
 * Persist the seen-id set, pruned to the ids still present in the current feed
 * (plus a small cap) so localStorage does not grow without bound as old
 * firings age out.
 */
export function persistSeenIds(seen: Set<string>, feed: FeedItem[]): void {
  const liveIds = new Set(feed.map((item) => item.id));
  const pruned = [...seen].filter((id) => liveIds.has(id)).slice(0, MAX_FEED);
  try {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(pruned));
  } catch {
    // Storage may be unavailable (private mode); the badge simply resets.
  }
}

export function countUnseen(feed: FeedItem[], seen: Set<string>): number {
  return feed.reduce((total, item) => (seen.has(item.id) ? total : total + 1), 0);
}

/**
 * Mark every item currently in the feed as seen. Returns the new seen set so the
 * caller can persist and re-render. Pure with respect to the input set.
 */
export function markAllSeen(feed: FeedItem[], seen: Set<string>): Set<string> {
  const next = new Set(seen);
  for (const item of feed) {
    next.add(item.id);
  }
  return next;
}

// Small deterministic hash so a needs-you signal keeps a stable id across polls
// (it has no server id of its own) and the seen state survives a refresh.
function stableHash(value: string): string {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  return (hash >>> 0).toString(36);
}
