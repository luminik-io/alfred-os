import type { NativeAction } from "../types";

// Job-shaped IA. Internal keys preserve legacy deep links; labels are product
// copy and live in primaryTabs.ts.
//   home     -> Inbox: needs you, live activity, shipped PRs
//   compose  -> Ask: conversational request intake
//   pipeline -> Work: plans, queue, PR lifecycle, shipped evidence
//   fleet    -> Agents: roster, schedules, activity, lessons
//   lessons  -> Lessons: what the fleet learned, rendered inside Agents
// Internal aliases:
//   settings -> Setup: onboarding, repos, collaborators, diagnostics
//   logs     -> the activity tail for one agent, rendered inside Fleet
export type TabKey =
  | "home"
  | "compose"
  | "pipeline"
  | "fleet"
  | "lessons"
  | "settings"
  | "logs";

// The depth surfaces grouped inside Fleet.
export type OperatorKey = "fleet" | "logs" | "lessons";

export type SetupMode = "guided" | "advanced";

export type FollowupAction = "convert" | "handled";

// The surface an inline action notice belongs to. A notice is rendered only on
// its originating surface so e.g. promoting a lesson never flashes a banner on
// Plans / Board / Setup. `null` notice = nothing to show.
export type NoticeDomain = "plans" | "board" | "memory" | "setup";

export type ActionNotice =
  | { tone: "ok" | "error"; message: string; domain: NoticeDomain }
  | null;

export type NativeActionRequest = {
  action: NativeAction;
  target?: string;
  cadence?: string;
  refreshAfter?: boolean;
};

export type AttentionItem = {
  id: string;
  label: string;
  title: string;
  detail: string;
  tone: "ok" | "warn" | "error" | "info";
  command?: string;
  href?: string;
  targetTab?: TabKey;
  icon: "plan" | "run" | "memory" | "setup";
  // Set for a single genuine Batman go/no-go plan awaiting a sign-off, so the
  // Needs-you card can offer in-place Approve / Decline. Absent for grouped
  // counts, memory, and inspection items (those route to a surface instead).
  planId?: string;
};

// One step in the request lifecycle stepper. `state` is lit/dimmed by which
// data the snapshot actually exposes; "missing" means the backend cannot yet
// confirm this stage (a flagged correlation gap, never fabricated).
export type ThreadStepState = "done" | "active" | "pending" | "missing";

export type ThreadStepKey = "intake" | "plan" | "queued" | "building" | "shipped";

export type RequestThreadStep = {
  key: ThreadStepKey;
  label: string;
  state: ThreadStepState;
  detail?: string;
};

// A single request followed across stages. Correlated by the issue ref
// (repo#number) and/or the compose draft_id where a stable cross-stage id
// exists; where it does not, the thread degrades gracefully and the unknown
// stages render as "missing" rather than inventing server state.
export type RequestThreadModel = {
  id: string;
  title: string;
  repo?: string | null;
  repos?: string[];
  issueNumber?: number | null;
  draftId?: string | null;
  url?: string | null;
  steps: RequestThreadStep[];
  // True when no stable cross-stage id ties the stages together yet, so the
  // stepper is best-effort. The UI surfaces this so the gap is honest.
  correlationApproximate?: boolean;
};
