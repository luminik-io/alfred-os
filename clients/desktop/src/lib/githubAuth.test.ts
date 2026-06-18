import { describe, expect, it, vi } from "vitest";

import { pollGithubAuthStatus } from "./githubAuth";
import type { SetupStatus } from "../types";

function setupStatus(ok: boolean): SetupStatus {
  return {
    github: {
      ok,
      account: ok ? "octocat" : null,
      detail: ok ? "Signed in to GitHub as octocat." : "Not signed in to GitHub.",
    },
    engines: [],
    engine_ready: true,
    repos: { selected: [], count: 0, keys: [] },
    demo: { present: false },
    ready: ok,
  };
}

function clock() {
  let current = 0;
  return {
    now: () => current,
    sleep: vi.fn(async (ms: number) => {
      current += ms;
    }),
  };
}

describe("pollGithubAuthStatus", () => {
  it("keeps polling until setup reports GitHub connected", async () => {
    const testClock = clock();
    const loadStatus = vi
      .fn<() => Promise<SetupStatus>>()
      .mockResolvedValueOnce(setupStatus(false))
      .mockResolvedValueOnce(setupStatus(false))
      .mockResolvedValueOnce(setupStatus(true));

    const result = await pollGithubAuthStatus(loadStatus, {
      pollIntervalMs: 500,
      timeoutMs: 5_000,
      now: testClock.now,
      sleep: testClock.sleep,
    });

    expect(result.state).toBe("success");
    expect(result.status?.github.account).toBe("octocat");
    expect(result.attempts).toBe(3);
    expect(testClock.sleep).toHaveBeenCalledTimes(2);
    expect(result.lastError).toBeNull();
  });

  it("times out with the last observed setup status", async () => {
    const testClock = clock();
    const loadStatus = vi.fn<() => Promise<SetupStatus>>().mockResolvedValue(setupStatus(false));

    const result = await pollGithubAuthStatus(loadStatus, {
      pollIntervalMs: 600,
      timeoutMs: 1_000,
      now: testClock.now,
      sleep: testClock.sleep,
    });

    expect(result.state).toBe("timeout");
    expect(result.status?.github.ok).toBe(false);
    expect(result.elapsedMs).toBe(1_000);
    expect(result.attempts).toBe(2);
  });

  it("keeps polling through transient setup read errors", async () => {
    const testClock = clock();
    const loadStatus = vi
      .fn<() => Promise<SetupStatus>>()
      .mockRejectedValueOnce(new Error("runtime warming up"))
      .mockResolvedValueOnce(setupStatus(true));

    const result = await pollGithubAuthStatus(loadStatus, {
      pollIntervalMs: 250,
      timeoutMs: 2_000,
      now: testClock.now,
      sleep: testClock.sleep,
    });

    expect(result.state).toBe("success");
    expect(result.attempts).toBe(2);
    expect(result.lastError).toBeNull();
  });
});
