import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PlansView } from "./PlansView";
import type { PlanDraft } from "../types";

// atoms.tsx reads supportsNativeActions and the Tauri opener at module load;
// stub the api module so the view renders in jsdom.
vi.mock("../api", () => ({
  supportsNativeActions: () => false,
}));

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

describe("PlansView (post-refactor)", () => {
  it("renders saved plans and exposes follow-up actions", async () => {
    const onFollowupAction = vi.fn();
    const onSwitch = vi.fn();
    const user = userEvent.setup();

    render(
      <PlansView
        plans={[plan()]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={onFollowupAction}
        onSwitch={onSwitch}
      />,
    );

    expect(screen.getAllByRole("heading", { name: /improve planning loop/i })).toHaveLength(2);
    expect(screen.getByLabelText(/selected plan details/i)).toHaveTextContent(
      /add a manual docs smoke test\./i,
    );

    await user.click(screen.getByRole("button", { name: /plan next pass/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(expect.objectContaining({ plan_id: "slack-C1-123" }), "convert");

    await user.click(screen.getByRole("button", { name: /mark handled/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(expect.objectContaining({ plan_id: "slack-C1-123" }), "handled");

    // The header "Compose new" action routes to the compose tab.
    await user.click(screen.getByRole("button", { name: /compose new/i }));
    expect(onSwitch).toHaveBeenCalledWith("compose");
  });

  it("renders the empty state when there are no plans", () => {
    render(
      <PlansView
        plans={[]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.getByText(/no plans saved yet\./i)).toBeInTheDocument();
  });

  it("shows an action notice when one is present", () => {
    render(
      <PlansView
        plans={[]}
        actionNotice={{ tone: "ok", message: "Marked the follow-up handled." }}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.getByText(/marked the follow-up handled\./i)).toBeInTheDocument();
  });
});
