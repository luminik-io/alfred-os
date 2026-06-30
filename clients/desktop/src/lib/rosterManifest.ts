import rosterManifestJson from "../../../../lib/roster_manifest.json";
import type { WorkflowRole } from "./agentRoster";

export type PresetRosterThemeId = "batman" | "transformers" | "justice-league";

export type RosterManifestAgent = {
  codename: string;
  role: WorkflowRole;
  names: Record<PresetRosterThemeId, string>;
};

export type RosterManifestTheme = {
  label?: string;
  blurb?: string;
};

export type RosterManifest = {
  default_theme: PresetRosterThemeId;
  preset_theme_ids: PresetRosterThemeId[];
  themes: Partial<Record<PresetRosterThemeId, RosterManifestTheme>>;
  role_labels: Record<WorkflowRole, string>;
  agents: RosterManifestAgent[];
};

export const ROSTER_MANIFEST = rosterManifestJson as RosterManifest;
