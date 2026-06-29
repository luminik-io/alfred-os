import { friendlyTime, plural, titleCase } from "../format";
import { parseIssueRef } from "./links";
import type {
  FiringRecord,
  PlanDraft,
  ScheduledRun,
  ShippedBoard,
  ShippedCard,
  Snapshot,
} from "../types";
import type {
  AttentionItem,
  RequestThreadModel,
  RequestThreadStep,
  ThreadStepState,
} from "./uiTypes";

// Any failure status reads as a snag. Covers plain "error" and the
// provider-level "llm-error" the runtime emits when a model call fails. Lives
// here (not chips.ts) so cost/health derivation can stay honest without a
// circular import.
export function isErrorStatus(status: string | null | undefined): boolean {
  const value = (status || "").toLowerCase();
  return value === "error" || value === "llm-error" || value === "llm_error";
}

// ---------------------------------------------------------------------------
// Needs you: the calm, client-owned decisions.
//
// The ONE action the client owns is the Alfred-native plan/spec sign-off
// BEFORE work starts (the human-in-the-loop gate), plus memory review.
// Reliability signals reach the operator through the Activity feed
// (lib/notifications.ts), which links each one to its agent's latest run.
// ---------------------------------------------------------------------------

export function buildNeedsYou(snapshot: Snapshot | null): AttentionItem[] {
  if (!snapshot) {
    return [
      {
        id: "connect",
        label: "Setup",
        title: "Connect to the local Alfred server",
        detail: "Start alfred serve so the client can read local state.",
        tone: "warn",
        command: "alfred serve",
        targetTab: "settings",
        icon: "setup",
      },
    ];
  }

  const items: AttentionItem[] = [];
  const waitingPlans = snapshot.plans.filter((plan) => planNeedsAttention(plan));
  if (waitingPlans.length === 1) {
    const plan = waitingPlans[0];
    items.push({
      id: `plan-${plan.plan_id}`,
      label: titleCase(plan.status || "plan"),
      title: plan.title,
      detail:
        plan.preview ||
        plan.affected_repos ||
        "Review the plan before Alfred starts the work.",
      tone: plan.status.toLowerCase().includes("question") ? "warn" : "info",
      targetTab: "pipeline",
      icon: "plan",
      planId: plan.plan_id,
    });
  } else if (waitingPlans.length > 1) {
    items.push({
      id: "plans-review",
      label: "Planning queue",
      title: `${plural(waitingPlans.length, "plan")} waiting`,
      detail: waitingPlans
        .slice(0, 3)
        .map((plan) => plan.title)
        .join("; "),
      tone: waitingPlans.some((plan) => plan.status.toLowerCase().includes("question"))
        ? "warn"
        : "info",
      targetTab: "pipeline",
      icon: "plan",
    });
  }

  const candidates = snapshot.memoryCandidates.rows || [];
  if (candidates.length) {
    const repos = Array.from(
      new Set(candidates.map((candidate) => candidate.repo).filter(Boolean)),
    );
    items.push({
      id: "memory-review",
      label: "Review queue",
      title: `${plural(candidates.length, "memory candidate")} ready`,
      detail: repos.length
        ? `Review before promotion: ${repos.slice(0, 3).join(", ")}${repos.length > 3 ? ", ..." : ""}.`
        : "Review candidates before they enter recall.",
      tone: candidates.some((candidate) => candidate.severity === "blocker")
        ? "error"
        : "info",
      targetTab: "lessons",
      icon: "memory",
    });
  } else {
    const suggestions = snapshot.actions.promotion_suggestions || [];
    if (suggestions.length) {
      items.push({
        id: "memory-suggestions",
        label: "Review queue",
        title: `${plural(suggestions.length, "memory suggestion")} ready`,
        detail: "Review suggested memory updates before they are saved.",
        tone: "info",
        targetTab: "lessons",
        icon: "memory",
      });
    }
  }

  return items.slice(0, 6);
}

// ---------------------------------------------------------------------------
// Running & scheduled
// ---------------------------------------------------------------------------

