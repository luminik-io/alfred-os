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

  // Thread: "Stale read still wins". If a save resolves before a slow initial
  // GET, the GET continuation must NOT overwrite the freshly persisted choice.
  it("does not let a slow initial read clobber a save that already won", async () => {
    // Hold the GET open so the save can resolve first.
    let resolveLoad: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme.mockReturnValue(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveLoad = resolve;
      }),
    );
    saveRosterTheme.mockResolvedValue(serverState({ theme: "transformers" }));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    // Operator switches before the GET resolves; the save wins and hydrates.
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalled();
    });
    expect(result.current.rosterTheme).toBe("transformers");

    // The stale server snapshot now resolves; it must be ignored.
    await act(async () => {
      resolveLoad(serverState({ theme: "justice-league" }));
    });
    expect(result.current.rosterTheme).toBe("transformers");
  });

  // Thread: "Preset switch clears cast". Switching to a preset must omit the
  // custom maps so the server retains the authored custom cast; sending empty
  // objects would wipe it.
  it("omits the custom maps when switching to a preset", async () => {
    loadRosterTheme.mockResolvedValue(serverState());
    saveRosterTheme.mockResolvedValue(serverState({ theme: "transformers" }));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledWith("http://127.0.0.1:7010", {
        theme: "transformers",
      });
    });
  });

  // A custom save still carries the cast so the operator's names/roles persist.
  it("sends the custom maps when saving the custom cast", async () => {
    loadRosterTheme.mockResolvedValue(serverState());
    saveRosterTheme.mockResolvedValue(
      serverState({ theme: "custom", custom_names: { batman: "Sherlock" } }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setCustomNames({ names: { batman: "Sherlock" }, roles: {} });
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledWith("http://127.0.0.1:7010", {
        theme: "custom",
        custom_names: { batman: "Sherlock" },
        custom_roles: {},
      });
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

  // Thread: "Out-of-order saves persist stale theme". Two fast switches must be
  // serialized: only the first POST goes out immediately, the newest is
  // coalesced and sent once the socket frees, so the server's final write is
  // the operator's last choice, in order.
  it("serializes rapid switches so only the latest reaches the server", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));

    // First save is held open so the second switch lands while it is in flight.
    let resolveFirst: (value: RosterThemeResponse) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveFirst = resolve;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    // Only the first POST is out; the newest choice is queued, not sent yet.
    expect(saveRosterTheme).toHaveBeenCalledTimes(1);
    expect(saveRosterTheme).toHaveBeenNthCalledWith(1, "http://127.0.0.1:7010", {
      theme: "justice-league",
    });

    saveRosterTheme.mockResolvedValueOnce(serverState({ theme: "transformers" }));
    await act(async () => {
      resolveFirst(serverState({ theme: "justice-league" }));
    });

    // The queued latest switch now goes out, in order, exactly once.
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledTimes(2);
    });
    expect(saveRosterTheme).toHaveBeenNthCalledWith(2, "http://127.0.0.1:7010", {
      theme: "transformers",
    });
    await waitFor(() => {
      expect(result.current.saveError).toBeNull();
    });
    expect(result.current.rosterTheme).toBe("transformers");
  });

  // Thread: "Hydration Ignores Runtime". Hydration is tied to the connected
  // url, not a global flag. Connecting to a second runtime must re-read it
  // rather than keep showing the first runtime's roster.
  it("re-hydrates when the desktop connects to a different runtime", async () => {
    loadRosterTheme.mockImplementation((baseUrl: string) =>
      Promise.resolve(
        serverState({
          theme: baseUrl.endsWith("7010") ? "transformers" : "justice-league",
        }),
      ),
    );

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("transformers");
    });

    // Point at a second runtime: its roster must be read, not skipped.
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledWith("http://127.0.0.1:7020");
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
  });

  // Thread: "Queued Save Uses Old Runtime". A change made after switching
  // runtimes must post to the runtime it was made against, not the one a
  // still-in-flight earlier save targeted.
  it("posts a queued change to the runtime it was made against", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));

    // Save against runtime A is held open.
    let resolveA: (value: RosterThemeResponse) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveA = resolve;
      }),
    );

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    expect(saveRosterTheme).toHaveBeenNthCalledWith(1, "http://127.0.0.1:7010", {
      theme: "justice-league",
    });

    // Switch to runtime B, then make another change while A's save is in flight.
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    saveRosterTheme.mockResolvedValueOnce(serverState({ theme: "transformers" }));
    await act(async () => {
      resolveA(serverState({ theme: "justice-league" }));
    });

    // The queued change must reach runtime B, not A.
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledTimes(2);
    });
    expect(saveRosterTheme).toHaveBeenNthCalledWith(2, "http://127.0.0.1:7020", {
      theme: "transformers",
    });
  });

  // Thread: "Stale Hydration Overwrites Edit". A change made on a runtime
  // whose hydration GET is still in flight must not be reverted when that GET
  // resolves: the operator's edit owns the runtime state immediately.
  it("does not let an in-flight hydration GET revert a change made on that runtime", async () => {
    // Runtime B's hydration GET is held open so it can resolve AFTER the edit.
    let resolveLoadB: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme.mockImplementation((baseUrl: string) => {
      if (baseUrl.endsWith("7020")) {
        return new Promise<RosterThemeResponse>((resolve) => {
          resolveLoadB = resolve;
        });
      }
      return new Promise<RosterThemeResponse>(() => {});
    });
    // Runtime A's save is held open so inFlight is true when the edit lands.
    saveRosterTheme.mockReturnValueOnce(new Promise<RosterThemeResponse>(() => {}));

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    // Edit on runtime A: its save is now in flight.
    act(() => {
      result.current.setRosterTheme("justice-league");
    });

    // Switch to runtime B (starts B's hydration GET), then edit on B while A's
    // save is still in flight (so the B edit is queued, not yet saved).
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    expect(result.current.rosterTheme).toBe("transformers");

    // B's hydration GET now resolves with the old server snapshot. It must be
    // ignored: the operator's edit owns runtime B's state.
    await act(async () => {
      resolveLoadB(serverState({ theme: "batman" }));
    });
    expect(result.current.rosterTheme).toBe("transformers");
  });

  // A superseded save that fails must stay quiet: the newer save owns the
  // reported outcome, so a stale rejection cannot raise a false error.
  it("keeps a superseded save failure from clobbering the latest success", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));

    let rejectFirst: (reason?: unknown) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((_resolve, reject) => {
        rejectFirst = reject;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    // The newest save (queued) will succeed once it goes out.
    saveRosterTheme.mockResolvedValueOnce(serverState({ theme: "transformers" }));

    // The first, now superseded, save fails. Its rejection must not surface.
    await act(async () => {
      rejectFirst(new Error("alfred serve returned 403"));
    });

    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.saveError).toBeNull();
    });
    expect(result.current.rosterTheme).toBe("transformers");
  });
});
