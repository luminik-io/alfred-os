import { Check, Moon, Sun } from "lucide-react";

import {
  THEME_META,
  THEME_NAMES,
  type ThemeMode,
  type ThemeName,
} from "../lib/useTheme";

// Settings > Appearance. A radio-card theme picker plus a light/dark segmented
// control. Both dimensions apply instantly and persist via useTheme. The cards
// preview each theme with a small swatch row so the choice is visible before
// selecting. See docs/THEME_SYSTEM.md.
export function AppearancePicker({
  themeName,
  mode,
  onSelectTheme,
  onSelectMode,
}: {
  themeName: ThemeName;
  mode: ThemeMode;
  onSelectTheme: (name: ThemeName) => void;
  onSelectMode: (mode: ThemeMode) => void;
}) {
  return (
    <section className="appearance" aria-label="Appearance">
      <div className="appearance__group" role="radiogroup" aria-label="Theme">
        <span className="appearance__legend">Theme</span>
        <div className="appearance__cards">
          {THEME_NAMES.map((name) => {
            const meta = THEME_META[name];
            const selected = themeName === name;
            return (
              <button
                key={name}
                type="button"
                role="radio"
                aria-checked={selected}
                className={`appearance__card${selected ? " appearance__card--selected" : ""}`}
                onClick={() => onSelectTheme(name)}
              >
                <span
                  className="appearance__swatch"
                  data-theme-preview={name}
                  aria-hidden="true"
                >
                  <span className="appearance__swatch-bg" />
                  <span className="appearance__swatch-card" />
                  <span className="appearance__swatch-accent" />
                </span>
                <span className="appearance__card-text">
                  <span className="appearance__card-head">
                    <strong>{meta.label}</strong>
                    {selected ? (
                      <Check size={14} aria-hidden="true" className="appearance__check" />
                    ) : null}
                  </span>
                  <span className="appearance__card-blurb">{meta.blurb}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="appearance__group" role="radiogroup" aria-label="Mode">
        <span className="appearance__legend">Mode</span>
        <div className="appearance__segment">
          <button
            type="button"
            role="radio"
            aria-checked={mode === "dark"}
            className={`appearance__segment-btn${mode === "dark" ? " appearance__segment-btn--active" : ""}`}
            onClick={() => onSelectMode("dark")}
          >
            <Moon size={15} aria-hidden="true" />
            <span>Dark</span>
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === "light"}
            className={`appearance__segment-btn${mode === "light" ? " appearance__segment-btn--active" : ""}`}
            onClick={() => onSelectMode("light")}
          >
            <Sun size={15} aria-hidden="true" />
            <span>Light</span>
          </button>
        </div>
      </div>
    </section>
  );
}
