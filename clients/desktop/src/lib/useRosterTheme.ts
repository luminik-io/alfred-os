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
  setRosterTheme: (next: RosterThemeId) => Promise<boolean>;
  setCustomNames: (next: CustomRosterNames) => Promise<boolean>;
  // Non-null when the most recent save did not reach the server (no token, 403,
  // offline). The local picker still reflects the choice, but Slack and a fresh
  // reload keep the old persisted cast until a save succeeds, so the UI must be
  // able to tell the operator the change is local-only.
  saveError: string | null;
  // True while the connected runtime's saved cast is still being read. Mutating
  // setup flows use this to avoid POSTing a localStorage fallback before the
  // server's existing roster has had a chance to load.
  hydrating: boolean;
  hydrationError: string | null;
  retryHydration: () => void;
};

export function useRosterTheme(baseUrl?: string, connected = Boolean(baseUrl)): UseRosterTheme {
  const [rosterTheme, setRosterThemeState] = useState<RosterThemeId>(readStoredTheme);
  const [customNames, setCustomNamesState] = useState<CustomRosterNames>(readStoredCustom);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [hydrating, setHydrating] = useState(false);
  const [hydrationError, setHydrationError] = useState<string | null>(null);
  const [hydrationRequestSeq, setHydrationRequestSeq] = useState(0);
  // Avoid clobbering a freshly persisted choice with a slow initial GET. We
  // track WHICH runtime we have synced with, not just whether we have synced:
  // a successful read or write records its `baseUrl` here. If the desktop later
  // connects to a different runtime, this no longer matches and the new
  // runtime is read fresh instead of being skipped. An offline-only change
  // never records a url, so a later server read still runs.
  const hydratedUrlRef = useRef<string | null>(null);
  const skippedHydrationUrlRef = useRef<string | null>(null);
  // The runtime the hook is currently pointed at. Kept in a ref so an
  // out-of-band save resolution can tell whether it still speaks for the
  // connected runtime before it records hydration.
  const baseUrlRef = useRef<string | undefined>(baseUrl);
  baseUrlRef.current = baseUrl;
  const connectedRef = useRef(connected);
  connectedRef.current = connected;
  // Serialize saves so a fast A -> B switch cannot land out of order. Each call
  // bumps `saveSeqRef`; only the latest seq decides the agreed state. While a
  // POST is in flight, later choices are queued and sent next, so the server's
  // final state always matches the operator's last action. The queue is keyed
  // by runtime url: a change made on one runtime is never posted to a different
  // one, and a queued edit for runtime A is not dropped when an edit for runtime
  // B is queued behind it. Re-editing the same runtime coalesces to the latest.
  const saveSeqRef = useRef(0);
  const inFlightRef = useRef(false);
  const pendingRef = useRef<
    Map<
      string,
      {
        theme: RosterThemeId;
        custom: CustomRosterNames;
        seq: number;
        resolve: (saved: boolean) => void;
      }
    >
  >(new Map());
  // The latest seq issued per runtime url. Staleness is per runtime: a save's
  // outcome is suppressed only when a NEWER save for the SAME runtime supersedes
  // it. Gating on the global `saveSeqRef` instead would let a save to runtime B
  // silence a real failure on runtime A (a cross-runtime failure vanishing).
  const latestSeqByUrlRef = useRef<Map<string, number>>(new Map());
  // Which runtime the currently surfaced `saveError` belongs to (null when there
  // is no error, or it is a connection-level one). A success for runtime B must
  // not clear a still-unresolved failure on runtime A, so the success path only
  // clears an error that belongs to the same runtime.
  const saveErrorUrlRef = useRef<string | null>(null);

  // On connect, read the server's persisted choice so the picker reflects the
  // cast the runtime (and Slack) already use. A failed read keeps the
  // localStorage value, so an offline desktop still works.
  useEffect(() => {
    if (saveErrorUrlRef.current && saveErrorUrlRef.current !== (baseUrl ?? null)) {
      saveErrorUrlRef.current = null;
      setSaveError(null);
    }
  }, [baseUrl]);

  useEffect(() => {
    if (!baseUrl || !connected) {
      hydratedUrlRef.current = null;
      skippedHydrationUrlRef.current = null;
      setHydrating(false);
      setHydrationError(null);
      return;
    }
    if (hydratedUrlRef.current === baseUrl) {
      setHydrating(false);
      setHydrationError(null);
      return;
    }
    let cancelled = false;
    setHydrationError(null);
    setHydrating(true);
    void (async () => {
      try {
        const remote = await loadRosterTheme(baseUrl);
        // A save can win the race and record this runtime as synced while the
        // GET is still in flight; honoring only `cancelled` would let the older
        // server snapshot overwrite the freshly persisted choice. Bail once this
        // runtime is already synced so the newer save stands.
        if (cancelled) return;
        if (hydratedUrlRef.current === baseUrl) {
          skippedHydrationUrlRef.current = baseUrl;
          return;
        }
        const theme = isRosterThemeId(remote.theme) ? remote.theme : DEFAULT_ROSTER_THEME;
        const custom: CustomRosterNames = {
          names: remote.custom_names ?? {},
          roles: remote.custom_roles ?? {},
        };
        hydratedUrlRef.current = baseUrl;
        skippedHydrationUrlRef.current = null;
        setRosterThemeState(theme);
        setCustomNamesState(custom);
        setHydrationError(null);
        saveErrorUrlRef.current = null;
        setSaveError(null);
        writeStored(theme, custom);
      } catch (err: unknown) {
        // Keep the localStorage fallback in state for display, but do not treat it
        // as safe to persist from setup: the server's current Slack cast is still
        // unknown, so mutating would risk overwriting it with a fallback.
        if (hydratedUrlRef.current === baseUrl) return;
        if (!cancelled && baseUrlRef.current === baseUrl && connectedRef.current) {
          setHydrationError(
            err instanceof Error && err.message
              ? `Could not load saved fleet names from Alfred: ${err.message}`
              : "Could not load saved fleet names from Alfred.",
          );
        }
      } finally {
        if (!cancelled && baseUrlRef.current === baseUrl && connectedRef.current) {
          setHydrating(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, connected, hydrationRequestSeq]);

  // Mirror every change to localStorage so the next launch is instant.
  useEffect(() => {
    writeStored(rosterTheme, customNames);
  }, [rosterTheme, customNames]);

  // Persist a theme switch to the server when connected. localStorage is always
  // written via the effect above, so a failed POST still keeps the local choice.
  const runSave = useCallback(
    (
      url: string,
      theme: RosterThemeId,
      custom: CustomRosterNames,
      seq: number,
    ): Promise<boolean> => {
      // Only a custom save carries the cast. A preset switch must omit both
      // maps so the server retains the authored custom cast (it replaces the
      // retained maps only when an explicit payload is present); sending empty
      // objects here would wipe the cast and lose it when switching back.
      const body =
        theme === "custom"
          ? { theme, custom_names: custom.names, custom_roles: custom.roles }
          : { theme };
      inFlightRef.current = true;
      return saveRosterTheme(url, body)
        .then(() => {
          // The server is now the agreed source of truth; clear any prior
          // failure and record this runtime as synced so a racing GET cannot
          // clobber the choice we just persisted. Skip if a newer save has
          // since been issued: that save owns the agreed state, not this one.
          if (seq !== latestSeqByUrlRef.current.get(url)) return false;
          // Only record hydration when this save targeted the runtime the
          // desktop is still connected to; a save that completed against a
          // runtime we have since left must not mark the current one synced.
          if (!connectedRef.current) return false;
          if (url !== baseUrlRef.current) return false;
          hydratedUrlRef.current = url;
          skippedHydrationUrlRef.current = null;
          setHydrationError(null);
          saveErrorUrlRef.current = null;
          setSaveError(null);
          return true;
        })
        .catch((err: unknown) => {
          // The local value still reflects the choice, but Slack and a fresh
          // reload keep the old server state. Surface that so the change does
          // not silently look successful. A superseded save stays quiet; the
          // newer one reports its own outcome.
          if (seq !== latestSeqByUrlRef.current.get(url)) return false;
          // The optimistic hydration recorded in persist() assumed this save
          // would land. It did not, so the server still holds the old cast.
          // Clear the marker for this runtime (if it is still ours) so an
          // in-flight or later GET re-reads the server instead of trusting the
          // unsaved local value forever, including across a reconnect.
          if (hydratedUrlRef.current === url) {
            hydratedUrlRef.current = null;
          }
          if (!connectedRef.current) return false;
          if (url !== baseUrlRef.current) return false;
          saveErrorUrlRef.current = url;
          setSaveError(
            err instanceof Error && err.message
              ? `Could not save to Alfred: ${err.message}`
              : "Could not save to Alfred. The cast is local-only until a save succeeds.",
          );
          if (skippedHydrationUrlRef.current === url) {
            skippedHydrationUrlRef.current = null;
            setHydrationRequestSeq((requestSeq) => requestSeq + 1);
          }
          return false;
        })
        .finally(() => {
          inFlightRef.current = false;
          // Drain the next queued runtime in insertion order. Each queued change
          // carries its own target url, so it posts to the runtime it was made
          // against; runtimes other than this one keep their queued edits rather
          // than being overwritten by a single shared pending slot.
          const nextUrl = pendingRef.current.keys().next().value;
          if (nextUrl !== undefined) {
            const next = pendingRef.current.get(nextUrl)!;
            pendingRef.current.delete(nextUrl);
            void runSave(nextUrl, next.theme, next.custom, next.seq).then(next.resolve);
          }
        });
    },
    [],
  );

  const persist = useCallback(
    (theme: RosterThemeId, custom: CustomRosterNames): Promise<boolean> => {
      if (!baseUrl || !connected) {
        // Offline change: keep it in memory/localStorage but do NOT mark the
        // hook hydrated. When the runtime later connects, the hydration effect
        // must still read the server's persisted cast rather than skip it.
        // A connection-level error is not tied to a specific synced runtime.
        saveErrorUrlRef.current = null;
        setSaveError("Not connected: this cast is local-only until Alfred is reachable.");
        return Promise.resolve(false);
      }
      if (saveErrorUrlRef.current === baseUrl) {
        saveErrorUrlRef.current = null;
        setSaveError(null);
      }
      // The operator's change now owns this runtime's state locally. Record the
      // runtime as synced immediately, before the (possibly queued) save even
      // goes out: otherwise a still-in-flight hydration GET for this runtime can
      // resolve and revert the change, because hydration is only recorded on
      // save success. Marking it here closes that window; the save still
      // reconciles with the server, and a failed save clears the marker again
      // so the server value is re-read rather than trusted indefinitely.
      hydratedUrlRef.current = baseUrl;
      const seq = ++saveSeqRef.current;
      latestSeqByUrlRef.current.set(baseUrl, seq);
      if (inFlightRef.current) {
        // A save is already running. Queue this choice under its runtime url;
        // the in-flight save's finally() drains it once the socket is free.
        // Same-runtime re-edits coalesce to the latest; other runtimes keep
        // their own queued edits.
        const previous = pendingRef.current.get(baseUrl);
        if (previous) {
          previous.resolve(false);
        }
        return new Promise((resolve) => {
          pendingRef.current.set(baseUrl, { theme, custom, seq, resolve });
        });
      }
      return runSave(baseUrl, theme, custom, seq);
    },
    [baseUrl, connected, runSave],
  );

  const setRosterTheme = useCallback(
    (next: RosterThemeId) => {
      setRosterThemeState(next);
      return persist(next, customNames);
    },
    [customNames, persist],
  );

  const setCustomNames = useCallback(
    (next: CustomRosterNames) => {
      // Editing the custom cast also selects it, so the change is visible.
      setCustomNamesState(next);
      setRosterThemeState("custom");
      return persist("custom", next);
    },
    [persist],
  );

  const retryHydration = useCallback(() => {
    if (!baseUrl || !connected) return;
    hydratedUrlRef.current = null;
    skippedHydrationUrlRef.current = null;
    setHydrationError(null);
    setHydrationRequestSeq((seq) => seq + 1);
  }, [baseUrl, connected]);

  return {
    rosterTheme,
    customNames,
    setRosterTheme,
    setCustomNames,
    saveError,
    hydrating,
    hydrationError,
    retryHydration,
  };
}
