// The human chip vocabulary (DESIGN_SPEC section 5, "Chip vocabulary"). Every
// status the product shows passes through here so a non-engineer can read it
// out loud. Raw jargon ("compose", "needs scope", "34/100", "running") never
// reaches a card face; it is mapped to one of these plain words.
//
// `tone` drives the only status color on a card and maps to the semantic status
// palette (--ok / --working / --attention / --error / --idle). idle is gray and
// never green; an llm-error never reads as ok. That is a correctness contract,
// not a style choice.

import type { FiringRecord, PlanDraft, ShippedCard } from "../types";
import { isErrorStatus, planNeedsAttention } from "./derive";

export type ChipTone = "ok" | "working" | "attention" | "error" | "idle";

export type Chip = {
  label: string;
  tone: ChipTone;
};

// ---------------------------------------------------------------------------
// Plans (lifecycle stage: Plan / approval gate)
// ---------------------------------------------------------------------------

// The human chip for a saved plan. A genuine Batman go/no-go awaiting a sign-off
// is "Needs your go-ahead". Everything else maps from its readiness + status.
export function planChip(plan: PlanDraft): Chip {
  const status = (plan.status || "").toLowerCase();
  if (planNeedsAttention(plan)) {
    return { label: "Needs your go-ahead", tone: "attention" };
  }
  if (status.includes("approved")) return { label: "Approved", tone: "ok" };
  if (status.includes("declined")) return { label: "Declined", tone: "idle" };
  // A working draft (compose / planning / follow-up) reads by readiness: a
  // thin request is "Needs detail", a complete one is "Ready to start".
  if (plan.readiness_ok === false) return { label: "Needs detail", tone: "attention" };
  if (plan.readiness_ok === true) return { label: "Ready to start", tone: "ok" };
  if (status.includes("ready")) return { label: "Ready to start", tone: "ok" };
  return { label: "Needs detail", tone: "attention" };
}

// ---------------------------------------------------------------------------
// Runs (lifecycle stage: Run)
// ---------------------------------------------------------------------------

// Honest run status. `llm-error` and `error` both read as "Hit a snag" and
// carry the error tone; they are never folded into "Done".
export function firingChip(firing: FiringRecord): Chip {
  const status = (firing.status || "").toLowerCase();
  if (status === "running") return { label: "Working now", tone: "working" };
  if (status === "ok") return { label: "Done", tone: "ok" };
  if (isErrorStatus(status)) return { label: "Hit a snag", tone: "error" };
  return { label: "Resting", tone: "idle" };
}

// ---------------------------------------------------------------------------
// Agents (the roster, not a stage). idle is gray, never green or "Running".
// ---------------------------------------------------------------------------

export function agentChip(agent: { status?: string; paused?: boolean }): Chip {
  const status = (agent.status || "").toLowerCase();
  if (isErrorStatus(status)) return { label: "Hit a snag", tone: "error" };
  if (status === "live") return { label: "Working now", tone: "working" };
  if (agent.paused) return { label: "Paused", tone: "idle" };
  // idle but scheduled: it is resting, not running, and never shows green.
  return { label: "Resting", tone: "idle" };
}

// ---------------------------------------------------------------------------
// Board cards (Queued / Working / Shipped) from GET /api/shipped.
// ---------------------------------------------------------------------------

export type BoardColumn = "queued" | "in_progress" | "shipped";

export function boardCardChip(card: ShippedCard, column: BoardColumn): Chip {
  if (card.demo) return { label: "Sample", tone: "idle" };
  if (column === "shipped") return { label: "Shipped", tone: "ok" };
  if (column === "in_progress") return { label: "Working now", tone: "working" };
  // Queued column: an armed issue is "Queued"; a parked one is "On hold".
  const labels = (card.labels || []).map((label) => label.toLowerCase());
  if (labels.includes("do-not-pickup")) return { label: "On hold", tone: "idle" };
  return { label: "Queued", tone: "working" };
}

// Map a tone to its CSS modifier so chips render with the one semantic color.
export function chipToneClass(tone: ChipTone): string {
  return `alfred-chip--${tone}`;
}

// Plain-language agent attribution for a shipped/board card ("Lucius shipped
// this"). Reads the author, labels, and agent evidence for a known codename.
export function agentForShipped(card: ShippedCard): string | null {
  const tokens = [
    card.author || "",
    ...(card.labels || []),
    ...(card.agent_evidence || []),
  ].map((token) => token.toLowerCase());
  if (tokens.some((t) => t.includes("batman") || t.includes("agent:large-feature"))) {
    return "Batman";
  }
  if (tokens.some((t) => t.includes("lucius") || t.includes("agent:implement"))) {
    return "Lucius";
  }
  if (tokens.some((t) => t.includes("nightwing"))) return "Nightwing";
  if (tokens.some((t) => t.includes("damian"))) return "Damian";
  if (tokens.some((t) => t.includes("bane"))) return "Bane";
  if (tokens.some((t) => t.includes("rasalghul") || t.includes("ra's al ghul"))) {
    return "Ra's al Ghul";
  }
  return null;
}

// Short repo name (last path segment): `repo`, not `acme-org/repo`.
export function repoShortName(repo: string): string {
  const slash = repo.lastIndexOf("/");
  return slash >= 0 ? repo.slice(slash + 1) : repo;
}

// Split a comma/space-separated affected-repos string into short names.
export function splitRepos(value: string | null | undefined): string[] {
  if (!value) return [];
  return value
    .split(/[,\s]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map(repoShortName);
}
