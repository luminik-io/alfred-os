import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAlfred } from "./useAlfred";
import type { AgentSummary, NativeCommandResult, Snapshot } from "../types";

// Mock the whole api surface so the hook's network/native calls are
// deterministic. trayEvents/notifications/derive/fleetControl stay real (pure).
// vi.mock is hoisted above module init, so the mocks and constants the factory
// needs are declared via vi.hoisted.
const DEFAULT_BASE_URL = "http://127.0.0.1:7000";
const FALLBACK_BASE_URL = "http://127.0.0.1:7010";

const hooks = vi.hoisted(() => ({
  loadSnapshotMock: vi.fn(),
  runNativeActionMock: vi.fn(),
  rememberBaseUrlMock: vi.fn(),
  DEFAULT_BASE_URL: "http://127.0.0.1:7000",
  FALLBACK_BASE_URL: "http://127.0.0.1:7010",
}));

const loadSnapshotMock = hooks.loadSnapshotMock as ReturnType<
  typeof vi.fn<(baseUrl: string) => Promise<Snapshot>>
>;
const runNativeActionMock = hooks.runNativeActionMock as ReturnType<
  typeof vi.fn<() => Promise<NativeCommandResult>>
>;
const rememberBaseUrlMock = hooks.rememberBaseUrlMock;

vi.mock("../api", () => ({
  FALLBACK_BASE_URL: hooks.FALLBACK_BASE_URL,
  initialBaseUrl: () => hooks.DEFAULT_BASE_URL,
  isDefaultBaseUrl: (value: string) =>
    value.trim().replace(/\/$/, "") === hooks.DEFAULT_BASE_URL,
  rememberBaseUrl: (value: string) => hooks.rememberBaseUrlMock(value),
  loadSnapshot: (baseUrl: string) => hooks.loadSnapshotMock(baseUrl),
  runNativeAction: () => hooks.runNativeActionMock(),
  convertFollowupToDraft: vi.fn(),
  markFollowupHandled: vi.fn(),
  startLocalRuntime: vi.fn(),
  setTrayStatus: vi.fn(async () => undefined),
  supportsNativeActions: () => true,
  errorDetail: (err: unknown) => (err instanceof Error ? err.message : null),
}));

vi.mock("../lib/trayEvents", () => ({
  listenTrayEvents: () => Promise.resolve(() => undefined),
}));

function agent(codename: string, overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    codename,
    last_firing_id: null,
    last_run_at: "2026-05-30T10:00:00Z",
    status: "live",
    last_summary: "ok",
    firings_today: 1,
    ...overrides,
  };
}

function snapshot(agents: AgentSummary[]): Snapshot {
  return {
    loadedAt: new Date(),
    status: { agents, total_today: 0, reliability: { status: "ok" } },
    actions: {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    firings: [],
    plans: [],
  };
}

// A deferred whose resolve we can fire later, to control resolution ordering.
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function nativeResult(): NativeCommandResult {
  return {
    command: ["alfred", "pause", "lucius"],
    stdout: "",
    stderr: "",
    status: 0,
    success: true,
    pid: null,
    message: null,
  };
}

beforeEach(() => {
  loadSnapshotMock.mockReset();
  runNativeActionMock.mockReset();
  rememberBaseUrlMock.mockReset();
  window.localStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useAlfred refresh race", () => {
  it("ignores a stale in-flight refresh that resolves after a newer one", async () => {
    // First (slow) refresh shows lucius running; second (fast) refresh shows
    // lucius paused. The slow one resolves LAST and must not clobber the fast
    // result. This is the post-pause clobber the request-id guard prevents.
    const slow = deferred<Snapshot>();
    const fast = deferred<Snapshot>();

    // Mount fires an initial refresh; satisfy it immediately, then drive the race.
    loadSnapshotMock.mockResolvedValueOnce(snapshot([agent("lucius", { status: "live" })]));

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());

    // Now stage the race: call A (slow, "running"), then call B (fast, "paused").
    loadSnapshotMock.mockReturnValueOnce(slow.promise);
    loadSnapshotMock.mockReturnValueOnce(fast.promise);

    let callA: Promise<void>;
    let callB: Promise<void>;
    act(() => {
      callA = result.current.refresh(DEFAULT_BASE_URL);
      callB = result.current.refresh(DEFAULT_BASE_URL);
    });

    // Resolve the fast (newer) call first, then the slow (older) call.
    await act(async () => {
      fast.resolve(snapshot([agent("lucius", { status: "live", paused: true })]));
      await callB;
      slow.resolve(snapshot([agent("lucius", { status: "running", paused: false })]));
      await callA;
    });

    // The newer (fast) refresh wins: lucius is paused, not running.
    const lucius = result.current.snapshot?.status.agents[0];
    expect(lucius?.paused).toBe(true);
    expect(lucius?.status).toBe("live");
  });
});

describe("useAlfred fallback port", () => {
  it("retries on 7010 when the default 7000 fails", async () => {
    // Initial mount refresh: default rejects, fallback resolves.
    loadSnapshotMock.mockImplementation(async (baseUrl: string) => {
      if (baseUrl === DEFAULT_BASE_URL) {
        throw new Error("connection refused");
      }
      return snapshot([agent("bane")]);
    });

    const { result } = renderHook(() => useAlfred());

    await waitFor(() => expect(result.current.baseUrl).toBe(FALLBACK_BASE_URL));
    expect(result.current.error).toBeNull();
    expect(loadSnapshotMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
    expect(loadSnapshotMock).toHaveBeenCalledWith(FALLBACK_BASE_URL);
    expect(rememberBaseUrlMock).toHaveBeenLastCalledWith(FALLBACK_BASE_URL);
  });
});

describe("useAlfred post-action refresh ordering", () => {
  it("refreshes after a pause and reflects the post-action snapshot", async () => {
    // Mount snapshot: lucius running. After pause, refresh returns paused.
    loadSnapshotMock.mockResolvedValueOnce(snapshot([agent("lucius", { paused: false })]));
    runNativeActionMock.mockResolvedValue(nativeResult());

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());
    expect(result.current.snapshot?.status.agents[0].paused).toBe(false);

    // The post-pause refresh (refreshAfter) and the fleet-service re-read both
    // resolve to the paused state.
    loadSnapshotMock.mockResolvedValue(snapshot([agent("lucius", { paused: true })]));

    await act(async () => {
      await result.current.runLocalAction({
        action: "pause",
        target: "lucius",
        refreshAfter: true,
      });
    });

    expect(runNativeActionMock).toHaveBeenCalled();
    expect(result.current.snapshot?.status.agents[0].paused).toBe(true);
    expect(result.current.nativeResult?.success).toBe(true);
  });
});
