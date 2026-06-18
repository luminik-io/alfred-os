import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAlfred } from "./useAlfred";
import type {
  AgentSummary,
  NativeCommandResult,
  ShippedBoard,
  Snapshot,
  UsageResponse,
} from "../types";

// Mock the whole api surface so the hook's network/native calls are
// deterministic. trayEvents/notifications/derive/fleetControl stay real (pure).
// vi.mock is hoisted above module init, so the mocks and constants the factory
// needs are declared via vi.hoisted.
const DEFAULT_BASE_URL = "http://127.0.0.1:7010";
const LEGACY_AIRPLAY_BASE_URL = "http://127.0.0.1:7000";

const hooks = vi.hoisted(() => ({
  loadSnapshotMock: vi.fn(),
  loadShippedMock: vi.fn(),
  loadUsageMock: vi.fn(),
  runNativeActionMock: vi.fn(),
  rememberBaseUrlMock: vi.fn(),
  decidePlanMock: vi.fn(),
  filePlanIssueMock: vi.fn(),
  DEFAULT_BASE_URL: "http://127.0.0.1:7010",
  LEGACY_AIRPLAY_BASE_URL: "http://127.0.0.1:7000",
}));

const loadSnapshotMock = hooks.loadSnapshotMock as ReturnType<
  typeof vi.fn<(baseUrl: string) => Promise<Snapshot>>
>;
const loadShippedMock = hooks.loadShippedMock as ReturnType<
  typeof vi.fn<(baseUrl: string) => Promise<ShippedBoard>>
>;
const loadUsageMock = hooks.loadUsageMock as ReturnType<
  typeof vi.fn<(baseUrl: string) => Promise<UsageResponse>>
>;
const runNativeActionMock = hooks.runNativeActionMock as ReturnType<
  typeof vi.fn<() => Promise<NativeCommandResult>>
>;
const rememberBaseUrlMock = hooks.rememberBaseUrlMock;