export type RunningState = {
  running: FiringRecord[];
  // Upcoming scheduled runs from GET /api/schedule (parsed from agents.conf).
  hasUpcoming: boolean;
  upcoming: ScheduledRun[];
};

export function buildRunning(snapshot: Snapshot | null): RunningState {
  const running = (snapshot?.firings || []).filter(
    (firing) => firing.status === "running",
  );
  // cron rows carry a concrete next_fire_at and sort soonest-first (the server
  // already ordered them); interval rows have no trustworthy next-fire and show
  // a cadence. Cap so the lane stays glanceable.
  const upcoming = (snapshot?.schedule || []).slice(0, 8);
  return { running, hasUpcoming: upcoming.length > 0, upcoming };
}

// ---------------------------------------------------------------------------
// Shipped digest: a plain-English "what Alfred shipped" line per merged PR.
// ---------------------------------------------------------------------------

export type ShippedDigestItem = {
  card: ShippedCard;
  agent: string | null;
  what: string;
  why: string;
};

export function buildShippedDigest(board: ShippedBoard | null): ShippedDigestItem[] {
  const cards = board?.columns.shipped || [];
  return cards.map((card) => ({
    card,
    agent: shippedAgent(card),
    what: shippedWhat(card),
    why: shippedWhy(card),
  }));
}

