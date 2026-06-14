import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "./ComposeView";
import {
  composeConverse,
  composeDraft,
  conversationControl,
  isLiveSessionUnavailable,
  streamComposeConverse,
} from "../api";
import { ApiError } from "../api";
import type { ComposeDraftResponse, ConverseResponse } from "../types";

// The guided chat only renders when the native bridge is present. These tests
// force supportsNativeActions() true so the conversational path is exercised;
// the existing ComposeView.test.tsx covers the one-shot (browser) path with it
// false.
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    supportsNativeActions: () => true,
    composeConverse: vi.fn(),
    composeDraft: vi.fn(),
    conversationControl: vi.fn(),
    streamComposeConverse: vi.fn(),
  };
});

const converseMock = vi.mocked(composeConverse);
const draftMock = vi.mocked(composeDraft);
const controlMock = vi.mocked(conversationControl);
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
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
    streamMock.mockReset();
    // By default the streaming transport "fails" with a non-live-session
    // transport error, so these turn-level tests exercise the buffered
    // `composeConverse` fallback (the same path they tested before streaming
    // existed). The dedicated streaming describe-block below overrides this to
    // assert the token-render path. A streaming failure that is NOT the live-
    // session degrade falls through to composeConverse, so each test only needs
    // to set up `converseMock`.
    streamMock.mockRejectedValue(new ApiError("stream unavailable", "load failed"));
  });

  it("sends a turn, echoes the user message, and renders Alfred's reply", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    const input = chatInput();
    await user.type(input, "Add a CSV download button to the attendees table");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(controlMock.mock.calls[0][1]).toMatchObject({
      text: "Add a CSV download button to the attendees table",
    });
    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    // The first turn sends the single user message, no prior draft id.
    expect(converseMock.mock.calls[0][1]).toMatchObject({
      messages: [
        { role: "user", content: "Add a CSV download button to the attendees table" },
      ],
      repos: ["your-org/frontend"],
    });
    expect(converseMock.mock.calls[0][1].draft_id).toBeUndefined();

    // Both turns render in the transcript.
    expect(
      screen.getByText(/add a csv download button to the attendees table/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/how should alfred verify this worked\?/i),
    ).toBeInTheDocument();
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

    await user.type(
      chatInput(),
      "Add a CSV download button",
    );
    await user.click(screen.getByRole("button", { name: /start/i }));
    await screen.findByText(/how should alfred verify this worked\?/i);

    await user.type(screen.getByPlaceholderText(/reply to alfred/i), "Run a table export test");
    await user.click(screen.getByRole("button", { name: /^send$/i }));

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(2));
    // The second turn carries the draft id and the full transcript.
    expect(converseMock.mock.calls[1][1].draft_id).toBe(
      "compose-20260603-120000-add-csv-export",
    );
    expect(converseMock.mock.calls[1][1].messages).toHaveLength(3);
  });

  it("shows a readiness meter that reflects the model-judged score", async () => {
    converseMock.mockResolvedValue(converseResponse({ readiness: { score: 35, ready: false, missing: ["a test plan", "repository scope"] } }));
    const user = userEvent.setup();
    renderChat();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    const meter = await screen.findByRole("progressbar", { name: /plan readiness/i });
    expect(meter).toHaveAttribute("aria-valuenow", "35");
    // Plain "N questions from ready" caption, not a gamey badge.
    expect(screen.getByText(/2 questions from ready/i)).toBeInTheDocument();
  });

  it("surfaces the Plans handoff when the spec is model-judged ready", async () => {
    converseMock.mockResolvedValue(
      converseResponse({ readiness: { score: 92, ready: true, missing: [] }, done: false }),
    );
    const user = userEvent.setup();
    const onSwitch = vi.fn();
    render(
      <ComposeView baseUrl="http://127.0.0.1:7010" onSwitch={onSwitch} />,
    );

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    const save = await screen.findByRole("button", { name: /open plans/i });
    expect(screen.getByText(/ready to review/i)).toBeInTheDocument();
    await user.click(save);
    expect(onSwitch).toHaveBeenCalledWith("plans");
  });

  it("saves a draft in the chat when no live session is configured", async () => {
    // The server returns a 503 live_session_unavailable; the chat uses the
    // reliable draft endpoint instead of dropping the typed request.
    converseMock.mockRejectedValue(
      new ApiError("Alfred serve is reachable but not ready yet.", "alfred serve returned 503: {\"error\": \"live_session_unavailable\"}"),
    );
    draftMock.mockResolvedValue(draftResponse());
    const user = userEvent.setup();
    renderChat();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(draftMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/i saved a draft plan for csv export/i)).toBeInTheDocument();
    expect(screen.getByText(/42%/i)).toBeInTheDocument();
  });

  it("keeps a real error visible and lets the person retry the same message", async () => {
    converseMock.mockRejectedValue(new Error("the engine timed out"));
    const user = userEvent.setup();
    const { container } = renderChat();

    const input = chatInput();
    await user.type(input, "Add a CSV export");
    await user.click(screen.getByRole("button", { name: /start/i }));

    expect(await screen.findByText(/the engine timed out/i)).toBeInTheDocument();
    // The message is restored to the composer so the person can retry.
    expect(screen.getByDisplayValue(/add a csv export/i)).toBeInTheDocument();
    // ...and the failed turn is rolled back rather than left as a dangling
    // user bubble in the transcript, so retrying re-sends it once instead of
    // duplicating it.
    expect(container.querySelector(".compose-bubble--user")).not.toBeInTheDocument();
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

    await user.type(chatInput(), "run batman");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(streamMock).not.toHaveBeenCalled();
    expect(converseMock).not.toHaveBeenCalled();
    expect(draftMock).not.toHaveBeenCalled();
    expect(await screen.findByText(/triggered one run/i)).toBeInTheDocument();
  });

  it("lets help-prefixed planning prose continue into the planning chat", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat();

    await user.type(chatInput(), "help me add onboarding tests");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(controlMock).toHaveBeenCalledTimes(1));
    expect(streamMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].messages).toEqual([
      { role: "user", content: "help me add onboarding tests" },
    ]);
  });

  it("adapts its copy in plain intake mode", () => {
    renderChat("plain");
    // The eyebrow is stable ("New request"); plain mode is confirmed by the
    // quiet note, not by flipping the eyebrow label.
    expect(screen.getByText(/new request/i)).toBeInTheDocument();
    expect(screen.getByText(/plain answers are on/i)).toBeInTheDocument();
  });

  it("seeds the plain-mode toggle from the server intake profile and sends plain=true", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat("plain");

    // The toggle is on because the server default is plain.
    const toggle = screen.getByRole("switch", { name: /plain mode/i });
    expect(toggle).toBeChecked();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    // The per-request plain flag rides the converse call.
    expect(converseMock.mock.calls[0][1].plain).toBe(true);
  });

  it("defaults the toggle off for a technical server and sends plain=false", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat(); // undefined intake profile -> technical

    const toggle = screen.getByRole("switch", { name: /plain mode/i });
    expect(toggle).not.toBeChecked();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].plain).toBe(false);
  });

  it("syncs the plain toggle when the server intake profile loads after mount", () => {
    // Compose can mount before /api/status resolves (intakeProfile undefined);
    // the toggle must follow the server default once it arrives rather than
    // staying off and overriding a server plain default on later converse calls.
    const { rerender } = renderChat(undefined);
    expect(screen.getByRole("switch", { name: /plain mode/i })).not.toBeChecked();

    rerender(
      <ComposeView baseUrl="http://127.0.0.1:7010" intakeProfile="plain" onSwitch={vi.fn()} />,
    );
    expect(screen.getByRole("switch", { name: /plain mode/i })).toBeChecked();
  });

  it("lets a non-developer flip plain mode on in-app, changing the sent flag", async () => {
    converseMock.mockResolvedValue(converseResponse());
    const user = userEvent.setup();
    renderChat(); // starts technical

    const toggle = screen.getByRole("switch", { name: /plain mode/i });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    expect(toggle).toBeChecked();
    // Copy reflects the flip without any server round trip.
    expect(screen.getByText(/plain answers are on/i)).toBeInTheDocument();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(converseMock.mock.calls[0][1].plain).toBe(true);
  });
});

