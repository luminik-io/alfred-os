import {
  ArrowRight,
  Bot,
  GitPullRequest,
  Inbox,
  MessageSquare,
  Moon,
  Radio,
  RefreshCw,
  Settings,
  Sun,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ConnectionBanner,
  NativeResultPanel,
} from "./components/atoms";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { ComposeView } from "./components/ComposeView";
import { FleetControlView } from "./components/FleetControlView";
import { KanbanBoard } from "./components/KanbanBoard";
import { AppShell } from "./components/layout/AppShell";
import { LogsView } from "./components/LogsView";
import { MemoryView } from "./components/MemoryView";
import { OnboardingView } from "./components/OnboardingView";
import { PlansView } from "./components/PlansView";
import { RequestThread } from "./components/RequestThread";
import { ReviewView } from "./components/ReviewView";
import { SetupView } from "./components/SetupView";
import { Tabs, type TabItem } from "./components/Tabs";
import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Dialog,
  DialogContent,
  DialogTitle,
} from "./components/ui";
import { useAlfred } from "./hooks/useAlfred";
import { supportsNativeActions } from "./api";
import type { AttentionItem, OperatorKey, RequestThreadModel, TabKey } from "./lib/uiTypes";
import { useTheme } from "./lib/useTheme";

// Job-shaped primary destinations. Agents hosts operator-depth subtabs.
const PRIMARY_TABS: Array<{ key: TabKey; label: string; icon: typeof Inbox }> = [
  { key: "review", label: "Inbox", icon: Inbox },
  { key: "compose", label: "Ask", icon: MessageSquare },
  { key: "board", label: "Work", icon: GitPullRequest },
  { key: "operator", label: "Agents", icon: Bot },
  { key: "setup", label: "Setup", icon: Settings },
];

// Operator-depth surfaces rendered as in-page subtabs under Agents.
const OPERATOR_KEYS: ReadonlySet<TabKey> = new Set<TabKey>([
  "plans",
  "memory",
  "fleet",
  "logs",
]);

// Agents subtab order: control, activity, lessons, and plans.
const FLEET_SUBTABS: Array<{ key: OperatorKey; label: string }> = [
  { key: "fleet", label: "Roster" },
  { key: "logs", label: "Activity" },
  { key: "memory", label: "Lessons" },
  { key: "plans", label: "Plans" },
];

function initialTabFromUrl(): TabKey {
  const fallback: TabKey = "review";
  if (typeof window === "undefined") return fallback;
  const raw = rawTabFromUrl();
  if (!raw) return fallback;
  if (raw === "agents") return "operator";
  if (OPERATOR_KEYS.has(raw as TabKey)) return "operator";
  const primary = PRIMARY_TABS.some((item) => item.key === raw);
  if (primary) return raw as TabKey;
  return fallback;
}

function initialOperatorTabFromUrl(): OperatorKey {
  if (typeof window === "undefined") return "fleet";
  const raw = new URLSearchParams(window.location.search).get("subtab") || rawTabFromUrl();
  return FLEET_SUBTABS.some((item) => item.key === raw) ? (raw as OperatorKey) : "fleet";
}

function rawTabFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  return (
    new URLSearchParams(window.location.search).get("tab") ||
    window.location.hash.replace(/^#/, "") ||
    null
  );
}

