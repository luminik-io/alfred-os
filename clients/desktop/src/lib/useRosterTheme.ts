import { useCallback, useEffect, useRef, useState } from "react";

import { loadRosterTheme, saveRosterTheme } from "../api";
import {
  type CustomRosterNames,
  DEFAULT_ROSTER_THEME,
  EMPTY_CUSTOM_NAMES,
  isRosterThemeId,
  type RosterThemeId,
} from "./agentThemes";

// The roster theme is the named cast applied to the agent roster (Batman by
// default, plus Transformers, Justice League, and the operator's own Custom
// cast). It is independent of the visual theme (useTheme: palette + light/dark).
//
// Persistence is server-first, localStorage-fallback: when connected, the
// runtime's `/api/roster-theme` is the source of truth so the choice (and any
// custom names) are shared with the Slack message path; the same value is
// mirrored to localStorage so the picker shows the right cast instantly on the
// next launch and still works when the runtime is unreachable.
const ROSTER_THEME_KEY = "alfred.rosterTheme";
const CUSTOM_NAMES_KEY = "alfred.rosterCustomNames";

function readStoredTheme(): RosterThemeId {
  try {
    const saved = window.localStorage.getItem(ROSTER_THEME_KEY);
    if (isRosterThemeId(saved)) {
      return saved;
    }
  } catch {
    // localStorage may be unavailable (private mode); fall back to the default.
  }
  return DEFAULT_ROSTER_THEME;
}

function readStoredCustom(): CustomRosterNames {
  try {
    const raw = window.localStorage.getItem(CUSTOM_NAMES_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<CustomRosterNames>;
      return {
        names: isStringMap(parsed?.names) ? parsed.names : {},
        roles: isStringMap(parsed?.roles) ? parsed.roles : {},
      };
    }
  } catch {
    // Corrupt or missing: start from an empty custom cast.
  }
  return EMPTY_CUSTOM_NAMES;
}

function isStringMap(value: unknown): value is Record<string, string> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((entry) => typeof entry === "string")
  );
}

function writeStored(theme: RosterThemeId, custom: CustomRosterNames): void {
  try {
    window.localStorage.setItem(ROSTER_THEME_KEY, theme);
    window.localStorage.setItem(CUSTOM_NAMES_KEY, JSON.stringify(custom));
  } catch {
    // Keep the choice in memory only when storage is unavailable.
  }
}

export type UseRosterTheme = {
  rosterTheme: RosterThemeId;
  customNames: CustomRosterNames;
  setRosterTheme: (next: RosterThemeId) => void;
  setCustomNames: (next: CustomRosterNames) => void;
  // Non-null when the most recent save did not reach the server (no token, 403,
  // offline). The local picker still reflects the choice, but Slack and a fresh
  // reload keep the old persisted cast until a save succeeds, so the UI must be
  // able to tell the operator the change is local-only.
  saveError: string | null;
};

