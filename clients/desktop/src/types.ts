export type AgentSummary = {
  codename: string;
  last_firing_id: string | null;
  last_run_at: string | null;
  status: "live" | "idle" | "error" | string;
  last_summary: string;
  firings_today: number;
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

export type PlansResponse = {
  rows: PlanDraft[];
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
};

export type NativeAction =
  | "dry_run"
  | "run"
  | "pause"
  | "resume"
  | "status"
  | "agents"
  | "auth_status"
  | "brain_doctor"
  | "redis_status"
  | "redis_sync_preview"
  | "memory_harvest";

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
};

export type Snapshot = {
  loadedAt: Date;
  status: StatusResponse;
  actions: ActionsResponse;
  memoryCandidates: MemoryCandidatesResponse;
  firings: FiringRecord[];
  plans: PlanDraft[];
  trustedSlack: TrustedSlackUsersResponse | null;
  // Per-section failures from the settled snapshot load. /api/status is the
  // spine and never lands here (its failure rejects the whole load); the other
  // three endpoints degrade independently so one outage cannot blank the view.
  degraded?: {
    actions?: string;
    firings?: string;
    plans?: string;
    memoryCandidates?: string;
    trustedSlack?: string;
  };
};
