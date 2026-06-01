import {
  Activity,
  PenLine,
  Radio,
  RefreshCw,
  Settings,
  SlidersHorizontal,
  ListChecks,
  MemoryStick,
} from "lucide-react";
import { useState } from "react";

import "./App.css";
import {
  ConnectionBanner,
  NativeResultPanel,
  StatusPill,
} from "./components/atoms";
import { ComposeView } from "./components/ComposeView";
import { FleetControlView } from "./components/FleetControlView";
import { HomeView } from "./components/HomeView";
import { MemoryView } from "./components/MemoryView";
import { NotificationsView } from "./components/NotificationsView";
import { PlansView } from "./components/PlansView";
import { RunsView } from "./components/RunsView";
import { SetupView } from "./components/SetupView";
import { useAlfred } from "./hooks/useAlfred";
import type { TabKey } from "./lib/uiTypes";

const tabs: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: "home", label: "Home", icon: Activity },
  { key: "compose", label: "Compose", icon: PenLine },
  { key: "plans", label: "Plans", icon: ListChecks },
  { key: "memory", label: "Memory", icon: MemoryStick },
  { key: "fleet", label: "Fleet", icon: SlidersHorizontal },
  { key: "logs", label: "Logs", icon: Radio },
];

function App() {
  const [tab, setTab] = useState<TabKey>("home");
  const {
    baseUrl,
    serverInput,
    snapshot,
    error,
    errorRaw,
    loading,
    busyPlanAction,
    busyMemoryAction,
    busyTrustedUser,
    actionNotice,
    nativeBusy,
    nativeResult,
    nativeError,
    nativeErrorRaw,
    attention,
    stats,
    fleetService,
    feed,
    unseenCount,
    seenIds,
    markActivitySeen,
    refresh,
    refreshFleetService,
    runFollowupAction,
    runMemoryCandidateAction,
    addTrustedUser,
    removeTrustedUser,
    runLocalAction,
    startRuntime,
  } = useAlfred();

  return (
    <main className="app-shell">
      <header className="topbar">
        <button
          className="brand"
          type="button"
          aria-label="Alfred home"
          onClick={() => setTab("home")}
        >
          <img src="/brand/alfred-logo-transparent.png" alt="" />
          <span>Alfred</span>
        </button>
        <nav className="topnav" aria-label="Primary">
          {tabs.map((item) => {
            const Icon = item.icon;
            const active = tab === item.key;
            const badge = item.key === "logs" && unseenCount > 0 ? unseenCount : null;
            return (
              <button
                key={item.key}
                className={active ? "nav-button nav-button--active" : "nav-button"}
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => setTab(item.key)}
              >
                <Icon size={17} aria-hidden="true" />
                <span>{item.label}</span>
                {badge ? (
                  <span className="nav-badge" aria-label={`${badge} unread`}>
                    {badge > 9 ? "9+" : badge}
                  </span>
                ) : null}
              </button>
            );
          })}
        </nav>
        <div className="topbar__status">
          <StatusPill snapshot={snapshot} error={error} />
          <button
            className="connect-chip"
            type="button"
            onClick={() => void refresh(serverInput)}
            disabled={loading}
            aria-label={error ? "Reconnect to Alfred serve" : "Refresh fleet state"}
            title={baseUrl}
          >
            <RefreshCw size={15} aria-hidden="true" className={loading ? "spin" : undefined} />
            <span>{loading ? "Checking" : error ? "Connect" : "Refresh"}</span>
          </button>
          <button
            className={
              tab === "setup" ? "settings-button settings-button--active" : "settings-button"
            }
            type="button"
            aria-label="Open setup"
            aria-current={tab === "setup" ? "page" : undefined}
            onClick={() => setTab("setup")}
          >
            <Settings size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      {error ? (
        <ConnectionBanner
          error={error}
          errorRaw={errorRaw}
          nativeBusy={nativeBusy}
          onStartRuntime={startRuntime}
        />
      ) : null}

      <NativeResultPanel error={nativeError} errorRaw={nativeErrorRaw} result={nativeResult} />

      {tab === "home" ? (
        <HomeView
          snapshot={snapshot}
          attention={attention}
          baseUrl={baseUrl}
          stats={stats}
          nativeBusy={nativeBusy}
          loading={loading}
          onRunLocalAction={runLocalAction}
          onRefresh={(value) => void refresh(value ?? serverInput)}
          onSwitch={setTab}
        />
      ) : null}
      {tab === "compose" ? (
        <ComposeView
          baseUrl={baseUrl}
          plans={snapshot?.plans || []}
          actionNotice={actionNotice}
          busyPlanAction={busyPlanAction}
          onFollowupAction={runFollowupAction}
          onSwitch={setTab}
        />
      ) : null}
      {tab === "plans" ? (
        <PlansView
          plans={snapshot?.plans || []}
          actionNotice={actionNotice}
          busyPlanAction={busyPlanAction}
          onFollowupAction={runFollowupAction}
          onSwitch={setTab}
        />
      ) : null}
      {tab === "memory" ? (
        <MemoryView
          snapshot={snapshot}
          actionNotice={actionNotice}
          busyMemoryAction={busyMemoryAction}
          nativeBusy={nativeBusy}
          onMemoryCandidateAction={runMemoryCandidateAction}
          onRunLocalAction={runLocalAction}
        />
      ) : null}
      {tab === "fleet" ? (
        <FleetControlView
          agents={snapshot?.status.agents || []}
          service={fleetService}
          nativeBusy={nativeBusy}
          nativeResult={nativeResult}
          nativeError={nativeError}
          nativeErrorRaw={nativeErrorRaw}
          onRunLocalAction={runLocalAction}
          onRefreshService={refreshFleetService}
        />
      ) : null}
      {tab === "logs" ? (
        <section className="logs-stack">
          <NotificationsView
            feed={feed}
            unseen={unseenCount}
            seen={seenIds}
            onMarkAllSeen={markActivitySeen}
          />
          <RunsView firings={snapshot?.firings || []} />
        </section>
      ) : null}
      {tab === "setup" ? (
        <SetupView
          actionNotice={actionNotice}
          trustedSlack={snapshot?.trustedSlack || null}
          busyTrustedUser={busyTrustedUser}
          nativeBusy={nativeBusy}
          onAddTrustedUser={addTrustedUser}
          onRemoveTrustedUser={removeTrustedUser}
          onRunLocalAction={runLocalAction}
          onStartRuntime={startRuntime}
          onSwitch={setTab}
        />
      ) : null}
    </main>
  );
}

export default App;
