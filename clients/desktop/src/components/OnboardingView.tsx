import {
  ArrowLeft,
  ArrowRight,
  GitPullRequest,
  ListChecks,
  MessageCircle,
  Plug,
  Settings2,
  Sparkles,
  TerminalSquare,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { errorDetail, loadSetupStatus, supportsNativeActions } from "../api";
import { pollGithubAuthStatus } from "../lib/githubAuth";
import {
  type CustomRosterNames,
  editableAgents,
  resolveThemedIdentity,
  rosterThemeBlurb,
  rosterThemeLabel,
  type RosterThemeId,
} from "../lib/agentThemes";
import type { NativeActionRequest, TabKey } from "../lib/uiTypes";
import type { NativeCommandResult, SetupStatus } from "../types";
import { EngineStep } from "./onboarding/EngineStep";
import { FirstRequestStep } from "./onboarding/FirstRequestStep";
import { GitHubStep } from "./onboarding/GitHubStep";
import { ReposStep } from "./onboarding/ReposStep";
import { SlackStep } from "./onboarding/SlackStep";
import { StepFrame } from "./onboarding/StepFrame";
import { Stepper, type StepperItem } from "./onboarding/Stepper";
import {
  ONBOARDING_STEP_ORDER,
  type GithubAuthFlow,
  type OnboardingNotice,
  type OnboardingStepKey,
  type StepProgress,
} from "./onboarding/types";
import { WelcomeStep } from "./onboarding/WelcomeStep";
import { RosterThemePicker } from "./RosterThemePicker";
import { Button, Card, CardContent } from "./ui";
import { cn } from "@/lib/utils";

/**
 * The setup takeover (DESIGN_SPEC section 7), built as a clean stepper. It
 * handles both true first-run setup and returning installs that need a quick
 * review. A seven-step journey can be completed without a terminal, ending on a
 * populated Home via a real first request or a clearly-labelled demo:
 *
 *   0 Welcome        mental model + two doors (Get started / I have a server)
 *   1 Tools          detect Claude / Codex (no API keys)
 *   2 GitHub         reuse the gh sign-in (auto-advance when signed in)
 *   3 Repositories   pick by name + description (private badge)
 *   4 Team           pick roster theme / custom names
 *   5 Slack          optional approvals, clearly skippable
 *   6 First request  a real Request, or a labelled sample
 *
 * The journey lives inside a single glass shell that floats over the ambient
 * base. A persistent, minimal numbered Stepper sits at the top (current / done /
 * upcoming), one decision lives in the centered column below it, and a Back /
 * Continue footer (with a first-class per-step Skip for the Dev persona) closes
 * the shell. Steel-violet accents only the single primary CTA per step;
 * everything data-shaped (repo list, engine probe) stays flat.
 *
 * Every step is skippable for the Dev persona, has honest empty/error states,
 * an Enter-key continue flow (suppressed inside text fields), and auto-advance
 * on a detected GitHub sign-in / fully-ready Tools step. The mutating steps
 * (repos, playbook, demo, Slack) need the per-launch token the native bridge
 * attaches; the browser preview cannot, so it degrades to a clear read-only note
 * with copy-paste fallback. The read steps work either way.
 *
 * "Advanced setup" (onOpenConnection) hands off to SetupView for the non-takeover
 * connection + diagnostics surface, which onboarding and Settings share.
 */

type StepMeta = {
  key: OnboardingStepKey;
  index: number;
  title: string;
  railTitle: string;
  blurb: string;
  icon: LucideIcon;
  optional: boolean;
};

const IDLE_GITHUB_AUTH_FLOW: GithubAuthFlow = {
  state: "idle",
  deviceUrl: null,
  deviceCode: null,
  message: null,
  detail: null,
};

const ROSTER_PREVIEW_AGENTS = (() => {
  const seenRoles = new Set<string>();
  const agents: ReturnType<typeof editableAgents> = [];
  for (const agent of editableAgents()) {
    if (seenRoles.has(agent.role)) continue;
    seenRoles.add(agent.role);
    agents.push(agent);
    if (agents.length === 4) break;
  }
  return agents;
})();

const GITHUB_DEVICE_URL = "https://github.com/login/device";

const STEP_META: Record<OnboardingStepKey, Omit<StepMeta, "index">> = {
  welcome: {
    key: "welcome",
    title: "Welcome to Alfred",
    railTitle: "Welcome",
    blurb: "A local fleet that ships pull requests while you stay in control.",
    icon: Sparkles,
    optional: false,
  },
  engine: {
    key: "engine",
    title: "Connect your tools",
    railTitle: "Tools",
    blurb: "Alfred checks Claude Code and Codex on this Mac. No API keys.",
    icon: TerminalSquare,
    optional: false,
  },
  github: {
    key: "github",
    title: "Connect GitHub",
    railTitle: "GitHub",
    blurb: "Alfred reuses your GitHub sign-in.",
    icon: GitPullRequest,
    optional: false,
  },
  repos: {
    key: "repos",
    title: "Choose repositories",
    railTitle: "Repositories",
    blurb: "Pick the projects Alfred may work in.",
    icon: Plug,
    optional: false,
  },
  team: {
    key: "team",
    title: "Name your team",
    railTitle: "Team",
    blurb: "Pick visible names for the same senior-engineering roles.",
    icon: Users,
    optional: false,
  },
  slack: {
    key: "slack",
    title: "Connect Slack",
    railTitle: "Slack",
    blurb: "Optional. Approvals and questions in Slack too.",
    icon: MessageCircle,
    optional: true,
  },
  request: {
    key: "request",
    title: "Your first request",
    railTitle: "First request",
    blurb: "End on a real result, or a sample to look at first.",
    icon: ListChecks,
    optional: false,
  },
};

export function OnboardingView({
  baseUrl,
  loading,
  connected,
  canRun,
  nativeBusy,
  nativeResult,
  rosterTheme,
  customNames,
  rosterSaveError,
  onConnectServer,
  onStartRuntime,
  onRunLocalAction,
  onRosterThemeChange,
  onEditCustomTheme,
  onOpenConnection,
  onSwitch,
  onRefreshBoard,
}: {
  baseUrl: string;
  loading: boolean;
  /** True once the client has a live snapshot (the runtime answered). */
  connected: boolean;
  canRun: boolean;
  nativeBusy: string | null;
  nativeResult: NativeCommandResult | null;
  rosterTheme: RosterThemeId;
  customNames: CustomRosterNames;
  rosterSaveError: string | null;
  onConnectServer: (url: string) => void;
  onStartRuntime: () => void;
  onRunLocalAction: (request: NativeActionRequest) => Promise<NativeCommandResult | null>;
  onRosterThemeChange: (next: RosterThemeId) => void;
  onEditCustomTheme: () => void;
  /** Jump to the full connection + diagnostics surface (the advanced handoff). */
  onOpenConnection: () => void;
  /** Navigate to another primary surface (e.g. Inbox, Ask) after an action. */
  onSwitch?: (tab: TabKey) => void;
  onRefreshBoard?: (options?: { demo?: boolean }) => Promise<void> | void;
}) {
  // The mutating steps need the per-launch token the native bridge attaches; the
  // browser preview cannot, so it shows a read-only note. The read steps work
  // either way.
  const canMutate = supportsNativeActions();

  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [notice, setNotice] = useState<OnboardingNotice>(null);
  const [stepKey, setStepKey] = useState<OnboardingStepKey>("welcome");
  // True once the first request / demo landed, so the rail shows the journey
  // complete even though the user has already been routed to Home / Ask.
  const [requestDone, setRequestDone] = useState(false);
  // Steps the user explicitly skipped (Dev persona). A skipped step is no longer
  // the blocker for "what's next" but is not marked done either.
  const [skipped, setSkipped] = useState<Set<OnboardingStepKey>>(new Set());
  // True once the user added a Slack approver, so the optional Slack step reads
  // as done in the rail (the server exposes no approver flag on SetupStatus).
  const [slackTouched, setSlackTouched] = useState(false);
  const [githubAuthFlow, setGithubAuthFlow] = useState<GithubAuthFlow>(IDLE_GITHUB_AUTH_FLOW);
  // The step the auto-advance effect last moved past, so a detected gh/engine
  // only auto-advances once and never fights a manual Back.
  const autoAdvancedFrom = useRef<Set<OnboardingStepKey>>(new Set());
  // Steps the user opened deliberately (rail click or Back). Auto-advance is
  // suppressed for these so revisiting a satisfied step to read it never yanks
  // the user forward; only the natural forward flow auto-advances on detection.
  const manualSteps = useRef<Set<OnboardingStepKey>>(new Set());
  const statusRequestSeq = useRef(0);
  const baseUrlRef = useRef(baseUrl);
  const connectedRef = useRef(connected);
  const connectionGenerationRef = useRef(0);
  const githubAuthRequestSeq = useRef(0);
  const githubAuthFlowRequestSeq = useRef<number | null>(null);

  const setInterruptedGithubAuthFlow = useCallback((message: string, requestId?: number) => {
    setStatusLoading(false);
    const activeFlowRequestId = githubAuthFlowRequestSeq.current;
    const ownsFlow =
      requestId === undefined || activeFlowRequestId === requestId || activeFlowRequestId === null;
    if (ownsFlow) {
      githubAuthFlowRequestSeq.current = null;
    }
    setGithubAuthFlow((current) => {
      const canInterrupt = current.state === "starting" || current.state === "waiting";
      if (!canInterrupt || !ownsFlow) {
        return current;
      }
      return {
        ...IDLE_GITHUB_AUTH_FLOW,
        state: "error",
        message,
      };
    });
  }, []);

  const resetStaleGithubAuthFlow = useCallback(
    (requestId: number, message: string) => {
      setInterruptedGithubAuthFlow(message, requestId);
    },
    [setInterruptedGithubAuthFlow],
  );
  const interruptStaleGithubAuthRequest = useCallback(
    (requestId: number) => {
      const activeFlowRequestId = githubAuthFlowRequestSeq.current;
      if (activeFlowRequestId !== requestId && activeFlowRequestId !== null) {
        return;
      }
      resetStaleGithubAuthFlow(
        requestId,
        "GitHub sign-in was interrupted. Start it again for this runtime.",
      );
    },
    [resetStaleGithubAuthFlow],
  );

  useEffect(() => {
    if (baseUrlRef.current !== baseUrl) {
      connectionGenerationRef.current += 1;
      statusRequestSeq.current += 1;
      githubAuthRequestSeq.current += 1;
      setStatus(null);
      setStatusError(null);
      setStatusLoading(false);
      setInterruptedGithubAuthFlow(
        "GitHub sign-in was interrupted. Start it again for this runtime.",
      );
    }
    baseUrlRef.current = baseUrl;
  }, [baseUrl, setInterruptedGithubAuthFlow]);

  useEffect(() => {
    const wasConnected = connectedRef.current;
    if (wasConnected !== connected) {
      connectionGenerationRef.current += 1;
      githubAuthRequestSeq.current += 1;
    }
    connectedRef.current = connected;
    if (!connected) {
      statusRequestSeq.current += 1;
      setStatus(null);
      setStatusError(null);
      setStatusLoading(false);
      setInterruptedGithubAuthFlow("GitHub sign-in was interrupted. Reconnect, then start it again.");
    } else if (wasConnected !== connected) {
      setInterruptedGithubAuthFlow("GitHub sign-in was interrupted. Start it again for this runtime.");
    }
  }, [connected, setInterruptedGithubAuthFlow]);

  const refreshStatus = useCallback(async () => {
    if (!connected) {
      statusRequestSeq.current += 1;
      setStatus(null);
      setStatusLoading(false);
      return;
    }
    const requestId = ++statusRequestSeq.current;
    const requestBaseUrl = baseUrl;
    const requestGeneration = connectionGenerationRef.current;
    setStatusLoading(true);
    try {
      const next = await loadSetupStatus(baseUrl);
      if (
        statusRequestSeq.current === requestId &&
        baseUrlRef.current === requestBaseUrl &&
        connectedRef.current &&
        connectionGenerationRef.current === requestGeneration
      ) {
        setStatus(next);
        setStatusError(null);
      }
    } catch (err) {
      if (
        statusRequestSeq.current === requestId &&
        baseUrlRef.current === requestBaseUrl &&
        connectedRef.current &&
        connectionGenerationRef.current === requestGeneration
      ) {
        setStatusError(errorDetail(err) || "Could not read setup status.");
      }
    } finally {
      if (
        statusRequestSeq.current === requestId &&
        baseUrlRef.current === requestBaseUrl &&
        connectedRef.current &&
        connectionGenerationRef.current === requestGeneration
      ) {
        setStatusLoading(false);
      }
    }
  }, [baseUrl, connected]);

  const startGithubAuthLogin = useCallback(async () => {
    if (!canRun || !connected) {
      githubAuthFlowRequestSeq.current = null;
      setGithubAuthFlow({
        ...IDLE_GITHUB_AUTH_FLOW,
        state: "error",
        message: "Open Alfred in the desktop app and connect to the local runtime first.",
      });
      return;
    }

    const requestAuthId = ++githubAuthRequestSeq.current;
    githubAuthFlowRequestSeq.current = requestAuthId;
    setStatusLoading(true);
    setGithubAuthFlow({
      ...IDLE_GITHUB_AUTH_FLOW,
      state: "starting",
      message: "Starting GitHub sign-in.",
    });

    const requestBaseUrl = baseUrl;
    const requestGeneration = connectionGenerationRef.current;
    const isCurrentRequest = () =>
      connectedRef.current &&
      baseUrlRef.current === requestBaseUrl &&
      connectionGenerationRef.current === requestGeneration &&
      githubAuthRequestSeq.current === requestAuthId;

    try {
      const result = await onRunLocalAction({ action: "github_auth_login" });
      const pollBelongsToCurrentRuntime = isCurrentRequest();
      if (!pollBelongsToCurrentRuntime) {
        interruptStaleGithubAuthRequest(requestAuthId);
        return;
      }
      if (!result) {
        throw new Error("Could not start GitHub sign-in.");
      }
      if (!result.success) {
        throw new Error(result.message || result.stderr || "GitHub sign-in did not start.");
      }

      const details = result.github_auth;
      const deviceUrl = details?.device_url || GITHUB_DEVICE_URL;
      const deviceCode = details?.device_code || null;
      setGithubAuthFlow({
        state: "waiting",
        deviceUrl,
        deviceCode,
        message: result.message || "Finish GitHub sign-in in your browser.",
        detail: null,
      });

      const poll = await pollGithubAuthStatus(
        async () => {
          const next = await loadSetupStatus(requestBaseUrl);
          if (isCurrentRequest()) {
            setStatus(next);
          }
          return next;
        },
        {
          pollIntervalMs: details?.poll_interval_ms,
          timeoutMs: details?.timeout_ms,
        },
      );

      if (!isCurrentRequest()) {
        interruptStaleGithubAuthRequest(requestAuthId);
        return;
      }
      githubAuthFlowRequestSeq.current = null;
      if (poll.status) {
        setStatus(poll.status);
      }
      if (poll.state === "success") {
        setGithubAuthFlow({
          state: "success",
          deviceUrl,
          deviceCode,
          message: poll.status?.github.detail || "GitHub is connected.",
          detail: null,
        });
      } else {
        setGithubAuthFlow({
          state: "timeout",
          deviceUrl,
          deviceCode,
          message: "Still waiting for GitHub. Finish sign-in, then press Recheck.",
          detail: poll.lastError,
        });
      }
    } catch (err) {
      if (!isCurrentRequest()) {
        interruptStaleGithubAuthRequest(requestAuthId);
        return;
      }
      githubAuthFlowRequestSeq.current = null;
      setGithubAuthFlow({
        ...IDLE_GITHUB_AUTH_FLOW,
        state: "error",
        message: err instanceof Error ? err.message : String(err),
        detail: errorDetail(err),
      });
    } finally {
      if (isCurrentRequest()) {
        setStatusLoading(false);
      }
    }
  }, [baseUrl, canRun, connected, interruptStaleGithubAuthRequest, onRunLocalAction]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const githubConnected = Boolean(status?.github.ok);
  const engineReady = Boolean(status?.engine_ready) || Boolean(nativeResult?.success);
  const capabilityActionableCount = status?.capability_plane?.summary.actionable ?? 0;
  const toolsReady = engineReady && capabilityActionableCount === 0;
  const reposSelected = (status?.repos.count ?? 0) > 0;

  const currentIndex = ONBOARDING_STEP_ORDER.indexOf(stepKey);

  // The furthest step the user has actually reached. The rail's "done" state and
  // the "N of M done" count are anchored to this cursor, never to a background
  // signal that happens to be satisfied for a step the user has not seen yet. So
  // a fresh launch where Claude Code, gh, and repos are all already detected
  // still opens on Welcome with 0 done, instead of a rail that makes first-run
  // feel skipped. The mark only ever moves forward.
  const [reachedIndex, setReachedIndex] = useState(0);
  useEffect(() => {
    setReachedIndex((prev) => Math.max(prev, currentIndex));
  }, [currentIndex]);

  // Whether a step's own readiness signal is satisfied, ignoring position.
  const stepSatisfied = useCallback(
    (key: OnboardingStepKey): boolean => {
      switch (key) {
        case "welcome":
          // Welcome is satisfied the moment the user steps off it (or finishes).
          return reachedIndex > 0 || requestDone;
        case "engine":
          return toolsReady;
        case "github":
          return githubConnected;
        case "repos":
          return reposSelected;
        case "team":
          // The shipped Batman roster is already valid. Keeping the default is a
          // complete state only after the operator continues past Team.
          return reachedIndex > ONBOARDING_STEP_ORDER.indexOf("team");
        case "slack":
          // Slack is optional and the server exposes no "approver added" flag on
          // SetupStatus, so it reads satisfied only when the user explicitly
          // skipped it or added an approver (tracked locally as slackTouched). We
          // never invent a "Slack done" signal the server did not send.
          return skipped.has("slack") || slackTouched;
        case "request":
          return requestDone;
        default:
          return false;
      }
    },
    [githubConnected, reachedIndex, reposSelected, requestDone, skipped, slackTouched, toolsReady],
  );

  // Per-step completion for the rail. A step is "done" only when the user has
  // reached it (its index is at or below the furthest-reached cursor) AND its
  // readiness signal is satisfied. This keeps the indicator and the "N of M
  // done" count honest to where the user is, never running ahead on a
  // pre-detected engine / gh / repo selection the user has not walked up to yet.
  const stepComplete = useCallback(
    (key: OnboardingStepKey): boolean => {
      const index = ONBOARDING_STEP_ORDER.indexOf(key);
      if (index > reachedIndex) return false;
      return stepSatisfied(key);
    },
    [reachedIndex, stepSatisfied],
  );

  const steps = useMemo<StepMeta[]>(
    () =>
      ONBOARDING_STEP_ORDER.map((key, index) => ({
        ...STEP_META[key],
        index,
      })),
    [],
  );

  const progressFor = useCallback(
    (key: OnboardingStepKey): StepProgress => {
      if (stepComplete(key)) return "done";
      if (key === stepKey) return "active";
      return "todo";
    },
    [stepComplete, stepKey],
  );

  const stepperItems = useMemo<StepperItem[]>(
    () =>
      steps.map((step) => ({
        key: step.key,
        label: step.railTitle,
        state: progressFor(step.key),
        optional: step.optional,
      })),
    [steps, progressFor],
  );

  const previousKey = ONBOARDING_STEP_ORDER[currentIndex - 1] ?? null;
  const nextKey = ONBOARDING_STEP_ORDER[currentIndex + 1] ?? null;

  const goToStep = useCallback((key: OnboardingStepKey, options?: { manual?: boolean }) => {
    if (options?.manual) {
      manualSteps.current.add(key);
    }
    setNotice(null);
    setStepKey(key);
  }, []);

  const advance = useCallback(() => {
    if (nextKey) goToStep(nextKey);
  }, [goToStep, nextKey]);

  const skipStep = useCallback(
    (key: OnboardingStepKey) => {
      setSkipped((prev) => {
        const next = new Set(prev);
        next.add(key);
        return next;
      });
      const idx = ONBOARDING_STEP_ORDER.indexOf(key);
      const following = ONBOARDING_STEP_ORDER[idx + 1] ?? null;
      if (following) goToStep(following);
    },
    [goToStep],
  );

  // Auto-advance once when a step's detection lands while the user is sitting on
  // it (DESIGN_SPEC: auto-advance on detected gh / engine). Never fights a Back.
  useEffect(() => {
    if (manualSteps.current.has(stepKey)) return;
    if (stepKey === "engine" && toolsReady && !autoAdvancedFrom.current.has("engine")) {
      autoAdvancedFrom.current.add("engine");
      goToStep("github");
    } else if (stepKey === "github" && githubConnected && !autoAdvancedFrom.current.has("github")) {
      autoAdvancedFrom.current.add("github");
      goToStep("repos");
    }
  }, [stepKey, toolsReady, githubConnected, goToStep]);

  // Enter advances when the focus is not in a text field (so typing a server URL
  // or Slack id never triggers a jump). The step bodies own their own submits.
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLElement>) => {
      if (event.key !== "Enter" || event.defaultPrevented) return;
      const target = event.target as HTMLElement;
      const tag = target.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "BUTTON" ||
        tag === "A" ||
        tag === "SUMMARY" ||
        target.isContentEditable
      ) {
        return;
      }
      if (nextKey) {
        event.preventDefault();
        advance();
      }
    },
    [advance, nextKey],
  );

  const meta = STEP_META[stepKey];
  const installInitialized = status !== null && Boolean(status.install?.initialized);
  const canReadSetupStatus = connected || loading || statusLoading;
  let shellCopy = {
    eyebrow: "First run",
    title: "Let's connect Alfred",
    lede: "Seven short steps, about two minutes. You will not need a terminal.",
  };
  if (status === null && !statusError && canReadSetupStatus) {
    shellCopy = {
      eyebrow: "Checking setup",
      title: "Checking this Mac",
      lede: "Alfred is reading the local runtime before choosing the right setup path.",
    };
  } else if (installInitialized) {
    shellCopy = {
      eyebrow: "Existing setup",
      title: "Review your Alfred setup",
      lede: "Alfred found a local runtime on this Mac. Recheck tools, repos, team names, and Slack before shipping more work.",
    };
  }

  return (
    <section className="alfred-onboarding" aria-label="Set up Alfred" onKeyDown={onKeyDown}>
      <div className="alfred-onboarding-shell alfred-glass">
        <header className="alfred-onboarding-shell__head">
          <div className="min-w-0">
            <p className="alfred-onboarding-shell__eyebrow">{shellCopy.eyebrow}</p>
            <h1 className="alfred-onboarding-shell__title">{shellCopy.title}</h1>
            <p className="alfred-onboarding-shell__lede">{shellCopy.lede}</p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            type="button"
            onClick={onOpenConnection}
            className="alfred-onboarding-shell__advanced"
          >
            <Settings2 size={15} aria-hidden="true" />
            <span>Advanced setup</span>
          </Button>
        </header>

        <Stepper
          steps={stepperItems}
          activeKey={stepKey}
          onSelect={(key) => goToStep(key, { manual: true })}
        />

        {statusError ? (
          <Card className="rounded-lg border-destructive/30 bg-destructive/10 text-destructive shadow-none">
            <CardContent className="px-4 text-sm">
              {statusError} The steps below still show their manual fallback.
            </CardContent>
          </Card>
        ) : null}
        {notice ? (
          <Card
            className={cn(
              "rounded-lg shadow-none",
              notice.tone === "ok"
                ? "border-primary/25 bg-primary/10 text-primary"
                : "border-destructive/25 bg-destructive/10 text-destructive",
            )}
          >
            <CardContent className="px-4 text-sm">{notice.message}</CardContent>
          </Card>
        ) : null}

        <div className="alfred-onboarding-shell__panel motion-fade" key={stepKey}>
          {stepKey === "welcome" ? (
            // Welcome is the hero screen, not a labelled step: it skips the
            // StepFrame icon/title/blurb so the value line is said once here, not
            // echoed by a step header above it.
            <WelcomeStep
              install={status?.install ?? null}
              queue={status?.queue ?? null}
              onGetStarted={() => goToStep("engine")}
              onDevShortcut={() => goToStep("github")}
            />
          ) : null}

          {stepKey === "engine" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb}>
              <EngineStep
                status={status}
                engineReady={engineReady}
                canRun={canRun}
                nativeBusy={nativeBusy}
                statusLoading={statusLoading}
                onRunLocalAction={onRunLocalAction}
                onRecheck={() => void refreshStatus()}
              />
            </StepFrame>
          ) : null}

          {stepKey === "github" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb}>
              <GitHubStep
                baseUrl={baseUrl}
                loading={loading}
                connected={connected}
                github={status?.github ?? null}
                canRun={canRun}
                nativeBusy={nativeBusy}
                authFlow={githubAuthFlow}
                statusLoading={statusLoading}
                onConnectServer={onConnectServer}
                onStartRuntime={onStartRuntime}
                onStartGithubAuth={startGithubAuthLogin}
                onRecheck={() => void refreshStatus()}
              />
            </StepFrame>
          ) : null}

          {stepKey === "repos" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb}>
              <ReposStep
                baseUrl={baseUrl}
                canMutate={canMutate}
                githubConnected={githubConnected}
                selectedCount={status?.repos.count ?? 0}
                onSaved={async () => {
                  await refreshStatus();
                }}
                setNotice={setNotice}
              />
            </StepFrame>
          ) : null}

          {stepKey === "team" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb}>
              <RosterThemeStep
                customNames={customNames}
                rosterTheme={rosterTheme}
                saveError={rosterSaveError}
                onChange={onRosterThemeChange}
                onEditCustom={onEditCustomTheme}
              />
            </StepFrame>
          ) : null}

          {stepKey === "slack" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb} accentLabel="Optional">
              <SlackStep
                baseUrl={baseUrl}
                connected={connected}
                canMutate={canMutate}
                onSkip={() => skipStep("slack")}
                onApproverAdded={() => setSlackTouched(true)}
                setNotice={setNotice}
              />
            </StepFrame>
          ) : null}

          {stepKey === "request" ? (
            <StepFrame icon={meta.icon} title={meta.title} blurb={meta.blurb} accentLabel="The payoff">
              <FirstRequestStep
                baseUrl={baseUrl}
                canMutate={canMutate}
                reposReady={reposSelected}
                demoPresent={Boolean(status?.demo.present)}
                setNotice={setNotice}
                onSwitch={onSwitch}
                onComplete={() => setRequestDone(true)}
                onSeedDemo={async () => {
                  await onRefreshBoard?.({ demo: true });
                  await refreshStatus();
                }}
                onClearDemo={async () => {
                  await onRefreshBoard?.({ demo: false });
                  await refreshStatus();
                }}
              />
            </StepFrame>
          ) : null}
        </div>

        <footer className="alfred-onboarding-shell__footer" aria-label="Onboarding navigation">
          <Button
            variant="outline"
            size="sm"
            type="button"
            disabled={!previousKey}
            onClick={() => {
              if (previousKey) goToStep(previousKey, { manual: true });
            }}
          >
            <ArrowLeft size={15} aria-hidden="true" />
            <span>Back</span>
          </Button>
          <span className="alfred-onboarding-shell__progress">
            Step {currentIndex + 1} of {ONBOARDING_STEP_ORDER.length}
          </span>
          <div className="flex items-center gap-2">
            {meta.optional && nextKey ? (
              <Button variant="ghost" size="sm" type="button" onClick={() => skipStep(stepKey)}>
                <span>Skip</span>
              </Button>
            ) : null}
            {nextKey ? (
              <Button type="button" size="sm" onClick={advance}>
                <span>Continue</span>
                <ArrowRight size={15} aria-hidden="true" />
              </Button>
            ) : (
              <Button type="button" size="sm" onClick={() => onSwitch?.("home")}>
                <span>Go to Inbox</span>
                <ArrowRight size={15} aria-hidden="true" />
              </Button>
            )}
          </div>
        </footer>
      </div>
    </section>
  );
}

