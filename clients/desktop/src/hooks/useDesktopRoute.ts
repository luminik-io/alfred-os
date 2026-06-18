import { useCallback, useEffect, useRef, useState } from "react";

import type { OperatorKey, SetupMode, TabKey } from "../lib/uiTypes";

export type DesktopRoute = {
  tab: TabKey;
  fleetTab: OperatorKey;
  setupMode: SetupMode;
};

const DEFAULT_ROUTE: DesktopRoute = {
  tab: "home",
  fleetTab: "fleet",
  setupMode: "guided",
};

const TAB_ALIASES: Record<string, DesktopRoute> = {
  activity: { ...DEFAULT_ROUTE, tab: "fleet", fleetTab: "logs" },
  agents: { ...DEFAULT_ROUTE, tab: "fleet" },
  ask: { ...DEFAULT_ROUTE, tab: "compose" },
  board: { ...DEFAULT_ROUTE, tab: "pipeline" },
  compose: { ...DEFAULT_ROUTE, tab: "compose" },
  fleet: { ...DEFAULT_ROUTE, tab: "fleet" },
  home: { ...DEFAULT_ROUTE, tab: "home" },
  inbox: { ...DEFAULT_ROUTE, tab: "home" },
  lessons: { ...DEFAULT_ROUTE, tab: "fleet", fleetTab: "lessons" },
  logs: { ...DEFAULT_ROUTE, tab: "fleet", fleetTab: "logs" },
  memory: { ...DEFAULT_ROUTE, tab: "fleet", fleetTab: "lessons" },
  operator: { ...DEFAULT_ROUTE, tab: "fleet" },
  pipeline: { ...DEFAULT_ROUTE, tab: "pipeline" },
  plans: { ...DEFAULT_ROUTE, tab: "pipeline" },
  review: { ...DEFAULT_ROUTE, tab: "home" },
  roster: { ...DEFAULT_ROUTE, tab: "fleet" },
  settings: { ...DEFAULT_ROUTE, tab: "settings" },
  setup: { ...DEFAULT_ROUTE, tab: "settings" },
  work: { ...DEFAULT_ROUTE, tab: "pipeline" },
};

function normalizeToken(value: string | null | undefined): string {
  return (value || "").trim().toLowerCase();
}

function fleetTabFromToken(value: string | null | undefined): OperatorKey {
  const raw = normalizeToken(value);
  if (raw === "activity" || raw === "logs") return "logs";
  if (raw === "lessons" || raw === "memory") return "lessons";
  return "fleet";
}

function setupModeFromToken(value: string | null | undefined): SetupMode {
  return normalizeToken(value) === "advanced" ? "advanced" : "guided";
}

export function parseDesktopRoute(
  locationLike: Pick<Location, "hash" | "search"> | null | undefined,
): DesktopRoute {
  if (!locationLike) return DEFAULT_ROUTE;
  const params = new URLSearchParams(locationLike.search || "");
  const rawTab =
    normalizeToken(params.get("tab")) ||
    normalizeToken((locationLike.hash || "").replace(/^#/, ""));
  const base = TAB_ALIASES[rawTab] || DEFAULT_ROUTE;
  const subtab = params.get("subtab");
  return {
    tab: base.tab,
    fleetTab: base.tab === "fleet" ? fleetTabFromToken(subtab || base.fleetTab) : base.fleetTab,
    setupMode:
      base.tab === "settings"
        ? setupModeFromToken(subtab || base.setupMode)
        : base.setupMode,
  };
}

export function desktopRouteToSearch(route: DesktopRoute): string {
  const params = new URLSearchParams();
  applyRouteParams(params, route);
  const search = params.toString();
  return search ? `?${search}` : "";
}

function routeToBrowserSearch(route: DesktopRoute): string {
  if (typeof window === "undefined") return desktopRouteToSearch(route);
  const params = new URLSearchParams(window.location.search || "");
  applyRouteParams(params, route);
  const search = params.toString();
  return search ? `?${search}` : "";
}

function applyRouteParams(params: URLSearchParams, route: DesktopRoute): void {
  params.delete("tab");
  params.delete("subtab");
  if (route.tab === "home") params.set("tab", "inbox");
  if (route.tab === "compose") params.set("tab", "ask");
  if (route.tab === "pipeline") params.set("tab", "work");
  if (route.tab === "fleet") {
    params.set("tab", "agents");
    if (route.fleetTab === "logs") params.set("subtab", "activity");
    if (route.fleetTab === "lessons") params.set("subtab", "lessons");
  }
  if (route.tab === "settings") {
    params.set("tab", "setup");
    if (route.setupMode === "advanced") params.set("subtab", "advanced");
  }
}

function browserRoute(): DesktopRoute {
  if (typeof window === "undefined") return DEFAULT_ROUTE;
  return parseDesktopRoute(window.location);
}

function syncBrowserRoute(route: DesktopRoute, mode: "push" | "replace"): void {
  if (typeof window === "undefined") return;
  const next = `${window.location.pathname}${routeToBrowserSearch(route)}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next !== current) {
    if (mode === "push") {
      window.history.pushState(null, "", next);
    } else {
      window.history.replaceState(null, "", next);
    }
  }
}

export function useDesktopRoute() {
  const [route, setRoute] = useState<DesktopRoute>(() => browserRoute());
  const historyModeRef = useRef<"none" | "push" | "replace">("replace");

  useEffect(() => {
    const mode = historyModeRef.current;
    historyModeRef.current = "replace";
    if (mode !== "none") syncBrowserRoute(route, mode);
  }, [route]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onPopState = () => {
      historyModeRef.current = "none";
      setRoute(browserRoute());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const setTab = useCallback((next: TabKey) => {
    historyModeRef.current = "push";
    setRoute((current) => {
      if (next === "logs") return { ...current, tab: "fleet", fleetTab: "logs" };
      if (next === "lessons") return { ...current, tab: "fleet", fleetTab: "lessons" };
      if (next === "settings") return { ...current, tab: "settings", setupMode: "guided" };
      return { ...current, tab: next };
    });
  }, []);

  const setFleetTab = useCallback((next: OperatorKey) => {
    historyModeRef.current = "push";
    setRoute((current) => ({ ...current, tab: "fleet", fleetTab: next }));
  }, []);

  const setSetupMode = useCallback((next: SetupMode) => {
    historyModeRef.current = "push";
    setRoute((current) => ({ ...current, tab: "settings", setupMode: next }));
  }, []);

  return {
    fleetTab: route.fleetTab,
    setFleetTab,
    setSetupMode,
    setTab,
    setupMode: route.setupMode,
    tab: route.tab,
  };
}
