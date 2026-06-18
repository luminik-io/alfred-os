import { useCallback, useEffect, useState } from "react";

// A theme is a named identity (palette + glass/density character). The mode is
// the light/dark twin within that theme. They are independent dimensions:
// data-theme on :root selects the theme; the .dark/.light class selects the
// mode. See docs/THEME_SYSTEM.md.
export type ThemeName = "alfred" | "linear";
export type ThemeMode = "dark" | "light";

// Back-compat alias: callers (and the toggle) still talk in light/dark.
export type Theme = ThemeMode;

export const THEME_NAMES: ThemeName[] = ["alfred", "linear"];

export const THEME_META: Record<ThemeName, { label: string; blurb: string }> = {
  alfred: {
    label: "Alfred",
    blurb: "Near-black steel-violet identity with floating glass chrome.",
  },
  linear: {
    label: "Linear Crisp",
    blurb: "Flatter and denser. Less glass, tighter spacing, maximum density.",
  },
};

const THEME_KEY = "alfred-theme-name";
const MODE_KEY = "alfred-theme";

function isThemeName(value: string | null): value is ThemeName {
  return value === "alfred" || value === "linear";
}

function isThemeMode(value: string | null): value is ThemeMode {
  return value === "dark" || value === "light";
}

function initialThemeName(): ThemeName {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (isThemeName(saved)) return saved;
  } catch {
    // localStorage may be unavailable; fall back to the default.
  }
  return "alfred";
}

function initialMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(MODE_KEY);
    if (isThemeMode(saved)) return saved;
  } catch {
    // localStorage may be unavailable; fall back to the default.
  }
  return "dark";
}

/** Theme system state: a named theme (data-theme) and a light/dark mode
 *  (.dark/.light class), each persisted to localStorage and applied to the
 *  document root. The CSS token themes key off both dimensions. */
export function useTheme() {
  const [themeName, setThemeNameState] = useState<ThemeName>(initialThemeName);
  const [mode, setModeState] = useState<ThemeMode>(initialMode);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = themeName;
    root.classList.toggle("dark", mode === "dark");
    root.classList.toggle("light", mode === "light");
    try {
      localStorage.setItem(THEME_KEY, themeName);
      localStorage.setItem(MODE_KEY, mode);
    } catch {
      // ignore persistence failures
    }
  }, [themeName, mode]);

  const setThemeName = useCallback((next: ThemeName) => {
    setThemeNameState(next);
  }, []);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
  }, []);

  const toggle = useCallback(() => {
    setModeState((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  return {
    // Back-compat: `theme` is the light/dark mode, `setTheme` sets the mode.
    theme: mode,
    setTheme: setMode,
    mode,
    setMode,
    themeName,
    setThemeName,
    toggle,
  };
}
