import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  addTrustedSlackUser,
  composeDraft,
  errorDetail,
  loadSnapshot,
  promoteMemoryCandidate,
} from "./api";

// In jsdom (no __TAURI_INTERNALS__) the api layer goes through global fetch, so
// we can drive every endpoint's outcome by stubbing fetch per URL.

const ENDPOINTS = {
  status: "/api/status",
  actions: "/api/actions",
  memoryCandidates: "/api/memory/candidates",
  firings: "/api/firings",
  plans: "/api/plans",
  trustedSlack: "/api/slack/trusted-users",
} as const;

type EndpointKey = keyof typeof ENDPOINTS;

function jsonFor(path: string): unknown {
  if (path.includes(ENDPOINTS.status)) {
    return { agents: [{ codename: "lucius" }], total_today: 2, reliability: { status: "ok" } };
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
  vi.unstubAllGlobals();
});

describe("loadSnapshot degradation", () => {
  it("renders every section when all endpoints resolve", async () => {
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.firings).toHaveLength(1);
    expect(snap.plans).toHaveLength(1);
    expect(snap.memoryCandidates.rows).toHaveLength(1);
    expect(snap.trustedSlack?.users[0].user_id).toBe("UOPERATOR");
    expect(snap.degraded).toBeUndefined();
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
});

describe("error humanization", () => {
  it("maps 403 to auth-mismatch guidance and keeps the raw text in details", async () => {
    vi.stubGlobal("fetch", stubFetch({ status: 403 }));
    try {
      await loadSnapshot("http://127.0.0.1:7000");
      throw new Error("expected loadSnapshot to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).message).toMatch(/auth token mismatch/i);
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
