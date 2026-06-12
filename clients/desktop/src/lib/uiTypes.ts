import type { NativeAction } from "../types";

// The lifecycle IA (DESIGN_SPEC section 4): five primary destinations, each a
// view of the one object, plus Settings behind the top-bar gear.
//   home     -> the morning-after story (Needs you / What shipped / Hit a snag)
//   compose  -> Ask: turn plain words into a Request (plain-mode spec coach)
//   pipeline -> the lifecycle board: Plans awaiting you, Queued, Working, Shipped
//   fleet    -> agent depth: roster, schedule, activity, capacity, reliability
//   lessons  -> what the fleet learned (the promotion pipeline)
//   settings -> connection, repos, Slack, diagnostics (gear, not the rail)
// Internal-only surface, rendered inside Fleet:
//   logs     -> the live activity tail for one agent
export type TabKey =
  | "home"
  | "compose"
  | "pipeline"
  | "fleet"
  | "lessons"
  | "settings"
  | "logs";

// The depth surfaces grouped inside Fleet.
export type OperatorKey = "fleet" | "logs";

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
