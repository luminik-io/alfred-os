import { useCallback, useEffect, useState } from "react";

import {
  DEFAULT_ROSTER_THEME,
  isRosterThemeId,
  type RosterThemeId,
} from "./agentThemes";

// The roster theme is the named cast applied to the agent roster (Batman by
// default, plus Transformers, Justice League). It is independent of the visual
// theme (useTheme: palette + light/dark). Persisted to localStorage so the
// operator's choice survives reloads.
const ROSTER_THEME_KEY = "alfred.rosterTheme";

function initialRosterTheme(): RosterThemeId {
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

export function useRosterTheme() {
  const [rosterTheme, setRosterThemeState] = useState<RosterThemeId>(initialRosterTheme);

  useEffect(() => {
    try {
      window.localStorage.setItem(ROSTER_THEME_KEY, rosterTheme);
    } catch {
      // Keep the choice in memory only when storage is unavailable.
    }
  }, [rosterTheme]);

  const setRosterTheme = useCallback((next: RosterThemeId) => {
    setRosterThemeState(next);
  }, []);

  return { rosterTheme, setRosterTheme };
}
