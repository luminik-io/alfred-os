// The delivery roster as a set of named ROLES, not a fixed list of codenames.
// Each agent the runtime reports is mapped to one of these roles from its own
// role metadata (with a name-keyed hint table for the default fleet), so the
// canvas and roster render the WHOLE fleet, not a hardcoded subset. An agent
// whose role we cannot place still lands in a sensible fallback lane and is
// never dropped.
//
// A "lane" and a "role" are the same axis here: the canonical engineering
// stages, left to right. The themed display name + role label come from a
// separate theme mapping (see agentThemes.ts); this module is identity-free.

export type WorkflowRole =
  | "triage"
  | "architect"
  | "implement"
  | "review"
  | "ship"
  | "ops";

// Ordered roles (left to right). This is the canonical pipeline order the
// canvas lays out and the order the list view groups by.
export const WORKFLOW_ROLES: readonly WorkflowRole[] = [
  "triage",
  "architect",
  "implement",
  "review",
  "ship",
  "ops",
] as const;

// The lane any agent we cannot place falls into, so an unknown agent the fleet
// reports still appears on the canvas rather than vanishing.
export const FALLBACK_ROLE: WorkflowRole = "ops";

// Plain stage headings used as the lane labels on the canvas. These are stage
// names, not agent names, so they are identical across every roster theme.
export const ROLE_LANE_LABEL: Record<WorkflowRole, string> = {
  triage: "Triage & plan",
  architect: "Architect",
  implement: "Implement",
  review: "Review",
  ship: "Ship",
  ops: "Ops & health",
};

// Canonical handoffs between ROLES (source role -> target role). A real run does
// not traverse every edge, but this is the shape work takes through the fleet.
// Edges are drawn between the agents that occupy each role, so adding an agent
// to a role automatically wires it into the flow without touching a name list.
export const ROLE_EDGES: readonly [WorkflowRole, WorkflowRole][] = [
  ["triage", "architect"],
  ["triage", "implement"],
  ["architect", "implement"],
  ["implement", "review"],
  ["review", "ship"],
  ["ship", "ops"],
];

// Name-keyed role hints for the default fleet. The runtime does not always send
// a machine-readable role for every agent, so we seed the canonical codenames
// here. This is a HINT, not a gate: an agent absent from this table is still
// placed by its reported role metadata or, failing that, the fallback lane.
export const CODENAME_ROLE_HINTS: Record<string, WorkflowRole> = {
  robin: "triage",
  drake: "triage",
  damian: "triage",
  batman: "architect",
  lucius: "implement",
  bane: "implement",
  nightwing: "implement",
  rasalghul: "review",
  automerge: "ship",
  gordon: "ops",
  "fleet-doctor": "ops",
  huntress: "ops",
  cleanup: "ops",
  "agent-cleanup": "ops",
  "memory-harvest": "ops",
  "code-map-refresh": "ops",
  "proof-telemetry": "ops",
};

// Keyword buckets used to infer a role from a free-text role title or purpose
// when the runtime reports one but the codename is unknown to us. Ordered by
// specificity so "reviewer" wins over a generic "engineer" mention.
const ROLE_KEYWORDS: ReadonlyArray<[WorkflowRole, readonly string[]]> = [
  ["review", ["review", "reviewer", "qa", "quality", "approve", "gatekeep"]],
  ["ship", ["ship", "merge", "release", "deploy", "publish"]],
  ["architect", ["architect", "plan", "design", "spec", "lead"]],
  ["triage", ["triage", "intake", "groom", "scope", "manager", "product"]],
  ["implement", ["implement", "develop", "engineer", "build", "code", "fix"]],
  ["ops", ["ops", "health", "monitor", "doctor", "cleanup", "harvest", "infra", "uptime"]],
];

/** Normalize a codename to the short, lowercase form we hint on. */
function shortCodename(codename: string): string {
  return (codename.split(".").pop() || codename).trim().toLowerCase();
}

/**
 * The display fields a workflow node needs, joined from the live roster row by
 * the caller. `role` here is the canonical WorkflowRole; the human role *label*
 * and themed name are layered on separately.
 */
export type RoleSource = {
  codename: string;
  // Free-text role title the runtime reports, if any (e.g. "Senior Developer").
  roleTitle?: string | null;
  // Free-text purpose, used only as a weak secondary signal.
  purpose?: string | null;
};

/**
 * Derive an agent's canonical role from, in priority order:
 *   1. the codename hint table (covers the default fleet exactly), then
 *   2. keyword inference over the reported role title, then
 *   3. keyword inference over the reported purpose, then
 *   4. the fallback lane, so nothing is ever dropped.
 * Pure and deterministic.
 */
export function deriveAgentRole(source: RoleSource): WorkflowRole {
  const short = shortCodename(source.codename);
  const hinted = CODENAME_ROLE_HINTS[short];
  if (hinted) {
    return hinted;
  }
  const fromTitle = inferRoleFromText(source.roleTitle);
  if (fromTitle) {
    return fromTitle;
  }
  const fromPurpose = inferRoleFromText(source.purpose);
  if (fromPurpose) {
    return fromPurpose;
  }
  return FALLBACK_ROLE;
}

function inferRoleFromText(text: string | null | undefined): WorkflowRole | null {
  if (!text) {
    return null;
  }
  const haystack = text.toLowerCase();
  for (const [role, keywords] of ROLE_KEYWORDS) {
    if (keywords.some((keyword) => haystack.includes(keyword))) {
      return role;
    }
  }
  return null;
}

export function roleOrder(role: WorkflowRole): number {
  const index = WORKFLOW_ROLES.indexOf(role);
  return index === -1 ? WORKFLOW_ROLES.length : index;
}
