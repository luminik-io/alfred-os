export type AgentSummary = {
  codename: string;
  last_firing_id: string | null;
  last_run_at: string | null;
  status: "live" | "idle" | "error" | string;
  last_summary: string;
  firings_today: number;
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
  title?: string;
  message?: string;
  summary?: string;
  codename?: string;
  repo?: string;
  severity?: string;
  action?: string;
  command?: string;
  reason?: string;
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

export type FollowupActionResponse = {
  draft_id?: string;
  draft_path?: string;
  archived_path?: string;
};

export type NativeAction =
  | "dry_run"
  | "status"
  | "agents"
  | "enabled_agents"
  | "auth_status"
  | "brain_doctor"
  | "redis_status";

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
  firings: FiringRecord[];
  plans: PlanDraft[];
};
