import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { ReviewView } from "./ReviewView";
import type { AttentionItem } from "../lib/uiTypes";
import type { FiringRecord, ShippedBoard, Snapshot, UsageResponse } from "../types";

vi.mock("../lib/links", () => ({
  openExternal: vi.fn(),
}));

function usage(overrides: Partial<UsageResponse> = {}): UsageResponse {
  return {
    available: true,
    kind: "subscription",
    source: "native",
    block: {
      start_at: "2026-06-02T10:00:00Z",
      reset_at: "2026-06-02T15:00:00Z",
      minutes_to_reset: 125,
      is_active: true,
      total_tokens: 142_200_916,
      cost_usd: 109.9,
      entries: 1021,
      token_counts: { input: 1, output: 2, cache_creation: 3, cache_read: 4 },
      projection: { total_tokens: 1_063_656_501, total_cost_usd: 822.07, remaining_minutes: 175 },
      burn_rate: { tokens_per_minute: 3_544_059, cost_per_hour: 164.34 },
      models: ["claude-opus-4-8"],
    },
    codex: {
      latest_day: { date: "2026-06-02", total_tokens: 75_778, cost_usd: 0.32, input_tokens: 62_886, output_tokens: 92 },
      totals: { total_tokens: 4_211_983_837, cost_usd: 3276.2 },
    },
    weekly: null,
    ...overrides,
  };
}

// Most tests only care about a lane; render with a benign available-usage panel
// and an idle state unless the test overrides them.
function renderReview(
  props: Partial<ComponentProps<typeof ReviewView>> = {},
) {
  return render(
    <ReviewView
      snapshot={snapshot()}
      needsYou={[]}
      shipped={null}
      usage={usage()}
      usageState="idle"
      onSwitch={vi.fn()}
      {...props}
    />,
  );
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    loadedAt: new Date("2026-06-02T12:00:00Z"),
    shipped: null,
    schedule: [],
    status: { agents: [], total_today: 3, reliability: { status: "ok" } },
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
    started_at: "2026-06-02T11:00:00Z",
    ended_at: null,
    status: "running",
    summary: "Working on the CSV export.",
    transcript_path: null,
    events_path: "",
    ...overrides,
  };
}

function board(overrides: Partial<ShippedBoard> = {}): ShippedBoard {
  return {
    generated_at: "2026-06-02T12:00:00Z",
    lookback_days: 14,
    repos: ["your-org/api"],
    columns: { queued: [], in_progress: [], shipped: [] },
    counts: { queued: 0, in_progress: 0, shipped: 0 },
    errors: [],
    ...overrides,
  };
}

const needsYouItem: AttentionItem = {
  id: "plan-1",
  label: "Draft",
  title: "Approve the export plan",
  detail: "Review before Alfred starts.",
  tone: "info",
  targetTab: "plans",
  icon: "plan",
};

