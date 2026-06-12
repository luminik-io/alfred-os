import {
  Bot,
  GitPullRequest,
  Home,
  Lightbulb,
  MessageSquare,
  Moon,
  RefreshCw,
  Settings,
  Sun,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ConnectionBanner,
  NativeResultPanel,
} from "./components/atoms";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { ComposeView } from "./components/ComposeView";
import { FleetControlView } from "./components/FleetControlView";
import { AppShell } from "./components/layout/AppShell";
import { LogsView } from "./components/LogsView";
import { MemoryView } from "./components/MemoryView";
import { OnboardingView } from "./components/OnboardingView";
import { PipelineView } from "./components/PipelineView";
import { RequestThread } from "./components/RequestThread";
import { ReviewView } from "./components/ReviewView";
import { SetupView } from "./components/SetupView";
import { Tabs, type TabItem } from "./components/Tabs";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "./components/ui";
import { useAlfred } from "./hooks/useAlfred";
import { supportsNativeActions } from "./api";
import type { OperatorKey, RequestThreadModel, TabKey } from "./lib/uiTypes";
import { useTheme } from "./lib/useTheme";

// The five lifecycle destinations. Settings is demoted to the top-bar gear.
const PRIMARY_TABS: Array<{ key: TabKey; label: string; icon: typeof Home }> = [
  { key: "home", label: "Home", icon: Home },
  { key: "compose", label: "Ask", icon: MessageSquare },
  { key: "pipeline", label: "Pipeline", icon: GitPullRequest },
  { key: "fleet", label: "Fleet", icon: Bot },
  { key: "lessons", label: "Lessons", icon: Lightbulb },
];

// Fleet groups the live roster and the per-agent activity tail.
const FLEET_SUBTABS: Array<{ key: OperatorKey; label: string }> = [
  { key: "fleet", label: "Roster" },
  { key: "logs", label: "Activity" },
];

// Old ?tab= values (and the legacy hash) map to their new lifecycle home, so a
// deep link from before the IA change still lands somewhere sensible.
const LEGACY_TAB_ALIASES: Record<string, TabKey> = {
  review: "home",
  inbox: "home",
  board: "pipeline",
  work: "pipeline",
  plans: "pipeline",
  agents: "fleet",
  operator: "fleet",
  roster: "fleet",
  memory: "lessons",
  setup: "settings",
};

function rawTabFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  return (
    new URLSearchParams(window.location.search).get("tab") ||
    window.location.hash.replace(/^#/, "") ||
    null
  );
}

function initialTabFromUrl(): TabKey {
  const fallback: TabKey = "home";
  const raw = rawTabFromUrl();
  if (!raw) return fallback;
  const aliased = LEGACY_TAB_ALIASES[raw];
  if (aliased) return aliased;
  if (PRIMARY_TABS.some((item) => item.key === raw)) return raw as TabKey;
  if (raw === "settings") return "settings";
  return fallback;
}

function initialFleetTabFromUrl(): OperatorKey {
  if (typeof window === "undefined") return "fleet";
  const raw = new URLSearchParams(window.location.search).get("subtab") || rawTabFromUrl();
  if (raw === "logs" || raw === "activity") return "logs";
  return "fleet";
}

