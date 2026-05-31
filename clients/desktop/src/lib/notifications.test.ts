import { beforeEach, describe, expect, it } from "vitest";

import {
  buildFeed,
  countUnseen,
  loadSeenIds,
  markAllSeen,
  persistSeenIds,
} from "./notifications";
import type { FiringRecord, Snapshot } from "../types";

function firing(overrides: Partial<FiringRecord> = {}): FiringRecord {
  return {
    firing_id: "f-1",
    codename: "lucius",
    started_at: "2026-05-30T10:00:00Z",
    ended_at: "2026-05-30T10:05:00Z",
    status: "ok",
    summary: "Shipped a fix.",
    transcript_path: null,
    events_path: "/events/f-1.jsonl",
    ...overrides,
  };
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    loadedAt: new Date("2026-05-30T12:00:00Z"),
    status: { agents: [], total_today: 0, reliability: {} },
    actions: {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    firings: [],
    plans: [],
    ...overrides,
  };
}

describe("buildFeed", () => {
  it("returns an empty feed without a snapshot", () => {
    expect(buildFeed(null)).toEqual([]);
  });

  it("turns firings and needs-you signals into items, needs-you first (no timestamp)", () => {
    const feed = buildFeed(
      snapshot({
        firings: [
          firing({ firing_id: "f-old", started_at: "2026-05-30T08:00:00Z", ended_at: "2026-05-30T08:01:00Z" }),
          firing({ firing_id: "f-new", started_at: "2026-05-30T11:00:00Z", ended_at: "2026-05-30T11:01:00Z" }),
        ],
        actions: {
          status: "needs-attention",
          actions: [{ codename: "bane", message: "Approval waiting on #42", action: "approve" }],
          failure_patterns: [],
          stale_workers: [],
          promotion_suggestions: [],
        },
      }),
    );
    // needs-you items have no timestamp, so they sort to the top.
    expect(feed[0].kind).toBe("needs-you");
    expect(feed[0].codename).toBe("bane");
    // firings are ordered newest-first after that.
    const firingIds = feed.filter((item) => item.kind === "firing").map((item) => item.id);
    expect(firingIds).toEqual(["firing:f-new", "firing:f-old"]);
  });

  it("tones error firings and failure patterns appropriately", () => {
    const feed = buildFeed(
      snapshot({
        firings: [firing({ firing_id: "f-err", status: "error" })],
        actions: {
          status: "needs-attention",
          actions: [],
          failure_patterns: [{ codename: "robin", message: "3 fails in a row" }],
          stale_workers: [],
          promotion_suggestions: [],
        },
      }),
    );
    expect(feed.find((item) => item.id === "firing:f-err")?.tone).toBe("error");
    expect(feed.find((item) => item.kind === "needs-you")?.tone).toBe("error");
  });

  it("gives needs-you signals a stable id across rebuilds", () => {
    const snap = snapshot({
      actions: {
        status: "needs-attention",
        actions: [{ codename: "bane", message: "Approval waiting" }],
        failure_patterns: [],
        stale_workers: [],
        promotion_suggestions: [],
      },
    });
    const a = buildFeed(snap)[0].id;
    const b = buildFeed(snap)[0].id;
    expect(a).toBe(b);
  });
});

describe("seen / unseen logic", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("counts every item as unseen initially", () => {
    const feed = buildFeed(snapshot({ firings: [firing(), firing({ firing_id: "f-2" })] }));
    expect(countUnseen(feed, new Set())).toBe(2);
  });

  it("markAllSeen marks the current feed and is pure on the input set", () => {
    const feed = buildFeed(snapshot({ firings: [firing(), firing({ firing_id: "f-2" })] }));
    const before = new Set<string>();
    const after = markAllSeen(feed, before);
    expect(before.size).toBe(0); // unchanged
    expect(countUnseen(feed, after)).toBe(0);
  });

  it("a new item arriving after mark-all is unseen again", () => {
    const first = buildFeed(snapshot({ firings: [firing({ firing_id: "f-1" })] }));
    const seen = markAllSeen(first, new Set());
    const second = buildFeed(
      snapshot({
        firings: [firing({ firing_id: "f-1" }), firing({ firing_id: "f-2", ended_at: "2026-05-30T11:30:00Z" })],
      }),
    );
    expect(countUnseen(second, seen)).toBe(1);
  });

  it("persists and reloads seen ids, pruning ids no longer in the feed", () => {
    const feed = buildFeed(snapshot({ firings: [firing({ firing_id: "f-1" })] }));
    const seen = markAllSeen(feed, new Set());
    // Add a stale id that is not in the current feed; it must be pruned out.
    seen.add("firing:gone");
    persistSeenIds(seen, feed);

    const reloaded = loadSeenIds();
    expect(reloaded.has("firing:f-1")).toBe(true);
    expect(reloaded.has("firing:gone")).toBe(false);
  });

  it("loadSeenIds returns an empty set on missing or corrupt storage", () => {
    expect(loadSeenIds().size).toBe(0);
    window.localStorage.setItem("alfred-desktop.notifications.seen", "{not an array");
    expect(loadSeenIds().size).toBe(0);
  });
});
