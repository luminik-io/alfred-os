import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
}));

import {
  ApiError,
  DEFAULT_BASE_URL,
  addTrustedSlackUser,
  alternateDefaultBaseUrl,
  clientBaseUrl,
  composeDraft,
  conversationControl,
  decidePlan,
  errorDetail,
  initialBaseUrl,
  loadShipped,
  loadSnapshot,
  promoteMemoryCandidate,
  streamComposeConverse,
  streamFiringTail,
} from "./api";
import type { ConverseRequest } from "./types";

// In jsdom (no __TAURI_INTERNALS__) the api layer goes through global fetch, so
// we can drive every endpoint's outcome by stubbing fetch per URL.

const ENDPOINTS = {
  status: "/api/status",
  actions: "/api/actions",
  memoryCandidates: "/api/memory/candidates",
  firings: "/api/firings",
  plans: "/api/plans",
  trustedSlack: "/api/slack/trusted-users",
  shipped: "/api/shipped",
  schedule: "/api/schedule",
} as const;

describe("base URL fallback", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("does not probe the legacy AirPlay port after the preferred port fails", () => {
    expect(alternateDefaultBaseUrl("http://127.0.0.1:7010")).toBeNull();
    expect(alternateDefaultBaseUrl("http://127.0.0.1:7000")).toBeNull();
  });

  it("recovers from stale localhost ports by trying the preferred Alfred serve port", () => {
    expect(alternateDefaultBaseUrl("http://127.0.0.1:7011")).toBe("http://127.0.0.1:7010");
    expect(alternateDefaultBaseUrl("http://localhost:7999/")).toBe("http://127.0.0.1:7010");
  });

  it("normalizes saved local ports in browser preview because the Vite proxy owns the target", () => {
    window.localStorage.setItem("alfred-desktop.base-url", "http://127.0.0.1:7000");

    expect(initialBaseUrl()).toBe("http://127.0.0.1:7010");
    expect(clientBaseUrl("http://localhost:7999")).toBe("http://127.0.0.1:7010");
  });

  it("keeps remote base URLs for browser preview", () => {
    window.localStorage.setItem("alfred-desktop.base-url", "https://alfred.example.com");

    expect(initialBaseUrl()).toBe("https://alfred.example.com");
    expect(clientBaseUrl("https://alfred.example.com")).toBe("https://alfred.example.com");
  });

  it("does not redirect arbitrary remote URLs", () => {
    expect(alternateDefaultBaseUrl("https://example.com")).toBeNull();
  });
});

type EndpointKey = keyof typeof ENDPOINTS;

function jsonFor(path: string): unknown {
  if (path.includes(ENDPOINTS.schedule)) {
    return {
      runs: [
        {
          codename: "bane",
          role: "Daily test author",
          kind: "cron-daily",
          cadence: "daily 02:00",
          next_fire_at: "2026-06-04T02:00:00",
          raw_schedule: "cron:2:00",
        },
        {
          codename: "lucius",
          role: "Single-repo engineer",
          kind: "interval",
          cadence: "every 10m",
          next_fire_at: null,
          raw_schedule: "interval:600",
        },
      ],
    };
  }
  if (path.includes(ENDPOINTS.status)) {
    return {
      agents: [{ codename: "lucius" }],
      total_today: 2,
      reliability: { status: "ok" },
      metrics: {
        spend_usd: 1.75,
        firings: 4,
        successes: 3,
        failures: 1,
        agents_with_spend: 2,
      },
      intake_profile: "technical",
    };
  }
  if (path.includes(ENDPOINTS.actions)) {
    return {
      status: "ok",
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    };
  }
  if (path.includes(ENDPOINTS.firings)) {
    return { rows: [{ firing_id: "f1", codename: "lucius" }] };
  }
  if (path.includes(ENDPOINTS.memoryCandidates)) {
    return {
      rows: [
        {
          id: "mem:1",
          codename: "lucius",
          repo: "your-org/api",
          body: "Use request fixtures.",
          tags: ["tests"],
          severity: "info",
          source: "slack",
          source_firing_id: null,
          evidence: "",
          confidence: 0.8,
          status: "candidate",
          created_at: "2026-05-30T12:00:00Z",
        },
      ],
    };
  }
  if (path.includes(ENDPOINTS.trustedSlack)) {
    return {
      operator_user_id: "UOPERATOR",
      users: [{ user_id: "UOPERATOR", sources: ["operator"], can_remove: false }],
      state_path: "/tmp/state/slack-trust/trusted-users.json",
    };
  }
  if (path.includes(ENDPOINTS.shipped)) {
    return {
      generated_at: "2026-06-02T00:00:00Z",
      lookback_days: 14,
      repos: ["your-org/api"],
      columns: {
        queued: [],
        in_progress: [],
        shipped: [
          {
            repo: "your-org/api",
            number: 7,
            title: "Ship it",
            url: "https://example.com/pr/7",
            author: "lucius",
            kind: "pr",
            timestamp: "2026-06-01T00:00:00Z",
            age_days: 1,
            is_draft: false,
            labels: [],
          },
        ],
      },
      counts: { queued: 0, in_progress: 0, shipped: 1 },
      errors: [],
    };
  }
  return { rows: [{ plan_id: "p1", title: "Plan" }] };
}

