import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LogsView } from "./LogsView";
import { streamFiringTail, type LogTailHandlers } from "../api";
import type { FiringRecord } from "../types";

// Mock the streaming + history helpers so the live tail is driven by the test
// rather than a real EventSource. `loadAgentFirings` is unused here (the agent
// is in the global feed) but mocked so the api module never touches the network.
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    streamFiringTail: vi.fn(() => () => {}),
    loadAgentFirings: vi.fn(async () => []),
  };
});

const tailMock = vi.mocked(streamFiringTail);

function runningFiring(overrides: Partial<FiringRecord> = {}): FiringRecord {
  return {
    firing_id: "lucius-2026-06-03-1200",
    codename: "lucius",
    started_at: "2026-06-03T12:00:00Z",
    ended_at: null,
    status: "running",
    summary: "",
    transcript_path: "/state/transcripts/lucius/2026-06/lucius-2026-06-03-1200.jsonl",
    events_path: "/state/lucius/events/lucius-2026-06-03-1200.jsonl",
    raw_events: [],
    ...overrides,
  };
}

function renderLogs(firings: FiringRecord[], agent: string | null = "lucius") {
  return render(
    <LogsView
      baseUrl="http://127.0.0.1:7010"
      feed={[]}
      unseen={0}
      seen={new Set()}
      onMarkAllSeen={vi.fn()}
      firings={firings}
      // A non-zero nonce deep-links straight to the "Latest run" tail tab.
      focus={{ agent, nonce: 1 }}
    />,
  );
}

