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
  deriveAgentRole,
  type RoleSource,
  type WorkflowRole,
} from "./agentRoster";

export type RosterThemeId = "batman" | "transformers" | "justice-league";

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

export const ROSTER_THEMES: Record<RosterThemeId, RosterTheme> = {
  batman: BATMAN_THEME,
  transformers: TRANSFORMERS_THEME,
  "justice-league": JUSTICE_LEAGUE_THEME,
};

export const ROSTER_THEME_IDS: readonly RosterThemeId[] = [
  "batman",
  "transformers",
  "justice-league",
];

export const DEFAULT_ROSTER_THEME: RosterThemeId = "batman";

export function isRosterThemeId(value: string | null): value is RosterThemeId {
  return value === "batman" || value === "transformers" || value === "justice-league";
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
): ThemedIdentity {
  const theme = ROSTER_THEMES[themeId] ?? BATMAN_THEME;
  const role = deriveAgentRole(source);
  const short = (source.codename.split(".").pop() || source.codename).trim().toLowerCase();
  const name = theme.nameByCodename[short] || titleizeCodename(source.codename);
  return { role, name, roleLabel: theme.roleLabels[role] };
}
