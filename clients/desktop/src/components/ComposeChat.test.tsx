import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "./ComposeView";
import {
  ApiError,
  composeConverse,
  composeDraft,
  conversationControl,
  filePlanIssue,
  isLiveSessionUnavailable,
  streamComposeConverse,
} from "../api";
import type { ComposeDraftResponse, ConverseResponse } from "../types";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    supportsNativeActions: () => true,
    composeConverse: vi.fn(),
    composeDraft: vi.fn(),
    conversationControl: vi.fn(),
    filePlanIssue: vi.fn(),
    streamComposeConverse: vi.fn(),
  };
});

const converseMock = vi.mocked(composeConverse);
const draftMock = vi.mocked(composeDraft);
const controlMock = vi.mocked(conversationControl);
const filePlanIssueMock = vi.mocked(filePlanIssue);
const streamMock = vi.mocked(streamComposeConverse);

function renderChat(intakeProfile?: string, selectedRepos = ["your-org/frontend"]) {
  return render(
    <ComposeView
      baseUrl="http://127.0.0.1:7010"
      intakeProfile={intakeProfile}
      selectedRepos={selectedRepos}
      onSwitch={vi.fn()}
    />,
  );
}

function chatInput() {
  return screen.getByLabelText(/your message to alfred/i);
}

async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(chatInput(), text);
  await user.click(screen.getByRole("button", { name: /send message/i }));
}

