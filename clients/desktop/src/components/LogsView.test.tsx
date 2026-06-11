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
    // A running firing with poll-captured events: a stream error must not blank
    // those, it just stops appending live lines.
    tailMock.mockImplementation((_baseUrl, _firingId, handlers) => {
      handlers.onError?.(new Error("stream down"));
      return () => {};
    });
    renderLogs([
      runningFiring({
        raw_events: [{ ts: "2026-06-03T12:00:01Z", event: "preflight_passed" }],
      }),
    ]);
    // The poll-derived event still renders; no error is surfaced to the user.
    expect(screen.getByText(/preflight_passed/i)).toBeInTheDocument();
    expect(screen.queryByText(/stream down/i)).not.toBeInTheDocument();
  });
});