describe("ReviewView", () => {
  it("offers the three lanes as in-page tabs, with capacity evidence pinned", () => {
    renderReview({ needsYou: [needsYouItem] });
    // Lanes are in-page tabs now (no long scroll); the right rail keeps the
    // useful capacity evidence in view without a duplicate cost strip.
    expect(screen.getByRole("tab", { name: /needs you/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /activity/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /shipped/i })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /capacity and proof/i })).toBeInTheDocument();
    // Real local subscription headroom is surfaced in the rail.
    expect(screen.getByRole("region", { name: /subscription usage/i })).toBeInTheDocument();
    // A waiting decision opens on the Needs-you lane by default.
    expect(screen.getByRole("region", { name: /needs you/i })).toBeInTheDocument();
    expect(screen.getByText(/approve the export plan/i)).toBeInTheDocument();
  });

  it("follows the smart default lane as state loads, until the operator pins one", async () => {
    // Before the first snapshot resolves, the dashboard surfaces a transient
    // connection item, so decisions is truthy and Review opens on Needs you.
    const { rerender } = render(
      <ReviewView
        snapshot={null}
        needsYou={[needsYouItem]}
        shipped={null}
        usage={usage()}
        usageState="idle"
        onSwitch={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: /needs you/i })).toHaveAttribute("aria-selected", "true");

    // A healthy snapshot with no real decisions arrives: the lane moves to
    // Activity on its own instead of stranding Review on an empty Needs-you panel.
    rerender(
      <ReviewView
        snapshot={snapshot()}
        needsYou={[]}
        shipped={null}
        usage={usage()}
        usageState="idle"
        onSwitch={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: /activity/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /needs you/i })).toHaveAttribute("aria-selected", "false");

    // Once the operator manually picks a lane, respect it even as state changes.
    await userEvent.setup().click(screen.getByRole("tab", { name: /shipped/i }));
    expect(screen.getByRole("tab", { name: /shipped/i })).toHaveAttribute("aria-selected", "true");
    rerender(
      <ReviewView
        snapshot={snapshot()}
        needsYou={[needsYouItem]}
        shipped={null}
        usage={usage()}
        usageState="idle"
        onSwitch={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: /shipped/i })).toHaveAttribute("aria-selected", "true");
  });

  it("shows an honest empty note when no schedule is surfaced", () => {
    renderReview();
    expect(screen.getByText(/no upcoming runs surfaced/i)).toBeInTheDocument();
    expect(screen.getByText(/could not read a launchd schedule/i)).toBeInTheDocument();
  });

  it("lists upcoming scheduled runs with a next-fire time or a cadence", () => {
    renderReview({
      snapshot: snapshot({
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
      }),
    });
    expect(screen.getByRole("list", { name: /upcoming scheduled runs/i })).toBeInTheDocument();
    expect(screen.getByText("bane")).toBeInTheDocument();
    // cron row shows a next-fire; interval row shows its cadence string.
    expect(screen.getByText(/^next/i)).toBeInTheDocument();
    expect(screen.getByText("every 10m")).toBeInTheDocument();
    // The empty-state note is gone once real runs render.
    expect(screen.queryByText(/no upcoming runs surfaced/i)).not.toBeInTheDocument();
  });

  it("shows running firings in the Running lane", () => {
    renderReview({ snapshot: snapshot({ firings: [firing()] }) });
    expect(screen.getByText(/working on the csv export\./i)).toBeInTheDocument();
  });

  it("renders shipped work as a plain-English digest", async () => {
    renderReview({
      shipped: board({
        columns: {
          queued: [],
          in_progress: [],
          shipped: [
            {
              repo: "your-org/api",
              number: 7,
              title: "feat: add CSV export",
              url: "https://github.com/your-org/api/pull/7",
              author: "lucius",
              kind: "pr",
              timestamp: "2026-06-02T11:00:00Z",
              age_days: 0,
              is_draft: false,
              labels: [],
            },
            {
              repo: "your-org/web",
              number: 8,
              title: "fix: simplify setup copy",
              url: "https://github.com/your-org/web/pull/8",
              author: "github-actions",
              kind: "pr",
              timestamp: "2026-06-02T10:30:00Z",
              age_days: 0,
              is_draft: false,
              labels: ["agent:large-feature"],
              agent_evidence: ["label:agent:large-feature"],
            },
          ],
        },
        counts: { queued: 0, in_progress: 0, shipped: 2 },
      }),
    });
    // Shipped is its own in-page lane now; open it first.
    await userEvent.setup().click(screen.getByRole("tab", { name: /shipped/i }));
    // Conventional-commit prefix stripped; reads as an outcome.
    expect(screen.getByText("Add CSV export.")).toBeInTheDocument();
    expect(screen.getByText("Simplify setup copy.")).toBeInTheDocument();
    expect(screen.getByText("Lucius")).toBeInTheDocument();
    expect(screen.getByText("Batman")).toBeInTheDocument();
    expect(screen.getByText(/merged into api/i)).toBeInTheDocument();
    expect(screen.getByText(/batman shipped and merged into web/i)).toBeInTheDocument();
  });

  it("shows real subscription headroom from the native reader, labelled as usage not billed-$", () => {
    renderReview();
    const panel = screen.getByRole("region", { name: /subscription usage/i });
    // Real token headroom + reset countdown, not a dollar figure.
    expect(screen.getByText("142.2M")).toBeInTheDocument();
    expect(screen.getByText(/claude window, codex/i)).toBeInTheDocument();
    expect(screen.getByText("2h 5m")).toBeInTheDocument();
    expect(screen.getByText(/local 5h window/i)).toBeInTheDocument();
    // A Codex row is present.
    expect(screen.getByText(/codex 75.8K/i)).toBeInTheDocument();
    // The compact rail still carries subscription usage, not a per-token bill.
    expect(panel.textContent).toMatch(/local 5h window/i);
    expect(panel.textContent).not.toMatch(/\$109/);
  });

  it("shows a plain 'usage unavailable' state when local usage cannot be read", () => {
    renderReview({
      usage: usage({ available: false, block: null, codex: null, error: "usage logs unavailable" }),
      usageState: "error",
    });
    expect(screen.getByText(/usage unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/usage logs unavailable/i)).toBeInTheDocument();
    // It must not crash the rest of the Review surface: the capacity rail still
    // renders alongside the degraded usage panel.
    expect(screen.getByRole("region", { name: /capacity and proof/i })).toBeInTheDocument();
  });

  it("routes the Ask action to the conversational planning surface", async () => {
    const onSwitch = vi.fn();
    renderReview({ onSwitch });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^ask alfred$/i }));
    expect(onSwitch).toHaveBeenCalledWith("compose");
  });

  it("approves a waiting Batman plan in-place from the Needs-you card", async () => {
    const onPlanDecision = vi.fn();
    const plan = {
      plan_id: "13-plan",
      title: "Approve the export plan",
      status: "draft",
      parent: null,
      affected_repos: null,
      updated_at: null,
      path: "",
      preview: "",
      content: "",
      source: "batman",
      readiness_score: null,
      readiness_ok: null,
      revision_count: 0,
    };
    renderReview({
      snapshot: snapshot({ plans: [plan] }),
      needsYou: [{ ...needsYouItem, id: "plan-13-plan", planId: "13-plan" }],
      onPlanDecision,
    });

    expect(screen.getByText(/approving starts this exact scope/i)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /^approve/i }));
    expect(onPlanDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "approve",
    );
  });
});