function converseResponse(overrides: Partial<ConverseResponse> = {}): ConverseResponse {
  return {
    draft_id: "compose-20260603-120000-add-csv-export",
    saved_path: "/state/planning-drafts/compose-20260603-120000-add-csv-export.json",
    reply: "How should Alfred verify this worked?",
    readiness: { score: 62, ready: false, missing: ["a test plan"] },
    done: false,
    draft: {
      title: "Add CSV export to the attendees table",
      problem: "Sales reps need to export attendees.",
      user: "Sales rep",
      current_behavior: "",
      desired_behavior: "A download button exports the visible rows as CSV.",
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

function draftResponse(overrides: Partial<ComposeDraftResponse> = {}): ComposeDraftResponse {
  return {
    draft_id: "compose-20260603-120000-add-csv-export",
    saved_path: "/state/planning-drafts/compose-20260603-120000-add-csv-export.json",
    title: "Add CSV export to the attendees table",
    readiness: { ok: false, score: 0.42 },
    questions: ["How should Alfred verify this worked?"],
    findings: [],
    summary: "I saved a draft plan for CSV export.",
    spec_body: "# Add CSV export\n",
    revision_count: 1,
    draft: converseResponse().draft,
    ...overrides,
  };
}

describe("ComposeView (conversational)", () => {
  beforeEach(() => {
    converseMock.mockReset();
    draftMock.mockReset();
    controlMock.mockReset();
    filePlanIssueMock.mockReset();
    streamMock.mockReset();
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
    streamMock.mockRejectedValue(new ApiError("stream unavailable", "load failed"));
  });

  it("sends a turn, echoes the user message, and renders Alfred's reply", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    await send(user, "Add a CSV download button to the attendees table");

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(controlMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
    });
    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1]).toMatchObject({
      messages: [
        { role: "user", content: "Add a CSV download button to the attendees table" },
      ],
      repos: ["your-org/frontend"],
    });
    expect(converseMock.mock.calls[0][1].draft_id).toBeUndefined();

    expect(
      screen.getByText(/add a csv download button to the attendees table/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/how should alfred verify this worked\?/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/needs detail/i)).toBeInTheDocument();
    expect(screen.getByText(/saved as a plan/i)).toBeInTheDocument();
  });

  it("carries the draft id across turns so the same spec is refined", async () => {
    converseMock.mockResolvedValueOnce(converseResponse());
    converseMock.mockResolvedValueOnce(
      converseResponse({
        reply: "Got it.",
        readiness: { score: 88, ready: true, missing: [] },
        draft: { ...converseResponse().draft, repos: ["your-org/frontend"] },
      }),
    );
    const user = userEvent.setup();
    renderChat();

    await send(user, "Add a CSV download button");
    await screen.findByText(/how should alfred verify this worked\?/i);

    await send(user, "Run a table export test");

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(2));
    expect(converseMock.mock.calls[1][1].draft_id).toBe(
      "compose-20260603-120000-add-csv-export",
    );
    expect(converseMock.mock.calls[1][1].messages).toHaveLength(3);
    expect(await screen.findByText(/ready to file/i)).toBeInTheDocument();
  });

  it("renders missing details on the saved plan card", async () => {
    converseMock.mockResolvedValue(
      converseResponse({
        readiness: { score: 35, ready: false, missing: ["a test plan", "repository scope"] },
      }),
    );
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    expect(await screen.findByText(/needs detail/i)).toBeInTheDocument();
    expect(screen.getByText(/a test plan/i)).toBeInTheDocument();
    expect(screen.getByText(/repository scope/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /file issue/i })).toBeDisabled();
  });

  it("files a model-ready spec from the chat card", async () => {
    converseMock.mockResolvedValue(
      converseResponse({ readiness: { score: 92, ready: true, missing: [] }, done: false }),
    );
    filePlanIssueMock.mockResolvedValue({
      ok: true,
      status: "filed",
      draft_id: "compose-20260603-120000-add-csv-export",
      issue_url: "https://github.com/your-org/frontend/issues/42",
      repo: "your-org/frontend",
      label: "agent:implement",
    });
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");
    await user.click(await screen.findByRole("button", { name: /file issue/i }));

    await waitFor(() => expect(filePlanIssueMock).toHaveBeenCalledTimes(1));
    expect(filePlanIssueMock).toHaveBeenCalledWith(
      "http://127.0.0.1:7010",
      "compose-20260603-120000-add-csv-export",
    );
    expect(await screen.findByText(/filed with agent:implement/i)).toBeInTheDocument();
  });

  it("saves a draft in the chat when no live session is configured", async () => {
    converseMock.mockRejectedValue(
      new ApiError(
        "Alfred serve is reachable but not ready yet.",
        'alfred serve returned 503: {"error": "live_session_unavailable"}',
      ),
    );
    draftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    await waitFor(() => expect(draftMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/i saved a draft plan for csv export/i)).toBeInTheDocument();
    expect(screen.getByText(/needs detail/i)).toBeInTheDocument();
  });

  it("keeps a real error visible and lets the person retry the same message", async () => {
    converseMock.mockRejectedValue(new Error("the engine timed out"));
    const user = userEvent.setup();
    const { container } = renderChat();

    await send(user, "Add a CSV export");

    expect(await screen.findByText(/the engine timed out/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/add a csv export/i)).toBeInTheDocument();
    expect(container.querySelector(".ask-bubble--user")).not.toBeInTheDocument();
  });

  it("handles Alfred control commands without starting a planning turn", async () => {
    controlMock.mockResolvedValueOnce({
      handled: true,
      action: "run",
      text: "*Triggered one run* `batman`.",
      detail: "",
      actor_user_id: "ULOCALCLIENT",
    });
    const user = userEvent.setup();
    renderChat();

    await send(user, "run batman");

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(streamMock).not.toHaveBeenCalled();
    expect(converseMock).not.toHaveBeenCalled();
    expect(draftMock).not.toHaveBeenCalled();
    expect(await screen.findByText(/triggered one run/i)).toBeInTheDocument();
    expect(chatInput()).toBeEnabled();
    expect(screen.getByRole("button", { name: /send message/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /new chat/i })).toBeEnabled();
  });

  it("lets help-prefixed planning prose continue into the planning chat", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    await send(user, "help me add onboarding tests");

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(streamMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].messages).toEqual([
      { role: "user", content: "help me add onboarding tests" },
    ]);
  });

  it("adapts its copy in plain intake mode", () => {
    renderChat("plain");
    expect(screen.getByText(/guided setup/i)).toBeInTheDocument();
    expect(screen.getByText(/say the outcome in your own words/i)).toBeInTheDocument();
  });

  it("seeds the plain-language toggle from the server intake profile and sends plain=true", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat("plain");

    const toggle = screen.getByRole("switch", { name: /plain language/i });
    expect(toggle).toBeChecked();

    await send(user, "Build it");

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].plain).toBe(true);
  });

  it("defaults the toggle off for a technical server and sends plain=false", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    const toggle = screen.getByRole("switch", { name: /plain language/i });
    expect(toggle).not.toBeChecked();

    await send(user, "Build it");

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].plain).toBe(false);
  });

  it("syncs the plain toggle when the server intake profile loads after mount", () => {
    const { rerender } = renderChat(undefined);
    expect(screen.getByRole("switch", { name: /plain language/i })).not.toBeChecked();

    rerender(
      <ComposeView baseUrl="http://127.0.0.1:7010" intakeProfile="plain" onSwitch={vi.fn()} />,
    );
    expect(screen.getByRole("switch", { name: /plain language/i })).toBeChecked();
  });

  it("lets a non-developer flip plain language on in-app, changing the sent flag", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    const toggle = screen.getByRole("switch", { name: /plain language/i });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    expect(toggle).toBeChecked();
    expect(screen.getByText(/say the outcome in your own words/i)).toBeInTheDocument();

    await send(user, "Build it");

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].plain).toBe(true);
  });
});

