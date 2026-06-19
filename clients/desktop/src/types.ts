export type AgentSummary = {
  codename: string;
  display_name?: string;
  role_title?: string;
  purpose?: string;
  theme?: string;
  theme_label?: string;
  theme_accent?: string;
  last_firing_id: string | null;
  last_run_at: string | null;
  status: "live" | "idle" | "error" | string;
  last_summary: string;
  firings_today: number;
  // Today's firings that ended in an honest failure, including a failure
  // outcome on an otherwise-"complete" firing. Older servers omit it, so it is
  // optional and absent means "not reported", never zero failures.
  failures_today?: number;
  // Paused/running service state now comes from the polled /api/status feed
  // instead of shelling `alfred status --json`. The server reads the same
  // pause marker the CLI writes; `loaded` is the inverse of `paused` because
  // `alfred pause` unloads the scheduler unit and `alfred resume` reloads it.
  // Older servers omit these, so they are optional and default to "running".
  paused?: boolean;
  paused_since?: string | null;
  loaded?: boolean;
};

export type FiringRecord = {
  firing_id: string;
  codename: string;
  started_at: string | null;
  ended_at: string | null;
  status: "ok" | "error" | "running" | "unknown" | string;
  summary: string;
  transcript_path: string | null;
  events_path: string;
  raw_events?: unknown[];
};

export type PlanDraft = {
  plan_id: string;
  title: string;
  status: string;
  parent: string | null;
  affected_repos: string | null;
  updated_at: string | null;
  path: string;
  preview: string;
  content: string;
  source: string;
  readiness_score: number | null;
  readiness_ok: boolean | null;
  revision_count: number;
};

export type ReliabilitySignal = {
  kind?: string;
  title?: string;
  message?: string;
  summary?: string;
  target?: string;
  agent?: string;
  codename?: string;
  repo?: string;
  severity?: string;
  action?: string;
  command?: string;
  reason?: string;
  subtype?: string;
  engine?: string | null;
  count?: number;
  first_seen?: string;
  last_seen?: string;
  latest_summary?: string;
  classification?: string;
  suggested_action?: string;
  evidence?: number[];
  evidence_ids?: number[];
};

// Today's fleet-wide spend rollup, aggregated server-side from the same
// per-agent spend-YYYY-MM-DD.json ledgers `alfred metrics` reads. spend_usd is
// null (not 0) when no ledger exists today, so the cost strip can say "not
// surfaced" instead of fabricating a zero-dollar day. Older servers omit the
// whole block, so it is optional.
export type FleetCostRollup = {
  spend_usd: number | null;
  firings: number;
  successes: number;
  failures: number;
  agents_with_spend: number;
};

export type StatusResponse = {
  agents: AgentSummary[];
  total_today: number;
  reliability: {
    status?: string;
    actions?: ReliabilitySignal[];
    failure_patterns?: ReliabilitySignal[];
    stale_workers?: ReliabilitySignal[];
    promotion_suggestions?: ReliabilitySignal[];
    error?: string;
    errors?: Record<string, string>;
  };
  // Today's aggregate spend + ok/fail counts. Optional: older servers omit it.
  metrics?: FleetCostRollup;
  // The active server-side intake profile. "plain" makes Compose adapt its copy
  // for non-developers. Optional + defaults to "technical" on older servers.
  intake_profile?: "plain" | "technical" | string;
  // Repositories selected in guided setup. Used as planning context so Compose
  // does not ask the operator to retype repo scope Alfred already knows.
  setup_repos?: { selected: string[]; count: number };
};

// One upcoming scheduled run from GET /api/schedule (parsed from agents.conf).
// cron rows carry a computed next_fire_at (local ISO-8601); interval rows carry
// only a cadence string ("every 15m") because the read-only server has no
// trustworthy last-fired anchor to compute the next fire from.
export type ScheduledRun = {
  codename: string;
  role: string;
  display_name?: string;
  role_title?: string;
  purpose?: string;
  theme?: string;
  theme_label?: string;
  theme_accent?: string;
  kind: "interval" | "cron-daily" | "cron-weekly" | string;
  cadence: string;
  next_fire_at: string | null;
  raw_schedule: string;
};

