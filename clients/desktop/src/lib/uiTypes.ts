import type { NativeAction } from "../types";

// The redesigned IA: job-shaped primary destinations plus the agent-depth
// surfaces that live inside the first-class Agents page.
//   review  -> home / heartbeat (Needs you / Running / Shipped + cost strip)
//   board   -> Alfred Kanban Board (issues, PRs, shipped work)
//   compose -> plan work with Alfred (plain-mode spec coach)
//   operator -> Agents page (service control, logs, lessons, plans)
//   setup   -> onboarding + connection
// Agent depth (rendered as subtabs inside Agents):
//   plans / memory / fleet / logs
export type TabKey =
  | "review"
  | "board"
  | "compose"
  | "operator"
  | "setup"
  | "plans"
  | "memory"
  | "fleet"
  | "logs";

// The four agent-depth surfaces, surfaced only inside Agents.
export type OperatorKey = "plans" | "memory" | "fleet" | "logs";

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