// Build a fetch stub whose listed endpoints fail; everything else succeeds.
function stubFetch(failing: Partial<Record<EndpointKey, number>> = {}) {
  return vi.fn(async (input: string) => {
    const path = String(input);
    for (const [key, status] of Object.entries(failing)) {
      if (path.includes(ENDPOINTS[key as EndpointKey])) {
        return new Response("Forbidden", { status: status as number });
      }
    }
    return new Response(JSON.stringify(jsonFor(path)), { status: 200 });
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", stubFetch());
});

afterEach(() => {
  invokeMock.mockReset();
  delete window.__TAURI_INTERNALS__;
  vi.unstubAllGlobals();
});

describe("loadSnapshot degradation", () => {
  it("normalizes a legacy AirPlay port before native bridge requests", async () => {
    window.__TAURI_INTERNALS__ = {};
    invokeMock.mockImplementation(async (_command: string, args: { baseUrl: string; path: string }) => {
      expect(args.baseUrl).toBe(DEFAULT_BASE_URL);
      return JSON.stringify(jsonFor(args.path));
    });

    const snap = await loadSnapshot("http://127.0.0.1:7000");

    expect(snap.status.agents).toHaveLength(1);
    expect(invokeMock).toHaveBeenCalledWith(
      "fetch_alfred_json",
      expect.objectContaining({ baseUrl: DEFAULT_BASE_URL, path: "/api/status" }),
    );
  });

  it("renders every section when all endpoints resolve", async () => {
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.firings).toHaveLength(1);
    expect(snap.plans).toHaveLength(1);
    expect(snap.memoryCandidates.rows).toHaveLength(1);
    expect(snap.trustedSlack?.users[0].user_id).toBe("UOPERATOR");
    // The upcoming schedule rolls into the snapshot alongside the spine.
    expect(snap.schedule).toHaveLength(2);
    expect(snap.schedule[0].codename).toBe("bane");
    // The cost rollup + intake profile ride on /api/status.
    expect(snap.status.metrics?.spend_usd).toBe(1.75);
    expect(snap.status.intake_profile).toBe("technical");
    // The Kanban board is fetched separately via loadShipped, not in the snapshot.
    expect(snap.shipped).toBeNull();
    expect(snap.degraded).toBeUndefined();
  });

  it("degrades the schedule lane to empty when /api/schedule fails", async () => {
    vi.stubGlobal("fetch", stubFetch({ schedule: 404 }));
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    // A missing schedule route (older server) never blanks the view; the lane
    // shows an honest empty state instead.
    expect(snap.schedule).toEqual([]);
    expect(snap.degraded?.schedule).toBeTruthy();
  });

  it("loadShipped returns the board, surfacing a build failure as an error", async () => {
    // The board endpoint returns 200 with empty columns + an `error` field when
    // GitHub / gh auth is down. loadShipped must surface that error so the
    // Kanban can show "couldn't build", not a false "nothing shipped".
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const path = String(input);
        if (path.includes(ENDPOINTS.shipped)) {
          return new Response(
            JSON.stringify({
              columns: { queued: [], in_progress: [], shipped: [] },
              counts: { queued: 0, in_progress: 0, shipped: 0 },
              repos: [],
              lookback_days: 14,
              error: "CalledProcessError: gh auth failed",
            }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify(jsonFor(path)), { status: 200 });
      }),
    );
    const board = await loadShipped("http://127.0.0.1:7000");
    expect(board.error).toBe("CalledProcessError: gh auth failed");
    expect(board.columns.shipped).toEqual([]);
  });

  it("requests demo cards only when loadShipped explicitly opts in", async () => {
    const fetchMock = vi.fn(async (input: string) => {
      const path = String(input);
      if (path.includes(ENDPOINTS.shipped)) {
        return new Response(
          JSON.stringify({
            columns: { queued: [], in_progress: [], shipped: [] },
            counts: { queued: 0, in_progress: 0, shipped: 0 },
            repos: [],
            lookback_days: 14,
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify(jsonFor(path)), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await loadShipped("http://127.0.0.1:7000");
    await loadShipped("http://127.0.0.1:7000", 14, { demo: true });

    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/shipped?days=14");
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("demo=1");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/shipped?days=14&demo=1");
  });

  it("surfaces all-empty shipped-board repo errors as an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const path = String(input);
        if (path.includes(ENDPOINTS.shipped)) {
          return new Response(
            JSON.stringify({
              generated_at: "2026-06-02T00:00:00Z",
              lookback_days: 14,
              repos: ["example-org/alfred"],
              columns: { queued: [], in_progress: [], shipped: [] },
              counts: { queued: 0, in_progress: 0, shipped: 0 },
              errors: ["example-org/alfred"],
            }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify(jsonFor(path)), { status: 200 });
      }),
    );
    const board = await loadShipped("http://127.0.0.1:7000");
    expect(board.error).toContain("GitHub data unavailable");
    expect(board.error).toContain("example-org/alfred");
    expect(board.columns.shipped).toEqual([]);
  });

  it("keeps partial shipped-board repo failures as soft errors when there are no cards", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const path = String(input);
        if (path.includes(ENDPOINTS.shipped)) {
          return new Response(
            JSON.stringify({
              generated_at: "2026-06-02T00:00:00Z",
              lookback_days: 14,
              repos: ["example-org/alfred", "example-org/webapp"],
              columns: { queued: [], in_progress: [], shipped: [] },
              counts: { queued: 0, in_progress: 0, shipped: 0 },
              errors: ["example-org/alfred"],
            }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify(jsonFor(path)), { status: 200 });
      }),
    );
    const board = await loadShipped("http://127.0.0.1:7000");
    expect(board.error).toBeUndefined();
    expect(board.errors).toEqual(["example-org/alfred"]);
  });

  it("keeps the dashboard when a non-spine endpoint fails", async () => {
    vi.stubGlobal("fetch", stubFetch({ plans: 500 }));
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    // status/actions/firings still rendered; only plans degraded.
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.firings).toHaveLength(1);
    expect(snap.plans).toEqual([]);
    expect(snap.memoryCandidates.rows).toHaveLength(1);
    expect(snap.trustedSlack?.users).toHaveLength(1);
    expect(snap.degraded?.plans).toBeTruthy();
    expect(snap.degraded?.firings).toBeUndefined();
  });

  it("keeps the dashboard when trusted Slack state is unavailable", async () => {
    vi.stubGlobal("fetch", stubFetch({ trustedSlack: 500 }));
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.trustedSlack).toBeNull();
    expect(snap.degraded?.trustedSlack).toBeTruthy();
  });

  it("keeps the dashboard when memory candidates are unavailable", async () => {
    vi.stubGlobal("fetch", stubFetch({ memoryCandidates: 404 }));
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.memoryCandidates.rows).toEqual([]);
    expect(snap.degraded?.memoryCandidates).toBeTruthy();
  });

  it("throws when the spine /api/status fails", async () => {
    vi.stubGlobal("fetch", stubFetch({ status: 403 }));
    await expect(loadSnapshot("http://127.0.0.1:7000")).rejects.toBeInstanceOf(ApiError);
  });

  it("posts Slack collaborator changes through the local API", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      expect(String(input)).toContain("/api/slack/trusted-users");
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe(JSON.stringify({ user_id: "UTEAM1" }));
      return new Response(
        JSON.stringify({
          operator_user_id: "UOPERATOR",
          users: [{ user_id: "UTEAM1", sources: ["local"], can_remove: true }],
          state_path: "/tmp/state/slack-trust/trusted-users.json",
          added: true,
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetch);

    const result = await addTrustedSlackUser("http://127.0.0.1:7000", "UTEAM1");

    expect(result.added).toBe(true);
    expect(result.users[0].user_id).toBe("UTEAM1");
  });

  it("posts memory candidate promotion through the local API", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      expect(String(input)).toContain("/api/memory/candidates/mem:1/promote");
      expect(init?.method).toBe("POST");
      return new Response(
        JSON.stringify({
          candidate_id: "mem:1",
          lesson_id: "lesson-1",
          status: "validated",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetch);

    const result = await promoteMemoryCandidate("http://127.0.0.1:7000", "mem:1");

    expect(result.lesson_id).toBe("lesson-1");
  });

  it("posts conversational control turns through the local API", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      expect(String(input)).toContain("/api/conversation/control");
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe(JSON.stringify({ text: "run batman" }));
      return new Response(
        JSON.stringify({
          handled: true,
          action: "run",
          text: "*Triggered one run* `batman`.",
          detail: "",
          actor_user_id: "ULOCALCLIENT",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetch);

    const result = await conversationControl("http://127.0.0.1:7000", {
      text: "run batman",
    });

    expect(result.handled).toBe(true);
    expect(result.action).toBe("run");
  });

  it("posts a plan go/no-go decision through the local API", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      expect(String(input)).toContain("/api/plans/13-plan/decision");
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe(JSON.stringify({ decision: "approve" }));
      return new Response(
        JSON.stringify({
          plan_id: "13-plan",
          issue_number: 13,
          decision: "approve",
          status: "approved",
          marker_path: "/tmp/state/../batman/approvals/13.approved",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetch);

    const result = await decidePlan("http://127.0.0.1:7000", "13-plan", "approve");

    expect(result.decision).toBe("approve");
    expect(result.issue_number).toBe(13);
    expect(result.status).toBe("approved");
  });

  it("includes the operator reason on a decline decision when provided", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      expect(String(input)).toContain("/api/plans/21-plan/decision");
      expect(init?.body).toBe(
        JSON.stringify({ decision: "decline", reason: "scope too broad" }),
      );
      return new Response(
        JSON.stringify({
          plan_id: "21-plan",
          issue_number: 21,
          decision: "decline",
          status: "declined",
          marker_path: "/tmp/state/../batman/approvals/21.rejected",
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetch);

    const result = await decidePlan(
      "http://127.0.0.1:7000",
      "21-plan",
      "decline",
      "scope too broad",
    );

    expect(result.status).toBe("declined");
  });
});

describe("error humanization", () => {
  it("maps browser 403s to desktop-token guidance and keeps the raw text in details", async () => {
    vi.stubGlobal("fetch", stubFetch({ status: 403 }));
    try {
      await loadSnapshot("http://127.0.0.1:7000");
      throw new Error("expected loadSnapshot to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).message).toMatch(/desktop app/i);
      expect((err as ApiError).message).toMatch(/launch token/i);
      expect((err as ApiError).message).not.toMatch(/^alfred serve returned 403/);
      expect(errorDetail(err)).toMatch(/403/);
    }
  });

  it("maps a connection refusal to plain-language guidance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    try {
      await loadSnapshot("http://127.0.0.1:7000");
      throw new Error("expected loadSnapshot to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).message).toMatch(/could not reach alfred serve/i);
      expect(errorDetail(err)).toMatch(/failed to fetch/i);
    }
  });

  it("uses server-provided 400 guidance as the visible message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(
          JSON.stringify({ error: "describe the work in the text field before drafting" }),
          { status: 400 },
        );
      }),
    );
    try {
      await composeDraft("http://127.0.0.1:7000", { text: "" });
      throw new Error("expected composeDraft to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).message).toBe(
        "describe the work in the text field before drafting",
      );
      expect(errorDetail(err)).toMatch(/400/);
    }
  });
});

