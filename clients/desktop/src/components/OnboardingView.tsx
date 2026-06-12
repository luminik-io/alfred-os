import {
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Columns3,
  GitPullRequest,
  ListChecks,
  PlayCircle,
  Plug,
  RefreshCw,
  Sparkles,
  TerminalSquare,
  Trash2,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  clearSetupDemo,
  composeSetupPlaybook,
  errorDetail,
  loadSetupPlaybooks,
  loadSetupRepos,
  loadSetupStatus,
  saveSetupRepos,
  seedSetupDemo,
  supportsNativeActions,
} from "../api";
import type { NativeActionRequest, TabKey } from "../lib/uiTypes";
import type {
  NativeCommandResult,
  SetupPlaybook,
  SetupRepo,
  SetupStatus,
} from "../types";
import {
  Badge,
  Button,
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  Input,
  Label,
} from "./ui";
import { cn } from "@/lib/utils";

/**
 * Onboarding-first Settings for the native developer tool: detect installed
 * CLIs (no API keys), connect GitHub via the gh CLI already signed in, choose
 * the repositories Alfred may work in, compose the first plan from a starter
 * spec, and seed sample Work cards.
 *
 * The default golden path is gh-auth + one repo, with NO AWS / Slack required.
 * The repository, first-plan, and Work preview steps are real now: they call the
 * /api/setup/* routes (read-only status/repos/playbooks work in the browser
 * preview; the mutations are token-gated and so need the desktop app, where
 * the native bridge attaches the per-launch token). Off-Tauri, the mutations
 * degrade to a clear "open the desktop app" note rather than faking success.
 *
 * The existing connection + diagnostics content still lives in SetupView,
 * surfaced again under the Fleet page's Diagnostics; the header links there.
 */

type StepStatus = "done" | "active" | "todo";
type StepIntent = "primary" | "optional" | "complete";
type SetupStepKey = "engine" | "github" | "repos" | "playbook" | "demo";

// Onboarding raises its own inline notice for the repo/playbook/demo steps. It
// is rendered only here, so it carries no cross-surface `domain` tag (unlike
// the app-wide ActionNotice that fans into Plans / Board / Memory / Setup).
type LocalNotice = { tone: "ok" | "error"; message: string } | null;
type SetupProgressStep = {
  key: SetupStepKey;
  index: number;
  title: string;
  detail: string;
  status: StepStatus;
  intent: StepIntent;
};
type SetupDetailStep = {
  key: SetupStepKey;
  index: number;
  status: StepStatus;
  intent: StepIntent;
  node: ReactNode;
};

