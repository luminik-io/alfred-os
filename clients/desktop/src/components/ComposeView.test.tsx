import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "./ComposeView";
import { composeDraft, conversationControl } from "../api";
import type { ComposeDraftResponse } from "../types";

vi.mock("../api", () => ({
  composeDraft: vi.fn(),
  conversationControl: vi.fn(),
  // ComposeView only imports composeDraft, but atoms.tsx (via the barrel of
  // shared imports) reads supportsNativeActions; provide a stub for safety.
  supportsNativeActions: () => false,
}));

const composeDraftMock = vi.mocked(composeDraft);
const conversationControlMock = vi.mocked(conversationControl);

function renderComposeView(intakeProfile?: string, selectedRepos = ["your-org/frontend"]) {
  return render(
    <ComposeView
      baseUrl="http://127.0.0.1:7000"
      intakeProfile={intakeProfile}
      selectedRepos={selectedRepos}
      onSwitch={vi.fn()}
    />,
  );
}

function draftResponse(overrides: Partial<ComposeDraftResponse> = {}): ComposeDraftResponse {
  return {
    draft_id: "compose-20260530-120000-add-csv-export",
    saved_path: "/state/planning-drafts/compose-20260530-120000-add-csv-export.json",
    title: "Add CSV export to the attendees table",
    readiness: { ok: false, score: 78 },
    questions: ["How will the operator verify this worked?"],
    findings: [
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
      repos: ["your-org/frontend"],
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
    conversationControlMock.mockReset();
    conversationControlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
  });

  it("submits a plain-language request and renders the plan thread, questions, and findings", async () => {
    composeDraftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderComposeView();

    const textarea = screen.getByLabelText(/what should alfred build/i);
    await user.type(textarea, "Add a CSV download button to the attendees table");
    await user.click(screen.getByRole("button", { name: /create plan/i }));

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    expect(conversationControlMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
    });
    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    // No prior draft id on the first submit.
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
      draft: { repos: ["your-org/frontend"] },
      context_repos: ["your-org/frontend"],
    });
    expect(composeDraftMock.mock.calls[0][1].draft_id).toBeUndefined();

    // The result renders as a request lifecycle thread (Intake -> Plan -> ...).
    expect(await screen.findByText("Intake")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: /request lifecycle/i })).toBeInTheDocument();

    // Clarifying questions render.
    expect(screen.getAllByText(/how will the operator verify this worked\?/i).length).toBeGreaterThanOrEqual(2);
    expect(
      screen.queryByText(/which repository or repositories should alfred touch\?/i),
    ).not.toBeInTheDocument();

    // Findings render.
    expect(screen.getByText(/add non-goals so alfred does not overbuild\./i)).toBeInTheDocument();
  });

  it("uses multiple selected repositories as context without confirming all as scope", async () => {
    composeDraftMock.mockResolvedValue(
      draftResponse({
        readiness: { ok: false, score: 82 },
        questions: ["Which part of the workspace should Alfred change?"],
        findings: [
          {
            code: "missing_repo_scope",
            severity: "error",
            message: "Choose the workspace area Alfred should change.",
          },
        ],
        draft: { ...draftResponse().draft, repos: [] },
      }),
    );
    const user = userEvent.setup();
    renderComposeView(undefined, ["your-org/frontend", "your-org/backend"]);

    await user.type(screen.getByLabelText(/what should alfred build/i), "Fix the login copy");
    await user.click(screen.getByRole("button", { name: /create plan/i }));

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "Fix the login copy",
      context_repos: ["your-org/frontend", "your-org/backend"],
    });
    expect(composeDraftMock.mock.calls[0][1].draft).toBeUndefined();
    expect((await screen.findAllByText(/which part of the workspace should alfred change\?/i)).length).toBeGreaterThan(0);
    expect(screen.getByText(/alfred already has 2 codebases available for context/i)).toBeInTheDocument();
  });

  it("reuses the draft id when iterating so the same request is refined", async () => {
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
      "Add a CSV download button",
    );
    await user.click(screen.getByRole("button", { name: /create plan/i }));
    await screen.findByText("Intake");

    // Second submission refines the same draft.
    await user.type(
      screen.getByLabelText(/add detail or answer a question/i),
      "It should only export the visible rows",
    );
    await user.click(screen.getByRole("button", { name: /update plan/i }));

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(2));
    expect(composeDraftMock.mock.calls[1][1]).toMatchObject({
      text: "It should only export the visible rows",
      draft_id: "compose-20260530-120000-add-csv-export",
    });
    // When the plan is ready, the "anything to clear up" lane is empty.
    expect(await screen.findByText(/nothing unclear\./i)).toBeInTheDocument();
  });

  it("adapts its copy when the server is in plain intake mode", () => {
    renderComposeView("plain");
    // The eyebrow confirms the mode and a quiet note tells a non-developer
    // Alfred answers without jargon.
    expect(screen.getByText(/ask · plain/i)).toBeInTheDocument();
    expect(screen.getByText(/plain answers are on/i)).toBeInTheDocument();
  });

  it("uses the default (technical) copy when no plain profile is active", () => {
    renderComposeView("technical");
    expect(screen.queryByText(/plain answers are on/i)).not.toBeInTheDocument();
    // The plain "Plain mode" eyebrow is not shown.
    expect(screen.queryByText(/ask · plain/i)).not.toBeInTheDocument();
  });

  it("surfaces an error when the draft request fails", async () => {
    composeDraftMock.mockRejectedValue(new Error("alfred serve returned 400"));
    const user = userEvent.setup();
    renderComposeView();

    await user.type(screen.getByLabelText(/what should alfred build/i), "vague idea");
    await user.click(screen.getByRole("button", { name: /create plan/i }));

    expect(await screen.findByText(/alfred serve returned 400/i)).toBeInTheDocument();
  });

  it("handles local Alfred commands before creating a draft", async () => {
    conversationControlMock.mockResolvedValueOnce({
      handled: true,
      action: "status",
      text: "*Fleet status*\n\nAgents: 2 configured, 2 loaded, 0 paused",
      detail: "",
      actor_user_id: "ULOCALCLIENT",
    });
    const user = userEvent.setup();
    renderComposeView();

    await user.type(screen.getByLabelText(/what should alfred build/i), "status");
    await user.click(screen.getByRole("button", { name: /create plan/i }));

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock).not.toHaveBeenCalled();
    expect(await screen.findByText(/fleet status/i)).toBeInTheDocument();
    expect(screen.getByText(/agents: 2 configured/i)).toBeInTheDocument();
  });

  it("lets help-prefixed planning prose fall through to draft creation", async () => {
    composeDraftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderComposeView();

    await user.type(
      screen.getByLabelText(/what should alfred build/i),
      "help me add onboarding tests",
    );
    await user.click(screen.getByRole("button", { name: /create plan/i }));

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "help me add onboarding tests",
    });
  });

  it("renders the result as a thread and opens the plan sign-off, not a Plans jump", async () => {
    // The result is shown AS a request thread (replacing the old dead-end
    // "Review in Plans" jump). Reviewing the plan is the one client-owned step.
    composeDraftMock.mockResolvedValue(draftResponse());
    const onSwitch = vi.fn();
    const user = userEvent.setup();
    render(<ComposeView baseUrl="http://127.0.0.1:7000" onSwitch={onSwitch} />);

    await user.type(screen.getByLabelText(/what should alfred build/i), "Add a CSV download");
    await user.click(screen.getByRole("button", { name: /create plan/i }));
    await screen.findByText("Intake");

    // The old "Review in Plans" button is gone.
    expect(screen.queryByRole("button", { name: /review in plans/i })).not.toBeInTheDocument();

    // The thread's plan stage is active and offers a "Review plan" sign-off.
    await user.click(screen.getByRole("button", { name: /review plan/i }));
    expect(onSwitch).toHaveBeenCalledWith("plans");
  });
});