describe("LogsView live tail (#41)", () => {
  beforeEach(() => {
    tailMock.mockReset();
    tailMock.mockImplementation(() => () => {});
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("subscribes to the live tail for a running firing and appends streamed lines", () => {
    let captured: LogTailHandlers | null = null;
    tailMock.mockImplementation((_baseUrl, _firingId, handlers) => {
      captured = handlers;
      return () => {};
    });
    renderLogs([runningFiring()]);

    // The tail is opened for the running firing.
    expect(tailMock).toHaveBeenCalledTimes(1);
    expect(tailMock.mock.calls[0][1]).toBe("lucius-2026-06-03-1200");

    // Streamed transcript lines render incrementally in the live region.
    act(() => {
      captured?.onLines([
        JSON.stringify({
          type: "assistant",
          message: { role: "assistant", content: [{ type: "text", text: "Working on it." }] },
        }),
      ]);
    });
    const live = screen.getByRole("list", { name: /live transcript/i });
    expect(within(live).getByText(/working on it\./i)).toBeInTheDocument();

    act(() => {
      captured?.onLines([
        JSON.stringify({
          type: "assistant",
          message: {
            role: "assistant",
            content: [{ type: "tool_use", name: "Bash", input: { command: "npm test" } }],
          },
        }),
      ]);
    });
    expect(within(live).getByText(/npm test/i)).toBeInTheDocument();
  });

  it("does not open a live tail for a completed run (the poll already has it)", () => {
    renderLogs([runningFiring({ status: "ok", ended_at: "2026-06-03T12:05:00Z" })]);
    expect(tailMock).not.toHaveBeenCalled();
  });

  it("falls back silently when the stream errors, leaving the poll-derived view", () => {
    // A running firing with a poll-captured, server-distilled timeline: a stream
    // error must not blank that, it just stops appending live lines.
    tailMock.mockImplementation((_baseUrl, _firingId, handlers) => {
      handlers.onError?.(new Error("stream down"));
      return () => {};
    });
    renderLogs([
      runningFiring({
        timeline: {
          headline: "Running",
          severity: "idle",
          error: null,
          outcome: null,
          steps: [
            { kind: "started", label: "Run started", detail: "", tone: "muted", ts: null },
            { kind: "picked", label: "Picked repo", detail: "api", tone: "ok", ts: null },
          ],
        },
      }),
    ]);
    // The poll-derived timeline still renders; no error is surfaced to the user.
    expect(screen.getByText(/picked repo/i)).toBeInTheDocument();
    expect(screen.queryByText(/stream down/i)).not.toBeInTheDocument();
  });

  it("collapses idle runs to one line and shouts an honest error for failures", () => {
    renderLogs([
      runningFiring({
        firing_id: "lucius-2026-06-03-1300",
        status: "error",
        ended_at: "2026-06-03T13:02:00Z",
        timeline: {
          headline: "Failed · authentication",
          severity: "error",
          error: "authentication",
          outcome: "llm-error_authentication",
          steps: [
            { kind: "started", label: "Run started", detail: "", tone: "muted", ts: null },
            {
              kind: "fallback",
              label: "Engine fallback",
              detail: "claude -> codex after authentication",
              tone: "warn",
              ts: null,
            },
            { kind: "complete", label: "Failed · authentication", detail: "", tone: "error", ts: null },
          ],
        },
      }),
    ]);
    // The honest cause is surfaced; the misleading downstream rate-limit text is not.
    expect(screen.getAllByText(/authentication/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/rate.?limit/i)).not.toBeInTheDocument();
  });

  it("filters to error runs when 'Errors only' is toggled on", async () => {
    const okRun = runningFiring({
      firing_id: "lucius-ok",
      status: "ok",
      ended_at: "2026-06-03T12:05:00Z",
      timeline: {
        headline: "Opened PR #1048",
        severity: "ok",
        error: null,
        outcome: "pr-opened",
        steps: [],
      },
    });
    const errRun = runningFiring({
      firing_id: "lucius-err",
      status: "error",
      ended_at: "2026-06-03T12:09:00Z",
      timeline: {
        headline: "Failed · rate limit",
        severity: "error",
        error: "rate_limit",
        outcome: "llm-error_rate_limit",
        steps: [],
      },
    });
    renderLogs([okRun, errRun]);

    // Both runs visible by default.
    expect(screen.getByText(/opened pr #1048/i)).toBeInTheDocument();
    expect(screen.getByText(/failed · rate limit/i)).toBeInTheDocument();

    // Flip the errors-only switch.
    const toggle = screen.getByRole("switch", { name: /errors only/i });
    await act(async () => {
      toggle.click();
    });

    // The clean PR run is hidden; the failure remains.
    expect(screen.queryByText(/opened pr #1048/i)).not.toBeInTheDocument();
    expect(screen.getByText(/failed · rate limit/i)).toBeInTheDocument();
  });

  it("resets the errors-only filter when switching to another agent", async () => {
    const luciusErr = runningFiring({
      firing_id: "lucius-err",
      codename: "lucius",
      status: "error",
      ended_at: "2026-06-03T12:09:00Z",
      timeline: {
        headline: "Failed · rate limit",
        severity: "error",
        error: "rate_limit",
        outcome: "llm-error_rate_limit",
        steps: [],
      },
    });
    // A second agent whose only run is clean: if the errors-only filter leaked
    // across the switch it would render the misleading "no runs" empty state.
    const baneOk = runningFiring({
      firing_id: "bane-ok",
      codename: "bane",
      status: "ok",
      ended_at: "2026-06-03T12:20:00Z",
      timeline: {
        headline: "Opened PR #2001",
        severity: "ok",
        error: null,
        outcome: "pr-opened",
        steps: [],
      },
    });
    renderLogs([luciusErr, baneOk], "lucius");

    // Turn the filter on for lucius (which has a failure).
    const toggle = screen.getByRole("switch", { name: /errors only/i });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText(/failed · rate limit/i)).toBeInTheDocument();

    // Switch to bane, whose runs are all clean.
    const baneTab = screen.getByRole("tab", { name: /bane/i });
    await act(async () => {
      baneTab.click();
    });

    // The filter reset, so bane's clean run shows instead of an empty state.
    expect(screen.getByText(/opened pr #2001/i)).toBeInTheDocument();
    expect(screen.queryByText(/no runs need attention/i)).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /errors only/i })).not.toBeChecked();
  });
});
