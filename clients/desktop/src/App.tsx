import {
  Moon,
  RefreshCw,
  Sun,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AppearancePicker } from "./components/AppearancePicker";
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
import { CustomThemeEditor } from "./components/CustomThemeEditor";
import { RosterThemePicker } from "./components/RosterThemePicker";
import { SetupView } from "./components/SetupView";
import { Tabs, type TabItem } from "./components/Tabs";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "./components/ui";
import { useAlfred } from "./hooks/useAlfred";
import { useDesktopRoute } from "./hooks/useDesktopRoute";
import { hasStoredBaseUrl, supportsNativeActions } from "./api";
import { FLEET_SUBTABS, PRIMARY_TABS } from "./lib/primaryTabs";
import type { OperatorKey, RequestThreadModel, TabKey } from "./lib/uiTypes";
import { useRosterTheme } from "./lib/useRosterTheme";
import { useTheme } from "./lib/useTheme";

function App() {
  const { fleetTab, setFleetTab, setSetupMode, setTab, setupMode, tab } =
    useDesktopRoute();
  // A request opened as a lifecycle thread (from Inbox shipped cards).
  const [openThread, setOpenThread] = useState<RequestThreadModel | null>(null);

  // An agent card can deep-link into the Activity live-tail for one agent.
  const [logsFocus, setLogsFocus] = useState<{ agent: string | null; nonce: number }>({
    agent: null,
    nonce: 0,
  });
  // Navigation router. Legacy callers may still say "logs" or "lessons"; the
  // route hook maps both into Agents subtabs.
  const goTo = useCallback((key: TabKey) => {
    // Agents opens on the role roster. Lessons and Activity remain subtabs.
    if (key === "fleet") {
      setFleetTab("fleet");
    }
    setTab(key);
  }, [setTab, setFleetTab]);

  const viewAgentLogs = (codename: string) => {
    setLogsFocus((prev) => ({ agent: codename, nonce: prev.nonce + 1 }));
    setFleetTab("logs");
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
    runPlanDiscard,
    runPlanIssueFile,
    runQueueAction,
    runMemoryCandidateAction,
    addTrustedUser,
    removeTrustedUser,
    runLocalAction,
    startRuntime,
  } = useAlfred();
  const runtimeConnected = Boolean(snapshot) && !error;

  const { theme, toggle: toggleTheme, themeName, setThemeName, mode, setMode } =
    useTheme();
  const {
    rosterTheme,
    customNames,
    setRosterTheme,
    setCustomNames,
    saveError: rosterSaveError,
    hydrating: rosterHydrating,
    hydrationError: rosterHydrationError,
    retryHydration: retryRosterHydration,
  } = useRosterTheme(baseUrl, runtimeConnected);
  const [customThemeEditorOpen, setCustomThemeEditorOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  // The Setup tab splits into Setup (get Alfred running) and Settings (appearance
  // and preferences), so theme selection no longer crowds the onboarding flow.
  const [settingsTab, setSettingsTab] = useState<"setup" | "settings">("setup");
  const rosterWritesBlocked = Boolean(rosterHydrating || rosterHydrationError);
  const rosterWriteError = rosterSaveError ?? rosterHydrationError;
  const rosterEditorBlockedError =
    rosterHydrationError ??
    (rosterHydrating ? "Wait for Alfred to load the saved fleet names before changing them." : null);

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

  // First-run routing. A clean launch with no `alfred serve` running settles
  // with a connection error and no snapshot. Without this, that user lands on
  // an empty Home behind an error banner with no obvious next step; the setup
  // wizard is only reachable by hunting through Settings. So the very first
  // time the initial load settles with no server reachable and Alfred has
  // never connected on this machine, route the user straight into guided
  // onboarding. It fires once: once they have connected, or once we have
  // redirected, we never yank them out of wherever they navigate next.
  //
  // Seed the guard from the persisted base URL so a returning user who has
  // connected before (and therefore has a stored URL) is never force-routed
  // into onboarding on a cold start where the runtime simply is not up yet.
  // Without this seed both signals reset every process start and the wizard
  // re-fires for established users.
  const [firstRunRouted, setFirstRunRouted] = useState(() => hasStoredBaseUrl());
  const hasEverConnected = useRef(false);
  useEffect(() => {
    if (snapshot && !error) {
      hasEverConnected.current = true;
    }
  }, [snapshot, error]);
  useEffect(() => {
    if (firstRunRouted) return;
    // Wait for the initial load to settle before deciding anything.
    if (loading) return;
    if (hasEverConnected.current || snapshot) {
      // Already connected at least once: this is not a fresh first run.
      setFirstRunRouted(true);
      return;
    }
    if (error) {
      // Fresh machine, runtime not up yet: take the user to the wizard.
      setFirstRunRouted(true);
      setSetupMode("guided");
      setTab("settings");
      setSettingsTab("setup");
    }
  }, [firstRunRouted, loading, error, snapshot, setSetupMode, setTab, setSettingsTab]);

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
            onDiscardPlan={runPlanDiscard}
            onFileIssue={runPlanIssueFile}
            onFollowupAction={runFollowupAction}
          />
        </section>
      ) : null}
      {tab === "compose" ? (
        <ComposeView
          baseUrl={baseUrl}
          selectedRepos={snapshot?.status.setup_repos?.selected || shipped?.repos || []}
          onSwitch={goTo}
        />
      ) : null}
      {tab === "settings" ? (
        <section className="settings-page space-y-4" aria-label="Setup and settings">
          <div className="space-y-1">
            <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
              {settingsTab === "settings" ? "Settings" : "Setup"}
            </h1>
            <p className="max-w-2xl text-sm text-muted-foreground">
              {settingsTab === "settings"
                ? "Tune how Alfred looks and behaves on this Mac."
                : "Connect local tools, choose repos, and get Alfred running."}
            </p>
          </div>
          <Tabs
            tabs={
              [
                { key: "setup", label: "Setup" },
                { key: "settings", label: "Settings" },
              ] as TabItem<"setup" | "settings">[]
            }
            active={settingsTab}
            onChange={setSettingsTab}
            idBase="settings"
            ariaLabel="Setup and settings sections"
          />
          {settingsTab === "settings" ? (
            <section className="alfred-page-hero px-4 py-4" aria-label="Appearance">
              <div className="space-y-1">
                <p className="text-[0.68rem] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                  Appearance
                </p>
                <h2 className="font-heading text-lg font-medium text-foreground">
                  Theme and mode
                </h2>
                <p className="max-w-2xl text-sm text-muted-foreground">
                  Choose how Alfred looks on this Mac.
                </p>
              </div>
              <div className="mt-3">
                <AppearancePicker
                  themeName={themeName}
                  mode={mode}
                  onSelectTheme={setThemeName}
                  onSelectMode={setMode}
                />
              </div>
            </section>
          ) : setupMode === "advanced" ? (
            <section className="setup-mode-stack">
              <button
                className="secondary-button setup-mode-back"
                type="button"
                onClick={() => setSetupMode("guided")}
              >
                <span>Back to guided setup</span>
              </button>
              <SetupView
                baseUrl={baseUrl}
                loading={loading}
                connected={runtimeConnected}
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
              connected={runtimeConnected}
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
              rosterTheme={rosterTheme}
              customNames={customNames}
              rosterSaveError={rosterSaveError}
              rosterHydrating={rosterHydrating}
              rosterHydrationError={rosterHydrationError}
              onRetryRosterHydration={retryRosterHydration}
              onRosterThemeChange={setRosterTheme}
              onCustomNamesChange={setCustomNames}
            />
          )}
        </section>
      ) : null}

      {tab === "fleet" ? (
        <section className="space-y-4" aria-label="Agents">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div className="space-y-1">
              <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
                Agents
              </h1>
              <p className="max-w-2xl text-sm text-muted-foreground">
                Understand roles, run agents manually, tune cadence, and inspect
                what the fleet learned.
              </p>
            </div>
            {fleetTab === "fleet" ? (
              <RosterThemePicker
                value={rosterTheme}
                onChange={setRosterTheme}
                onEditCustom={() => setCustomThemeEditorOpen(true)}
                disabled={rosterWritesBlocked}
                saveError={rosterWriteError}
                onRetry={rosterHydrationError ? retryRosterHydration : undefined}
              />
            ) : null}
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
            ariaLabel="Agent sections"
          />
          {fleetTab === "fleet" ? (
            <div className="space-y-4 motion-fade" key="fleet-roster">
              <FleetControlView
                agents={snapshot?.status.agents || []}
                schedule={snapshot?.schedule || []}
                service={fleetService}
                nativeBusy={nativeBusy}
                rosterTheme={rosterTheme}
                customNames={customNames}
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
          {fleetTab === "lessons" ? (
            <section className="space-y-4 motion-fade" aria-label="Lessons">
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

      <CustomThemeEditor
        open={customThemeEditorOpen}
        value={customNames}
        blockedError={rosterEditorBlockedError}
        onOpenChange={setCustomThemeEditorOpen}
        onSave={setCustomNames}
        onRetryBlocked={rosterHydrationError ? retryRosterHydration : undefined}
      />
    </AppShell>
  );
}

// A focused modal that shows a single request as a lifecycle thread, opened
// from an Inbox shipped card. Read-only: it deep-links to GitHub and to the plan
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
