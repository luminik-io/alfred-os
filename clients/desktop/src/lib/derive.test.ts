import { describe, expect, it } from "vitest";

import { buildAttention, buildStats, planNeedsAttention } from "./derive";
import type { PlanDraft, Snapshot } from "../types";

function emptySnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
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
    trustedSlack: null,
    ...overrides,
  };
}

describe("derive helpers (extracted from App.tsx)", () => {
  it("returns a connect prompt when there is no snapshot", () => {
    const items = buildAttention(null, "http://127.0.0.1:7000");
    expect(items).toHaveLength(1);
    expect(items[0].id).toBe("connect");
  });

  it("flags draft/follow-up/blocked plans as needing attention", () => {
    const base: PlanDraft = {
      plan_id: "p1",
      title: "t",
      status: "ready",
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
    };
    expect(planNeedsAttention({ ...base, status: "draft" })).toBe(true);
    expect(planNeedsAttention({ ...base, status: "needs follow-up" })).toBe(true);
    expect(planNeedsAttention({ ...base, status: "ready" })).toBe(false);
  });

  it("summarises stats from a snapshot", () => {
    const stats = buildStats(
      emptySnapshot({
        status: {
          agents: [
            { codename: "a", last_firing_id: null, last_run_at: null, status: "live", last_summary: "", firings_today: 1 },
            { codename: "b", last_firing_id: null, last_run_at: null, status: "error", last_summary: "", firings_today: 0 },
          ],
          total_today: 3,
          reliability: {},
        },
      }),
    );
    const agentsStat = stats.find((stat) => stat.label === "Agents");
    expect(agentsStat?.value).toBe("1/2");
    const runsStat = stats.find((stat) => stat.label === "Runs today");
    expect(runsStat?.value).toBe("3");
  });
});
