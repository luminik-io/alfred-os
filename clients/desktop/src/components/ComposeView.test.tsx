import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "./ComposeView";
import { composeDraft } from "../api";
import type { ComposeDraftResponse, PlanDraft } from "../types";

vi.mock("../api", () => ({
  composeDraft: vi.fn(),
  // ComposeView only imports composeDraft, but atoms.tsx (via the barrel of
  // shared imports) reads supportsNativeActions; provide a stub for safety.
  supportsNativeActions: () => false,
}));

const composeDraftMock = vi.mocked(composeDraft);

function renderComposeView(plans: PlanDraft[] = []) {
  return render(
    <ComposeView
      baseUrl="http://127.0.0.1:7000"
      plans={plans}
      actionNotice={null}
      busyPlanAction={null}
      onFollowupAction={vi.fn()}
      onSwitch={vi.fn()}
    />,
  );
}

function draftResponse(overrides: Partial<ComposeDraftResponse> = {}): ComposeDraftResponse {
  return {
    draft_id: "compose-20260530-120000-add-csv-export",
    saved_path: "/state/planning-drafts/compose-20260530-120000-add-csv-export.json",
    title: "Add CSV export to the attendees table",
    readiness: { ok: false, score: 56 },
    questions: [
      "How will the operator verify this worked?",
      "Which repository or repositories should Alfred touch?",
    ],
    findings: [
      { code: "missing_repo_scope", severity: "error", message: "Choose at least one owner/repo scope." },
      { code: "missing_non_goals", severity: "warning", message: "Add non-goals so Alfred does not overbuild." },
    ],
    summary: "No structured amendments found; draft needs scope before implementation.",
    spec_body: "# Add CSV export\n",
    revision_count: 1,
    draft: {
      title: "Add CSV export to the attendees table",
      problem: "Sales reps need to export attendees.",
      user: "",
      current_behavior: "",
      desired_behavior: "A download button exports the table as CSV.",
      repos: [],
      acceptance_criteria: [],
      test_plan: "",
      out_of_scope: "",
      rollout: "",
      open_questions: "",
    },
    ...overrides,
  };
}

describe("ComposeView", () => {
  beforeEach(() => {
    composeDraftMock.mockReset();
  });

  it("submits intent and renders readiness score, questions, and findings", async () => {
    composeDraftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderComposeView();

    const textarea = screen.getByLabelText(/what should alfred build/i);
    await user.type(textarea, "title: Add CSV export to the attendees table");
    await user.click(screen.getByRole("button", { name: /draft it/i }));

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    // No prior draft id on the first submit.
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "title: Add CSV export to the attendees table",
    });
    expect(composeDraftMock.mock.calls[0][1].draft_id).toBeUndefined();

    // Readiness score renders.
    expect(await screen.findByText("56")).toBeInTheDocument();
    // The "Needs scope" badge appears (exact match avoids colliding with the
    // summary sentence that also contains "needs scope before implementation").
    expect(screen.getByText("Needs scope")).toBeInTheDocument();

    // Clarifying questions render.
    expect(screen.getByText(/how will the operator verify this worked\?/i)).toBeInTheDocument();
    expect(
      screen.getByText(/which repository or repositories should alfred touch\?/i),
    ).toBeInTheDocument();

    // Findings render.
    expect(screen.getByText(/choose at least one owner\/repo scope\./i)).toBeInTheDocument();
  });

  it("reuses the draft id when iterating so the same draft is refined", async () => {
    composeDraftMock.mockResolvedValueOnce(draftResponse());
    composeDraftMock.mockResolvedValueOnce(
      draftResponse({
        readiness: { ok: true, score: 94 },
        questions: [],
        findings: [],
        revision_count: 2,
        draft: { ...draftResponse().draft, repos: ["your-org/frontend"] },
      }),
    );
    const user = userEvent.setup();
    renderComposeView();

    await user.type(
      screen.getByLabelText(/what should alfred build/i),
      "title: Add CSV export",
    );
    await user.click(screen.getByRole("button", { name: /draft it/i }));
    await screen.findByText("56");

    // Second submission refines the same draft.
    await user.type(
      screen.getByLabelText(/add detail or answer a question/i),
      "repo: your-org/frontend",
    );
    await user.click(screen.getByRole("button", { name: /refine draft/i }));

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(2));
    expect(composeDraftMock.mock.calls[1][1]).toMatchObject({
      text: "repo: your-org/frontend",
      draft_id: "compose-20260530-120000-add-csv-export",
    });
    expect(await screen.findByText("94")).toBeInTheDocument();
    expect(screen.getByText(/no open questions\./i)).toBeInTheDocument();
  });

  it("surfaces an error when the draft request fails", async () => {
    composeDraftMock.mockRejectedValue(new Error("alfred serve returned 400"));
    const user = userEvent.setup();
    renderComposeView();

    await user.type(screen.getByLabelText(/what should alfred build/i), "vague idea");
    await user.click(screen.getByRole("button", { name: /draft it/i }));

    expect(await screen.findByText(/alfred serve returned 400/i)).toBeInTheDocument();
  });

  it("renders saved planning drafts under the composer", () => {
    renderComposeView([
      {
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
      },
    ]);

    expect(screen.getByRole("heading", { name: /improve planning loop/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /saved plans and follow-ups/i })).toBeInTheDocument();
  });
});
