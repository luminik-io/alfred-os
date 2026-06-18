import { describe, expect, it } from "vitest";

import {
  buildActiveThreads,
  buildCostHealth,
  buildNeedsYou,
  buildRunning,
  buildShippedDigest,
  dedupePlans,
  planNeedsAttention,
  threadForCompose,
} from "./derive";
import type { FiringRecord, PlanDraft, ShippedBoard, ShippedCard, Snapshot } from "../types";

function emptySnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    loadedAt: new Date("2026-05-30T12:00:00Z"),
    shipped: null,
    schedule: [],
    status: { agents: [], total_today: 0, reliability: {} },
    actions: {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    memoryCandidates: { rows: [] },
    firings: [],
    plans: [],
    trustedSlack: null,
    ...overrides,
  };
}

function firing(overrides: Partial<FiringRecord> = {}): FiringRecord {
  return {
    firing_id: "f1",
    codename: "lucius",
    started_at: "2026-05-30T11:00:00Z",
    ended_at: "2026-05-30T11:05:00Z",
    status: "ok",
    summary: "ok",
    transcript_path: null,
    events_path: "",
    ...overrides,
  };
}

function shippedCard(overrides: Partial<ShippedCard> = {}): ShippedCard {
  return {
    repo: "your-org/api",
    number: 12,
    title: "Add CSV export",
    url: "https://example.com/12",
    author: "lucius",
    kind: "pr",
    timestamp: "2026-05-30T11:00:00Z",
    age_days: 0,
    is_draft: false,
    labels: [],
    ...overrides,
  };
}

function board(overrides: Partial<ShippedBoard> = {}): ShippedBoard {
  return {
    generated_at: "2026-05-30T12:00:00Z",
    lookback_days: 14,
    repos: ["your-org/api"],
    columns: { queued: [], in_progress: [], shipped: [] },
    counts: { queued: 0, in_progress: 0, shipped: 0 },
    errors: [],
    ...overrides,
  };
}

describe("buildNeedsYou (calm client-owned decisions)", () => {
  it("returns a connect prompt when there is no snapshot", () => {
    const items = buildNeedsYou(null);
    expect(items).toHaveLength(1);
    expect(items[0].id).toBe("connect");
  });

  it("surfaces a genuine Batman go/no-go plan as the human-in-the-loop sign-off", () => {
    const base: PlanDraft = {
      plan_id: "13-plan",
      title: "Plan A",
      status: "draft",
      parent: null,
      affected_repos: null,
      updated_at: null,
      path: "",
      preview: "Review before work starts.",
      content: "",
      source: "batman",
      readiness_score: null,
      readiness_ok: true,
      revision_count: 0,
    };
    const items = buildNeedsYou(emptySnapshot({ plans: [base] }));
    expect(items[0].id).toBe("plan-13-plan");
    expect(items[0].targetTab).toBe("pipeline");
    // The single-plan card carries the plan id so it can offer in-place
    // approve/decline.
    expect(items[0].planId).toBe("13-plan");
  });

  it("does NOT count compose drafts or stale Slack follow-ups as decisions waiting", () => {
    // The old count conflated batman-plans (real go/no-go), planning-drafts
    // (Compose working docs), and followups (stale Slack snapshots). Only
    // source==="batman" is a genuine decision before work starts.
    const draftLike = (overrides: Partial<PlanDraft>): PlanDraft => ({
      plan_id: "x",
      title: "t",
      status: "draft",
      parent: null,
      affected_repos: null,
      updated_at: null,
      path: "",
      preview: "",
      content: "",
      source: "compose",
      readiness_score: null,
      readiness_ok: true,
      revision_count: 0,
      ...overrides,
    });
    const items = buildNeedsYou(
      emptySnapshot({
        plans: [
          draftLike({ plan_id: "d1", source: "compose", status: "draft" }),
          draftLike({ plan_id: "d2", source: "planning", status: "needs scope" }),
          draftLike({ plan_id: "f1", source: "followup", status: "needs follow-up" }),
        ],
      }),
    );
    // None of the three are go/no-go decisions, so Needs-you has no plan item.
    expect(items.filter((item) => item.icon === "plan")).toHaveLength(0);
  });

  it("prefers concrete memory candidates over promotion suggestion counts", () => {
    const snap = emptySnapshot({
      memoryCandidates: {
        rows: [
          {
            id: "mem:1",
            codename: "lucius",
            repo: "your-org/api",
            body: "Use request fixtures.",
            tags: ["tests"],
            severity: "info",
            source: "slack",
            source_firing_id: null,
            evidence: "",
            confidence: 0.8,
            status: "candidate",
            created_at: "2026-05-30T12:00:00Z",
          },
        ],
      },
      actions: {
        status: "ok",
        actions: [],
        failure_patterns: [],
        stale_workers: [],
        promotion_suggestions: [{ title: "Fallback" }],
      },
    });
    const items = buildNeedsYou(snap);
    expect(items[0].id).toBe("memory-review");
    expect(items[0].title).toBe("1 memory candidate ready");
  });

  it("does not include reliability inspection signals (those are operator depth)", () => {
    const snap = emptySnapshot({
      actions: {
        status: "ok",
        actions: [{ title: "Stale worker", message: "x" }],
        failure_patterns: [{ agent: "lucius", subtype: "diff-too-large", count: 3 }],
        stale_workers: [],
        promotion_suggestions: [],
      },
    });
    expect(buildNeedsYou(snap)).toHaveLength(0);
  });
});