function RosterThemeStep({
  customNames,
  rosterTheme,
  saveError,
  onChange,
  onEditCustom,
}: {
  customNames: CustomRosterNames;
  rosterTheme: RosterThemeId;
  saveError: string | null;
  onChange: (next: RosterThemeId) => void;
  onEditCustom: () => void;
}) {
  const preview = useMemo(
    () =>
      ROSTER_PREVIEW_AGENTS.map(({ codename }) => ({
        codename,
        identity: resolveThemedIdentity({ codename }, rosterTheme, customNames),
      })),
    [customNames, rosterTheme],
  );

  return (
    <div className="space-y-4">
      <RosterThemePicker
        value={rosterTheme}
        onChange={onChange}
        onEditCustom={onEditCustom}
        saveError={saveError}
      />
      <div className="grid gap-3 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="rounded-lg border border-border/70 bg-card/60 p-4">
          <p className="text-xs font-medium uppercase text-muted-foreground">Active roster</p>
          <h3 className="mt-1 text-lg font-medium text-foreground">
            {rosterThemeLabel(rosterTheme)}
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {rosterThemeBlurb(rosterTheme)}
          </p>
        </div>
        <div className="rounded-lg border border-border/70 bg-card/60 p-4">
          <p className="text-xs font-medium uppercase text-muted-foreground">Preview</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {preview.map(({ codename, identity }) => (
              <div key={codename} className="rounded-md border border-border/60 bg-background/40 p-3">
                <p className="text-sm font-medium text-foreground">{identity.name}</p>
                <p className="text-xs text-muted-foreground">{identity.roleLabel}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Roles, permissions, schedules, labels, worktrees, and merge gates stay unchanged.
      </p>
    </div>
  );
}