describe("ComposeView (conversational, token streaming)", () => {
  beforeEach(() => {
    converseMock.mockReset();
    draftMock.mockReset();
    controlMock.mockReset();
    filePlanIssueMock.mockReset();
    streamMock.mockReset();
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
  });

  it("uses streamed tokens when available, then reconciles to the final reply", async () => {
    streamMock.mockImplementation(async (_baseUrl, _request, onToken) => {
      onToken("Which repository ");
      onToken("is the attendees table in?");
      return converseResponse();
    });
    const user = userEvent.setup();
    renderChat();

    await send(user, "Add a CSV download button");

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    expect(converseMock).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/how should alfred verify this worked\?/i),
    ).toBeInTheDocument();
  });

  it("falls back to buffered converse when the stream transport fails", async () => {
    streamMock.mockRejectedValue(new ApiError("stream broke", "load failed"));
    converseMock.mockResolvedValue(converseResponse({ reply: "Buffered reply." }));
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/buffered reply\./i)).toBeInTheDocument();
  });

  it("saves a draft when the stream reports no live session", async () => {
    streamMock.mockRejectedValue(
      new ApiError("not ready", 'alfred serve returned 503: {"error": "live_session_unavailable"}'),
    );
    draftMock.mockResolvedValue(draftResponse({ summary: "Draft saved from stream fallback." }));
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    expect(await screen.findByText(/draft saved from stream fallback/i)).toBeInTheDocument();
    expect(converseMock).not.toHaveBeenCalled();
    expect(draftMock).toHaveBeenCalledTimes(1);
  });
});

describe("ComposeView (conversational, cancellation)", () => {
  beforeEach(() => {
    converseMock.mockReset();
    draftMock.mockReset();
    controlMock.mockReset();
    filePlanIssueMock.mockReset();
    streamMock.mockReset();
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
  });

  it("passes an AbortSignal to the stream and aborts it on unmount", async () => {
    let capturedSignal: AbortSignal | undefined;
    let releaseStream: ((value: ConverseResponse) => void) | undefined;
    streamMock.mockImplementation((_baseUrl, _request, _onToken, signal) => {
      capturedSignal = signal;
      return new Promise<ConverseResponse>((resolve) => {
        releaseStream = resolve;
      });
    });

    const user = userEvent.setup();
    const { unmount } = renderChat();

    await send(user, "Add a CSV export");

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    expect(capturedSignal).toBeInstanceOf(AbortSignal);
    expect(capturedSignal?.aborted).toBe(false);

    unmount();
    expect(capturedSignal?.aborted).toBe(true);

    releaseStream?.(converseResponse({ reply: "late reply that should be dropped" }));
    await Promise.resolve();
    expect(
      screen.queryByText(/late reply that should be dropped/i),
    ).not.toBeInTheDocument();
  });

  it("drops a stale stream resolve so it cannot resurrect an abandoned chat", async () => {
    let firstSignal: AbortSignal | undefined;
    let firstOnToken: ((text: string) => void) | undefined;
    streamMock.mockImplementationOnce((_baseUrl, _request, onToken, signal) => {
      firstSignal = signal;
      firstOnToken = onToken;
      return new Promise<ConverseResponse>(() => {
        // abandoned by unmount
      });
    });

    const user = userEvent.setup();
    const { unmount } = renderChat();
    await send(user, "first message");
    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));

    unmount();
    expect(firstSignal?.aborted).toBe(true);
    expect(() => firstOnToken?.("orphaned token")).not.toThrow();
  });
});

describe("isLiveSessionUnavailable", () => {
  it("matches the structured error code and the 503 status line", () => {
    expect(
      isLiveSessionUnavailable(new ApiError("x", "live_session_unavailable")),
    ).toBe(true);
    expect(isLiveSessionUnavailable(new ApiError("x", "alfred serve returned 503: ..."))).toBe(
      true,
    );
    expect(isLiveSessionUnavailable(new ApiError("x", "alfred serve returned 500"))).toBe(false);
    expect(isLiveSessionUnavailable(new Error("plain network error"))).toBe(false);
  });
});
