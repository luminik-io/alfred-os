import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryView } from "./MemoryView";
import type { MemoryCandidate, Snapshot } from "../types";

vi.mock("../api", () => ({
  supportsNativeActions: () => true,
}));

function candidate(overrides: Partial<MemoryCandidate> = {}): MemoryCandidate {
  return {
    id: "mem:1",
    codename: "lucius",
    repo: "your-org/api",
    body: "Use request fixtures for attendee imports.",
    tags: ["tests"],
    severity: "info",
    source: "slack",
    source_firing_id: null,
    evidence: JSON.stringify({ thread_ts: "1716480000.000000" }),
    confidence: 0.82,
    status: "candidate",
    created_at: "2026-05-30T12:00:00Z",
    ...overrides,
  };
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    loadedAt: new Date("2026-05-30T12:00:00Z"),
    shipped: null,
    schedule: [],
    status: { agents: [], total_today: 0, reliability: { status: "ok" } },
    actions: {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    memoryCandidates: { rows: [candidate()] },
    firings: [],
    plans: [],
    trustedSlack: null,
    ...overrides,
  };
}

describe("MemoryView", () => {
  it("renders a lesson as a plain-language card and keeps the keep/dismiss actions", async () => {
    const onMemoryCandidateAction = vi.fn();
    const user = userEvent.setup();

    render(
      <MemoryView
        snapshot={snapshot()}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={onMemoryCandidateAction}
        onRunLocalAction={vi.fn()}
      />,
    );

    // The lesson body is the headline, framed in plain words around it.
    expect(screen.getByRole("heading", { name: /use request fixtures/i })).toBeInTheDocument();
    // Provenance reads in plain language: where it came from + who noticed it.
    expect(screen.getByText(/from a slack conversation/i)).toBeInTheDocument();
    expect(screen.getByText(/about your-org\/api/i)).toBeInTheDocument();
    expect(screen.getByText(/noticed by lucius/i)).toBeInTheDocument();
    // There is an explanation of what keeping a lesson does.
    expect(screen.getByText(/keeping a lesson lets alfred use it/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /keep this lesson/i }));
    expect(onMemoryCandidateAction).toHaveBeenCalledWith("mem:1", "promote");

    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(onMemoryCandidateAction).toHaveBeenCalledWith("mem:1", "reject");
  });

  it("hides the raw JSON evidence behind a closed disclosure", () => {
    render(
      <MemoryView
        snapshot={snapshot()}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={vi.fn()}
      />,
    );

    const evidenceSummary = screen.getByText("Technical detail");
    const evidenceDetails = evidenceSummary.closest("details");
    expect(evidenceDetails).not.toBeNull();
    // Disclosure is closed by default: the raw JSON is not surfaced up front.
    expect(evidenceDetails).not.toHaveAttribute("open");
  });

  it("maps planning and repeated-failure sources to plain origins", () => {
    render(
      <MemoryView
        snapshot={snapshot({
          memoryCandidates: {
            rows: [
              candidate({ id: "mem:2", source: "planning", body: "Planning lesson." }),
              candidate({ id: "mem:3", source: "memory_candidate", body: "Failure lesson." }),
            ],
          },
        })}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={vi.fn()}
      />,
    );

    expect(screen.getByText(/from planning a request/i)).toBeInTheDocument();
    expect(screen.getByText(/from a repeated problem alfred hit/i)).toBeInTheDocument();
  });

  it("shows a calm empty state when nothing has been learned", () => {
    render(
      <MemoryView
        snapshot={snapshot({ memoryCandidates: { rows: [] } })}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/alfred has not learned anything new yet/i),
    ).toBeInTheDocument();
  });

  it("keeps the Redis / memory probes behind a closed Advanced disclosure", async () => {
    const onRunLocalAction = vi.fn();
    const user = userEvent.setup();

    render(
      <MemoryView
        snapshot={snapshot({ memoryCandidates: { rows: [] } })}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={onRunLocalAction}
      />,
    );

    // The Advanced disclosure is present but closed by default, so the Redis
    // plumbing does not lead the surface.
    const advancedSummary = screen.getByText(/advanced \(technical detail\)/i);
    const advancedDetails = advancedSummary.closest("details");
    expect(advancedDetails).not.toBeNull();
    expect(advancedDetails).not.toHaveAttribute("open");

    // The probes still exist and still dispatch the real native actions once
    // the operator opens the disclosure.
    await user.click(advancedSummary);
    const advanced = within(advancedDetails as HTMLElement);
    await user.click(advanced.getByRole("button", { name: /run memory check/i }));
    await user.click(advanced.getByRole("button", { name: /preview redis sync/i }));
    await user.click(advanced.getByRole("button", { name: /queue failure lessons/i }));

    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "brain_doctor" });
    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "redis_sync_preview" });
    expect(onRunLocalAction).toHaveBeenCalledWith({
      action: "memory_harvest",
      refreshAfter: true,
    });
  });

  it("surfaces the active lessons Alfred is using below the review queue", () => {
    render(
      <MemoryView
        snapshot={snapshot({
          memoryLessons: {
            rows: [
              {
                id: "lesson:1",
                codename: "lucius",
                repo: "your-org/api",
                body: "GraphQL schema lives in src/schema.graphql.",
                tags: ["graphql"],
                severity: "info",
                created_at: "2026-05-30T12:00:00Z",
                firing_id: null,
              },
            ],
          },
        })}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: /lessons alfred is using/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/graphql schema lives in/i)).toBeInTheDocument();
  });

  it("omits the active-lessons section when there are none", () => {
    render(
      <MemoryView
        snapshot={snapshot()}
        actionNotice={null}
        busyMemoryAction={null}
        nativeBusy={null}
        onMemoryCandidateAction={vi.fn()}
        onRunLocalAction={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("heading", { name: /lessons alfred is using/i }),
    ).not.toBeInTheDocument();
  });
});
