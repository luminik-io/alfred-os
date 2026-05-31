import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, errorDetail, loadSnapshot } from "./api";

// In jsdom (no __TAURI_INTERNALS__) the api layer goes through global fetch, so
// we can drive every endpoint's outcome by stubbing fetch per URL.

const ENDPOINTS = {
  status: "/api/status",
  actions: "/api/actions",
  firings: "/api/firings",
  plans: "/api/plans",
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
    expect(snap.degraded).toBeUndefined();
  });

  it("keeps the dashboard when a non-spine endpoint fails", async () => {
    vi.stubGlobal("fetch", stubFetch({ plans: 500 }));
    const snap = await loadSnapshot("http://127.0.0.1:7000");
    // status/actions/firings still rendered; only plans degraded.
    expect(snap.status.agents).toHaveLength(1);
    expect(snap.firings).toHaveLength(1);
    expect(snap.plans).toEqual([]);
    expect(snap.degraded?.plans).toBeTruthy();
    expect(snap.degraded?.firings).toBeUndefined();
  });

  it("throws when the spine /api/status fails", async () => {
    vi.stubGlobal("fetch", stubFetch({ status: 403 }));
    await expect(loadSnapshot("http://127.0.0.1:7000")).rejects.toBeInstanceOf(ApiError);
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
});
