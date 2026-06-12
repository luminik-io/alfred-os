import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PipelineView } from "./PipelineView";
import type { PlanDraft, ShippedBoard, ShippedCard } from "../types";

// PipelineView merges the old Work board and Plans page into the single
// lifecycle board. Render in desktop-capable mode so the queue actions appear.
vi.mock("../api", () => ({
  supportsNativeActions: () => true,
}));

vi.mock("../lib/links", async () => {
  const actual = await vi.importActual<typeof import("../lib/links")>("../lib/links");
  return { ...actual, openExternal: vi.fn() };
});

function plan(overrides: Partial<PlanDraft> = {}): PlanDraft {
  return {
    plan_id: "slack-C1-123",
    title: "Improve planning loop",
    status: "needs follow-up",
    parent: "https://github.com/your-org/repo/issues/120",
    affected_repos: "your-org/repo",
    updated_at: "2026-05-29T06:45:00Z",
    path: "/state/followups/slack-C1-123.md",
    preview: "Add a manual docs smoke test.",
    content: "Add a manual docs smoke test.",
    source: "followup",
    readiness_score: null,
    readiness_ok: null,
    revision_count: 0,
    ...overrides,
  };
}

function card(overrides: Partial<ShippedCard> = {}): ShippedCard {
  return {
    repo: "your-org/api",
    number: 12,
    title: "Ready issue",
    url: "https://example.com/issues/12",
    author: "lucius",
    kind: "issue",
    timestamp: "2026-06-02T11:00:00Z",
    age_days: 0,
    is_draft: false,
    labels: [],
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

function renderPipeline(props: Partial<Parameters<typeof PipelineView>[0]> = {}) {
  return render(
    <PipelineView
      board={board()}
      state="idle"
      plans={[]}
      busyPlanAction={null}
      onDecision={vi.fn()}
      onFileIssue={vi.fn()}
      onFollowupAction={vi.fn()}
      {...props}
    />,
  );
}

describe("PipelineView", () => {
  it("teaches the four columns with an empty state when nothing is in flight", () => {
    renderPipeline();
    expect(screen.getByText(/nothing in the pipeline yet/i)).toBeInTheDocument();
  });

  it("renders a server hard failure as an honest error, not a false-empty board", () => {
    renderPipeline({
      board: board({ error: "GitHub data unavailable for 3 watched repos" }),
    });
    expect(screen.getByText(/pipeline failed to build/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing in the pipeline yet/i)).not.toBeInTheDocument();
  });

  it("places plans in the go-ahead column and board cards in their lifecycle columns", () => {
    renderPipeline({
      plans: [plan({ title: "Approve the export plan", source: "batman", status: "draft" })],
      board: board({
        columns: {
          queued: [card()],
          in_progress: [],
          shipped: [card({ kind: "pr", number: 7, title: "feat: add CSV export" })],
        },
        counts: { queued: 1, in_progress: 0, shipped: 1 },
      }),
    });
    expect(screen.getByRole("region", { name: /needs your go-ahead/i })).toBeInTheDocument();
    expect(screen.getByText(/approve the export plan/i)).toBeInTheDocument();
    // The conventional-commit prefix is stripped on the shipped card outcome.
    expect(screen.getByText("Add CSV export.")).toBeInTheDocument();
  });

  it("uses the human chip vocabulary, never raw jargon, on card faces", () => {
    renderPipeline({
      plans: [plan({ title: "Approve the export plan", source: "batman", status: "draft" })],
      board: board({
        columns: {
          queued: [card()],
          in_progress: [card({ kind: "pr", number: 5, title: "wip" })],
          shipped: [card({ kind: "pr", number: 7, title: "done" })],
        },
        counts: { queued: 1, in_progress: 1, shipped: 1 },
      }),
    });
    expect(screen.getAllByText(/needs your go-ahead/i).length).toBeGreaterThan(0);
    // "Queued" and "Shipped" appear as both a column header and a card chip.
    expect(screen.getAllByText("Queued").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("Shipped").length).toBeGreaterThanOrEqual(2);
    // No source / readiness jargon chips leak onto a card face.
    expect(screen.queryByText("followup")).not.toBeInTheDocument();
    expect(screen.queryByText(/\/100/)).not.toBeInTheDocument();
  });

  it("offers both Plan next pass and Mark handled on a Slack follow-up", async () => {
    const onFollowupAction = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      plans: [plan({ plan_id: "slack-C1-123", title: "Improve planning loop", source: "followup", status: "needs follow-up" })],
      onFollowupAction,
    });
    await user.click(screen.getByText(/improve planning loop/i));
    await user.click(screen.getByRole("button", { name: /plan next pass/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "slack-C1-123" }),
      "convert",
    );
    await user.click(screen.getByRole("button", { name: /mark handled/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "slack-C1-123" }),
      "handled",
    );
  });

  it("approves a waiting Batman plan in-place from its card primary action", async () => {
    const onDecision = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      plans: [plan({ plan_id: "13-plan", title: "Add CSV export", source: "batman", status: "Draft (awaiting approval)" })],
      onDecision,
    });
    await user.click(screen.getByRole("button", { name: /^approve/i }));
    expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "approve",
    );
  });

  it("opens the plan detail panel and exposes approve/decline plus the dev-only readiness", async () => {
    const onDecision = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      plans: [plan({ plan_id: "13-plan", title: "Add CSV export", source: "batman", status: "Draft (awaiting approval)", readiness_score: 88 })],
      onDecision,
    });
    // Select the card body (no separate Inspect verb).
    await user.click(screen.getByRole("button", { name: /add csv export/i }));
    const panel = screen.getByLabelText(/selected plan details/i);
    expect(panel).toBeInTheDocument();
    // The raw readiness number survives only in the detail panel.
    expect(panel).toHaveTextContent("88/100");
    await user.click(screen.getByRole("button", { name: /approve plan/i }));
    expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "approve",
    );
    await user.click(screen.getByRole("button", { name: /^decline/i }));
    expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "decline",
    );
  });

  it("files a ready planning draft from the detail panel", async () => {
    const onFileIssue = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      plans: [
        plan({
          plan_id: "compose-export",
          title: "Add export planning",
          status: "ready",
          parent: null,
          source: "compose",
          readiness_score: 92,
          readiness_ok: true,
        }),
      ],
      onFileIssue,
    });
    await user.click(screen.getByRole("button", { name: /add export planning/i }));
    await user.click(screen.getByRole("button", { name: /file github issue/i }));
    expect(onFileIssue).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "compose-export" }),
    );
  });

  it("holds and marks done a queued issue from its detail panel", async () => {
    const onQueueAction = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      board: board({
        columns: { queued: [card()], in_progress: [], shipped: [] },
        counts: { queued: 1, in_progress: 0, shipped: 0 },
      }),
      onQueueAction,
    });
    await user.click(screen.getByRole("button", { name: /ready issue/i }));
    await user.click(screen.getByRole("button", { name: /^hold$/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 12, "hold");
    await user.click(screen.getByRole("button", { name: /mark done/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 12, "done");
  });

  it("does not offer Hold or Mark done on demo cards", async () => {
    const onQueueAction = vi.fn();
    const user = userEvent.setup();
    renderPipeline({
      board: board({
        columns: {
          queued: [card({ repo: "alfred/demo", title: "[Demo] Try the board", url: null, demo: true })],
          in_progress: [],
          shipped: [],
        },
        counts: { queued: 1, in_progress: 0, shipped: 0 },
      }),
      onQueueAction,
    });
    expect(screen.getByText("Sample")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /try the board/i }));
    expect(screen.queryByRole("button", { name: /^hold$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mark done/i })).not.toBeInTheDocument();
  });

  it("collapses identical drafts to one card with a revision count (issue 314)", () => {
    const dup = (id: string, updated: string): PlanDraft =>
      plan({
        plan_id: id,
        title: "Add CSV export",
        affected_repos: "your-org/api",
        source: "batman",
        status: "draft",
        updated_at: updated,
      });
    renderPipeline({
      plans: [
        dup("p1", "2026-06-01T10:00:00Z"),
        dup("p2", "2026-06-02T10:00:00Z"),
        dup("p3", "2026-06-03T10:00:00Z"),
      ],
    });
    // The three identical drafts collapse to a single card carrying a count.
    expect(screen.getByText(/add csv export \(3 revisions\)/i)).toBeInTheDocument();
  });

  it("hides low-signal drafts behind a disclosure", async () => {
    const user = userEvent.setup();
    renderPipeline({
      plans: [
        plan({ plan_id: "junk", title: "Hi", source: "compose", status: "draft", readiness_score: 34, readiness_ok: false }),
      ],
    });
    // The sub-threshold draft is not shown by default.
    expect(screen.queryByText(/^Hi$/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /1 low signal/i }));
    expect(screen.getByText("Hi")).toBeInTheDocument();
  });

  it("surfaces a queue action error notice on the board", () => {
    renderPipeline({
      notice: { tone: "error", message: "forbidden", domain: "board" },
      onQueueAction: vi.fn(),
    });
    expect(screen.getByText(/forbidden/i)).toBeInTheDocument();
  });
});