// Build a Response whose body streams an SSE byte sequence in chunks, so the
// reader exercises the incremental parse (a frame split across chunks must
// still parse). jsdom has ReadableStream + TextEncoder.
function sseResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

const CONVERSE_REQUEST: ConverseRequest = {
  messages: [{ role: "user", content: "Add CSV export" }],
};

describe("streamComposeConverse", () => {
  it("attaches the launch token for native streams after the local URL check", async () => {
    const result = {
      draft_id: "compose-1",
      saved_path: "/s/compose-1.json",
      reply: "Which repo?",
      readiness: { score: 30, ready: false, missing: ["repo"] },
      done: false,
      draft: { title: "Export", repos: [] },
    };
    window.__TAURI_INTERNALS__ = {};
    invokeMock.mockResolvedValueOnce("local-token");
    const fetchMock = vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string>;
      expect(headers["X-Alfred-Token"]).toBe("local-token");
      return sseResponse([`event: result\ndata: ${JSON.stringify(result)}\n\n`]);
    });
    vi.stubGlobal("fetch", fetchMock);

    const reply = await streamComposeConverse(
      "http://127.0.0.1:7000",
      CONVERSE_REQUEST,
      () => {},
    );

    expect(reply.draft_id).toBe("compose-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/alfred-api/api/compose/converse/stream",
      expect.objectContaining({ method: "POST" }),
    );
    expect(invokeMock).toHaveBeenCalledWith("alfred_server_token");
  });

  it("rejects non-local native streams before reading the launch token", async () => {
    window.__TAURI_INTERNALS__ = {};

    await expect(
      streamComposeConverse("https://example.com", CONVERSE_REQUEST, () => {}),
    ).rejects.toMatchObject({
      message: "Streaming is only available against a local Alfred runtime.",
      detail: expect.stringContaining("localhost"),
    });

    expect(invokeMock).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("renders tokens in order and reconciles to the result event", async () => {
    const result = {
      draft_id: "compose-1",
      saved_path: "/s/compose-1.json",
      reply: "Which repo?",
      readiness: { score: 30, ready: false, missing: ["repo"] },
      done: false,
      draft: { title: "Export", repos: [] },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          'event: open\ndata: {}\n\n',
          'event: token\ndata: {"text": "Which "}\n\n',
          // A frame deliberately split across two network chunks.
          'event: token\nda',
          'ta: {"text": "repo?"}\n\n',
          `event: result\ndata: ${JSON.stringify(result)}\n\n`,
        ]),
      ),
    );
    const tokens: string[] = [];
    const reply = await streamComposeConverse(
      "http://127.0.0.1:7000",
      CONVERSE_REQUEST,
      (t) => tokens.push(t),
    );
    expect(tokens.join("")).toBe("Which repo?");
    expect(reply.reply).toBe("Which repo?");
    expect(reply.draft_id).toBe("compose-1");
  });

  it("rejects with the live-session detail when the stream emits an error event", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          'event: open\ndata: {}\n\n',
          'event: error\ndata: {"detail": "live_session_unavailable"}\n\n',
        ]),
      ),
    );
    await expect(
      streamComposeConverse("http://127.0.0.1:7000", CONVERSE_REQUEST, () => {}),
    ).rejects.toMatchObject({ detail: "live_session_unavailable" });
  });

  it("rejects on a non-200 so the caller can fall back to buffered converse", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"error": "live_session_unavailable"}', { status: 503 })),
    );
    await expect(
      streamComposeConverse("http://127.0.0.1:7000", CONVERSE_REQUEST, () => {}),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("rejects when the stream closes with no result event", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse(['event: open\ndata: {}\n\n'])),
    );
    await expect(
      streamComposeConverse("http://127.0.0.1:7000", CONVERSE_REQUEST, () => {}),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("streamFiringTail", () => {
  // A minimal EventSource stub: tests drive events by reaching into the
  // instance the api created. Mirrors the browser API surface the code uses.
  class FakeEventSource {
    static instances: FakeEventSource[] = [];
    url: string;
    listeners = new Map<string, (event: MessageEvent) => void>();
    onerror: (() => void) | null = null;
    closed = false;
    constructor(url: string) {
      this.url = url;
      FakeEventSource.instances.push(this);
    }
    addEventListener(name: string, fn: (event: MessageEvent) => void) {
      this.listeners.set(name, fn);
    }
    emit(name: string, data: unknown) {
      this.listeners.get(name)?.({ data: JSON.stringify(data) } as MessageEvent);
    }
    close() {
      this.closed = true;
    }
  }

  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  });

  it("appends streamed lines and closes on the done event", () => {
    const lines: string[] = [];
    let doneReason: string | null = null;
    const dispose = streamFiringTail("http://127.0.0.1:7000", "fire-1", {
      onLines: (batch) => lines.push(...batch),
      onDone: (reason) => {
        doneReason = reason;
      },
    });
    const source = FakeEventSource.instances[0];
    expect(source.url).toContain("/api/firings/fire-1/tail");
    source.emit("append", { lines: ["alpha", "beta"], offset: 12 });
    source.emit("append", { lines: ["gamma"], offset: 18 });
    source.emit("done", { reason: "complete", offset: 18 });
    expect(lines).toEqual(["alpha", "beta", "gamma"]);
    expect(doneReason).toBe("complete");
    expect(source.closed).toBe(true);
    dispose();
  });

  it("reports a transport error so the caller falls back to its poll", () => {
    let errored = false;
    streamFiringTail("http://127.0.0.1:7000", "fire-2", {
      onLines: () => {},
      onError: () => {
        errored = true;
      },
    });
    const source = FakeEventSource.instances[0];
    source.onerror?.();
    expect(errored).toBe(true);
    expect(source.closed).toBe(true);
  });

  it("degrades to a no-op when EventSource is unavailable", () => {
    vi.stubGlobal("EventSource", undefined);
    let errored = false;
    const dispose = streamFiringTail("http://127.0.0.1:7000", "fire-3", {
      onLines: () => {},
      onError: () => {
        errored = true;
      },
    });
    expect(errored).toBe(true);
    // The disposer is always safe to call even when nothing was opened.
    expect(() => dispose()).not.toThrow();
  });
});