export type ScheduleResponse = {
  runs: ScheduledRun[];
  error?: string;
};

export type ActionsResponse = {
  status: string;
  actions: ReliabilitySignal[];
  failure_patterns: ReliabilitySignal[];
  stale_workers: ReliabilitySignal[];
  promotion_suggestions: ReliabilitySignal[];
  error?: string;
  errors?: Record<string, string>;
};

export type FiringsResponse = {
  rows: FiringRecord[];
};

// assign -> choose Batman or Lucius, then label for that lane
// queue  -> arm an issue for Lucius pickup directly (agent:implement)
// hold   -> take it out of Alfred's reach (do-not-pickup)
// done   -> close the issue using GitHub's native closed state (no new label)
export type QueueAction = "assign" | "queue" | "hold" | "done";
export type AssignmentTargetAgent = "auto" | "batman" | "lucius";

// Response from POST /api/queue (assign, arm, hold, or close an issue).
export type QueueActionResponse = {
  ok: boolean;
  repo: string;
  number: number;
  action: QueueAction;
  target_agent?: AssignmentTargetAgent | string;
  detail: string;
};

export type PlansResponse = {
  rows: PlanDraft[];
};

// GET /api/shipped (see lib/shipped_board.py). A kanban-shaped feed of what the
// fleet is doing, for the Review board's "Shipped" + "Running" lanes. Optional:
// older servers without the route degrade gracefully (shipped stays null).
export type ShippedCard = {
  repo: string;
  number: number | null;
  title: string;
  // Plain-language outcome sentence derived server-side (conventional-commit
  // prefix stripped, sentence case, PR body first line preferred when better).
  // Older servers omit it, so the client falls back to a cleaned title.
  outcome?: string;
  url: string | null;
  author: string | null;
  kind: "pr" | "issue" | string;
  timestamp: string | null;
  age_days: number | null;
  is_draft: boolean;
  labels: string[];
  agent_evidence?: string[];
  demo?: boolean;
};

export type ShippedBoard = {
  // generated_at / errors are absent on the server's hard-failure payload
  // (GitHub/auth error), which still returns 200 with empty columns + `error`.
  generated_at?: string;
  lookback_days: number;
  repos: string[];
  columns: {
    queued: ShippedCard[];
    in_progress: ShippedCard[];
    shipped: ShippedCard[];
  };
  counts: { queued: number; in_progress: number; shipped: number };
  // Per-repo soft failures: the board still built from the repos that worked.
  errors?: string[];
  // Hard failure: the board could not be built at all (auth/gh down). The
  // client must show a "couldn't build" state, not "nothing shipped".
  error?: string;
};

export type TrustedSlackUser = {
  user_id: string;
  sources: string[];
  added_at: string | null;
  added_by: string | null;
  can_remove: boolean;
};

export type TrustedSlackUsersResponse = {
  operator_user_id: string | null;
  users: TrustedSlackUser[];
  state_path: string;
  added?: boolean;
  removed?: boolean;
};

export type MemoryCandidate = {
  id: string;
  codename: string;
  repo: string;
  body: string;
  // Plain one-line statement built server-side from the structured failure
  // fields (agent, subtype, engine, count). Older servers omit it, so the
  // client falls back to the raw body.
  statement?: string;
  tags: string[];
  severity: "info" | "warning" | "blocker" | string;
  source: string;
  source_firing_id: string | null;
  evidence: string;
  confidence: number;
  status: "candidate" | "validated" | "rejected" | "retired" | string;
  created_at: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  review_note?: string | null;
  promoted_lesson_id?: string | null;
};

export type MemoryCandidatesResponse = {
  rows: MemoryCandidate[];
  error?: string;
};

