import { Sparkles } from "lucide-react";

import {
  ROSTER_THEME_IDS,
  ROSTER_THEMES,
  type RosterThemeId,
} from "../lib/agentThemes";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

// A compact named-preset picker for the roster theme (the agent cast: Batman,
// Transformers, Justice League). It re-skins the display names + role labels
// only; the canonical roles and the live status data are unchanged. Sits in the
// Agents view header next to the workflow/list toggle.
export function RosterThemePicker({
  value,
  onChange,
}: {
  value: RosterThemeId;
  onChange: (next: RosterThemeId) => void;
}) {
  const active = ROSTER_THEMES[value] ?? ROSTER_THEMES.batman;
  return (
    <div className="roster-theme-picker">
      <Sparkles aria-hidden="true" className="roster-theme-picker__icon" />
      <span className="roster-theme-picker__label">Roster theme</span>
      <Select value={value} onValueChange={(next) => onChange(next as RosterThemeId)}>
        <SelectTrigger
          size="sm"
          className="roster-theme-picker__trigger"
          aria-label="Roster theme"
        >
          <SelectValue placeholder={active.label} />
        </SelectTrigger>
        <SelectContent>
          {ROSTER_THEME_IDS.map((id) => {
            const theme = ROSTER_THEMES[id];
            return (
              <SelectItem key={id} value={id}>
                {theme.label}
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
    </div>
  );
}