function App() {
  const initialTab = initialTabFromUrl();
  const [tab, setTab] = useState<TabKey>(initialTab);
  // The Agents page's active subtab.
  const [operatorTab, setOperatorTab] = useState<OperatorKey>(() =>
    initialOperatorTabFromUrl()
  );
  // A request opened as a lifecycle thread (from Review board / shipped cards).
  const [openThread, setOpenThread] = useState<RequestThreadModel | null>(null);

  // An agent card can deep-link into the Activity live-tail for one agent.
  // The nonce lets the same agent be re-focused on repeated clicks.
  const [logsFocus, setLogsFocus] = useState<{ agent: string | null; nonce: number }>({
    agent: null,
    nonce: 0,
  });
  const [setupMode, setSetupMode] = useState<"guided" | "advanced">("guided");

  // Navigation target router: primary keys switch the main surface; operator
  // keys land on the Agents page with that subtab selected.
  const goTo = useCallback((key: TabKey) => {
    if (OPERATOR_KEYS.has(key)) {
      setOperatorTab(key as OperatorKey);
      setTab("operator");
      return;
    }
    if (key === "setup") {
      setSetupMode("guided");
    }
    setTab(key);
  }, []);

  const viewAgentLogs = (codename: string) => {
    setLogsFocus((prev) => ({ agent: codename, nonce: prev.nonce + 1 }));
    goTo("logs");
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
    inspectionItems,
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
  }, [tab, operatorTab, setupMode]);

  const commands = useMemo<Command[]>(() => {
    const nav: Command[] = PRIMARY_TABS.map((item) => ({
      id: `go-${item.key}`,
      label: `Go to ${item.label}`,
      hint: "Navigate",
      icon: item.icon,
      run: () => goTo(item.key),
    }));
    const operatorNav: Command[] = (
      [
        { key: "plans", label: "Plans" },
        { key: "memory", label: "Lessons" },
        { key: "fleet", label: "Roster" },
        { key: "logs", label: "Activity" },
      ] as Array<{ key: OperatorKey; label: string }>
    ).map((item) => ({
      id: `op-${item.key}`,
      label: `Agents: ${item.label}`,
      hint: "Agents",
      icon: Wrench,
      run: () => {
        goTo(item.key);
      },
    }));
    return [
      ...nav,
      ...operatorNav,
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

      {tab === "review" ? (
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
      {tab === "board" ? (
        <section className="board-page">
          <KanbanBoard
            board={shipped}
            state={shippedState}
            error={shippedError}
            onRefresh={() => void refreshShipped()}
            onQueueAction={runQueueAction}
            busyQueue={busyQueue}
            notice={noticeFor("board")}
            onSwitch={goTo}
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
      {tab === "setup" ? (
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

      {tab === "operator" ? (
        <section className="space-y-4" aria-label="Agents">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div className="space-y-1">
              <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
                Agents
              </h1>
              <p className="max-w-2xl text-sm text-muted-foreground">
                Run, schedule, inspect, and teach Alfred's agent fleet.
              </p>
            </div>
          </div>
          <Tabs
            tabs={FLEET_SUBTABS.map<TabItem<OperatorKey>>((s) => ({
              key: s.key,
              label: s.label,
              badge: s.key === "logs" && unseenCount > 0 ? unseenCount : null,
            }))}
            active={operatorTab}
            onChange={setOperatorTab}
            idBase="operator"
            ariaLabel="Agent workspace sections"
          />
        {operatorTab === "plans" ? (
          <div>
            <PlansView
              plans={snapshot?.plans || []}
              actionNotice={noticeFor("plans")}
              busyPlanAction={busyPlanAction}
              onFollowupAction={runFollowupAction}
              onDecision={runPlanDecision}
              onFileIssue={runPlanIssueFile}
              onSwitch={(next) => {
                goTo(next);
              }}
            />
          </div>
        ) : null}
        {operatorTab === "memory" ? (
          <MemoryView
            snapshot={snapshot}
            actionNotice={noticeFor("memory")}
            busyMemoryAction={busyMemoryAction}
            nativeBusy={nativeBusy}
            onMemoryCandidateAction={runMemoryCandidateAction}
            onRunLocalAction={runLocalAction}
          />
        ) : null}
        {operatorTab === "fleet" ? (
          <div className="space-y-4">
            <FleetControlView
              agents={snapshot?.status.agents || []}
              schedule={snapshot?.schedule || []}
              service={fleetService}
              nativeBusy={nativeBusy}
              onRunLocalAction={runLocalAction}
              onViewLogs={viewAgentLogs}
            />
            <InspectionSignals
              items={inspectionItems}
              onNavigate={(target) => {
                if (target) goTo(target);
              }}
            />
          </div>
        ) : null}
        {operatorTab === "logs" ? (
          <LogsView
            baseUrl={baseUrl}
            feed={feed}
            unseen={unseenCount}
            seen={seenIds}
            onMarkAllSeen={markActivitySeen}
            firings={snapshot?.firings || []}
            focus={logsFocus}
          />
        ) : null}
        </section>
      ) : null}

      {/* A request opened as a lifecycle thread from a Review shipped card. */}
      {openThread ? (
        <ThreadModal thread={openThread} onClose={() => setOpenThread(null)} onOpenPlan={() => {
          setOpenThread(null);
          goTo("plans");
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

function InspectionSignals({
  items,
  onNavigate,
}: {
  items: AttentionItem[];
  onNavigate: (tab: AttentionItem["targetTab"]) => void;
}) {
  if (!items.length) return null;
  return (
    <section className="space-y-3" aria-label="Reliability signals">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="font-heading text-base font-medium text-foreground">
            Reliability signals
          </h2>
          <p className="text-sm text-muted-foreground">
            Stale runs and repeated failures that need inspection.
          </p>
        </div>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {items.map((item) => (
          <Card key={item.id} className="border-border/70 bg-card/80 shadow-sm backdrop-blur">
            <CardHeader className="gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <span className="grid size-8 shrink-0 place-items-center rounded-lg border border-accent/30 bg-accent/10 text-accent">
                  <Radio className="size-4" aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <CardDescription>{item.label}</CardDescription>
                  <CardTitle className="truncate text-base">{item.title}</CardTitle>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="line-clamp-2 text-sm text-muted-foreground">{item.detail}</p>
              {item.command ? (
                <code className="block truncate rounded-md border border-border/70 bg-muted/30 px-2 py-1 text-xs text-muted-foreground">
                  {item.command}
                </code>
              ) : null}
              {item.targetTab ? (
                <Button type="button" variant="outline" onClick={() => onNavigate(item.targetTab)}>
                  <ArrowRight aria-hidden="true" />
                  Inspect runs
                </Button>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

// A focused modal that shows a single request as a lifecycle thread, opened
// from a Review shipped card. Read-only: it deep-links to GitHub and to the
// plan sign-off, never embedding a diff or merge UI.
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
