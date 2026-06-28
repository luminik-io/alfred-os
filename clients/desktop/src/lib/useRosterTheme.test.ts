import { act, render, renderHook, waitFor } from "@testing-library/react";
import { createElement, useState } from "react";
import { flushSync } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RosterThemeResponse } from "../api";
import { type UseRosterTheme, useRosterTheme } from "./useRosterTheme";

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

  it("reports hydration while the connected runtime roster is loading", async () => {
    let resolveLoad: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme.mockReturnValue(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveLoad = resolve;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    expect(result.current.hydrating).toBe(true);

    await act(async () => {
      resolveLoad(serverState({ theme: "transformers" }));
    });

    await waitFor(() => {
      expect(result.current.hydrating).toBe(false);
    });
    expect(result.current.rosterTheme).toBe("transformers");
  });

  it("surfaces roster load failure until hydration is retried", async () => {
    loadRosterTheme
      .mockRejectedValueOnce(new Error("runtime returned 500"))
      .mockResolvedValueOnce(serverState({ theme: "justice-league" }));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    await waitFor(() => {
      expect(result.current.hydrationError).toContain("runtime returned 500");
    });
    expect(result.current.hydrating).toBe(false);
    expect(result.current.rosterTheme).toBe("batman");

    act(() => {
      result.current.retryHydration();
    });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
    expect(result.current.hydrationError).toBeNull();
  });

  it("clears a failed runtime's hydration error when returning to a hydrated runtime", async () => {
    loadRosterTheme
      .mockResolvedValueOnce(serverState({ theme: "transformers" }))
      .mockRejectedValueOnce(new Error("runtime B returned 500"));

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } },
    );

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("transformers");
    });
    expect(result.current.hydrationError).toBeNull();

    rerender({ baseUrl: "http://127.0.0.1:7011" });
    await waitFor(() => {
      expect(result.current.hydrationError).toContain("runtime B returned 500");
    });

    rerender({ baseUrl: "http://127.0.0.1:7010" });
    await waitFor(() => {
      expect(result.current.hydrationError).toBeNull();
    });
    expect(result.current.rosterTheme).toBe("transformers");
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

  it("does not hydrate or save while the runtime URL is disconnected", async () => {
    loadRosterTheme.mockResolvedValue(serverState({ theme: "justice-league" }));
    saveRosterTheme.mockResolvedValue(serverState({ theme: "transformers" }));

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: false } },
    );

    expect(result.current.hydrating).toBe(false);
    expect(loadRosterTheme).not.toHaveBeenCalled();

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    expect(result.current.saveError).toContain("Not connected");
    expect(saveRosterTheme).not.toHaveBeenCalled();

    rerender({ connected: true });
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledWith("http://127.0.0.1:7010");
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
  });

  it("retries hydration on a same-url reconnect after a load failure", async () => {
    loadRosterTheme
      .mockRejectedValueOnce(new Error("runtime returned 500"))
      .mockResolvedValueOnce(serverState({ theme: "transformers" }));

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: true } },
    );

    await waitFor(() => {
      expect(result.current.hydrationError).toContain("runtime returned 500");
    });

    rerender({ connected: false });
    await waitFor(() => {
      expect(result.current.hydrationError).toBeNull();
    });
    rerender({ connected: true });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("transformers");
    });
  });

  // Thread: "Rejected Saves Look Successful". A 403 (or any rejection) from the
  // save must surface through saveError instead of being silently swallowed.
  it("surfaces a save failure rather than looking successful", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    saveRosterTheme.mockRejectedValue(new Error("alfred serve returned 403"));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

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

  it("ignores a failed initial read after a save already won", async () => {
    let rejectLoad: (reason?: unknown) => void = () => {};
    loadRosterTheme.mockReturnValue(
      new Promise<RosterThemeResponse>((_resolve, reject) => {
        rejectLoad = reject;
      }),
    );
    saveRosterTheme.mockResolvedValue(serverState({ theme: "transformers" }));

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalled();
    });

    await act(async () => {
      rejectLoad(new Error("old GET returned 500"));
    });

    expect(result.current.rosterTheme).toBe("transformers");
    expect(result.current.hydrationError).toBeNull();
    expect(result.current.saveError).toBeNull();
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

  it("clears a same-runtime save error when a newer save starts", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    let rejectSecond: (reason?: unknown) => void = () => {};
    saveRosterTheme
      .mockRejectedValueOnce(new Error("alfred serve returned 403"))
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((_resolve, reject) => {
          rejectSecond = reject;
        }),
      );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    await waitFor(() => {
      expect(result.current.saveError).toContain("403");
    });

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    expect(result.current.saveError).toBeNull();

    await act(async () => {
      rejectSecond(new Error("alfred serve returned 500"));
    });
    await waitFor(() => {
      expect(result.current.saveError).toContain("500");
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

    let firstSave: Promise<boolean> | undefined;
    let secondSave: Promise<boolean> | undefined;
    act(() => {
      firstSave = result.current.setRosterTheme("justice-league");
    });
    act(() => {
      secondSave = result.current.setRosterTheme("transformers");
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
    await expect(firstSave!).resolves.toBe(false);
    await expect(secondSave!).resolves.toBe(true);
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

  it("does not acknowledge a save that resolved after switching runtimes", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
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

    let saveResult: Promise<boolean> | null = null;
    act(() => {
      saveResult = result.current.setRosterTheme("justice-league");
    });
    rerender({ baseUrl: "http://127.0.0.1:7020" });

    await act(async () => {
      resolveA(serverState({ theme: "justice-league" }));
    });

    if (saveResult === null) throw new Error("setRosterTheme did not return a save promise");
    await expect(saveResult).resolves.toBe(false);
    expect(result.current.saveError).toBeNull();
  });

  it("does not surface a save failure from a runtime after switching away", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    let rejectA: (reason?: unknown) => void = () => {};
    saveRosterTheme
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((_resolve, reject) => {
          rejectA = reject;
        }),
      )
      // Runtime B's drained save is held open; A's error should still stay quiet
      // because the current UI has moved to B.
      .mockReturnValueOnce(new Promise<RosterThemeResponse>(() => {}));

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    // Switch to runtime B and edit it: this bumps the global save seq for B.
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    // A's save now fails. The current UI is already on runtime B, so A's stale
    // failure must not keep Fleet incomplete on B.
    await act(async () => {
      rejectA(new Error("alfred serve returned 500"));
    });
    expect(result.current.saveError).toBeNull();
  });

  it("restores a runtime save failure when returning to that runtime", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    let rejectA: (reason?: unknown) => void = () => {};
    saveRosterTheme
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((_resolve, reject) => {
          rejectA = reject;
        }),
      )
      .mockReturnValueOnce(new Promise<RosterThemeResponse>(() => {}));

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    await act(async () => {
      rejectA(new Error("alfred serve returned 500"));
    });
    expect(result.current.saveError).toBeNull();

    rerender({ baseUrl: "http://127.0.0.1:7010" });
    await waitFor(() => {
      expect(result.current.saveError).toContain("500");
    });
  });

  it("a drained successful save on the current runtime clears stale save errors", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    let rejectA: (reason?: unknown) => void = () => {};
    let resolveB: (value: RosterThemeResponse) => void = () => {};
    saveRosterTheme
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((_resolve, reject) => {
          rejectA = reject;
        }),
      )
      // Runtime B's drained save SUCCEEDS this time (the real drained-success path).
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((resolve) => {
          resolveB = resolve;
        }),
      );

    const { result, rerender } = renderHook(
      ({ baseUrl }: { baseUrl?: string }) => useRosterTheme(baseUrl),
      { initialProps: { baseUrl: "http://127.0.0.1:7010" } as { baseUrl?: string } },
    );

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    // A fails after the UI has moved to B. It should stay quiet and drain B's
    // queued save.
    await act(async () => {
      rejectA(new Error("alfred serve returned 500"));
    });
    expect(result.current.saveError).toBeNull();

    // B's drained save now succeeds. The visible state remains clean for B.
    await act(async () => {
      resolveB(serverState({ theme: "transformers" }));
    });
    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledTimes(2);
    });
    expect(result.current.saveError).toBeNull();
  });

  it("preserves an offline local-only warning when an older save resolves", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));
    let resolveSave: (value: RosterThemeResponse) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveSave = resolve;
      }),
    );

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: true } },
    );

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    rerender({ connected: false });
    act(() => {
      result.current.setRosterTheme("transformers");
    });

    expect(result.current.saveError).toContain("Not connected");

    await act(async () => {
      resolveSave(serverState({ theme: "justice-league" }));
    });

    expect(result.current.saveError).toContain("Not connected");
  });

  it("supersedes an in-flight same-runtime save when edited offline", async () => {
    let resolveReconnectLoad: (value: RosterThemeResponse) => void = () => {};
    let resolveReconcileLoad: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme
      .mockReturnValueOnce(new Promise<RosterThemeResponse>(() => {}))
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((resolve) => {
          resolveReconnectLoad = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((resolve) => {
          resolveReconcileLoad = resolve;
        }),
      );
    let resolveOldSave: (value: RosterThemeResponse) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveOldSave = resolve;
      }),
    );

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: true } },
    );

    let oldSaveResult: Promise<boolean> | null = null;
    act(() => {
      oldSaveResult = result.current.setRosterTheme("justice-league");
    });
    rerender({ connected: false });
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    rerender({ connected: true });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      resolveOldSave(serverState({ theme: "justice-league" }));
    });
    if (oldSaveResult === null) throw new Error("setRosterTheme did not return a save promise");
    await expect(oldSaveResult).resolves.toBe(false);
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(3);
    });
    await act(async () => {
      resolveReconnectLoad(serverState({ theme: "batman" }));
    });
    await act(async () => {
      resolveReconcileLoad(serverState({ theme: "batman" }));
    });

    expect(result.current.rosterTheme).toBe("batman");
  });

  it("does not accept a same-url save after disconnect and reconnect", async () => {
    let resolveSave: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockResolvedValueOnce(serverState({ theme: "transformers" }));
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveSave = resolve;
      }),
    );

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: true } },
    );

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    let saveResult: Promise<boolean> | null = null;
    act(() => {
      saveResult = result.current.setRosterTheme("transformers");
    });
    rerender({ connected: false });
    rerender({ connected: true });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    await act(async () => {
      resolveSave(serverState({ theme: "transformers" }));
    });

    if (saveResult === null) throw new Error("setRosterTheme did not return a save promise");
    await expect(saveResult).resolves.toBe(false);
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(3);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("transformers");
    });
  });

  it("re-hydrates after a failed same-url save crosses a fast reconnect", async () => {
    let rejectSave: (reason?: unknown) => void = () => {};
    loadRosterTheme
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockReturnValueOnce(new Promise<RosterThemeResponse>(() => {}))
      .mockResolvedValueOnce(serverState({ theme: "batman" }));
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((_resolve, reject) => {
        rejectSave = reject;
      }),
    );

    const current: { value: UseRosterTheme | null } = { value: null };
    let setConnected: (next: boolean) => void = () => {};
    function Harness() {
      const [connected, setConnectedState] = useState(true);
      setConnected = setConnectedState;
      current.value = useRosterTheme("http://127.0.0.1:7010", connected);
      return null;
    }

    render(createElement(Harness));

    await waitFor(() => {
      expect(current.value?.rosterTheme).toBe("batman");
    });

    let saveResult: Promise<boolean> | null = null;
    act(() => {
      saveResult = current.value?.setRosterTheme("transformers") ?? null;
    });
    expect(current.value?.rosterTheme).toBe("transformers");

    // Render both connection generations before passive effects run, matching
    // a fast same-url reconnect while the optimistic hydration marker remains.
    act(() => {
      flushSync(() => {
        setConnected(false);
      });
      flushSync(() => {
        setConnected(true);
      });
    });

    await act(async () => {
      rejectSave(new Error("alfred serve returned 503"));
    });

    if (saveResult === null) throw new Error("setRosterTheme did not return a save promise");
    await expect(saveResult).resolves.toBe(false);
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(3);
    });
    await waitFor(() => {
      expect(current.value?.rosterTheme).toBe("batman");
    });
    expect(current.value?.saveError).toBeNull();
  });

  it("re-hydrates after a superseded same-runtime POST lands after reconnect", async () => {
    let resolveOldSave: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockResolvedValueOnce(serverState({ theme: "justice-league" }));
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveOldSave = resolve;
      }),
    );

    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) =>
        useRosterTheme("http://127.0.0.1:7010", connected),
      { initialProps: { connected: true } },
    );

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    let oldSaveResult: Promise<boolean> | null = null;
    act(() => {
      oldSaveResult = result.current.setRosterTheme("justice-league");
    });
    rerender({ connected: false });
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    rerender({ connected: true });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    await act(async () => {
      resolveOldSave(serverState({ theme: "justice-league" }));
    });

    if (oldSaveResult === null) throw new Error("setRosterTheme did not return a save promise");
    await expect(oldSaveResult).resolves.toBe(false);
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(3);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
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

  // Thread: "Failed Saves Stay Hydrated". The optimistic hydration marker set
  // on edit must be cleared when the save fails, so an in-flight GET for that
  // runtime reconciles to the server instead of trusting the unsaved value.
  it("reconciles to the server when a save fails and a hydration GET is pending", async () => {
    // The hydration GET is held open so it can resolve AFTER the save fails.
    let resolveLoad: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme.mockReturnValue(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveLoad = resolve;
      }),
    );
    let rejectSave: (reason?: unknown) => void = () => {};
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((_resolve, reject) => {
        rejectSave = reject;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    expect(result.current.rosterTheme).toBe("transformers");

    // The save fails: the optimistic hydration marker must be cleared.
    await act(async () => {
      rejectSave(new Error("alfred serve returned 503"));
    });

    // The still-pending GET now resolves with the server's real value. Because
    // the failed save cleared hydration, it reconciles rather than being bailed.
    await act(async () => {
      resolveLoad(serverState({ theme: "justice-league" }));
    });
    expect(result.current.rosterTheme).toBe("justice-league");
    expect(result.current.saveError).toBeNull();
  });

  // Thread: "Hydration still skips". If the initial server read resolves while
  // an optimistic save has marked the runtime hydrated, that read bails. A
  // later save failure must start a replacement read; otherwise the hook keeps
  // showing an unsaved local cast forever.
  it("retries server hydration when a failed save already skipped the initial read", async () => {
    let resolveInitialLoad: (value: RosterThemeResponse) => void = () => {};
    let rejectSave: (reason?: unknown) => void = () => {};
    loadRosterTheme
      .mockReturnValueOnce(
        new Promise<RosterThemeResponse>((resolve) => {
          resolveInitialLoad = resolve;
        }),
      )
      .mockResolvedValueOnce(serverState({ theme: "justice-league" }));
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((_resolve, reject) => {
        rejectSave = reject;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    act(() => {
      result.current.setRosterTheme("transformers");
    });
    expect(result.current.rosterTheme).toBe("transformers");

    await act(async () => {
      resolveInitialLoad(serverState({ theme: "batman" }));
    });
    expect(result.current.rosterTheme).toBe("transformers");

    await act(async () => {
      rejectSave(new Error("alfred serve returned 503"));
    });

    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
    expect(result.current.saveError).toBeNull();
  });

  it("restores the saved choice when hydration retries while a save is pending", async () => {
    let resolveSave: (value: RosterThemeResponse) => void = () => {};
    loadRosterTheme
      .mockResolvedValueOnce(serverState({ theme: "batman" }))
      .mockResolvedValueOnce(serverState({ theme: "batman" }));
    saveRosterTheme.mockReturnValueOnce(
      new Promise<RosterThemeResponse>((resolve) => {
        resolveSave = resolve;
      }),
    );

    const { result } = renderHook(() => useRosterTheme("http://127.0.0.1:7010"));

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    expect(result.current.rosterTheme).toBe("justice-league");

    act(() => {
      result.current.retryHydration();
    });
    await waitFor(() => {
      expect(loadRosterTheme).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("batman");
    });

    await act(async () => {
      resolveSave(serverState({ theme: "justice-league" }));
    });

    await waitFor(() => {
      expect(result.current.rosterTheme).toBe("justice-league");
    });
    expect(result.current.saveError).toBeNull();
  });

  // Thread: "Queued Runtime Edit Drops". A queued edit for one runtime must not
  // be discarded when an edit for a second runtime is queued behind it: the
  // pending queue is keyed per runtime, so both saves eventually go out.
  it("keeps queued edits for distinct runtimes from overwriting each other", async () => {
    loadRosterTheme.mockReturnValue(new Promise<RosterThemeResponse>(() => {}));

    // Runtime A's save is held open so B's and C's edits queue behind it.
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

    // Edit A (in flight), then queue an edit on B and an edit on C.
    act(() => {
      result.current.setRosterTheme("justice-league");
    });
    rerender({ baseUrl: "http://127.0.0.1:7020" });
    act(() => {
      result.current.setRosterTheme("transformers");
    });
    rerender({ baseUrl: "http://127.0.0.1:7030" });
    act(() => {
      result.current.setRosterTheme("batman");
    });

    // Drain: A resolves, then B and C each go out to their own runtime.
    saveRosterTheme.mockResolvedValue(serverState());
    await act(async () => {
      resolveA(serverState({ theme: "justice-league" }));
    });

    await waitFor(() => {
      expect(saveRosterTheme).toHaveBeenCalledTimes(3);
    });
    const targets = saveRosterTheme.mock.calls.map((call) => call[0]);
    expect(targets).toContain("http://127.0.0.1:7020");
    expect(targets).toContain("http://127.0.0.1:7030");
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