describe("ComposeView (conversational, token streaming)", () => {
  beforeEach(() => {
    converseMock.mockReset();
    draftMock.mockReset();
    controlMock.mockReset();
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
    streamMock.mockReset();
  });

  it("renders tokens incrementally as they stream, then reconciles to the reply", async () => {
    // The stream calls the token callback with each fragment, then resolves to
    // the final ConverseResponse. The transcript should show the streamed text
    // mid-flight and the reconciled reply at the end.
    streamMock.mockImplementation(async (_baseUrl, _request, onToken) => {
      onToken("Which repository ");
      onToken("is the attendees table in?");
      return converseResponse();
    });
    const user = userEvent.setup();
    renderChat();

    await user.type(
      chatInput(),
      "Add a CSV download button",
    );
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    // The buffered converse must NOT be called when streaming succeeds.
    expect(converseMock).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/how should alfred verify this worked\?/i),
    ).toBeInTheDocument();
  });

  it("falls back to buffered converse when the stream transport fails", async () => {
    // A non-live-session streaming failure (e.g. the streaming route is missing
    // or the connection dropped) must transparently fall back to the buffered
    // converse, which returns the same shape. The reply still renders.
    streamMock.mockRejectedValue(new ApiError("stream broke", "load failed"));
    converseMock.mockResolvedValue(converseResponse({ reply: "Buffered reply." }));
    const user = userEvent.setup();
    renderChat();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(converseMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/buffered reply\./i)).toBeInTheDocument();
  });

  it("saves a draft when the stream reports no live session", async () => {
    // A live_session_unavailable from the stream must NOT fall back to buffered
    // converse (it would just 503 again); it uses the reliable draft endpoint.
    streamMock.mockRejectedValue(
      new ApiError("not ready", 'alfred serve returned 503: {"error": "live_session_unavailable"}'),
    );
    draftMock.mockResolvedValue(draftResponse({ summary: "Draft saved from stream fallback." }));
    const user = userEvent.setup();
    renderChat();

    await user.type(chatInput(), "Build it");
    await user.click(screen.getByRole("button", { name: /start/i }));

    expect(await screen.findByText(/draft saved from stream fallback/i)).toBeInTheDocument();
    // The buffered converse is never reached on a live-session degrade.
    expect(converseMock).not.toHaveBeenCalled();
    expect(draftMock).toHaveBeenCalledTimes(1);
  });
});

