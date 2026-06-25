import { Pencil, Sparkles } from "lucide-react";

import {
  ROSTER_THEME_IDS,
  rosterThemeLabel,
  type RosterThemeId,
} from "../lib/agentThemes";
import { Button } from "./ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

// A compact named-preset picker for the roster theme (the agent cast: Batman,
// Transformers, Justice League, plus the operator's own Custom cast). It
// re-skins the display names + role labels only; the canonical roles and the
// live status data are unchanged. Sits in the Agents view header next to the
// workflow/list toggle. The Edit button opens the custom-name editor.
export function RosterThemePicker({
  value,
  onChange,
  onEditCustom,
}: {
  value: RosterThemeId;
  onChange: (next: RosterThemeId) => void;
  onEditCustom?: () => void;
}) {
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
          <SelectValue placeholder={rosterThemeLabel(value)} />
        </SelectTrigger>
        <SelectContent>
          {ROSTER_THEME_IDS.map((id) => (
            <SelectItem key={id} value={id}>
              {rosterThemeLabel(id)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {onEditCustom ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="roster-theme-picker__edit"
          onClick={onEditCustom}
        >
          <Pencil aria-hidden="true" className="size-3.5" />
          {value === "custom" ? "Edit names" : "Customize"}
        </Button>
      ) : null}
    </div>
  );
}