export function OnboardingView({
  baseUrl,
  loading,
  connected,
  canRun,
  nativeBusy,
  nativeResult,
  onConnectServer,
  onStartRuntime,
  onRunLocalAction,
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
  onConnectServer: (url: string) => void;
  onStartRuntime: () => void;
  onRunLocalAction: (request: NativeActionRequest) => void;
  /** Jump to the full connection + diagnostics surface. */
  onOpenConnection: () => void;
  /** Navigate to another primary surface (e.g. Board, Compose) after an action. */
  onSwitch?: (tab: TabKey) => void;
  onRefreshBoard?: (options?: { demo?: boolean }) => Promise<void> | void;
}) {
  // The mutating steps (pick repos, playbook, demo) need the per-launch token
  // the native bridge attaches; the browser preview cannot, so it shows a
  // read-only note. The read steps (status, repo list) work either way.
  const canMutate = supportsNativeActions();

  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [notice, setNotice] = useState<LocalNotice>(null);
  const [selectedStepKey, setSelectedStepKey] = useState<SetupStepKey>("engine");

  const refreshStatus = useCallback(async () => {
    if (!connected) {
      setStatus(null);
      return;
    }
    setStatusLoading(true);
    try {
      const next = await loadSetupStatus(baseUrl);
      setStatus(next);
      setStatusError(null);
    } catch (err) {
      setStatusError(errorDetail(err) || "Could not read setup status.");
    } finally {
      setStatusLoading(false);
    }
  }, [baseUrl, connected]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const githubConnected = Boolean(status?.github.ok);
  const engineReady = Boolean(status?.engine_ready);
  const reposSelected = (status?.repos.count ?? 0) > 0;
  const demoPresent = Boolean(status?.demo.present);
  // CLI confirmation: trust the server-side engine probe when we have it, else
  // fall back to a native auth/agents result (the strongest local signal).
  const cliConfirmed = engineReady || Boolean(nativeResult?.success);
  const firstPlanStatus: StepStatus = reposSelected ? "active" : "todo";
  const demoStatus: StepStatus = demoPresent ? "done" : reposSelected ? "active" : "todo";
  const stepStates = useMemo(
    () =>
      [
        {
          key: "engine",
          index: 1,
          title: "Tools",
          detail: cliConfirmed ? "Claude Code / Codex found" : "Needs local check",
          status: cliConfirmed ? "done" : "active",
          intent: "primary",
        },
        {
          key: "github",
          index: 2,
          title: "GitHub",
          detail: githubConnected ? status?.github.detail || "Signed in" : "Use the local gh session",
          status: githubConnected ? "done" : connected ? "active" : "todo",
          intent: "primary",
        },
        {
          key: "repos",
          index: 3,
          title: "Repositories",
          detail: reposSelected
            ? `${status?.repos.count ?? 0} ${(status?.repos.count ?? 0) === 1 ? "repository" : "repositories"} selected`
            : "Select allowed repos",
          status: reposSelected ? "done" : githubConnected ? "active" : "todo",
          intent: "primary",
        },
        {
          key: "playbook",
          index: 4,
          title: "First plan",
          detail: reposSelected ? "Draft from a starter spec" : "Choose repositories first",
          status: firstPlanStatus,
          intent: "primary",
        },
        {
          key: "demo",
          index: 5,
          title: "Work preview",
          detail: demoPresent ? "Demo cards are in Work" : "Optional sample cards",
          status: demoStatus,
          intent: demoPresent ? "complete" : "optional",
        },
      ] satisfies SetupProgressStep[],
    [
      cliConfirmed,
      connected,
      demoPresent,
      demoStatus,
      firstPlanStatus,
      githubConnected,
      reposSelected,
      status?.github.detail,
      status?.repos.count,
    ],
  );
  const nextStep =
    stepStates.find((step) => step.status !== "done" && step.intent !== "optional") ??
    stepStates.find((step) => step.status !== "done") ??
    null;
  const recommendedStepKey = nextStep?.key ?? (status?.ready ? "playbook" : "engine");

  useEffect(() => {
    setSelectedStepKey((current) => {
      const currentStep = stepStates.find((step) => step.key === current);
      if (!currentStep || currentStep.status === "done") {
        return recommendedStepKey;
      }
      return current;
    });
  }, [recommendedStepKey, stepStates]);

  const detailSteps = [
    {
      key: "engine",
      index: 1,
      status: stepStates[0].status,
      intent: stepStates[0].intent,
      node: (
        <OnboardingStep
          key="engine"
          index={1}
          title="Use the tools already on this Mac"
          blurb="Alfred checks Claude Code and Codex from the native app."
          icon={TerminalSquare}
          status={stepStates[0].status}
        >
          <EngineStep
            status={status}
            canRun={canRun}
            nativeBusy={nativeBusy}
            onRunLocalAction={onRunLocalAction}
          />
        </OnboardingStep>
      ),
    },
    {
      key: "github",
      index: 2,
      status: stepStates[1].status,
      intent: stepStates[1].intent,
      node: (
        <OnboardingStep
          key="github"
          index={2}
          title="Connect GitHub"
          blurb="Reuses your GitHub CLI sign-in."
          icon={GitPullRequest}
          status={stepStates[1].status}
        >
          <GitHubStep
            baseUrl={baseUrl}
            loading={loading}
            connected={connected}
            github={status?.github ?? null}
            onConnectServer={onConnectServer}
            onStartRuntime={onStartRuntime}
            canRun={canRun}
            nativeBusy={nativeBusy}
          />
        </OnboardingStep>
      ),
    },
    {
      key: "repos",
      index: 3,
      status: stepStates[2].status,
      intent: stepStates[2].intent,
      node: (
        <OnboardingStep
          key="repos"
          index={3}
          title="Choose repositories Alfred can work in"
          blurb="Repository access stays bounded to your selection."
          icon={Plug}
          status={stepStates[2].status}
        >
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
        </OnboardingStep>
      ),
    },
    {
      key: "playbook",
      index: 4,
      status: stepStates[3].status,
      intent: stepStates[3].intent,
      node: (
        <OnboardingStep
          key="playbook"
          index={4}
          title="Draft the first plan from a spec"
          blurb="Starter specs create a reviewable plan before any agent runs."
          icon={ListChecks}
          status={stepStates[3].status}
          accentLabel={stepStates[3].status === "active" ? "Recommended next" : undefined}
        >
          <PlaybooksStep
            baseUrl={baseUrl}
            canMutate={canMutate}
            setNotice={setNotice}
            onSwitch={onSwitch}
          />
        </OnboardingStep>
      ),
    },
    {
      key: "demo",
      index: 5,
      status: stepStates[4].status,
      intent: stepStates[4].intent,
      node: (
        <OnboardingStep
          key="demo"
          index={5}
          title="Seed Work preview"
          blurb="Optional sample cards for the Work view."
          icon={Columns3}
          status={stepStates[4].status}
          accentLabel={demoPresent ? "Ready" : "Optional"}
        >
          <DemoStep
            baseUrl={baseUrl}
            canMutate={canMutate}
            demoPresent={demoPresent}
            onChanged={async () => {
              await refreshStatus();
            }}
            setNotice={setNotice}
            onSwitch={onSwitch}
            onRefreshBoard={onRefreshBoard}
          />
        </OnboardingStep>
      ),
    },
  ] satisfies SetupDetailStep[];
  const selectedDetail =
    detailSteps.find((step) => step.key === selectedStepKey) ??
    detailSteps.find((step) => step.key === recommendedStepKey) ??
    detailSteps[0];
  const selectedIndex = Math.max(0, detailSteps.findIndex((step) => step.key === selectedDetail.key));
  const previousDetail = detailSteps[selectedIndex - 1] ?? null;
  const nextDetail = detailSteps[selectedIndex + 1] ?? null;
  const completedCount = stepStates.filter((step) => step.status === "done").length;
  const progressPercent = Math.round((completedCount / stepStates.length) * 100);

  return (
    <section className="grid gap-4" aria-label="Setup">
      <section className="alfred-page-hero px-4 py-4" aria-label="Setup summary">
        <div className="relative flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0 space-y-1">
            <Badge variant="outline" className="mb-1">
              Setup
            </Badge>
            <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
              Connect Alfred
            </h1>
            <p className="max-w-3xl text-sm text-muted-foreground">
              Connect local tools, GitHub, and approved repositories. No cloud dashboard
              or token paste.
            </p>
          </div>
          <Button variant="outline" type="button" onClick={onOpenConnection}>
            Diagnostics
          </Button>
        </div>
      </section>

      {statusError ? (
        <Card className="rounded-lg border-destructive/30 bg-destructive/10 text-destructive shadow-none">
          <CardContent className="px-4 text-sm">
            {statusError} The steps below show their manual fallback.
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

      <div className="grid gap-4 lg:grid-cols-[minmax(17rem,21rem)_1fr]">
        <aside aria-label="Setup readiness" className="grid gap-3">
          <Card
            className={cn(
              "rounded-lg border-border/70 bg-card/70 shadow-none",
              status?.ready && "border-primary/25 bg-primary/10",
            )}
          >
            <CardHeader>
              <Badge variant={status?.ready ? "default" : "outline"} className="mb-1 w-fit">
                Setup status
              </Badge>
              <CardTitle>
                {status?.ready ? "Ready to plan" : nextStep ? `Next: ${nextStep.title}` : "Checking setup"}
              </CardTitle>
              <CardDescription>
                {status?.ready
                  ? "Tools, GitHub, and repository access are ready."
                  : nextStep
                    ? "Complete the highlighted step to unlock planning."
                    : "Alfred is checking the local setup."}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3">
              <div
                className="h-2 overflow-hidden rounded-full bg-muted"
                aria-label={`${completedCount} of ${stepStates.length} setup steps complete`}
              >
                <span
                  className="block h-full rounded-full bg-primary transition-[width]"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
              <div className="flex flex-wrap gap-2">
                {status?.ready ? (
                  <Button size="sm" type="button" onClick={() => onSwitch?.("compose")}>
                    <Sparkles size={15} aria-hidden="true" />
                    <span>Plan</span>
                  </Button>
                ) : null}
                <Button
                  variant="outline"
                  size="sm"
                  type="button"
                  onClick={() => void refreshStatus()}
                  disabled={!connected || statusLoading}
                >
                  <RefreshCw size={14} aria-hidden="true" className={statusLoading ? "animate-spin" : undefined} />
                  <span>{statusLoading ? "Checking" : "Recheck"}</span>
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="rounded-lg border-border/70 bg-card/70 shadow-none">
            <CardContent className="px-2">
              <ol className="grid gap-1" aria-label="Setup progress">
                {stepStates.map((step) => {
                  const selected = selectedStepKey === step.key;
                  return (
                    <li key={step.key}>
                      <Button
                        variant={selected ? "secondary" : "ghost"}
                        className="h-auto w-full justify-start gap-3 px-2 py-2 text-left"
                        type="button"
                        onClick={() => setSelectedStepKey(step.key)}
                        aria-current={selected ? "step" : undefined}
                        aria-label={step.title}
                      >
                        <span
                          className={cn(
                            "flex size-6 shrink-0 items-center justify-center rounded-full border text-xs",
                            step.status === "done"
                              ? "border-primary/25 bg-primary text-primary-foreground"
                              : "border-border bg-background text-muted-foreground",
                          )}
                          aria-hidden="true"
                        >
                          {step.status === "done" ? <CheckCircle2 size={14} /> : step.index}
                        </span>
                        <span className="grid min-w-0 flex-1 gap-0.5">
                          <span className="truncate text-sm font-medium">{step.title}</span>
                          <span className="truncate text-xs text-muted-foreground">{step.detail}</span>
                        </span>
                        {step.intent === "optional" && step.status !== "done" ? (
                          <Badge variant="outline">Optional</Badge>
                        ) : null}
                      </Button>
                    </li>
                  );
                })}
              </ol>
            </CardContent>
          </Card>
        </aside>

        <div className="grid min-w-0 gap-3">
          <ol className="grid gap-3">{selectedDetail.node}</ol>
          <Card className="rounded-lg border-border/70 bg-card/70 shadow-none" aria-label="Setup step navigation">
            <CardFooter className="justify-between gap-3 rounded-lg bg-muted/35 px-3 py-3">
              <Button
                variant="outline"
                type="button"
                disabled={!previousDetail}
                onClick={() => {
                  if (previousDetail) setSelectedStepKey(previousDetail.key);
                }}
              >
                <span>Back</span>
              </Button>
              <span className="text-sm text-muted-foreground">
                Step {selectedDetail.index} of {detailSteps.length}
              </span>
              <Button
                variant={nextDetail ? "default" : "outline"}
                type="button"
                disabled={!nextDetail}
                onClick={() => {
                  if (nextDetail) setSelectedStepKey(nextDetail.key);
                }}
              >
                <span>{nextDetail ? "Continue" : "Done"}</span>
                {nextDetail ? <ChevronRight size={15} aria-hidden="true" /> : null}
              </Button>
            </CardFooter>
          </Card>
        </div>
      </div>
    </section>
  );
}

function EngineStep({
  status,
  canRun,
  nativeBusy,
  onRunLocalAction,
}: {
  status: SetupStatus | null;
  canRun: boolean;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const engines = status?.engines ?? [];
  return (
    <div className="grid gap-3">
      {engines.length ? (
        <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none">
          <CardContent className="px-3">
            <ul className="grid gap-2" aria-label="Installed developer tools">
              {engines.map((engine) => (
                <li
                  key={engine.name}
                  className="flex items-center gap-2 rounded-md border border-border/60 bg-card/60 px-2.5 py-2 text-sm"
                >
                  {engine.installed ? (
                    <CheckCircle2 size={15} aria-hidden="true" className="text-primary" />
                  ) : (
                    <XCircle size={15} aria-hidden="true" className="text-muted-foreground" />
                  )}
                  <code className="font-mono text-xs">{engine.name}</code>
                  <Badge variant={engine.installed ? "secondary" : "outline"} className="ml-auto">
                    {engine.installed ? "installed" : "not found"}
                  </Badge>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
      <Button
        className="w-fit"
        type="button"
        disabled={!canRun || nativeBusy === "auth_status:fleet"}
        onClick={() => onRunLocalAction({ action: "auth_status" })}
      >
        <CheckCircle2 size={15} aria-hidden="true" />
        <span>Check my tools</span>
      </Button>
      {!canRun ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          Desktop mode can run the deeper CLI check.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          No API keys needed.
        </p>
      )}
    </div>
  );
}

function GitHubStep({
  baseUrl,
  loading,
  connected,
  github,
  onConnectServer,
  onStartRuntime,
  canRun,
  nativeBusy,
}: {
  baseUrl: string;
  loading: boolean;
  connected: boolean;
  github: SetupStatus["github"] | null;
  onConnectServer: (url: string) => void;
  onStartRuntime: () => void;
  canRun: boolean;
  nativeBusy: string | null;
}) {
  const [url, setUrl] = useState(baseUrl);
  useEffect(() => {
    setUrl(baseUrl);
  }, [baseUrl]);
  return (
    <div className="grid gap-3">
      <form
        className="grid gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const next = url.trim();
          if (next) onConnectServer(next);
        }}
      >
        <Label htmlFor="onboarding-server-url">Local server URL</Label>
        <div className="grid gap-2 md:grid-cols-[1fr_auto_auto]">
          <Input
            id="onboarding-server-url"
            value={url}
            onChange={(event) => setUrl(event.currentTarget.value)}
            placeholder="http://127.0.0.1:7010"
            spellCheck={false}
          />
          <Button variant="outline" type="submit" disabled={loading || !url.trim()}>
            <span>{loading ? "Checking" : "Connect"}</span>
          </Button>
          {canRun && !connected ? (
            <Button
              type="button"
              disabled={nativeBusy === "runtime:start"}
              onClick={onStartRuntime}
            >
              <PlayCircle size={15} aria-hidden="true" />
              <span>{nativeBusy === "runtime:start" ? "Starting" : "Start runtime"}</span>
            </Button>
          ) : null}
        </div>
      </form>
      {github ? (
        <Card
          size="sm"
          className={cn(
            "rounded-lg shadow-none",
            github.ok
              ? "border-primary/25 bg-primary/10 text-primary"
              : "border-border/70 bg-muted/35 text-muted-foreground",
          )}
        >
          <CardContent className="flex items-center gap-2 px-3 text-sm">
          {github.ok ? (
            <>
              <CheckCircle2 size={14} aria-hidden="true" /> {github.detail}
            </>
          ) : (
            github.detail
          )}
          </CardContent>
        </Card>
      ) : null}
      <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none">
        <CardContent className="px-3">
          <details className="group grid gap-2">
            <summary className="cursor-pointer list-none">
              <span className="grid gap-0.5">
                <strong className="text-sm font-medium">Advanced: sign in from a terminal</strong>
                <span className="text-xs text-muted-foreground">
                  The one-time GitHub sign-in Alfred reuses.
                </span>
              </span>
            </summary>
            <p className="mt-3 text-sm text-muted-foreground">
              Alfred reuses your <code>gh</code> session. Run this once if GitHub is
              not connected yet.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Badge variant="outline" className="font-mono">gh auth login</Badge>
              <Badge variant="outline" className="font-mono">gh auth status</Badge>
            </div>
          </details>
        </CardContent>
      </Card>
    </div>
  );
}

function ReposStep({
  baseUrl,
  canMutate,
  githubConnected,
  selectedCount,
  onSaved,
  setNotice,
}: {
  baseUrl: string;
  canMutate: boolean;
  githubConnected: boolean;
  selectedCount: number;
  onSaved: () => Promise<void>;
  setNotice: (notice: LocalNotice) => void;
}) {
  const [repos, setRepos] = useState<SetupRepo[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedRepos, setSavedRepos] = useState<string[] | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await loadSetupRepos(baseUrl);
      setRepos(result.repos);
      setPicked(new Set(result.selected.map((r) => r.toLowerCase())));
      setError(result.error || null);
      setLoaded(true);
    } catch (err) {
      setError(errorDetail(err) || "Could not list your repositories.");
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  const toggle = (slug: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      const key = slug.toLowerCase();
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      const visible = new Map(
        repos.map((repo) => [repo.name_with_owner.toLowerCase(), repo.name_with_owner] as const),
      );
      const selected = Array.from(picked).map((slug) => visible.get(slug) || slug);
      const result = await saveSetupRepos(baseUrl, selected);
      setSavedRepos(result.repos);
      setNotice({
        tone: "ok",
        message: `Saved ${result.repos.length} ${result.repos.length === 1 ? "repository" : "repositories"} Alfred can work in.`,
      });
      await onSaved();
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not save your repository selection." });
    } finally {
      setSaving(false);
    }
  };

  if (!githubConnected) {
    return (
      <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
        <CardContent className="px-3 text-sm text-muted-foreground">
          Connect GitHub first (step 2). Once you are signed in, your repositories
          appear here to choose from.
        </CardContent>
      </Card>
    );
  }

  const pickedLabel = `${picked.size} ${picked.size === 1 ? "repository" : "repositories"}`;

  return (
    <div className="grid gap-3">
      {!loaded ? (
        <Button variant="outline" className="w-fit" type="button" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={14} aria-hidden="true" className={loading ? "animate-spin" : undefined} />
          <span>{loading ? "Loading repositories" : "Load my repositories"}</span>
        </Button>
      ) : null}

      {error ? (
        <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
          <CardContent className="px-3 text-sm text-muted-foreground">{error}</CardContent>
        </Card>
      ) : null}

      {loaded && !error ? (
        repos.length ? (
          <>
            <div
              className="grid max-h-[42vh] gap-2 overflow-y-auto pr-1"
              role="group"
              aria-label="Repositories Alfred may work in"
            >
              {repos.map((repo) => (
                <label
                  className="grid cursor-pointer grid-cols-[auto_1fr_auto] gap-2 rounded-lg border border-border/70 bg-background/55 px-3 py-2 transition-colors hover:bg-muted/45"
                  key={repo.name_with_owner}
                >
                  <input
                    className="mt-1 size-4 accent-primary"
                    type="checkbox"
                    checked={picked.has(repo.name_with_owner.toLowerCase())}
                    onChange={() => toggle(repo.name_with_owner)}
                  />
                  <span className="grid min-w-0 gap-0.5">
                    <span className="truncate font-mono text-sm">{repo.name_with_owner}</span>
                    {repo.description ? (
                      <span className="line-clamp-2 text-xs text-muted-foreground">{repo.description}</span>
                    ) : null}
                  </span>
                  <span className="flex flex-wrap justify-end gap-1">
                    {repo.is_private ? <Badge variant="outline">private</Badge> : null}
                    {repo.listed === false ? <Badge variant="secondary">saved</Badge> : null}
                  </span>
                </label>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                onClick={() => void save()}
                disabled={!canMutate || saving}
              >
                <CheckCircle2 size={15} aria-hidden="true" />
                <span>{saving ? "Saving" : `Save ${pickedLabel}`}</span>
              </Button>
              <Button variant="outline" type="button" onClick={() => void load()} disabled={loading}>
                <RefreshCw size={14} aria-hidden="true" />
                <span>Refresh</span>
              </Button>
            </div>
          </>
        ) : (
          <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
            <CardContent className="px-3 text-sm text-muted-foreground">
              <strong className="block text-foreground">No repositories found.</strong>
              gh did not return any repositories for your account.
            </CardContent>
          </Card>
        )
      ) : null}

      {savedRepos ? (
        <p className="text-sm text-muted-foreground">
          Alfred is now scoped to: {savedRepos.length ? savedRepos.join(", ") : "no repositories"}.
        </p>
      ) : selectedCount ? (
        <p className="text-sm text-muted-foreground">
          {selectedCount} {selectedCount === 1 ? "repository" : "repositories"} selected. Load the list to change them.
        </p>
      ) : null}

      {!canMutate ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          Desktop mode can save repository choices.
        </p>
      ) : null}
    </div>
  );
}

function PlaybooksStep({
  baseUrl,
  canMutate,
  setNotice,
  onSwitch,
}: {
  baseUrl: string;
  canMutate: boolean;
  setNotice: (notice: LocalNotice) => void;
  onSwitch?: (tab: TabKey) => void;
}) {
  const [playbooks, setPlaybooks] = useState<SetupPlaybook[]>([]);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadSetupPlaybooks(baseUrl)
      .then((result) => {
        if (!cancelled) setPlaybooks(result.playbooks);
      })
      .catch((err) => {
        if (!cancelled) setError(errorDetail(err) || "Could not load starter specs.");
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl]);

  const pick = async (key: string) => {
    setBusyKey(key);
    try {
      const result = await composeSetupPlaybook(baseUrl, key);
      setNotice({
        tone: "ok",
        message: `Drafted a first plan: "${result.title}". Open Plan or Plans to review it.`,
      });
      onSwitch?.("compose");
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not draft from that spec." });
    } finally {
      setBusyKey(null);
    }
  };

  if (error) {
    return (
      <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
        <CardContent className="px-3 text-sm text-muted-foreground">{error}</CardContent>
      </Card>
    );
  }

  return (
    <div className="grid gap-3">
      <div className="grid gap-2">
        {playbooks.map((playbook) => (
          <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none" key={playbook.key}>
            <CardHeader className="gap-2 md:grid-cols-[1fr_auto]">
              <div className="min-w-0">
                <CardTitle className="text-sm">{playbook.title}</CardTitle>
                <CardDescription>{playbook.summary}</CardDescription>
              </div>
              <CardAction>
                <Button
                  variant="outline"
                  type="button"
                  onClick={() => void pick(playbook.key)}
                  disabled={!canMutate || busyKey !== null}
                >
                  <Sparkles size={14} aria-hidden="true" />
                  <span>{busyKey === playbook.key ? "Drafting" : "Use this"}</span>
                </Button>
              </CardAction>
            </CardHeader>
          </Card>
        ))}
      </div>
      {!canMutate ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          Desktop mode can draft first-plan requests.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Pick one to draft a first plan. Alfred saves it for review before any agent starts.
        </p>
      )}
    </div>
  );
}

function DemoStep({
  baseUrl,
  canMutate,
  demoPresent,
  onChanged,
  setNotice,
  onSwitch,
  onRefreshBoard,
}: {
  baseUrl: string;
  canMutate: boolean;
  demoPresent: boolean;
  onChanged: () => Promise<void>;
  setNotice: (notice: LocalNotice) => void;
  onSwitch?: (tab: TabKey) => void;
  onRefreshBoard?: (options?: { demo?: boolean }) => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);

  const seed = async () => {
    setBusy(true);
    try {
      await seedSetupDemo(baseUrl);
      setNotice({ tone: "ok", message: "Seeded demo cards. Open Work to see them." });
      await onChanged();
      await onRefreshBoard?.({ demo: true });
      onSwitch?.("pipeline");
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not seed the demo." });
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    try {
      await clearSetupDemo(baseUrl);
      setNotice({ tone: "ok", message: "Cleared the demo cards." });
      await onChanged();
      await onRefreshBoard?.({ demo: false });
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not clear the demo." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">
        <Button type="button" onClick={() => void seed()} disabled={!canMutate || busy}>
          <PlayCircle size={15} aria-hidden="true" />
          <span>{busy ? "Working" : demoPresent ? "Re-seed demo" : "Seed Work preview"}</span>
        </Button>
        {demoPresent ? (
          <Button variant="outline" type="button" onClick={() => void clear()} disabled={!canMutate || busy}>
            <Trash2 size={14} aria-hidden="true" />
            <span>Clear demo</span>
          </Button>
        ) : null}
      </div>
      {demoPresent ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <CheckCircle2 size={14} aria-hidden="true" /> Demo cards are in Work,
          clearly labelled. Clear them whenever you like.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Adds sample cards marked <em>demo</em> to Work.
        </p>
      )}
      {!canMutate ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          Desktop mode can seed the demo.
        </p>
      ) : null}
    </div>
  );
}

function OnboardingStep({
  index,
  title,
  blurb,
  icon: Icon,
  status,
  accentLabel,
  children,
}: {
  index: number;
  title: string;
  blurb: string;
  icon: typeof Plug;
  status: StepStatus;
  accentLabel?: string;
  children?: ReactNode;
}) {
  return (
    <li>
      <Card
        className={cn(
          "rounded-lg border-border/70 bg-card/70 shadow-none",
          status === "active" && "border-primary/25 bg-primary/10",
        )}
      >
        <CardHeader className="gap-3 md:grid-cols-[auto_1fr_auto]">
          <span
            className={cn(
              "flex size-9 items-center justify-center rounded-full border text-sm font-medium",
              status === "done"
                ? "border-primary/25 bg-primary text-primary-foreground"
                : status === "active"
                  ? "border-primary/35 bg-primary/15 text-primary"
                  : "border-border bg-background text-muted-foreground",
            )}
            aria-hidden="true"
          >
            {status === "done" ? (
              <CheckCircle2 size={18} />
            ) : status === "todo" ? (
              <CircleDashed size={18} />
            ) : (
              index
            )}
          </span>
          <div className="min-w-0">
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              <Icon size={16} aria-hidden="true" />
              <span>{title}</span>
              {accentLabel ? <Badge variant="outline">{accentLabel}</Badge> : null}
            </CardTitle>
            <CardDescription>{blurb}</CardDescription>
          </div>
          {status === "active" ? (
            <CardAction>
              <ChevronRight size={15} aria-hidden="true" className="text-primary" />
            </CardAction>
          ) : null}
        </CardHeader>
        <CardContent className="px-4">{children}</CardContent>
      </Card>
    </li>
  );
}