describe("ComposeView (conversational, cancellation)", () => {
  beforeEach(() => {
    converseMock.mockReset();
    draftMock.mockReset();
    controlMock.mockReset();
    controlMock.mockResolvedValue({
      handled: false,
      action: "not_a_command",
      text: "",
      detail: "no leading control verb",
      actor_user_id: "ULOCALCLIENT",
    });
    streamMock.mockReset();
  });

  it("passes an AbortSignal to the stream and aborts it on unmount", async () => {
    // Hold the stream open so it is still in flight when we unmount. A real
    // implementation would never resolve a torn-down component's state; here we
    // assert the controller is aborted so the late resolve is dropped.
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

    await user.type(chatInput(), "Add a CSV export");
    await user.click(screen.getByRole("button", { name: /start/i }));

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    expect(capturedSignal).toBeInstanceOf(AbortSignal);
    expect(capturedSignal?.aborted).toBe(false);

    // Tear the view down while the stream is still pending.
    unmount();
    expect(capturedSignal?.aborted).toBe(true);

    // A late resolve after unmount must not throw or commit state on the dead
    // component (no act() warning, no unhandled rejection).
    releaseStream?.(converseResponse({ reply: "late reply that should be dropped" }));
    await Promise.resolve();
    expect(
      screen.queryByText(/late reply that should be dropped/i),
    ).not.toBeInTheDocument();
  });

  it("drops a stale stream resolve so it cannot resurrect a reset chat", async () => {
    // Simulate the reset()-while-in-flight race directly: the first send's
    // stream is still pending when reset() runs (here driven by unmounting and
    // remounting a fresh view), so its late resolve must not repaint a cleared
    // transcript. We assert the first run's signal is aborted, proving the
    // late token callback is gated out by isCurrent().
    let firstSignal: AbortSignal | undefined;
    let firstOnToken: ((text: string) => void) | undefined;
    streamMock.mockImplementationOnce((_baseUrl, _request, onToken, signal) => {
      firstSignal = signal;
      firstOnToken = onToken;
      return new Promise<ConverseResponse>(() => {
        // never resolves; the run is abandoned by unmount
      });
    });

    const user = userEvent.setup();
    const { unmount } = renderChat();
    await user.type(chatInput(), "first message");
    await user.click(screen.getByRole("button", { name: /start/i }));
    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));

    unmount();
    expect(firstSignal?.aborted).toBe(true);
    // A late token from the abandoned run is a no-op (guarded by isCurrent());
    // calling it must not throw.
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
