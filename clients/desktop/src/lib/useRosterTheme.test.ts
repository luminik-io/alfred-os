import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RosterThemeResponse } from "../api";
import { useRosterTheme } from "./useRosterTheme";

// The hook talks to the runtime through these two api helpers; mock them so the
// test can drive the server-read and server-save outcomes directly.
const loadRosterTheme = vi.fn<(baseUrl: string) => Promise<RosterThemeResponse>>();
const saveRosterTheme =
  vi.fn<(baseUrl: string, body: unknown) => Promise<RosterThemeResponse>>();

vi.mock("../api", () => ({
  loadRosterTheme: (baseUrl: string) => loadRosterTheme(baseUrl),
  saveRosterTheme: (baseUrl: string, body: unknown) => saveRosterTheme(baseUrl, body),
}));

function serverState(overrides: Partial<RosterThemeResponse> = {}): RosterThemeResponse {
  return {
    theme: "batman",
    custom_names: {},
    custom_roles: {},
    updated_at: null,
    state_path: null,
    ...overrides,
  };
}

describe("useRosterTheme", () => {
  beforeEach(() => {
    window.localStorage.clear();
    loadRosterTheme.mockReset();
    saveRosterTheme.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // Thread: "Offline Change Blocks Hydration". A change made while baseUrl is
  // absent must not mark the hook hydrated, so the later server read still runs
  // and the desktop converges on the server (and Slack) cast.
  it("still hydrates from the server after an offline change", async () => {
    loadRosterTheme.mockResolvedValue(serverState({ theme: "justice-league" }));

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: undefined } as { baseUrl?: string } },
    );

    // Offline switch: kept locally, flagged as local-only, no server call.
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    expect(result.current.rosterTheme).toBe("transformers");
    expect(result.current.saveError).not.toBeNull();
    expect(saveRosterTheme).not.toHaveBeenCalled();

    // The runtime connects: the hook must read the server's persisted cast
    // rather than skip the read because of the offline change.
    rerender({ baseUrl: "http://127.0.0.1:7010" });
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledWith("http://127.0.0.1:7010");
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
  });

  // Thread: "Rejected Saves Look Successful". A 403 (or any rejection) from the
  // save must surface through saveError instead of being silently swallowed.
  it("surfaces a save failure rather than looking successful", async () => {
    saveRosterTheme.mockRejectedValue(new Error("alfred serve returned 403"));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));
    // Drain the initial hydration read so it does not interfere.
    loadRosterTheme.mockResolvedValue(serverState());

    act(() => {
      result.current.setRosterTheme("transformers");
    });

    // The local picker still reflects the choice...
    expect(result.current.rosterTheme).toBe("transformers");
    // ...but the failed server save is reported, not hidden.
    await waitFor(() => {
      expect(result.current.saveError).toContain("403");
    });
  });

  // A successful save clears any prior error and is treated as the source of
  // truth (no stale "local-only" warning lingers).
  it("clears the save error after a successful save", async () => {
    loadRosterTheme.mockResolvedValue(serverState());
    saveRosterTheme.mockResolvedValue(serverState({ theme: "transformers" }));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(result.current.saveError).toBeNull();
    });
  });
});
