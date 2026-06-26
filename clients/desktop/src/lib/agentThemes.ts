// Roster themes: where an agent's DISPLAY NAME and ROLE LABEL come from, kept
// entirely separate from its canonical WorkflowRole (see agentRoster.ts). A
// theme maps each canonical role to a plain role label, and maps each known
// fleet codename to a themed persona name. The default theme reproduces the
// shipped Batman roster exactly (no visible change unless the operator picks
// another theme); presets re-skin the SAME fleet with a matched cast while the
// roles stay identical.
//
// This is the client-side foundation only. Deferred to follow-ups: operator-
// authored custom names via the Ask chat, applying themes to Slack rendering,
// and persisting custom themes server-side. See the PR body.

import {
  CODENAME_ROLE_HINTS,
  deriveAgentRole,
  type RoleSource,
  type WorkflowRole,
} from "./agentRoster";

// Canonical role per known fleet codename, used to map an operator's per-agent
// custom role label back onto the canonical role the theme keys on.
const BATMAN_ROLE_BY_CODENAME: Record<string, WorkflowRole> = CODENAME_ROLE_HINTS;

// The preset ids re-skin the shipped fleet. `custom` is the operator-authored
// theme whose names + role labels are persisted server-side (and mirrored to
// localStorage), so the choice is shared across the desktop and the Slack path.
export type PresetRosterThemeId = "batman" | "transformers" | "justice-league";
export type RosterThemeId = PresetRosterThemeId | "custom";

export type RosterTheme = {
  id: RosterThemeId;
  label: string;
  blurb: string;
  // Plain role labels, one per canonical role. Same concept across themes (a
  // Reviewer is a Reviewer); a theme may phrase it in its own register but the
  // role itself never changes when the theme changes.
  roleLabels: Record<WorkflowRole, string>;
  // Themed display name per known fleet codename. Unknown agents (not in this
  // map) fall back to their own runtime name or a titleized codename, never to
  // another agent's persona, so two agents can never collide on one name.
  nameByCodename: Record<string, string>;
  // Per-codename role label override (only the `custom` theme uses this). The
  // operator authors a role label PER AGENT, so it must not bleed onto every
  // other agent that happens to share the same canonical role. Resolution
  // prefers this over the canonical `roleLabels[role]`, matching the Slack path
  // (RosterThemeState.role_label_for, which is also keyed by codename).
  roleLabelByCodename?: Record<string, string>;
};

// The canonical fleet codenames the presets re-skin. Kept in one place so every
// theme covers the same cast and a missing entry is obvious.
const ROLE_LABELS_DEFAULT: Record<WorkflowRole, string> = {
  triage: "Triage lead",
  architect: "Architect",
  implement: "Senior developer",
  review: "Reviewer",
  ship: "Release",
  ops: "Ops & health",
};

const BATMAN_THEME: RosterTheme = {
  id: "batman",
  label: "Batman",
  blurb: "The shipped Gotham roster. Roles stay plain; names are the codenames.",
  roleLabels: ROLE_LABELS_DEFAULT,
  nameByCodename: {
    robin: "Robin",
    drake: "Drake",
    damian: "Damian",
    batman: "Batman",
    lucius: "Lucius",
    bane: "Bane",
    nightwing: "Nightwing",
    rasalghul: "Ra's al Ghul",
    huntress: "Huntress",
    automerge: "Auto-merge",
    gordon: "Gordon",
    "fleet-doctor": "Fleet doctor",
    cleanup: "Cleanup",
    "agent-cleanup": "Cleanup",
    "memory-harvest": "Memory harvest",
    "code-map-refresh": "Code map",
    "proof-telemetry": "Telemetry",
  },
};