export function useRosterTheme(baseUrl?: string): UseRosterTheme {
  const [rosterTheme, setRosterThemeState] = useState<RosterThemeId>(readStoredTheme);
  const [customNames, setCustomNamesState] = useState<CustomRosterNames>(readStoredCustom);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Avoid clobbering a freshly persisted choice with a slow initial GET. We
  // track WHICH runtime we have synced with, not just whether we have synced:
  // a successful read or write records its `baseUrl` here. If the desktop later
  // connects to a different runtime, this no longer matches and the new
  // runtime is read fresh instead of being skipped. An offline-only change
  // never records a url, so a later server read still runs.
  const hydratedUrlRef = useRef<string | null>(null);
  // The runtime the hook is currently pointed at. Kept in a ref so an
  // out-of-band save resolution can tell whether it still speaks for the
  // connected runtime before it records hydration.
  const baseUrlRef = useRef<string | undefined>(baseUrl);
  baseUrlRef.current = baseUrl;
  // Serialize saves so a fast A -> B switch cannot land out of order. Each call
  // bumps `saveSeqRef`; only the latest seq decides the agreed state. While a
  // POST is in flight, the newest pending choice is coalesced and sent next, so
  // the server's final state always matches the operator's last action. Each
  // queued save carries the `url` it was made against, so a change made on one
  // runtime is never posted to a different one the desktop later connected to.
  const saveSeqRef = useRef(0);
  const inFlightRef = useRef(false);
  const pendingRef = useRef<{
    url: string;
    theme: RosterThemeId;
    custom: CustomRosterNames;
    seq: number;
  } | null>(null);

  // On connect, read the server's persisted choice so the picker reflects the
  // cast the runtime (and Slack) already use. A failed read keeps the
  // localStorage value, so an offline desktop still works.
  useEffect(() => {
    if (!baseUrl || hydratedUrlRef.current === baseUrl) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const remote = await loadRosterTheme(baseUrl);
        // A save can win the race and record this runtime as synced while the
        // GET is still in flight; honoring only `cancelled` would let the older
        // server snapshot overwrite the freshly persisted choice. Bail once this
        // runtime is already synced so the newer save stands.
        if (cancelled || hydratedUrlRef.current === baseUrl) return;
        const theme = isRosterThemeId(remote.theme) ? remote.theme : DEFAULT_ROSTER_THEME;
        const custom: CustomRosterNames = {
          names: remote.custom_names ?? {},
          roles: remote.custom_roles ?? {},
        };
        hydratedUrlRef.current = baseUrl;
        setRosterThemeState(theme);
        setCustomNamesState(custom);
        writeStored(theme, custom);
      } catch {
        // Unreachable runtime: keep the localStorage fallback already in state.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl]);

  // Mirror every change to localStorage so the next launch is instant.
  useEffect(() => {
    writeStored(rosterTheme, customNames);
  }, [rosterTheme, customNames]);

  // Persist a theme switch to the server when connected. localStorage is always
  // written via the effect above, so a failed POST still keeps the local choice.
  const runSave = useCallback(
    (url: string, theme: RosterThemeId, custom: CustomRosterNames, seq: number) => {
      // Only a custom save carries the cast. A preset switch must omit both
      // maps so the server retains the authored custom cast (it replaces the
      // retained maps only when an explicit payload is present); sending empty
      // objects here would wipe the cast and lose it when switching back.
      const body =
        theme === "custom"
          ? { theme, custom_names: custom.names, custom_roles: custom.roles }
          : { theme };
      inFlightRef.current = true;
      void saveRosterTheme(url, body)
        .then(() => {
          // The server is now the agreed source of truth; clear any prior
          // failure and record this runtime as synced so a racing GET cannot
          // clobber the choice we just persisted. Skip if a newer save has
          // since been issued: that save owns the agreed state, not this one.
          if (seq !== saveSeqRef.current) return;
          // Only record hydration when this save targeted the runtime the
          // desktop is still connected to; a save that completed against a
          // runtime we have since left must not mark the current one synced.
          if (url === baseUrlRef.current) {
            hydratedUrlRef.current = url;
          }
          setSaveError(null);
        })
        .catch((err: unknown) => {
          // The local value still reflects the choice, but Slack and a fresh
          // reload keep the old server state. Surface that so the change does
          // not silently look successful. A superseded save stays quiet; the
          // newer one reports its own outcome.
          if (seq !== saveSeqRef.current) return;
          setSaveError(
            err instanceof Error && err.message
              ? `Could not save to Alfred: ${err.message}`
              : "Could not save to Alfred. The cast is local-only until a save succeeds.",
          );
        })
        .finally(() => {
          inFlightRef.current = false;
          // Drain to the latest queued choice so the final server write always
          // reflects the operator's last action, in order. The queued change
          // carries its own target url, so it posts to the runtime it was made
          // against rather than reusing this completed save's url.
          const next = pendingRef.current;
          if (next) {
            pendingRef.current = null;
            runSave(next.url, next.theme, next.custom, next.seq);
          }
        });
    },
    [],
  );

  const persist = useCallback(
    (theme: RosterThemeId, custom: CustomRosterNames) => {
      if (!baseUrl) {
        // Offline change: keep it in memory/localStorage but do NOT mark the
        // hook hydrated. When the runtime later connects, the hydration effect
        // must still read the server's persisted cast rather than skip it.
        setSaveError("Not connected: this cast is local-only until Alfred is reachable.");
        return;
      }
      const seq = ++saveSeqRef.current;
      if (inFlightRef.current) {
        // A save is already running. Coalesce to this newest choice; the
        // in-flight save's finally() will send it once the socket is free,
        // posting to the runtime this change was made against.
        pendingRef.current = { url: baseUrl, theme, custom, seq };
        return;
      }
      runSave(baseUrl, theme, custom, seq);
    },
    [baseUrl, runSave],
  );

  const setRosterTheme = useCallback(
    (next: RosterThemeId) => {
      setRosterThemeState(next);
      persist(next, customNames);
    },
    [customNames, persist],
  );

  const setCustomNames = useCallback(
    (next: CustomRosterNames) => {
      // Editing the custom cast also selects it, so the change is visible.
      setCustomNamesState(next);
      setRosterThemeState("custom");
      persist("custom", next);
    },
    [persist],
  );

  return { rosterTheme, customNames, setRosterTheme, setCustomNames, saveError };
}
