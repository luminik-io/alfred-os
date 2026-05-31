import {
  Activity,
  Bell,
  ListChecks,
  MemoryStick,
  PenLine,
  Radio,
  RefreshCw,
  Server,
  Settings,
  SlidersHorizontal,
} from "lucide-react";
import { useState } from "react";

import "./App.css";
import {
  ConnectionBanner,
  NativeResultPanel,
  StatusPill,
} from "./components/atoms";
import { AgentsView } from "./components/AgentsView";
import { ComposeView } from "./components/ComposeView";
import { FleetControlView } from "./components/FleetControlView";
import { MemoryView } from "./components/MemoryView";
import { NotificationsView } from "./components/NotificationsView";
import { NowView } from "./components/NowView";
import { PlansView } from "./components/PlansView";
import { RunsView } from "./components/RunsView";
import { SetupView } from "./components/SetupView";
import { useAlfred } from "./hooks/useAlfred";
import type { TabKey } from "./lib/uiTypes";

const tabs: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: "now", label: "Now", icon: Activity },
  { key: "activity", label: "Activity", icon: Bell },
  { key: "compose", label: "Compose", icon: PenLine },
  { key: "plans", label: "Plans", icon: ListChecks },
  { key: "runs", label: "Runs", icon: Radio },
  { key: "agents", label: "Agents", icon: Server },
  { key: "fleet", label: "Fleet", icon: SlidersHorizontal },
  { key: "memory", label: "Memory", icon: MemoryStick },
  { key: "setup", label: "Setup", icon: Settings },
];

function App() {
  const [tab, setTab] = useState<TabKey>("now");
  const {
    baseUrl,
    serverInput,
    setServerInput,
    snapshot,
    error,
    errorRaw,
    loading,
    busyPlanAction,
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
          onClick={() => setTab("now")}
        >
          <img src="/brand/alfred-logo-transparent.png" alt="" />
          <span>Alfred</span>
        </button>
        <nav className="topnav" aria-label="Primary">
          {tabs.map((item) => {
            const Icon = item.icon;
            const active = tab === item.key;
            const badge = item.key === "activity" && unseenCount > 0 ? unseenCount : null;
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

      {tab === "now" ? (
        <NowView
          snapshot={snapshot}
          attention={attention}
          baseUrl={baseUrl}
          stats={stats}
          serverInput={serverInput}
          setServerInput={setServerInput}
          loading={loading}
          onRefresh={(value) => void refresh(value ?? serverInput)}
          onSwitch={setTab}
        />
      ) : null}
      {tab === "compose" ? <ComposeView baseUrl={baseUrl} /> : null}
      {tab === "plans" ? (
        <PlansView
          actionNotice={actionNotice}
          busyPlanAction={busyPlanAction}
          plans={snapshot?.plans || []}
          baseUrl={baseUrl}
          onFollowupAction={runFollowupAction}
          onSwitch={setTab}
        />
      ) : null}
      {tab === "runs" ? <RunsView firings={snapshot?.firings || []} baseUrl={baseUrl} /> : null}
      {tab === "activity" ? (
        <NotificationsView
          feed={feed}
          unseen={unseenCount}
          seen={seenIds}
          onMarkAllSeen={markActivitySeen}
        />
      ) : null}
      {tab === "agents" ? (
        <AgentsView
          agents={snapshot?.status.agents || []}
          nativeBusy={nativeBusy}
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
      {tab === "memory" ? (
        <MemoryView snapshot={snapshot} nativeBusy={nativeBusy} onRunLocalAction={runLocalAction} />
      ) : null}
      {tab === "setup" ? (
        <SetupView
          baseUrl={baseUrl}
          actionNotice={actionNotice}
          trustedSlack={snapshot?.trustedSlack || null}
          busyTrustedUser={busyTrustedUser}
          nativeBusy={nativeBusy}
          onAddTrustedUser={addTrustedUser}
          onRemoveTrustedUser={removeTrustedUser}
          onRunLocalAction={runLocalAction}
          onStartRuntime={startRuntime}
        />
      ) : null}
    </main>
  );
}

export default App;