// Plain words for what the PR did. Prefer the server-derived outcome sentence;
// fall back to a cleaned title when no outcome is present.
function shippedWhat(card: ShippedCard): string {
  const serverOutcome = (card.outcome || "").trim();
  if (serverOutcome) return serverOutcome;
  const title = (card.title || "").trim();
  if (!title) return "Shipped a change to this repo.";
  // A title is usually already imperative ("Add X", "Fix Y"); present it as a
  // sentence so a non-developer reads an outcome, not a commit subject.
  const cleaned = title.replace(/^\s*(feat|fix|chore|docs|refactor|test)(\([^)]*\))?:\s*/i, "");
  const sentence = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
  return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`;
}

function shippedWhy(card: ShippedCard): string {
  const repo = repoShortName(card.repo);
  const when = card.timestamp ? friendlyTime(card.timestamp) : "recently";
  // The agent is already shown as a badge on the card, so the sentence states
  // the outcome without repeating the agent name (no repeated info per card).
  if (shippedAgent(card)) {
    return `Shipped and merged into ${repo} ${when}.`;
  }
  const who = card.author ? `${card.author} ` : "";
  return `${who}merged into ${repo} ${when}.`;
}

function repoShortName(repo: string): string {
  const slash = repo.lastIndexOf("/");
  return slash >= 0 ? repo.slice(slash + 1) : repo;
}

function shippedAgent(card: ShippedCard): string | null {
  const tokens = [
    card.author || "",
    ...(card.labels || []),
    ...(card.agent_evidence || []),
  ].map((token) => token.toLowerCase());

  if (tokens.some((token) => token.includes("batman") || token.includes("agent:large-feature"))) {
    return "Batman";
  }
  if (tokens.some((token) => token.includes("lucius") || token.includes("agent:implement"))) {
    return "Lucius";
  }
  if (tokens.some((token) => token.includes("nightwing"))) return "Nightwing";
  if (tokens.some((token) => token.includes("damian"))) return "Damian";
  if (tokens.some((token) => token.includes("bane"))) return "Bane";
  if (tokens.some((token) => token.includes("rasalghul") || token.includes("ra's al ghul"))) {
    return "Ra's al Ghul";
  }
  return null;
}

// ---------------------------------------------------------------------------
// Cost / health strip
// ---------------------------------------------------------------------------

export type CostHealth = {
  // Tonight's run count (the snapshot exposes total_today, not a window split).
  runsToday: number;
  succeeded: number;
  failed: number;
  // Today's aggregate spend, rolled up server-side from the per-agent spend
  // ledgers (status.metrics). null means "no cost data surfaced" (no ledger
  // today), not $0.
  spendUsd: number | null;
  // True when spend came from the server's today rollup (real ledgers) rather
  // than the firings fallback, so the strip can label it precisely.
  spendIsTodayRollup: boolean;
  // Last run per repo is not derivable client-side: firings carry a codename,
  // not a repo, and there is no per-repo last-run field. Flagged, surfaced as
  // last-run per agent instead from the firings we do have.
  lastRunByAgent: Array<{ codename: string; at: string | null; status: string }>;
};

// A firing may or may not carry a cost field depending on the runtime build.
// Read it defensively without widening the FiringRecord type with a server
// field we have not confirmed exists on every build.
function firingCost(firing: FiringRecord): number | null {
  const value = (firing as unknown as { cost_usd?: unknown }).cost_usd;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function buildCostHealth(snapshot: Snapshot | null): CostHealth {
  const firings = snapshot?.firings || [];
  // Prefer the server's today rollup (real spend ledgers) when present; fall
  // back to whatever cost the visible firings carry.
  const rollup = snapshot?.status.metrics;
  let succeeded = 0;
  let failed = 0;
  let firingSpend = 0;
  let sawFiringCost = false;
  for (const firing of firings) {
    if (firing.status === "ok") succeeded += 1;
    // An llm-error is a real failure, not a quiet success. Count it honestly so
    // the Home rollup never treats an errored run as fine.
    if (isErrorStatus(firing.status)) failed += 1;
    const cost = firingCost(firing);
    if (cost !== null) {
      firingSpend += cost;
      sawFiringCost = true;
    }
  }
  const agents = snapshot?.status.agents || [];
  const agentFailures = agents.reduce(
    (total, agent) =>
      typeof agent.failures_today === "number"
        ? total + Math.max(0, agent.failures_today)
        : total,
    0,
  );
  const hasAgentFailureCounts = agents.some(
    (agent) => typeof agent.failures_today === "number",
  );
  const byAgent = new Map<string, { at: string | null; status: string }>();
  for (const firing of firings) {
    if (!byAgent.has(firing.codename)) {
      byAgent.set(firing.codename, {
        at: firing.started_at,
        status: firing.status,
      });
    }
  }
  const hasRollup = rollup !== undefined;
  const spendUsd = hasRollup
    ? rollup.spend_usd
    : sawFiringCost
      ? firingSpend
      : null;
  return {
    runsToday: snapshot?.status.total_today ?? 0,
    // The rollup's ok/fail counts cover the whole day; the firings fallback
    // only covers the visible window, so prefer the rollup when present. The
    // agent event-log count is newer and catches completed runs with failure
    // outcomes that spend ledgers can miss, so let it raise the displayed count.
    succeeded: hasRollup ? rollup.successes : succeeded,
    failed: hasAgentFailureCounts
      ? Math.max(agentFailures, hasRollup ? rollup.failures : failed)
      : hasRollup
        ? rollup.failures
        : failed,
    spendUsd,
    spendIsTodayRollup: hasRollup,
    lastRunByAgent: Array.from(byAgent.entries()).map(([codename, info]) => ({
      codename,
      at: info.at,
      status: info.status,
    })),
  };
}

// ---------------------------------------------------------------------------
// Request lifecycle threads
//
// Correlate a request across stages by its issue ref (repo#number). The
// snapshot does not yet carry a stable cross-stage id (compose draft_id ->
// plan_id -> firing -> PR), so the stepper is best-effort: a stage with no
// evidence renders as "missing", and `correlationApproximate` is set so the UI
// can say so. This is a flagged backend gap, not invented server state.
// ---------------------------------------------------------------------------

const STEP_LABELS: Record<RequestThreadStep["key"], string> = {
  intake: "Intake",
  plan: "Plan",
  queued: "Queued",
  building: "Building",
  shipped: "Shipped",
};

function step(
  key: RequestThreadStep["key"],
  state: ThreadStepState,
  detail?: string,
): RequestThreadStep {
  return { key, label: STEP_LABELS[key], state, detail };
}

// Build a thread for a single shipped/in-flight/queued board card. The board
// is the only place that carries a real cross-stage anchor (the issue/PR ref).
export function threadForCard(
  card: ShippedCard,
  board: ShippedBoard | null,
): RequestThreadModel {
  const repo = card.repo;
  const number = card.number ?? null;
  const queued = (board?.columns.queued || []).some(
    (c) => c.repo === repo && c.number === number && c.kind === "issue",
  );
  const inProgress = (board?.columns.in_progress || []).some(
    (c) => c.repo === repo && c.number === number,
  );
  const shipped = (board?.columns.shipped || []).some(
    (c) => c.repo === repo && c.number === number,
  );

  // Lifecycle is monotonic: a shipped card implies it was built and queued at
  // some point even if those columns no longer list it.
  const steps: RequestThreadStep[] = [
    step("intake", "done"),
    // The Alfred-native plan sign-off is not represented on the board, so we
    // cannot confirm it from card data alone: mark it missing (flagged gap).
    step("plan", "missing", "Plan sign-off is not linked to this issue yet."),
    step("queued", queued ? "active" : shipped || inProgress ? "done" : "pending"),
    step("building", inProgress ? "active" : shipped ? "done" : "pending"),
    step("shipped", shipped ? "done" : "pending"),
  ];

  return {
    id: number ? `${repo}#${number}` : `${repo}:${card.title}`,
    title: card.title || "Untitled request",
    repo,
    repos: [repo],
    issueNumber: number,
    url: card.url,
    steps,
    correlationApproximate: true,
  };
}

