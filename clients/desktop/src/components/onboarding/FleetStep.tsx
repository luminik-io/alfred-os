import { AlertTriangle, Pencil, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

import {
  type CustomRosterNames,
  ROSTER_THEME_IDS,
  type RosterThemeId,
  resolveThemedIdentity,
  rosterThemeBlurb,
  rosterThemeLabel,
} from "../../lib/agentThemes";
import { CustomThemeEditor } from "../CustomThemeEditor";
import { Badge, Button } from "../ui";

const PREVIEW_CODENAMES = ["batman", "lucius", "rasalghul", "bane", "nightwing"];

export function FleetStep({
  value,
  customNames,
  saveError,
  disabled = false,
  onChange,
  onSaveCustom,
}: {
  value: RosterThemeId;
  customNames: CustomRosterNames;
  saveError: string | null;
  disabled?: boolean;
  onChange: (next: RosterThemeId) => void;
  onSaveCustom: (next: CustomRosterNames) => boolean | void | Promise<boolean | void>;
}) {
  const [customOpen, setCustomOpen] = useState(false);
  const activePreview = useMemo(
    () =>
      PREVIEW_CODENAMES.map((codename) => ({
        codename,
        ...resolveThemedIdentity({ codename }, value, customNames),
      })),
    [customNames, value],
  );

  return (
    <div className="fleet-step grid gap-4">
      <div className="fleet-step__intro rounded-lg border border-border/70 bg-muted/35 px-3 py-3 text-sm text-muted-foreground">
        Alfred installs the full engineering fleet by default. The roster theme changes the
        names people see in Desktop and Slack; the underlying roles, gates, and schedules stay
        the same.
      </div>

      <fieldset className="fleet-step__themes" aria-label="Roster theme">
        <legend className="sr-only">Roster theme</legend>
        <div className="grid gap-2 md:grid-cols-2">
          {ROSTER_THEME_IDS.map((themeId) => {
            const selected = value === themeId;
            return (
              <label
                key={themeId}
                className="fleet-step__theme rounded-lg border border-border/70 bg-background/55 px-3 py-3 transition-colors hover:bg-muted/45"
                data-selected={selected ? "true" : "false"}
              >
                <input
                  className="sr-only"
                  type="radio"
                  name="roster-theme"
                  checked={selected}
                  disabled={disabled}
                  onChange={() => onChange(themeId)}
                />
                <span className="flex items-start justify-between gap-3">
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-sm font-medium text-foreground">
                      <Sparkles size={14} aria-hidden="true" />
                      {rosterThemeLabel(themeId)}
                    </span>
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {rosterThemeBlurb(themeId)}
                    </span>
                  </span>
                  {selected ? <Badge variant="secondary">selected</Badge> : null}
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <div className="fleet-step__preview rounded-lg border border-border/70 bg-background/55 px-3 py-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-medium text-foreground">Preview</p>
            <p className="text-xs text-muted-foreground">Same fleet, different names.</p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => setCustomOpen(true)}
          >
            <Pencil size={14} aria-hidden="true" />
            <span>{value === "custom" ? "Edit custom names" : "Use custom names"}</span>
          </Button>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {activePreview.map((agent) => (
            <div
              key={agent.codename}
              className="fleet-step__preview-row rounded-md border border-border/60 bg-muted/25 px-2 py-2"
            >
              <p className="truncate text-sm font-medium text-foreground">{agent.name}</p>
              <p className="truncate text-xs text-muted-foreground">{agent.roleLabel}</p>
            </div>
          ))}
        </div>
      </div>

      {saveError ? (
        <p className="inline-notice inline-notice--error" role="alert">
          <AlertTriangle size={14} aria-hidden="true" />
          {saveError}
        </p>
      ) : null}

      <CustomThemeEditor
        open={customOpen}
        value={customNames}
        onOpenChange={setCustomOpen}
        onSave={onSaveCustom}
      />
    </div>
  );
}