const TRANSFORMERS_THEME: RosterTheme = {
  id: "transformers",
  label: "Transformers",
  blurb: "Autobots on the line. Optimus Prime leads the architecture.",
  roleLabels: ROLE_LABELS_DEFAULT,
  nameByCodename: {
    robin: "Bumblebee",
    drake: "Hot Rod",
    damian: "Blurr",
    batman: "Optimus Prime",
    lucius: "Ironhide",
    bane: "Grimlock",
    nightwing: "Sideswipe",
    rasalghul: "Ratchet",
    huntress: "Arcee",
    automerge: "Jazz",
    gordon: "Wheeljack",
    "fleet-doctor": "Perceptor",
    cleanup: "Cosmos",
    "agent-cleanup": "Cosmos",
    "memory-harvest": "Brainstorm",
    "code-map-refresh": "Beachcomber",
    "proof-telemetry": "Blaster",
  },
};

const JUSTICE_LEAGUE_THEME: RosterTheme = {
  id: "justice-league",
  label: "Justice League",
  blurb: "The League takes the pipeline. Batman architects, the others ship.",
  roleLabels: ROLE_LABELS_DEFAULT,
  nameByCodename: {
    robin: "The Flash",
    drake: "Green Arrow",
    damian: "Hawkgirl",
    batman: "Batman",
    lucius: "Superman",
    bane: "Shazam",
    nightwing: "Aquaman",
    rasalghul: "Wonder Woman",
    huntress: "Martian Manhunter",
    automerge: "Green Lantern",
    gordon: "Cyborg",
    "fleet-doctor": "Doctor Fate",
    cleanup: "Atom",
    "agent-cleanup": "Atom",
    "memory-harvest": "Zatanna",
    "code-map-refresh": "Vixen",
    "proof-telemetry": "Firestorm",
  },
};

// The preset themes only (the `custom` theme is built at runtime from the
// operator's persisted names, so it has no static entry here).
export const PRESET_ROSTER_THEMES: Record<PresetRosterThemeId, RosterTheme> = {
  batman: BATMAN_THEME,
  transformers: TRANSFORMERS_THEME,
  "justice-league": JUSTICE_LEAGUE_THEME,
};

export const PRESET_ROSTER_THEME_IDS: readonly PresetRosterThemeId[] = [
  "batman",
  "transformers",
  "justice-league",
];

// The full set the picker offers, custom last so the presets read first.
export const ROSTER_THEME_IDS: readonly RosterThemeId[] = [
  ...PRESET_ROSTER_THEME_IDS,
  "custom",
];

export const DEFAULT_ROSTER_THEME: RosterThemeId = "batman";

// The operator's authored maps for the `custom` theme: codename -> display
// name and codename -> role label. Anything the operator has not named falls
// back to the Batman base, so a half-filled custom theme is never blank.
export type CustomRosterNames = {
  names: Record<string, string>;
  roles: Record<string, string>;
};

export const EMPTY_CUSTOM_NAMES: CustomRosterNames = { names: {}, roles: {} };

const CUSTOM_THEME_META = {
  label: "Custom",
  blurb: "Your own cast. Rename each agent; blanks keep the Batman name.",
} as const;

// Build the `custom` theme by overlaying the operator's names/roles on the
// Batman base so every agent has a name even when only a few are edited. Role
// labels are keyed by canonical role; an operator role label is applied to the
// role of any codename it names.
function buildCustomTheme(custom: CustomRosterNames): RosterTheme {
  const nameByCodename: Record<string, string> = { ...BATMAN_THEME.nameByCodename };
  for (const [codename, name] of Object.entries(custom.names)) {
    const clean = name.trim();
    if (clean) nameByCodename[normalizeCodename(codename)] = clean;
  }
  // A custom role label is authored PER AGENT, so it is stored against that one
  // codename and never folded into the role-wide labels. The canonical
  // roleLabels stay at the Batman defaults; resolution overlays the per-codename
  // override on top so naming Batman "Lead detective" relabels only Batman, not
  // every other architect-role agent (which is exactly what Slack does).
  const roleLabelByCodename: Record<string, string> = {};
  for (const [codename, label] of Object.entries(custom.roles)) {
    const clean = label.trim();
    if (clean) roleLabelByCodename[normalizeCodename(codename)] = clean;
  }
  return {
    id: "custom",
    label: CUSTOM_THEME_META.label,
    blurb: CUSTOM_THEME_META.blurb,
    roleLabels: { ...ROLE_LABELS_DEFAULT },
    nameByCodename,
    roleLabelByCodename,
  };
}

