import {
  type CustomRosterNames,
  DEFAULT_ROSTER_THEME,
  EMPTY_CUSTOM_NAMES,
  normalizeCodename,
  resolveThemedIdentity,
  type RosterThemeId,
} from "./agentThemes";
import type { FleetControlRow } from "./fleetControl";
import type { WorkflowNodeInput } from "./workflowGraph";
import type { ScheduledRun } from "../types";

// The resolved display profile for one agent under the active roster theme.
// `label` keeps the legacy "Name · Role" form for the aria title; `name` and
// `roleLabel` render separately so the role is always plain.
export type AgentProfile = {
  name: string;
  role: WorkflowNodeInput["role"];
  roleLabel: string;
  label: string;
  purpose: string;
  themeAccent: string;
};

// Resolve an agent's display profile under the active roster theme. The themed
// name + role label come from the theme mapping (keyed off the agent's derived
// role, never a literal name list); the runtime's own reported display name /
// role title still take precedence when present so a server that labels its
// agents is honored.
export function agentProfile(
  row: FleetControlRow,
  schedule?: ScheduledRun,
  themeId: RosterThemeId = DEFAULT_ROSTER_THEME,
  customNames: CustomRosterNames = EMPTY_CUSTOM_NAMES,
): AgentProfile {
  const identity = resolveThemedIdentity(
    {
      codename: row.codename,
      roleTitle: row.summary?.role_title || schedule?.role_title || schedule?.role,
      purpose: row.summary?.purpose || schedule?.purpose,
    },
    themeId,
    customNames,
  );
  // The runtime's own labels win when set, so existing server-side naming is
  // preserved; otherwise the theme persona supplies the name and role label.
  // Under the `custom` theme the operator's authored override wins, but ONLY for
  // an agent they actually named/relabeled: an un-overridden agent must keep its
  // runtime label rather than be replaced by a Batman default or a titleized
  // codename. So the override is scoped per agent and per field, not theme-wide.
  const short = normalizeCodename(row.codename);
  const hasCustomName = themeId === "custom" && Boolean(customNames.names[short]?.trim());
  const hasCustomRole = themeId === "custom" && Boolean(customNames.roles[short]?.trim());
  const name = hasCustomName
    ? identity.name
    : row.summary?.display_name || schedule?.display_name || identity.name;
  const roleLabel = hasCustomRole
    ? identity.roleLabel
    : row.summary?.role_title || schedule?.role_title || identity.roleLabel;
  const purpose = row.summary?.purpose || schedule?.purpose || "";
  return {
    name,
    role: identity.role,
    roleLabel,
    label: roleLabel ? `${name} · ${roleLabel}` : name,
    purpose,
    themeAccent: row.summary?.theme_accent || schedule?.theme_accent || "var(--primary)",
  };
}