// Build a thread for a freshly composed draft. A compose result has no board
// presence yet (no issue exists), so only Intake is real; everything after the
// plan sign-off is pending until the backend links draft_id to a plan/issue.
export function threadForCompose(input: {
  draftId?: string | null;
  title: string;
  repos?: string[];
  ready?: boolean;
}): RequestThreadModel {
  const repos = uniqueRepos(input.repos || []);
  const repoRef = repos[0] || null;
  const steps: RequestThreadStep[] = [
    step("intake", "done", "You described the work."),
    step(
      "plan",
      "active",
      input.ready
        ? "Alfred has a draft ready for your sign-off."
        : "Alfred is shaping the plan; answer the open questions to sign off.",
    ),
    step("queued", "pending"),
    step("building", "pending"),
    step("shipped", "pending"),
  ];
  return {
    id: input.draftId ? `draft:${input.draftId}` : `draft:${input.title}`,
    title: input.title || "New request",
    repo: repoRef,
    repos,
    draftId: input.draftId,
    steps,
    // There is no stable id tying a compose draft to a later plan/issue/PR yet.
    correlationApproximate: true,
  };
}

function parseRepoSlug(value: string): string | null {
  const ref = parseIssueRef(`${value}#1`);
  if (ref) return ref.repo;
  const slug = value.trim().match(/^[\w.-]+\/[\w.-]+$/);
  return slug ? slug[0] : null;
}

function uniqueRepos(values: string[]): string[] {
  const seen = new Set<string>();
  const repos: string[] = [];
  for (const value of values) {
    const repo = parseRepoSlug(value);
    if (!repo) continue;
    const key = repo.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    repos.push(repo);
  }
  return repos;
}

// Pick the request threads worth surfacing in Review: in-flight and queued
// board cards (the live work), capped so the lane stays glanceable.
export function buildActiveThreads(board: ShippedBoard | null, limit = 6): RequestThreadModel[] {
  if (!board) return [];
  const cards = [...board.columns.in_progress, ...board.columns.queued];
  return cards.slice(0, limit).map((card) => threadForCard(card, board));
}