export type MemoryCandidateActionResponse = {
  candidate_id?: string;
  lesson_id?: string;
  status?: string;
  codename?: string;
  repo?: string;
  id?: string;
};

export type FollowupActionResponse = {
  draft_id?: string;
  draft_path?: string;
  archived_path?: string;
};

// Discarding a local planning draft archives it (never hard-deletes) so it is
// recoverable. Idempotent: a second discard returns "already_discarded".
export type DiscardPlanResponse = {
  ok: boolean;
  status: "discarded" | "already_discarded" | string;
  draft_id: string;
  draft_ids?: string[];
  discarded_count?: number;
  archived_path?: string;
  archived_paths?: string[];
  error?: string;
};

export type FilePlanIssueResponse = {
  ok: boolean;
  status: "filed" | "already_filed" | string;
  draft_id: string;
  issue_url?: string;
  repo?: string;
  label?: string;
  detail?: string;
  error?: string;
};

// The go/no-go an operator can record on a genuine Batman plan from the client.
// The server writes the same marker Batman's file poll watches.
export type PlanDecision = "approve" | "decline";

export type PlanDecisionResponse = {
  plan_id: string;
  issue_number: number;
  decision: PlanDecision;
  status: string;
  marker_path: string;
};

export type ComposeDraftFields = {
  title: string;
  problem: string;
  user: string;
  current_behavior: string;
  desired_behavior: string;
  repos: string[];
  acceptance_criteria: string[];
  test_plan: string;
  out_of_scope: string;
  rollout: string;
  open_questions: string;
};

export type ComposeFinding = {
  code: string;
  severity: "error" | "warning" | string;
  message: string;
};

export type ComposeDraftResponse = {
  draft_id: string;
  saved_path: string;
  title: string;
  readiness: { ok: boolean; score: number };
  questions: string[];
  findings: ComposeFinding[];
  summary: string;
  spec_body: string;
  revision_count: number;
  draft: ComposeDraftFields;
};

export type ComposeDraftRequest = {
  text: string;
  draft_id?: string;
  draft?: Partial<ComposeDraftFields>;
  // Repositories Alfred may use as context while planning. This is not confirmed
  // implementation scope unless `draft.repos` also names the target repo(s).
  context_repos?: string[];
};

// One message in the conversational spec-builder transcript. `role` is the
// author of the turn; the server coerces anything other than user/assistant to
// "user" so a transcript can never smuggle a trusted system turn.
export type ConverseMessage = {
  role: "user" | "assistant";
  content: string;
};

// Model-judged readiness for the conversational spec-builder. `score`/`ready`
// are the interrogator's own verdict (the planning rubric only nudges them
// down server-side); `missing` lists what still needs a look, in plain words.
export type ConverseReadiness = {
  score: number;
  ready: boolean;
  missing: string[];
};

// POST /api/compose/converse: one assistant turn of the guided chat. The
// server seeds a repo-grounded interrogator, asks a clarifying question or two,
// co-authors the structured `draft`, and judges readiness. `done` is true only
// once the person has accepted the plan AND it is ready to hand off.
export type ConverseResponse = {
  draft_id: string;
  saved_path: string;
  reply: string;
  readiness: ConverseReadiness;
  done: boolean;
  draft: ComposeDraftFields;
};

export type ConverseRequest = {
  messages: ConverseMessage[];
  draft_id?: string;
  // Legacy name used by the conversational endpoint as grounding context. The
  // server does not treat this as confirmed implementation scope.
  repos?: string[];
  context_repos?: string[];
  // Per-request plain-mode toggle. true forces jargon-free coaching, false
  // forces technical, and omitting it falls back to the server's
  // ALFRED_INTAKE_PROFILE default. Lets a non-developer flip the mode in-app.
  plain?: boolean;
};

export type ConversationControlRequest = {
  text: string;
  actor_user_id?: string;
};

export type ConversationControlResponse = {
  handled: boolean;
  action: string;
  text: string;
  detail: string;
  actor_user_id: string;
};

