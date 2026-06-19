import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "./ComposeView";
import { composeDraft, conversationControl, filePlanIssue } from "../api";
import type { ComposeDraftResponse } from "../types";

vi.mock("../api", () => ({
  composeConverse: vi.fn(),
  composeDraft: vi.fn(),
  conversationControl: vi.fn(),
  filePlanIssue: vi.fn(),
  isLiveSessionUnavailable: (err: unknown) =>
    err instanceof Error
      ? err.message.includes("live_session_unavailable") || err.message.includes("503")
      : false,
  streamComposeConverse: vi.fn(),
  supportsNativeActions: () => false,
}));

const composeDraftMock = vi.mocked(composeDraft);
const conversationControlMock = vi.mocked(conversationControl);
const filePlanIssueMock = vi.mocked(filePlanIssue);

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
    questions: ["How will you verify this worked after Alfred opens a PR?"],
    findings: [
      {
        code: "missing_non_goals",
        severity: "warning",
        message: "Add non-goals so Alfred does not overbuild.",
      },
    ],
    summary: "I saved a plan for the CSV export. Answer the open question before filing it.",
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

function input() {
  return screen.getByLabelText(/your message to alfred/i);
}

async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(input(), text);
  await user.click(screen.getByRole("button", { name: /send message/i }));
}

describe("ComposeView", () => {
  beforeEach(() => {
    composeDraftMock.mockReset();
    conversationControlMock.mockReset();
    filePlanIssueMock.mockReset();
    conversationControlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
  });

  it("saves a plain-language request as an inline plan card in browser fallback mode", async () => {
    composeDraftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderComposeView();

    await send(user, "Add a CSV download button to the attendees table");

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    expect(conversationControlMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
    });
    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
      draft: { repos: ["your-org/frontend"] },
      context_repos: ["your-org/frontend"],
    });
    expect(composeDraftMock.mock.calls[0][1].draft_id).toBeUndefined();

    expect(
      screen.getByText(/add a csv download button to the attendees table/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/i saved a plan for the csv export/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/needs detail/i)).toBeInTheDocument();
    expect(screen.getByText(/saved as a plan/i)).toBeInTheDocument();
    expect(
      screen.getByText(/how will you verify this worked after Alfred opens a PR\?/i),
    ).toBeInTheDocument();
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

    await send(user, "Fix the login copy");

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "Fix the login copy",
      context_repos: ["your-org/frontend", "your-org/backend"],
    });
    expect(composeDraftMock.mock.calls[0][1].draft).toBeUndefined();
    expect(
      await screen.findByText(/which part of the workspace should alfred change\?/i),
    ).toBeInTheDocument();
  });

  it("reuses the draft id when iterating so the same request is refined", async () => {
    composeDraftMock.mockResolvedValueOnce(draftResponse());
    composeDraftMock.mockResolvedValueOnce(
      draftResponse({
        readiness: { ok: true, score: 94 },
        questions: [],
        findings: [],
        revision_count: 2,
        summary: "The plan is ready to file.",
        draft: { ...draftResponse().draft, repos: ["your-org/frontend"] },
      }),
    );
    const user = userEvent.setup();
    renderComposeView();

    await send(user, "Add a CSV download button");
    await screen.findByText(/how will you verify this worked after Alfred opens a PR\?/i);

    await send(user, "It should only export the visible rows");

    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(2));
    expect(composeDraftMock.mock.calls[1][1]).toMatchObject({
      text: "It should only export the visible rows",
      draft_id: "compose-20260530-120000-add-csv-export",
    });
    await screen.findByText(/the plan is ready to file\./i);
    expect(screen.getAllByText(/^Ready to file$/i).length).toBeGreaterThan(0);
    const fileButtons = screen.getAllByRole("button", { name: /file issue/i });
    expect(fileButtons[fileButtons.length - 1]).toBeEnabled();
  });

  it("adapts its copy when the server is in plain intake mode", () => {
    renderComposeView("plain");
    // The eyebrow is stable; the mode shows through the intro copy and toggle.
    expect(screen.getByText(/new request/i)).toBeInTheDocument();
    expect(screen.getByText(/say the outcome in your own words/i)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /plain language/i })).toBeChecked();
  });

  it("uses the default technical copy when no plain profile is active", () => {
    renderComposeView("technical");
    expect(screen.getByText(/new request/i)).toBeInTheDocument();
    expect(screen.getByText(/give the outcome, repo scope, and constraints/i)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /plain language/i })).not.toBeChecked();
  });

  it("surfaces an error and restores the typed message when the draft request fails", async () => {
    composeDraftMock.mockRejectedValue(new Error("alfred serve returned 400"));
    const user = userEvent.setup();
    renderComposeView();

    await send(user, "vague idea");

    expect(await screen.findByText(/alfred serve returned 400/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/vague idea/i)).toBeInTheDocument();
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

    await send(user, "status");

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock).not.toHaveBeenCalled();
    expect(await screen.findByText(/fleet status/i)).toBeInTheDocument();
    expect(screen.getByText(/agents: 2 configured/i)).toBeInTheDocument();
    expect(input()).toBeEnabled();
    expect(screen.getByRole("button", { name: /send message/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /new chat/i })).toBeEnabled();
  });

  it("lets help-prefixed planning prose fall through to draft creation", async () => {
    composeDraftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderComposeView();

    await send(user, "help me add onboarding tests");

    await waitFor(() => expect(conversationControlMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(composeDraftMock).toHaveBeenCalledTimes(1));
    expect(composeDraftMock.mock.calls[0][1]).toMatchObject({
      text: "help me add onboarding tests",
    });
  });

  it("files a ready plan directly from the saved card", async () => {
    composeDraftMock.mockResolvedValue(
      draftResponse({
        readiness: { ok: true, score: 96 },
        questions: [],
        summary: "The plan is ready to file.",
      }),
    );
    filePlanIssueMock.mockResolvedValue({
      ok: true,
      status: "filed",
      draft_id: "compose-20260530-120000-add-csv-export",
      issue_url: "https://github.com/your-org/frontend/issues/42",
      repo: "your-org/frontend",
      label: "agent:implement",
    });
    const user = userEvent.setup();
    renderComposeView();

    await send(user, "Add a CSV download button");
    await user.click(await screen.findByRole("button", { name: /file issue/i }));

    await waitFor(() => expect(filePlanIssueMock).toHaveBeenCalledTimes(1));
    expect(filePlanIssueMock).toHaveBeenCalledWith(
      "http://127.0.0.1:7000",
      "compose-20260530-120000-add-csv-export",
    );
    expect(await screen.findByText(/filed with agent:implement/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view issue/i })).toBeInTheDocument();
  });
});