describe("planNeedsAttention", () => {
  const base: PlanDraft = {
    plan_id: "13-plan",
    title: "t",
    status: "draft",
    parent: null,
    affected_repos: null,
    updated_at: null,
    path: "",
    preview: "",
    content: "",
    source: "batman",
    readiness_score: null,
    readiness_ok: true,
    revision_count: 0,
  };
  it("flags an awaiting Batman go/no-go plan", () => {
    expect(planNeedsAttention({ ...base, status: "draft" })).toBe(true);
    expect(planNeedsAttention({ ...base, status: "Draft (awaiting approval)" })).toBe(true);
    expect(planNeedsAttention({ ...base, status: "blocked" })).toBe(true);
  });
  it("excludes non-Batman sources (compose drafts, Slack follow-ups)", () => {
    expect(planNeedsAttention({ ...base, source: "compose", status: "draft" })).toBe(false);
    expect(planNeedsAttention({ ...base, source: "planning", status: "draft" })).toBe(false);
    expect(planNeedsAttention({ ...base, source: "followup", status: "needs follow-up" })).toBe(
      false,
    );
  });
  it("drops a decided Batman plan out of the queue", () => {
    expect(planNeedsAttention({ ...base, status: "approved" })).toBe(false);
    expect(planNeedsAttention({ ...base, status: "declined" })).toBe(false);
  });
});

describe("dedupePlans", () => {
  const base: PlanDraft = {
    plan_id: "p1",
    title: "Alfred planning draft",
    status: "draft",
    parent: null,
    affected_repos: "your-org/api",
    updated_at: "2026-06-01T10:00:00Z",
    path: "",
    preview: "",
    content: "",
    source: "compose",
    readiness_score: null,
    readiness_ok: true,
    revision_count: 0,
  };

  it("keeps placeholder-titled drafts distinct even when repos match", () => {
    const rows = dedupePlans([
      base,
      {
        ...base,
        plan_id: "p2",
        updated_at: "2026-06-01T11:00:00Z",
      },
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.plan.plan_id)).toEqual(["p1", "p2"]);
    expect(rows.map((row) => row.revisions)).toEqual([1, 1]);
  });

  it("keeps Batman approval plans distinct from matching compose drafts", () => {
    const rows = dedupePlans([
      {
        ...base,
        plan_id: "compose",
        title: "Add CSV export",
      },
      {
        ...base,
        plan_id: "batman",
        title: "Add CSV export",
        source: "batman",
      },
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.plan.plan_id)).toEqual(["compose", "batman"]);
  });

  it("uses compose revision_count as the visible revision count", () => {
    const rows = dedupePlans([
      {
        ...base,
        plan_id: "compose",
        title: "Add CSV export",
        revision_count: 2,
      },
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0].revisions).toBe(2);
  });
});

describe("buildRunning", () => {
  it("collects running firings and shows an empty upcoming lane with no schedule", () => {
    const snap = emptySnapshot({
      firings: [firing({ status: "running" }), firing({ firing_id: "f2", status: "ok" })],
    });
    const running = buildRunning(snap);
    expect(running.running).toHaveLength(1);
    // No schedule rows in the snapshot -> nothing upcoming.
    expect(running.hasUpcoming).toBe(false);
    expect(running.upcoming).toEqual([]);
  });

  it("surfaces upcoming scheduled runs from the snapshot schedule", () => {
    const snap = emptySnapshot({
      firings: [firing({ status: "running" })],
      schedule: [
        {
          codename: "bane",
          role: "Daily test author",
          kind: "cron-daily",
          cadence: "daily 02:00",
          next_fire_at: "2026-06-04T02:00:00",
          raw_schedule: "cron:2:00",
        },
        {
          codename: "lucius",
          role: "Single-repo engineer",
          kind: "interval",
          cadence: "every 10m",
          next_fire_at: null,
          raw_schedule: "interval:600",
        },
      ],
    });
    const running = buildRunning(snap);
    expect(running.hasUpcoming).toBe(true);
    expect(running.upcoming.map((run) => run.codename)).toEqual(["bane", "lucius"]);
  });
});

