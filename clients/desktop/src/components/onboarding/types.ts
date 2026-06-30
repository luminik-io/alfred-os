import type { TabKey } from "../../lib/uiTypes";

// The seven steps of the first-run takeover (DESIGN_SPEC section 7). Each is a
// view with a Maya path (guided, no terminal) and a Dev shortcut.
//   welcome -> mental model + "Get started" / "I have a server running"
//   engine  -> detect installed Claude / Codex CLIs (no API keys)
//   github  -> reuse the gh sign-in
//   repos   -> pick repos by name + description
//   team    -> choose the display cast for agents while roles stay stable
//   slack   -> optional approvals in Slack (clearly skippable)
//   request -> the payoff: a real first Request, or a labelled demo
export type OnboardingStepKey =
  | "welcome"
  | "engine"
  | "github"
  | "repos"
  | "team"
  | "slack"
  | "request";

export const ONBOARDING_STEP_ORDER: OnboardingStepKey[] = [
  "welcome",
  "engine",
  "github",
  "repos",
  "team",
  "slack",
  "request",
];

// A step's progress state for the persistent rail.
export type StepProgress = "done" | "active" | "todo";

// A plain inline notice scoped to the onboarding takeover only (it carries no
// cross-surface domain tag, unlike the app-wide ActionNotice).
export type OnboardingNotice = { tone: "ok" | "error"; message: string } | null;

export type GithubAuthFlow = {
  state: "idle" | "starting" | "waiting" | "success" | "timeout" | "error";
  deviceUrl: string | null;
  deviceCode: string | null;
  message: string | null;
  detail: string | null;
};

// What each step reports up to the orchestrator so the rail and the
// continue/skip controls can reflect real readiness.
export type StepOutcome = {
  // True once this step's goal is met (engine ready, gh signed in, repos saved,
  // a request filed / demo seeded). Drives the rail's check and auto-advance.
  complete: boolean;
};

// Shared props every step body receives from the orchestrator.
export type OnboardingStepProps = {
  baseUrl: string;
  // True once the client has a live snapshot (the runtime answered).
  connected: boolean;
  // True in the desktop app, where the native bridge attaches the launch token
  // the mutating routes need. Browser preview is read-only and degrades.
  canMutate: boolean;
  // Advance to the next step (used by primary actions that should move forward).
  onAdvance: () => void;
  // Raise a plain notice on the takeover surface.
  setNotice: (notice: OnboardingNotice) => void;
  // Navigate to a primary surface after the journey completes.
  onSwitch?: (tab: TabKey) => void;
};
