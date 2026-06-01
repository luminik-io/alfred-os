import { render, screen } from "@testing-library/react";
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
  it("renders candidates and dispatches review actions", async () => {
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

    expect(screen.getByRole("heading", { name: /use request fixtures/i })).toBeInTheDocument();
    expect(screen.getByText("your-org/api")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^promote$/i }));
    expect(onMemoryCandidateAction).toHaveBeenCalledWith("mem:1", "promote");

    await user.click(screen.getByRole("button", { name: /^reject$/i }));
    expect(onMemoryCandidateAction).toHaveBeenCalledWith("mem:1", "reject");
  });

  it("runs health and Redis actions from the side panel", async () => {
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

    await user.click(screen.getByRole("button", { name: /run memory check/i }));
    await user.click(screen.getByRole("button", { name: /preview redis sync/i }));
    await user.click(screen.getByRole("button", { name: /queue failure lessons/i }));

    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "brain_doctor" });
    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "redis_sync_preview" });
    expect(onRunLocalAction).toHaveBeenCalledWith({
      action: "memory_harvest",
      refreshAfter: true,
    });
  });
});