function App() {
  const [tab, setTab] = useState<TabKey>(() => initialTabFromUrl());
  // Fleet's active subtab (roster vs activity).
  const [fleetTab, setFleetTab] = useState<OperatorKey>(() => initialFleetTabFromUrl());
  // A request opened as a lifecycle thread (from Home shipped cards).
  const [openThread, setOpenThread] = useState<RequestThreadModel | null>(null);

  // An agent card can deep-link into the Activity live-tail for one agent.
  const [logsFocus, setLogsFocus] = useState<{ agent: string | null; nonce: number }>({
    agent: null,
    nonce: 0,
  });
  const [setupMode, setSetupMode] = useState<"guided" | "advanced">("guided");

  // Navigation router. Settings opens guided onboarding; the rest switch the
  // primary surface directly.
  const goTo = useCallback((key: TabKey) => {
    if (key === "logs") {
      setFleetTab("logs");
      setTab("fleet");
      return;
    }
    if (key === "settings") {
      setSetupMode("guided");
    }
    setTab(key);
  }, []);

  const viewAgentLogs = (codename: string) => {
    setLogsFocus((prev) => ({ agent: codename, nonce: prev.nonce + 1 }));
    setFleetTab("logs");
    setTab("fleet");
  };

  const {
    baseUrl,
    snapshot,
    error,
    errorRaw,
    loading,
    busyPlanAction,
    busyMemoryAction,
    busyTrustedUser,
    busyQueue,
    noticeFor,
    nativeBusy,
    nativeResult,
    nativeError,
    nativeErrorRaw,
    clearNativeResult,
    needsYou,
    fleetService,
    feed,
    unseenCount,
    seenIds,
    markActivitySeen,
    shipped,
    shippedState,
    shippedError,
    refreshShipped,
    usage,
    usageState,
    refresh,
    runFollowupAction,
    runPlanDecision,
    runPlanIssueFile,
    runQueueAction,
    runMemoryCandidateAction,
    addTrustedUser,
    removeTrustedUser,
    runLocalAction,
    startRuntime,
  } = useAlfred();

  const { theme, toggle: toggleTheme } = useTheme();
  const [paletteOpen, setPaletteOpen] = useState(false);

  // ⌘K / Ctrl+K opens the command palette anywhere.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [tab, fleetTab, setupMode]);

  const commands = useMemo<Command[]>(() => {
    const nav: Command[] = PRIMARY_TABS.map((item) => ({
      id: `go-${item.key}`,
      label: `Go to ${item.label}`,
      hint: "Navigate",
      icon: item.icon,
      run: () => goTo(item.key),
    }));
    return [
      ...nav,
      {
        id: "go-settings",
        label: "Go to Settings",
        hint: "Navigate",
        icon: Settings,
        run: () => goTo("settings"),
      },
      { id: "refresh", label: "Refresh agent state", hint: "Action", icon: RefreshCw, run: () => void refresh() },
      {
        id: "theme",
        label: `Switch to ${theme === "dark" ? "light" : "dark"} theme`,
        hint: "Appearance",
        icon: theme === "dark" ? Sun : Moon,
        run: toggleTheme,
      },
    ];
  }, [goTo, refresh, theme, toggleTheme]);

  return (
    <AppShell
      baseUrl={baseUrl}
      error={error}
      loading={loading}
      navItems={PRIMARY_TABS}
      onCommand={() => setPaletteOpen(true)}
      onNavigate={goTo}
      onOpenSettings={() => goTo("settings")}
      onRefresh={() => void refresh()}
      onToggleTheme={toggleTheme}
      snapshot={snapshot}
      tab={tab}
      theme={theme}
      unseenCount={unseenCount}
    >

      {error ? (
        <ConnectionBanner
          error={error}
          errorRaw={errorRaw}
          nativeBusy={nativeBusy}
          onStartRuntime={startRuntime}
        />
      ) : null}

      <NativeResultPanel
        error={nativeError}
        errorRaw={nativeErrorRaw}
        result={nativeResult}
        onDismiss={clearNativeResult}
      />

      {tab === "home" ? (
        <ReviewView
          snapshot={snapshot}
          needsYou={needsYou}
          shipped={shipped}
          usage={usage}
          usageState={usageState}
          onSwitch={goTo}
          onOpenThread={setOpenThread}
          onPlanDecision={runPlanDecision}
          busyPlanAction={busyPlanAction}
        />
      ) : null}
      {tab === "pipeline" ? (
        <section className="board-page">
          <PipelineView
            board={shipped}
            state={shippedState}
            error={shippedError}
            plans={snapshot?.plans || []}
            busyPlanAction={busyPlanAction}
            busyQueue={busyQueue}
            notice={noticeFor("board") || noticeFor("plans")}
            onRefresh={() => void refreshShipped()}
            onQueueAction={runQueueAction}
            onDecision={runPlanDecision}
            onFileIssue={runPlanIssueFile}
            onFollowupAction={runFollowupAction}
          />
        </section>
      ) : null}
      {tab === "compose" ? (
        <ComposeView
          baseUrl={baseUrl}
          intakeProfile={snapshot?.status.intake_profile}
          selectedRepos={snapshot?.status.setup_repos?.selected || shipped?.repos || []}
          onSwitch={goTo}
        />
      ) : null}
      {tab === "settings" ? (
        setupMode === "advanced" ? (
          <section className="setup-mode-stack">
            <button className="secondary-button setup-mode-back" type="button" onClick={() => setSetupMode("guided")}>
              <span>Back to guided setup</span>
            </button>
            <SetupView
              baseUrl={baseUrl}
              loading={loading}
              actionNotice={noticeFor("setup")}
              trustedSlack={snapshot?.trustedSlack || null}
              busyTrustedUser={busyTrustedUser}
              nativeBusy={nativeBusy}
              onAddTrustedUser={addTrustedUser}
              onRemoveTrustedUser={removeTrustedUser}
              onRunLocalAction={runLocalAction}
              onStartRuntime={startRuntime}
              onConnectServer={(url) => void refresh(url)}
            />
          </section>
        ) : (
          <OnboardingView
            baseUrl={baseUrl}
            loading={loading}
            connected={Boolean(snapshot) && !error}
            canRun={supportsNativeActions()}
            nativeBusy={nativeBusy}
            nativeResult={nativeResult}
            onConnectServer={(url) => void refresh(url)}
            onStartRuntime={startRuntime}
            onRunLocalAction={runLocalAction}
            onOpenConnection={() => {
              setSetupMode("advanced");
            }}
            onSwitch={goTo}
            onRefreshBoard={(options) => refreshShipped(baseUrl, options)}
          />
        )
      ) : null}

      {tab === "lessons" ? (
        <section className="space-y-4" aria-label="Lessons">
          <MemoryView
            snapshot={snapshot}
            actionNotice={noticeFor("memory")}
            busyMemoryAction={busyMemoryAction}
            nativeBusy={nativeBusy}
            onMemoryCandidateAction={runMemoryCandidateAction}
            onRunLocalAction={runLocalAction}
          />
        </section>
      ) : null}

      {tab === "fleet" ? (
        <section className="space-y-4" aria-label="Fleet">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div className="space-y-1">
              <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
                Fleet
              </h1>
              <p className="max-w-2xl text-sm text-muted-foreground">
                Run, schedule, inspect, and follow Alfred's agent fleet.
              </p>
            </div>
          </div>
          <Tabs
            tabs={FLEET_SUBTABS.map<TabItem<OperatorKey>>((s) => ({
              key: s.key,
              label: s.label,
              badge: s.key === "logs" && unseenCount > 0 ? unseenCount : null,
            }))}
            active={fleetTab}
            onChange={setFleetTab}
            idBase="fleet"
            ariaLabel="Fleet sections"
          />
          {fleetTab === "fleet" ? (
            <div className="space-y-4">
              <FleetControlView
                agents={snapshot?.status.agents || []}
                schedule={snapshot?.schedule || []}
                service={fleetService}
                nativeBusy={nativeBusy}
                onRunLocalAction={runLocalAction}
                onViewLogs={viewAgentLogs}
              />
            </div>
          ) : null}
          {fleetTab === "logs" ? (
            <LogsView
              baseUrl={baseUrl}
              feed={feed}
              unseen={unseenCount}
              seen={seenIds}
              onMarkAllSeen={markActivitySeen}
              onOpenMemory={() => goTo("lessons")}
              firings={snapshot?.firings || []}
              focus={logsFocus}
            />
          ) : null}
        </section>
      ) : null}

      {/* A request opened as a lifecycle thread from a Home shipped card. */}
      {openThread ? (
        <ThreadModal thread={openThread} onClose={() => setOpenThread(null)} onOpenPlan={() => {
          setOpenThread(null);
          goTo("pipeline");
        }} />
      ) : null}

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />
    </AppShell>
  );
}

// A focused modal that shows a single request as a lifecycle thread, opened
// from a Home shipped card. Read-only: it deep-links to GitHub and to the plan
// sign-off, never embedding a diff or merge UI.
function ThreadModal({
  thread,
  onClose,
  onOpenPlan,
}: {
  thread: RequestThreadModel;
  onClose: () => void;
  onOpenPlan: () => void;
}) {
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="thread-modal"
        aria-label="Request thread"
      >
        <DialogTitle className="sr-only">Request thread</DialogTitle>
        <RequestThread thread={thread} onOpenPlan={onOpenPlan} />
      </DialogContent>
    </Dialog>
  );
}

export default App;
