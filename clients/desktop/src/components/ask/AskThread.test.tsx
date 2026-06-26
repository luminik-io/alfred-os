import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComposeView } from "../ComposeView";
import {
  ApiError,
  composeConverse,
  composeDraft,
  conversationControl,
  filePlanIssue,
  streamComposeConverse,
} from "../../api";
import type { ConverseResponse } from "../../types";

// These tests exercise the assistant-ui ExternalStore adapter wiring that the
// migration introduced: onNew streaming (tokens appended in place), message
// conversion (user vs assistant bubbles, the draft tool-call part), and the
// last-5 recent-threads switcher (resume, v1 migration surfacing). The broader
// converse/draft/control/cancel behavior is covered in ComposeChat.test.tsx.

vi.mock("../../api", async () => {
  const actual = await vi.importActual<typeof import("../../api")>("../../api");
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

function renderChat(selectedRepos = ["your-org/frontend"]) {
  return render(
    <ComposeView baseUrl="http://127.0.0.1:7010" selectedRepos={selectedRepos} onSwitch={vi.fn()} />,
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

beforeEach(() => {
  window.localStorage.clear();
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

describe("Ask adapter: onNew streaming + message conversion", () => {
  it("streams assistant tokens in place through onNew, then reconciles", async () => {
    streamMock.mockImplementation(async (_baseUrl, _request, onToken) => {
      onToken("Which repository ");
      onToken("is the attendees table in?");
      return converseResponse({ reply: "Which repository is the attendees table in?" });
    });
    const user = userEvent.setup();
    renderChat();

    await send(user, "Add a CSV download button");

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    // The streamed text landed in a single assistant bubble (converted from our
    // external store, not a default assistant-ui bubble).
    expect(
      await screen.findByText(/which repository is the attendees table in\?/i),
    ).toBeInTheDocument();
    expect(converseMock).not.toHaveBeenCalled();
  });

  it("renders the user turn and the assistant turn as distinct bubbles", async () => {
    streamMock.mockImplementation(async () => converseResponse());
    const user = userEvent.setup();
    const { container } = renderChat();

    await send(user, "Add a CSV download button to the attendees table");

    expect(
      await screen.findByText(/how should alfred verify this worked\?/i),
    ).toBeInTheDocument();
    expect(container.querySelector(".ask-bubble--user")).toBeInTheDocument();
    expect(container.querySelector(".ask-bubble--assistant")).toBeInTheDocument();
    // The "You" / "Alfred" role labels come from the converted message parts.
    expect(screen.getByText(/^You$/)).toBeInTheDocument();
    expect(screen.getByText(/^Alfred$/)).toBeInTheDocument();
  });

  it("renders a substantive draft as the inline lifecycle card (tool-call part)", async () => {
    streamMock.mockImplementation(async () =>
      converseResponse({ readiness: { score: 92, ready: true, missing: [] } }),
    );
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    // The custom alfred-draft tool-call part renders the lifecycle card.
    expect(await screen.findByLabelText(/plan alfred is shaping/i)).toBeInTheDocument();
    expect(screen.getByText(/^Ready to file$/)).toBeInTheDocument();
  });

  it("keeps the regenerate control on the reply when a draft card trails it", async () => {
    // A build turn emits the text reply AND a separate trailing draft message,
    // so a strict "last message" gate would hide regenerate. It must stay on
    // the reply.
    streamMock.mockImplementation(async () =>
      converseResponse({ readiness: { score: 92, ready: true, missing: [] } }),
    );
    const user = userEvent.setup();
    renderChat();

    await send(user, "Build it");

    expect(await screen.findByLabelText(/plan alfred is shaping/i)).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /regenerate this reply/i }),
    ).toBeInTheDocument();
  });

  it("files a ready plan straight from the inline card", async () => {
    streamMock.mockImplementation(async () =>
      converseResponse({ readiness: { score: 92, ready: true, missing: [] } }),
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
    expect(await screen.findByText(/filed with agent:implement/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view issue/i })).toBeInTheDocument();
  });
});

describe("Ask recent-threads switcher (last-5 persistence)", () => {
  it("hides the Recent control until there is more than one stored thread", async () => {
    streamMock.mockImplementation(async () => converseResponse());
    const user = userEvent.setup();
    renderChat();

    await send(user, "Add a CSV export");
    await screen.findByText(/how should alfred verify this worked\?/i);

    // One active thread only: nothing to switch to yet.
    expect(screen.queryByRole("button", { name: /recent/i })).not.toBeInTheDocument();
  });

  it("resumes a prior conversation from the recent switcher", async () => {
    // Seed one settled conversation, then a new chat creates a second, so the
    // switcher has two entries.
    streamMock.mockImplementationOnce(async () =>
      converseResponse({ reply: "First conversation reply." }),
    );
    const user = userEvent.setup();
    renderChat();

    await send(user, "First conversation question");
    await screen.findByText(/first conversation reply\./i);

    // Start a fresh chat (the prior one is kept in the last-5 list).
    await user.click(screen.getByRole("button", { name: /new chat/i }));
    streamMock.mockImplementationOnce(async () =>
      converseResponse({ reply: "Second conversation reply." }),
    );
    await send(user, "Second conversation question");
    await screen.findByText(/second conversation reply\./i);

    // The second conversation is the active thread, so its reply is on screen
    // and the first conversation's reply is not.
    expect(screen.queryByText(/first conversation reply\./i)).not.toBeInTheDocument();

    // The Recent switcher now has two entries; open it and resume the first.
    await user.click(screen.getByRole("button", { name: /recent/i }));
    const menu = await screen.findByLabelText(/recent ask conversations/i);
    await user.click(within(menu).getByText(/first conversation question/i));

    // Resuming restores the first conversation's transcript and drops the
    // second's from view.
    expect(await screen.findByText(/first conversation reply\./i)).toBeInTheDocument();
    expect(screen.queryByText(/second conversation reply\./i)).not.toBeInTheDocument();
  });

  it("preserves the active conversation when switching threads mid-stream", async () => {
    const user = userEvent.setup();
    renderChat();

    // Two settled conversations so the Recent switcher is available.
    streamMock.mockImplementationOnce(async () => converseResponse({ reply: "B reply." }));
    await send(user, "Question B");
    await screen.findByText(/b reply\./i);
    await user.click(screen.getByRole("button", { name: /new chat/i }));
    streamMock.mockImplementationOnce(async () => converseResponse({ reply: "C reply." }));
    await send(user, "Question C");
    await screen.findByText(/c reply\./i);
    await user.click(screen.getByRole("button", { name: /new chat/i }));

    // Start conversation A with a stream that never settles, so it stays busy
    // and the settle effect does not persist it.
    streamMock.mockImplementationOnce(() => new Promise<ConverseResponse>(() => {}));
    await send(user, "Question A unfinished");
    expect(await screen.findByText(/question a unfinished/i)).toBeInTheDocument();

    // Switch to B mid-stream. Without the persist-before-switch fix, A's turn is
    // dropped because the swap replaces it while busy.
    await user.click(screen.getByRole("button", { name: /recent/i }));
    let menu = await screen.findByLabelText(/recent ask conversations/i);
    await user.click(within(menu).getByText(/question b/i));
    await screen.findByText(/b reply\./i);

    // A must still be recoverable from Recent: its message was persisted.
    await user.click(screen.getByRole("button", { name: /recent/i }));
    menu = await screen.findByLabelText(/recent ask conversations/i);
    await user.click(within(menu).getByText(/question a unfinished/i));
    expect(await screen.findByText(/question a unfinished/i)).toBeInTheDocument();
  });

  it("rehydrates the most recent conversation on mount (and survives across mounts)", async () => {
    streamMock.mockImplementation(async () =>
      converseResponse({ reply: "Persisted reply." }),
    );
    const user = userEvent.setup();
    const first = renderChat();

    await send(user, "A question to persist");
    await screen.findByText(/persisted reply\./i);

    first.unmount();

    // A fresh mount reads the persisted conversation back from localStorage.
    renderChat();
    expect(await screen.findByText(/persisted reply\./i)).toBeInTheDocument();
    expect(screen.getByText(/a question to persist/i)).toBeInTheDocument();
  });

  it("rehydrates a migrated legacy v1 conversation on mount", async () => {
    window.localStorage.setItem(
      "alfred.ask.history.v1",
      JSON.stringify({
        version: 1,
        draftId: "legacy-draft",
        draft: converseResponse().draft,
        turns: [
          { kind: "message", role: "user", content: "legacy request" },
          { kind: "message", role: "assistant", content: "legacy reply" },
        ],
        updatedAt: 1000,
      }),
    );

    renderChat();
    // The migrated v1 conversation shows up as the active thread.
    expect(await screen.findByText(/legacy reply/i)).toBeInTheDocument();
    expect(screen.getByText(/legacy request/i)).toBeInTheDocument();
  });
});

describe("Ask hero and copy", () => {
  it("shows the ask-anything hero and no plain/technical toggle", () => {
    renderChat();
    expect(screen.getByRole("heading", { name: /ask alfred anything/i })).toBeInTheDocument();
    expect(screen.getByText(/ask a question, or describe a change/i)).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /plain language/i })).not.toBeInTheDocument();
  });

  it("seeds the composer from a starter chip", async () => {
    const user = userEvent.setup();
    renderChat();

    await user.click(screen.getByRole("button", { name: /how does alfred work/i }));
    expect((chatInput() as HTMLTextAreaElement).value).toMatch(/how does alfred work/i);
  });
});
