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
};

export function useRosterTheme(baseUrl?: string): UseRosterTheme {
  const [rosterTheme, setRosterThemeState] = useState<RosterThemeId>(readStoredTheme);
  const [customNames, setCustomNamesState] = useState<CustomRosterNames>(readStoredCustom);
  // Avoid clobbering a freshly persisted choice with a slow initial GET.
  const hydratedRef = useRef(false);

  // On connect, read the server's persisted choice so the picker reflects the
  // cast the runtime (and Slack) already use. A failed read keeps the
  // localStorage value, so an offline desktop still works.
  useEffect(() => {
    if (!baseUrl || hydratedRef.current) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const remote = await loadRosterTheme(baseUrl);
        if (cancelled) return;
        const theme = isRosterThemeId(remote.theme) ? remote.theme : DEFAULT_ROSTER_THEME;
        const custom: CustomRosterNames = {
          names: remote.custom_names ?? {},
          roles: remote.custom_roles ?? {},
        };
        hydratedRef.current = true;
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
  const persist = useCallback(
    (theme: RosterThemeId, custom: CustomRosterNames) => {
      hydratedRef.current = true;
      if (!baseUrl) return;
      void saveRosterTheme(baseUrl, {
        theme,
        custom_names: custom.names,
        custom_roles: custom.roles,
      }).catch(() => {
        // Best-effort: the local value remains correct; a later edit retries.
      });
    },
    [baseUrl],
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

  return { rosterTheme, customNames, setRosterTheme, setCustomNames };
}