// Resolve the active theme by id, building the custom theme from the operator's
// authored names when needed. A preset ignores the custom maps entirely.
export function rosterThemeFor(
  themeId: RosterThemeId,
  custom: CustomRosterNames = EMPTY_CUSTOM_NAMES,
): RosterTheme {
  if (themeId === "custom") return buildCustomTheme(custom);
  return PRESET_ROSTER_THEMES[themeId] ?? BATMAN_THEME;
}

export function isRosterThemeId(value: string | null): value is RosterThemeId {
  return ROSTER_THEME_IDS.includes(value as RosterThemeId);
}

// Picker-facing label + blurb for any theme id (presets read from their static
// entry; custom from its meta), so the picker never has to special-case custom.
export function rosterThemeLabel(themeId: RosterThemeId): string {
  if (themeId === "custom") return CUSTOM_THEME_META.label;
  return PRESET_ROSTER_THEMES[themeId]?.label ?? BATMAN_THEME.label;
}

export function rosterThemeBlurb(themeId: RosterThemeId): string {
  if (themeId === "custom") return CUSTOM_THEME_META.blurb;
  return PRESET_ROSTER_THEMES[themeId]?.blurb ?? BATMAN_THEME.blurb;
}

export function normalizeCodename(codename: string): string {
  return (codename.split(".").pop() || codename).trim().toLowerCase();
}

/** The plain, theme-independent fallback name when nothing else fits. */
function titleizeCodename(codename: string): string {
  const short = (codename.split(".").pop() || codename).trim();
  return short
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export type ThemedIdentity = {
  // The canonical role the agent occupies.
  role: WorkflowRole;
  // The themed display name to show.
  name: string;
  // The plain role label to show alongside the name, always present so every
  // card/node can render the role independent of the themed name.
  roleLabel: string;
};

/**
 * Resolve an agent's themed identity:
 *   - role comes from its metadata (deriveAgentRole), never a name list;
 *   - name comes from the theme's per-codename map when present, else a
 *     titleized codename so an unknown agent is never blank and never borrows
 *     another agent's persona;
 *   - roleLabel comes from the theme's role labels.
 * Pure and deterministic. Callers layer the runtime's own reported display name
 * / role title on top when the server already labels an agent (see
 * FleetControlView.agentProfile).
 */
export function resolveThemedIdentity(
  source: RoleSource,
  themeId: RosterThemeId,
  custom: CustomRosterNames = EMPTY_CUSTOM_NAMES,
): ThemedIdentity {
  const theme = rosterThemeFor(themeId, custom);
  const role = deriveAgentRole(source);
  const short = normalizeCodename(source.codename);
  const name = theme.nameByCodename[short] || titleizeCodename(source.codename);
  // A per-codename custom role label wins over the role-wide label, so an
  // operator's "Batman = Lead detective" does not relabel every architect.
  const roleLabel = theme.roleLabelByCodename?.[short] ?? theme.roleLabels[role];
  return { role, name, roleLabel };
}

// The known fleet codenames the custom-theme editor lets the operator rename,
// each with its canonical role and the shipped Batman name as the placeholder.
// Drawn from the Batman base so the editor always covers the full default cast.
export type EditableAgent = {
  codename: string;
  role: WorkflowRole;
  defaultName: string;
  defaultRoleLabel: string;
};

export function editableAgents(): EditableAgent[] {
  return Object.keys(BATMAN_THEME.nameByCodename)
    .filter((codename) => codename !== "agent-cleanup") // alias of `cleanup`
    .map((codename) => {
      const role = BATMAN_ROLE_BY_CODENAME[codename] ?? "ops";
      return {
        codename,
        role,
        defaultName: BATMAN_THEME.nameByCodename[codename],
        defaultRoleLabel: ROLE_LABELS_DEFAULT[role],
      };
    });
}
