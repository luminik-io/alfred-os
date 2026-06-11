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
        onDecision={vi.fn()}
        onFileIssue={vi.fn()}
        onSwitch={onSwitch}
      />,
    );

    expect(screen.getByRole("heading", { name: /improve planning loop/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/selected plan details/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /plan next pass/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(expect.objectContaining({ plan_id: "slack-C1-123" }), "convert");

    await user.click(screen.getByRole("button", { name: /mark handled/i }));
    expect(onFollowupAction).toHaveBeenCalledWith(expect.objectContaining({ plan_id: "slack-C1-123" }), "handled");

    await user.click(screen.getByRole("button", { name: /inspect/i }));
    expect(screen.getByLabelText(/selected plan details/i)).toHaveTextContent(
      /add a manual docs smoke test\./i,
    );
    await user.click(screen.getByRole("button", { name: /close/i }));

    // The header "Ask" action routes to the planning tab.
    await user.click(screen.getByRole("button", { name: /^ask$/i }));
    expect(onSwitch).toHaveBeenCalledWith("compose");
  });

  it("renders the empty state when there are no plans", () => {
    render(
      <PlansView
        plans={[]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={vi.fn()}
        onFileIssue={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.getByText(/no saved plans yet\./i)).toBeInTheDocument();
  });

  it("shows an action notice when one is present", () => {
    render(
      <PlansView
        plans={[]}
        actionNotice={{ tone: "ok", message: "Marked the follow-up handled.", domain: "plans" }}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={vi.fn()}
        onFileIssue={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.getByText(/marked the follow-up handled\./i)).toBeInTheDocument();
  });

  it("exposes approve/decline only on a genuine Batman plan awaiting sign-off", async () => {
    const onDecision = vi.fn();
    const user = userEvent.setup();
    const batmanPlan = plan({
      plan_id: "13-plan",
      title: "Add CSV export",
      status: "Draft (awaiting approval)",
      source: "batman",
      parent: "https://github.com/your-org/repo/issues/13",
      path: "/batman-plans/13-plan.md",
    });

    render(
      <PlansView
        plans={[batmanPlan]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={onDecision}
        onFileIssue={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    // The card and the inspector both surface the affirmative action; the
    // confirmation note spells out what approving does.
    expect(screen.getAllByText(/approving starts this exact scope/i).length).toBeGreaterThan(0);

    await user.click(screen.getAllByRole("button", { name: /^approve/i })[0]);
    expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "approve",
    );

    await user.click(screen.getAllByRole("button", { name: /^decline/i })[0]);
    expect(onDecision).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "13-plan" }),
      "decline",
    );
  });

  it("hides approve/decline once the Batman plan has been decided", () => {
    render(
      <PlansView
        plans={[
          plan({
            plan_id: "13-plan",
            title: "Add CSV export",
            status: "approved",
            source: "batman",
          }),
        ]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={vi.fn()}
        onFileIssue={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /^approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^decline/i })).not.toBeInTheDocument();
  });

  it("files a ready planning draft from the inspector", async () => {
    const onFileIssue = vi.fn();
    const user = userEvent.setup();
    const readyDraft = plan({
      plan_id: "compose-20260604-export",
      title: "Add export planning",
      status: "ready",
      parent: null,
      source: "compose",
      readiness_score: 92,
      readiness_ok: true,
      path: "/state/planning-drafts/compose-20260604-export.json",
    });

    render(
      <PlansView
        plans={[readyDraft]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={vi.fn()}
        onFileIssue={onFileIssue}
        onSwitch={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /inspect/i }));
    await user.click(screen.getByRole("button", { name: /file github issue/i }));
    expect(onFileIssue).toHaveBeenCalledWith(
      expect.objectContaining({ plan_id: "compose-20260604-export" }),
    );
  });

  it("opens an already filed planning draft instead of filing it again", () => {
    render(
      <PlansView
        plans={[
          plan({
            plan_id: "compose-20260604-export",
            title: "Add export planning",
            status: "ready",
            parent: "https://github.com/your-org/repo/issues/144",
            source: "compose",
            readiness_score: 92,
            readiness_ok: true,
          }),
        ]}
        actionNotice={null}
        busyPlanAction={null}
        onFollowupAction={vi.fn()}
        onDecision={vi.fn()}
        onFileIssue={vi.fn()}
        onSwitch={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /file github issue/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /open issue/i }).length).toBeGreaterThan(0);
  });
});