export type SetupGithub = {
  ok: boolean;
  account: string | null;
  detail: string;
};

export type SetupEngine = {
  name: string;
  installed: boolean;
  path: string | null;
};

export type SetupStatus = {
  github: SetupGithub;
  engines: SetupEngine[];
  engine_ready: boolean;
  repos: { selected: string[]; count: number; keys: string[] };
  demo: { present: boolean };
  ready: boolean;
  error?: string;
};

export type SetupRepo = {
  name_with_owner: string;
  description: string | null;
  is_private: boolean;
  is_fork: boolean;
  updated_at: string | null;
  selected: boolean;
  listed?: boolean;
};

export type SetupReposResponse = {
  repos: SetupRepo[];
  selected: string[];
  error?: string;
};

export type SetupSelectReposResponse = {
  ok: boolean;
  repos: string[];
  env_path: string;
  keys: string[];
};

export type SetupPlaybook = {
  key: string;
  title: string;
  summary: string;
};

export type SetupPlaybooksResponse = {
  playbooks: SetupPlaybook[];
};

export type SetupPlaybookComposeResponse = {
  ok: boolean;
  playbook: string;
  draft_id: string;
  saved_path: string;
  title: string;
  repos: string[];
  readiness: { ok: boolean; score: number };
};

export type SetupDemoResponse = {
  seeded?: boolean;
  cleared?: boolean;
  removed?: boolean;
  counts?: Record<string, number>;
  path?: string;
};

// GET /api/usage (see lib/server/usage.py). REAL subscription headroom from
// local Claude/Codex logs, not the API list-price of tokens. `available` is
// false (with an `error`) only when both sources fail.
export type UsageBlock = {
  start_at: string | null;
  reset_at: string | null;
  minutes_to_reset: number | null;
  is_active: boolean;
  total_tokens: number | null;
  cost_usd: number | null;
  entries: number | null;
  token_counts: {
    input: number | null;
    output: number | null;
    cache_creation: number | null;
    cache_read: number | null;
  };
  projection: {
    total_tokens: number | null;
    total_cost_usd: number | null;
    remaining_minutes: number | null;
  } | null;
  burn_rate: {
    tokens_per_minute: number | null;
    cost_per_hour: number | null;
  } | null;
  models: string[];
};

// One Codex rate-limit window (5h "primary" or weekly "secondary"): the used
// percentage and when it resets. Both fields can be null when the CLI omitted
// them from the rate_limits block.
export type UsageCodexQuotaWindow = {
  used_percent: number | null;
  resets_at: string | null;
};

export type UsageCodex = {
  latest_day: {
    date: string | null;
    total_tokens: number | null;
    cost_usd: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
  } | null;
  totals: {
    total_tokens: number | null;
    cost_usd: number | null;
  } | null;
  // Codex's own rate_limits block, when the session JSONL carried one. primary
  // is the 5-hour window, secondary the weekly window. Absent under older Codex
  // CLIs or when no rate_limits event was written.
  quota?: {
    primary: UsageCodexQuotaWindow | null;
    secondary: UsageCodexQuotaWindow | null;
    plan_type: string | null;
  } | null;
};

export type UsageLimitBucket = {
  utilization: number | null;
  remaining_percent: number | null;
  resets_at: string | null;
  minutes_to_reset: number | null;
};

export type UsageLimits = {
  source: string;
  path?: string;
  updated_at?: string;
  five_hour: UsageLimitBucket | null;
  seven_day: UsageLimitBucket | null;
  seven_day_sonnet: UsageLimitBucket | null;
  seven_day_opus: UsageLimitBucket | null;
  extra_usage: {
    is_enabled: boolean;
    monthly_limit: number | null;
    used_credits: number | null;
    utilization: number | null;
  } | null;
};