// A plan is a genuine go/no-go decision only when Batman is awaiting a sign-off
// on it. That is exactly `source === "batman"`: those are the plans posted for
// approval (Slack reaction or the in-app approve/decline). Compose working
// drafts (`source` "compose"/"planning") and stale Slack follow-ups
// (`source === "followup"`) are NOT decisions waiting on the operator before
// work starts, so they must not inflate the Needs-you count. A plan whose
// status already reads approved/declined has been decided and drops out too.
export function planNeedsAttention(plan: PlanDraft): boolean {
  if (plan.source !== "batman") return false;
  const status = plan.status.toLowerCase();
  if (status.includes("approved") || status.includes("declined")) return false;
  return (
    status.includes("draft") ||
    status.includes("await") ||
    status.includes("follow") ||
    status.includes("question") ||
    status.includes("blocked")
  );
}

// ---------------------------------------------------------------------------
// Client-side draft dedupe + low-signal floor (issue 314, Phase 1 item 7).
//
// The server can emit several identical drafts (same title + repos) and a junk
// sub-threshold draft. Until the backend dedupes at the source (Phase 2), the
// client collapses identical drafts to the newest revision and tucks low-signal
// drafts behind a disclosure so they never crowd the Pipeline.
// ---------------------------------------------------------------------------

// Drafts at or below this readiness are "low signal" and hidden behind a
// disclosure. The screenshot junk ("Hi", readiness 34/100) sits below this.
export const READINESS_FLOOR = 40;

export type DedupedPlan = {
  plan: PlanDraft;
  // How many drafts collapsed into this one (1 = no duplicates).
  revisions: number;
};

const PLACEHOLDER_PLAN_TITLE = "alfred planning draft";
const DEDUPEABLE_PLAN_SOURCES = new Set(["compose", "planning", "slack"]);

function planDedupeKey(plan: PlanDraft): string | null {
  if (!DEDUPEABLE_PLAN_SOURCES.has(plan.source)) return null;
  if (plan.parent) return null;
  const title = (plan.title || "").trim().toLowerCase();
  if (!title || title === PLACEHOLDER_PLAN_TITLE) return null;
  const repos = (plan.affected_repos || "")
    .split(/[,\s]+/)
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean)
    .sort()
    .join(",");
  if (!repos) return null;
  return `${title}::${repos}`;
}

function planTimeValue(plan: PlanDraft): number {
  const parsed = plan.updated_at ? new Date(plan.updated_at).getTime() : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

// A low-signal draft is a working draft (not a genuine go/no-go) whose readiness
// is known and at or below the floor. A plan that needs your go-ahead is never
// low signal, no matter its score: that is a decision you must see.
export function isLowSignalPlan(plan: PlanDraft): boolean {
  if (planNeedsAttention(plan)) return false;
  if (plan.readiness_score === null) return false;
  return plan.readiness_score <= READINESS_FLOOR;
}

// Total visible revisions represented by a single row. Compose persists
// ``revision_count`` as an actual count (len(revisions)), while server-side
// duplicate folding also returns a count ready for display. A row with no count
// still represents one visible draft.
function rowGroupSize(plan: PlanDraft): number {
  return Math.max(1, plan.revision_count ?? 0);
}

// Collapse identical drafts (same title + repos) to their newest revision,
// carrying a revision count. The server already collapses duplicates, so this
// is primarily a defensive second pass; it seeds each group's size from the
// server's revision_count and adds any rows the server did not collapse.
// Order is preserved by first appearance.
export function dedupePlans(plans: PlanDraft[]): DedupedPlan[] {
  const byKey = new Map<string, DedupedPlan>();
  const out: DedupedPlan[] = [];
  for (const plan of plans) {
    const key = planDedupeKey(plan);
    if (!key) {
      out.push({ plan, revisions: rowGroupSize(plan) });
      continue;
    }
    const existing = byKey.get(key);
    if (!existing) {
      const row = { plan, revisions: rowGroupSize(plan) };
      byKey.set(key, row);
      out.push(row);
      continue;
    }
    existing.revisions += rowGroupSize(plan);
    // Keep the newest by updated_at; fall back to the explicit revision_count.
    const newer =
      planTimeValue(plan) > planTimeValue(existing.plan) ||
      (plan.revision_count ?? 0) > (existing.plan.revision_count ?? 0);
    if (newer) existing.plan = plan;
  }
  return out;
}
