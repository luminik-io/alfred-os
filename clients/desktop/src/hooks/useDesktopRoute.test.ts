import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { desktopRouteToSearch, parseDesktopRoute, useDesktopRoute } from "./useDesktopRoute";

function loc(search: string, hash = "") {
  return { search, hash } as Location;
}

describe("desktop route parsing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps legacy and product tab names to the same internal surfaces", () => {
    expect(parseDesktopRoute(loc("?tab=inbox")).tab).toBe("home");
    expect(parseDesktopRoute(loc("?tab=review")).tab).toBe("home");
    expect(parseDesktopRoute(loc("?tab=ask")).tab).toBe("compose");
    expect(parseDesktopRoute(loc("?tab=work")).tab).toBe("pipeline");
    expect(parseDesktopRoute(loc("?tab=pipeline")).tab).toBe("pipeline");
  });

  it("keeps lessons and activity as Agents subtabs", () => {
    expect(parseDesktopRoute(loc("?tab=lessons"))).toMatchObject({
      tab: "fleet",
      fleetTab: "lessons",
    });
    expect(parseDesktopRoute(loc("?tab=agents&subtab=activity"))).toMatchObject({
      tab: "fleet",
      fleetTab: "logs",
    });
    expect(parseDesktopRoute(loc("?tab=fleet&subtab=memory"))).toMatchObject({
      tab: "fleet",
      fleetTab: "lessons",
    });
  });

  it("parses setup mode without leaking that subtab to other surfaces", () => {
    expect(parseDesktopRoute(loc("?tab=setup&subtab=advanced"))).toMatchObject({
      tab: "settings",
      setupMode: "advanced",
    });
    expect(parseDesktopRoute(loc("?tab=work&subtab=advanced"))).toMatchObject({
      tab: "pipeline",
      setupMode: "guided",
    });
  });

  it("writes canonical product-facing search params", () => {
    expect(
      desktopRouteToSearch({ tab: "home", fleetTab: "fleet", setupMode: "guided" }),
    ).toBe("?tab=inbox");
    expect(
      desktopRouteToSearch({ tab: "pipeline", fleetTab: "fleet", setupMode: "guided" }),
    ).toBe("?tab=work");
    expect(
      desktopRouteToSearch({ tab: "fleet", fleetTab: "lessons", setupMode: "guided" }),
    ).toBe("?tab=agents&subtab=lessons");
    expect(
      desktopRouteToSearch({ tab: "settings", fleetTab: "fleet", setupMode: "advanced" }),
    ).toBe("?tab=setup&subtab=advanced");
  });

  it("canonicalizes legacy URLs without dropping unrelated query params", async () => {
    window.history.replaceState(null, "", "/?debug=1&tab=lessons&token=abc");
    const replaceState = vi.spyOn(window.history, "replaceState");

    renderHook(() => useDesktopRoute());

    await waitFor(() => {
      const params = new URLSearchParams(window.location.search);
      expect(params.get("debug")).toBe("1");
      expect(params.get("token")).toBe("abc");
      expect(params.get("tab")).toBe("agents");
      expect(params.get("subtab")).toBe("lessons");
    });
    expect(replaceState).toHaveBeenCalled();
  });

  it("pushes user-initiated tab changes and maps Lessons into Agents", async () => {
    window.history.replaceState(null, "", "/?tab=inbox");
    const pushState = vi.spyOn(window.history, "pushState");
    const replaceState = vi.spyOn(window.history, "replaceState");
    const { result } = renderHook(() => useDesktopRoute());

    act(() => {
      result.current.setTab("lessons");
    });

    await waitFor(() => {
      expect(result.current.tab).toBe("fleet");
      expect(result.current.fleetTab).toBe("lessons");
      expect(new URLSearchParams(window.location.search).get("tab")).toBe("agents");
      expect(new URLSearchParams(window.location.search).get("subtab")).toBe("lessons");
    });
    expect(pushState).toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalledWith(null, "", "/?tab=agents&subtab=lessons");
  });

  it("resyncs from popstate without writing a new history entry", async () => {
    window.history.replaceState(null, "", "/?tab=inbox");
    const pushState = vi.spyOn(window.history, "pushState");
    const replaceState = vi.spyOn(window.history, "replaceState");
    const { result } = renderHook(() => useDesktopRoute());

    window.history.replaceState(null, "", "/?tab=agents&subtab=activity");
    pushState.mockClear();
    replaceState.mockClear();
    act(() => {
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    await waitFor(() => {
      expect(result.current.tab).toBe("fleet");
      expect(result.current.fleetTab).toBe("logs");
      expect(new URLSearchParams(window.location.search).get("tab")).toBe("agents");
      expect(new URLSearchParams(window.location.search).get("subtab")).toBe("activity");
    });
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