// The weekly subscription window. `utilization`/`remaining_percent` are non-null
// only when the OAuth usage cache provided a real seven-day quota; we never
// invent one. When that cache is absent the window falls back to the local
// Claude state file (~/.claude/.claude.json): `resets_at` + `tier` from state,
// and `used_tokens_7d` derived from transcripts, with the percentage left null.
export type UsageWeekly = {
  available?: boolean;
  total_tokens: number | null;
  cost_usd: number | null;
  utilization?: number | null;
  remaining_percent?: number | null;
  resets_at?: string | null;
  minutes_to_reset?: number | null;
  source?: string | null;
  // Trailing-7-day token total derived from transcripts (local-state fallback
  // only). Null when the OAuth quota cache fed the window.
  used_tokens_7d?: number | null;
  // Plan tier from the local state file, e.g. "default_claude_max_20x", plus a
  // short display label, e.g. "Max 20x" (null for unrecognized slugs).
  tier?: string | null;
  tier_label?: string | null;
  unavailable_reason?: string | null;
};

export type UsageResponse = {
  available: boolean;
  kind: string;
  source: string;
  generated_at?: string;
  // The active Claude 5-hour rolling window, or null when no session is live.
  block: UsageBlock | null;
  // Latest-day Codex usage, or null when unavailable.
  codex: UsageCodex | null;
  // Real Claude usage-limit percentages, when a local cache is available.
  limits?: UsageLimits | null;
  // The weekly window. See `UsageWeekly`.
  weekly: UsageWeekly | null;
  // Set when the whole rollup is unavailable.
  error?: string;
  // Per-source failures when one local reader worked and the other did not.
  errors?: { block?: string; codex?: string; limits?: string };
};

export type NativeAction =
  | "dry_run"
  | "run"
  | "pause"
  | "resume"
  | "schedule"
  | "status"
  | "agents"
  | "auth_status"
  | "github_auth_login"
  | "brain_doctor"
  | "redis_status"
  | "redis_sync_preview"
  | "memory_harvest";

export type GithubAuthLoginDetails = {
  device_url: string | null;
  device_code: string | null;
  poll_interval_ms: number;
  timeout_ms: number;
};

// Shape of a single agent entry in `alfred status --json`. The CLI exposes the
// paused/running state that the read-only /api/status endpoint does not, so the
// Fleet Control panel parses this snapshot to reflect live service state.
export type AlfredStatusAgent = {
  agent: string;
  loaded: boolean;
  paused: boolean;
  paused_since: string | null;
  today_consecutive_failures?: number;
  blocked_until?: string | null;
};

export type AlfredStatusJson = {
  ts?: string;
  global?: unknown;
  agents: AlfredStatusAgent[];
};

export type NativeCommandResult = {
  command: string[];
  stdout: string;
  stderr: string;
  status: number | null;
  success: boolean;
  pid: number | null;
  message: string | null;
  github_auth?: GithubAuthLoginDetails | null;
};

export type Snapshot = {
  loadedAt: Date;
  status: StatusResponse;
  actions: ActionsResponse;
  memoryCandidates: MemoryCandidatesResponse;
  firings: FiringRecord[];
  plans: PlanDraft[];
  trustedSlack: TrustedSlackUsersResponse | null;
  // What shipped / is in flight, from GET /api/shipped. Null when the server
  // predates the route (the Review board's Shipped lane then shows a hint).
  shipped: ShippedBoard | null;
  // Upcoming scheduled runs from GET /api/schedule (parsed from agents.conf).
  // Empty when the server predates the route or no schedule is readable; the
  // Running & scheduled lane then shows an honest empty note.
  schedule: ScheduledRun[];
  // Per-section failures from the settled snapshot load. /api/status is the
  // spine and never lands here (its failure rejects the whole load); the other
  // endpoints degrade independently so one outage cannot blank the view.
  degraded?: {
    actions?: string;
    firings?: string;
    plans?: string;
    memoryCandidates?: string;
    trustedSlack?: string;
    shipped?: string;
    schedule?: string;
  };
};