describe("buildShippedDigest", () => {
  it("renders a plain-English what/why per merged PR", () => {
    const digest = buildShippedDigest(
      board({
        columns: {
          queued: [],
          in_progress: [],
          shipped: [shippedCard({ title: "feat: add CSV export" })],
        },
        counts: { queued: 0, in_progress: 0, shipped: 1 },
      }),
    );
    expect(digest).toHaveLength(1);
    // Conventional-commit prefix is stripped and the sentence is capitalised.
    expect(digest[0].what).toBe("Add CSV export.");
    expect(digest[0].agent).toBe("Lucius");
    expect(digest[0].why).toContain("merged into api");
    // Card hygiene: the agent is in the `agent` field/badge, not repeated in the sentence.
    expect(digest[0].why).toMatch(/^Shipped and merged into api/);
    expect(digest[0].why).not.toContain("Lucius");
  });
});

describe("buildCostHealth", () => {
  it("sums success/fail and reports a null spend when no cost data is present", () => {
    const snap = emptySnapshot({
      status: { agents: [], total_today: 4, reliability: {} },
      firings: [firing({ status: "ok" }), firing({ firing_id: "f2", status: "error" })],
    });
    const health = buildCostHealth(snap);
    expect(health.runsToday).toBe(4);
    expect(health.succeeded).toBe(1);
    expect(health.failed).toBe(1);
    // No cost_usd on the firings -> spend is null (flagged), not $0.
    expect(health.spendUsd).toBeNull();
  });

  it("aggregates cost_usd when firings carry it (fallback path, no server rollup)", () => {
    const withCost = (cost: number, id: string): FiringRecord =>
      ({ ...firing({ firing_id: id }), cost_usd: cost }) as FiringRecord;
    const snap = emptySnapshot({ firings: [withCost(0.5, "a"), withCost(1.25, "b")] });
    const health = buildCostHealth(snap);
    expect(health.spendUsd).toBeCloseTo(1.75);
    expect(health.spendIsTodayRollup).toBe(false);
  });

  it("prefers the server's today rollup over the firings fallback", () => {
    const snap = emptySnapshot({
      status: {
        agents: [],
        total_today: 9,
        reliability: {},
        metrics: { spend_usd: 4.2, firings: 9, successes: 7, failures: 2, agents_with_spend: 3 },
      },
      // Firings carry their own cost, but the rollup wins when present.
      firings: [{ ...firing({ firing_id: "a" }), cost_usd: 0.5 } as FiringRecord],
    });
    const health = buildCostHealth(snap);
    expect(health.spendUsd).toBeCloseTo(4.2);
    expect(health.spendIsTodayRollup).toBe(true);
    // ok/fail counts come from the whole-day rollup, not the visible window.
    expect(health.succeeded).toBe(7);
    expect(health.failed).toBe(2);
  });

  it("lets event-log agent failures raise a stale metrics failure total", () => {
    const snap = emptySnapshot({
      status: {
        agents: [
          {
            codename: "lucius",
            last_firing_id: "f1",
            last_run_at: "2026-06-01T10:00:00Z",
            status: "error",
            last_summary: "provider rate limit",
            firings_today: 1,
            failures_today: 1,
          },
        ],
        total_today: 1,
        reliability: {},
        metrics: { spend_usd: 0, firings: 1, successes: 0, failures: 0, agents_with_spend: 1 },
      },
    });

    const health = buildCostHealth(snap);

    expect(health.failed).toBe(1);
  });

  it("reports a null (not zero) spend from a rollup with no ledgers today", () => {
    const snap = emptySnapshot({
      status: {
        agents: [],
        total_today: 0,
        reliability: {},
        metrics: { spend_usd: null, firings: 0, successes: 0, failures: 0, agents_with_spend: 0 },
      },
    });
    const health = buildCostHealth(snap);
    expect(health.spendUsd).toBeNull();
    expect(health.spendIsTodayRollup).toBe(true);
  });
});

describe("request threads", () => {
  it("builds active threads from in-flight and queued board cards", () => {
    const threads = buildActiveThreads(
      board({
        columns: {
          queued: [shippedCard({ number: 5, kind: "issue", title: "Queued issue" })],
          in_progress: [shippedCard({ number: 9, title: "Open PR" })],
          shipped: [],
        },
        counts: { queued: 1, in_progress: 1, shipped: 0 },
      }),
    );
    expect(threads).toHaveLength(2);
    // Every thread flags that correlation across stages is approximate.
    expect(threads.every((t) => t.correlationApproximate)).toBe(true);
    // The plan step is "missing" because sign-off is not linked to the issue.
    const planStep = threads[0].steps.find((s) => s.key === "plan");
    expect(planStep?.state).toBe("missing");
  });

  it("builds a compose thread with intake done and plan active", () => {
    const thread = threadForCompose({
      draftId: "compose-1",
      title: "Add CSV export",
      repos: ["your-org/frontend"],
      ready: true,
    });
    expect(thread.steps.find((s) => s.key === "intake")?.state).toBe("done");
    expect(thread.steps.find((s) => s.key === "plan")?.state).toBe("active");
    expect(thread.repo).toBe("your-org/frontend");
    expect(thread.correlationApproximate).toBe(true);
  });
});