vi.mock("../api", () => ({
  initialBaseUrl: () => hooks.DEFAULT_BASE_URL,
  clientBaseUrl: (value?: string | null) => {
    const normalized = (value?.trim() || hooks.DEFAULT_BASE_URL).replace(/\/$/, "");
    return normalized === hooks.LEGACY_AIRPLAY_BASE_URL ? hooks.DEFAULT_BASE_URL : normalized;
  },
  alternateDefaultBaseUrl: (value: string) => {
    const normalized = value.trim().replace(/\/$/, "");
    if (normalized === hooks.DEFAULT_BASE_URL) return null;
    if (/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/i.test(normalized)) {
      return hooks.DEFAULT_BASE_URL;
    }
    return null;
  },
  rememberBaseUrl: (value: string) => hooks.rememberBaseUrlMock(value),
  loadSnapshot: (baseUrl: string) => hooks.loadSnapshotMock(baseUrl),
  loadShipped: (baseUrl: string) => hooks.loadShippedMock(baseUrl),
  loadUsage: (baseUrl: string) => hooks.loadUsageMock(baseUrl),
  runNativeAction: () => hooks.runNativeActionMock(),
  convertFollowupToDraft: vi.fn(),
  markFollowupHandled: vi.fn(),
  decidePlan: (...args: unknown[]) => hooks.decidePlanMock(...args),
  filePlanIssue: (...args: unknown[]) => hooks.filePlanIssueMock(...args),
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
    shipped: null,
    schedule: [],
    status: { agents, total_today: 0, reliability: { status: "ok" } },
    actions: {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    memoryCandidates: { rows: [] },
    firings: [],
    plans: [],
    trustedSlack: null,
  };
}

function shippedBoard(): ShippedBoard {
  return {
    lookback_days: 14,
    repos: [],
    columns: { queued: [], in_progress: [], shipped: [] },
    counts: { queued: 0, in_progress: 0, shipped: 0 },
  };
}

function usage(): UsageResponse {
  return {
    available: true,
    kind: "subscription",
    source: "native",
    block: null,
    codex: null,
    limits: null,
    weekly: null,
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
  loadShippedMock.mockReset();
  loadUsageMock.mockReset();
  runNativeActionMock.mockReset();
  rememberBaseUrlMock.mockReset();
  hooks.filePlanIssueMock.mockReset();
  loadShippedMock.mockResolvedValue(shippedBoard());
  loadUsageMock.mockResolvedValue(usage());
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
  it("does not retry on 7000 when the preferred 7010 endpoint fails", async () => {
    loadSnapshotMock.mockRejectedValue(new Error("connection refused"));

    const { result } = renderHook(() => useAlfred());

    await waitFor(() => expect(result.current.error).toMatch(/connection refused/i));
    expect(result.current.baseUrl).toBe(DEFAULT_BASE_URL);
    expect(loadSnapshotMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
    expect(loadSnapshotMock).not.toHaveBeenCalledWith(LEGACY_AIRPLAY_BASE_URL);
    expect(rememberBaseUrlMock).not.toHaveBeenCalledWith(LEGACY_AIRPLAY_BASE_URL);
  });

  it("normalizes a saved legacy 7000 endpoint before fetching", async () => {
    loadSnapshotMock.mockResolvedValueOnce(snapshot([agent("bane")]));

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());

    loadSnapshotMock.mockClear();
    rememberBaseUrlMock.mockClear();
    loadSnapshotMock.mockResolvedValueOnce(snapshot([agent("lucius")]));

    await act(async () => {
      await result.current.refresh(LEGACY_AIRPLAY_BASE_URL);
    });

    expect(result.current.baseUrl).toBe(DEFAULT_BASE_URL);
    expect(result.current.error).toBeNull();
    expect(loadSnapshotMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
    expect(loadSnapshotMock).not.toHaveBeenCalledWith(LEGACY_AIRPLAY_BASE_URL);
    expect(rememberBaseUrlMock).toHaveBeenLastCalledWith(DEFAULT_BASE_URL);
  });

  it("recovers from a stale saved localhost port", async () => {
    const staleUrl = "http://127.0.0.1:7011";
    loadSnapshotMock.mockResolvedValueOnce(snapshot([agent("bane")]));

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());

    loadSnapshotMock.mockImplementation(async (baseUrl: string) => {
      if (baseUrl === staleUrl) {
        throw new Error("connection refused");
      }
      return snapshot([agent("lucius")]);
    });

    await act(async () => {
      await result.current.refresh(staleUrl);
    });

    expect(result.current.baseUrl).toBe(DEFAULT_BASE_URL);
    expect(result.current.error).toBeNull();
    expect(loadSnapshotMock).toHaveBeenCalledWith(staleUrl);
    expect(loadSnapshotMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
    expect(rememberBaseUrlMock).toHaveBeenLastCalledWith(DEFAULT_BASE_URL);
  });

  it("also retries board and usage on the preferred port when the saved port is stale", async () => {
    const staleUrl = "http://127.0.0.1:7011";
    loadSnapshotMock.mockResolvedValue(snapshot([agent("bane")]));
    loadShippedMock.mockImplementation(async (baseUrl: string) => {
      if (baseUrl === staleUrl) throw new Error("board timed out");
      return shippedBoard();
    });
    loadUsageMock.mockImplementation(async (baseUrl: string) => {
      if (baseUrl === staleUrl) throw new Error("usage timed out");
      return usage();
    });

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());

    await act(async () => {
      await Promise.all([result.current.refreshShipped(staleUrl), result.current.refreshUsage(staleUrl)]);
    });

    expect(result.current.baseUrl).toBe(DEFAULT_BASE_URL);
    expect(result.current.shippedState).toBe("idle");
    expect(result.current.shippedError).toBeNull();
    expect(result.current.usageState).toBe("idle");
    expect(result.current.usage?.available).toBe(true);
    expect(loadShippedMock).toHaveBeenCalledWith(staleUrl);
    expect(loadShippedMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
    expect(loadUsageMock).toHaveBeenCalledWith(staleUrl);
    expect(loadUsageMock).toHaveBeenCalledWith(DEFAULT_BASE_URL);
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

describe("useAlfred plan go/no-go", () => {
  function batmanPlan() {
    return {
      plan_id: "13-plan",
      title: "Add CSV export",
      status: "draft",
      parent: null,
      affected_repos: null,
      updated_at: null,
      path: "",
      preview: "",
      content: "",
      source: "batman",
      readiness_score: null,
      readiness_ok: null,
      revision_count: 0,
    };
  }

  it("writes a decision and refreshes so the decided plan leaves the queue", async () => {
    loadSnapshotMock.mockResolvedValue(snapshot([agent("batman")]));
    hooks.decidePlanMock.mockResolvedValue({
      plan_id: "13-plan",
      issue_number: 13,
      decision: "approve",
      status: "approved",
      marker_path: "/tmp/state/../batman/approvals/13.approved",
    });

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());
    loadSnapshotMock.mockClear();

    await act(async () => {
      await result.current.runPlanDecision(batmanPlan(), "approve");
    });

    expect(hooks.decidePlanMock).toHaveBeenCalledWith(
      DEFAULT_BASE_URL,
      "13-plan",
      "approve",
    );
    expect(result.current.noticeFor("plans")?.tone).toBe("ok");
    expect(result.current.noticeFor("plans")?.message).toMatch(/issue #13/i);
    // The notice is scoped to its surface: it shows on Plans, never on Board /
    // Memory / Setup, so a decision banner cannot bleed across surfaces.
    expect(result.current.noticeFor("board")).toBeNull();
    expect(result.current.noticeFor("memory")).toBeNull();
    expect(result.current.noticeFor("setup")).toBeNull();
    // A refresh fires after the write so the decided plan reflects its state.
    expect(loadSnapshotMock).toHaveBeenCalled();
  });

  it("surfaces a decision failure as an error notice", async () => {
    loadSnapshotMock.mockResolvedValue(snapshot([agent("batman")]));
    hooks.decidePlanMock.mockRejectedValueOnce(new Error("forbidden"));

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());

    await act(async () => {
      await result.current.runPlanDecision(batmanPlan(), "decline");
    });

    expect(result.current.noticeFor("plans")?.tone).toBe("error");
    expect(result.current.noticeFor("plans")?.message).toMatch(/forbidden/i);
    // An error notice is also surface-scoped.
    expect(result.current.noticeFor("board")).toBeNull();
  });

  it("files a ready planning draft and refreshes the plans plus board", async () => {
    loadSnapshotMock.mockResolvedValue(snapshot([agent("batman")]));
    hooks.filePlanIssueMock.mockResolvedValue({
      ok: true,
      status: "filed",
      draft_id: "compose-20260604-export",
      issue_url: "https://github.com/owner/web/issues/42",
      repo: "owner/web",
      label: "agent:implement",
    });

    const { result } = renderHook(() => useAlfred());
    await waitFor(() => expect(result.current.snapshot).not.toBeNull());
    loadSnapshotMock.mockClear();
    loadShippedMock.mockClear();

    await act(async () => {
      await result.current.runPlanIssueFile({
        plan_id: "compose-20260604-export",
        title: "Add export planning",
        status: "ready",
        parent: null,
        affected_repos: "owner/web",
        updated_at: null,
        path: "",
        preview: "",
        content: "",
        source: "compose",
        readiness_score: 92,
        readiness_ok: true,
        revision_count: 0,
      });
    });

    expect(hooks.filePlanIssueMock).toHaveBeenCalledWith(
      DEFAULT_BASE_URL,
      "compose-20260604-export",
    );
    expect(result.current.noticeFor("plans")?.tone).toBe("ok");
    expect(result.current.noticeFor("plans")?.message).toMatch(/agent:implement/i);
    expect(loadSnapshotMock).toHaveBeenCalled();
    expect(loadShippedMock).toHaveBeenCalled();
  });
});
