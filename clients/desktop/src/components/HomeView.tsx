import { Brain, ListChecks, Pause, Play, RefreshCw, Settings } from "lucide-react";
import { useMemo, useState } from "react";

import { supportsNativeActions } from "../api";
import type { AttentionItem, NativeActionRequest, StatCard, TabKey } from "../lib/uiTypes";
import type { Snapshot } from "../types";
import {
  AttentionCard,
  CompactPlanList,
  CompactRunList,
  EmptyState,
  PanelHeader,
  SignalCard,
} from "./atoms";

const METRIC_TAB: Partial<Record<string, TabKey>> = {
  Agents: "fleet",
  "Runs today": "logs",
  Planning: "plans",
  Memory: "memory",
};

export function HomeView({
  snapshot,
  attention,
  baseUrl,
  stats,
  nativeBusy,
  loading,
  onRunLocalAction,
  onRefresh,
  onSwitch,
}: {
  snapshot: Snapshot | null;
  attention: AttentionItem[];
  baseUrl: string;
  stats: StatCard[];
  nativeBusy: string | null;
  loading: boolean;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onRefresh: (value?: string) => void;
  onSwitch: (tab: TabKey) => void;
}) {
  const [pendingAll, setPendingAll] = useState<"pause" | "resume" | null>(null);
  const canRun = supportsNativeActions();
  const plans = snapshot?.plans.slice(0, 4) || [];
  const firings = snapshot?.firings.slice(0, 5) || [];
  const memoryCandidates = snapshot?.memoryCandidates.rows || [];
  const suggestions = snapshot?.actions.promotion_suggestions || [];
  const memoryErrors = {
    ...(snapshot?.actions.errors || {}),
    ...(snapshot?.memoryCandidates.error ? { candidates: snapshot.memoryCandidates.error } : {}),
  };
  const memoryErrorEntries = Object.entries(memoryErrors);
  const errorCount = useMemo(
    () => snapshot?.status.agents.filter((agent) => agent.status === "error").length || 0,
    [snapshot],
  );
  const todaySummary = snapshot
    ? `${attention.length ? `${attention.length} decision${attention.length === 1 ? "" : "s"} waiting` : "No decisions waiting"} · ${
        snapshot.status.total_today
      } run${snapshot.status.total_today === 1 ? "" : "s"} today`
    : "Waiting for the local runtime";

  const confirmAll = () => {
    if (!pendingAll) return;
    onRunLocalAction({ action: pendingAll, target: "all", refreshAfter: true });
    setPendingAll(null);
  };

  return (
    <div className="home-view animate-rise">
      <section className="hero-strip hero-strip--home" aria-label="Alfred command center">
        <div className="home-brief">
          <span className="section-kicker">Local command center</span>
          <h1 className="visually-hidden">Alfred command center</h1>
          <p>{todaySummary}</p>
        </div>
        <div className="home-side">
          <div className="home-actions" aria-label="Primary actions">
            <button className="icon-button" type="button" onClick={() => onSwitch("compose")}>
              <ListChecks size={17} aria-hidden="true" />
              <span>Draft work</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={loading}
              onClick={() => onRefresh()}
            >
              <RefreshCw size={17} aria-hidden="true" className={loading ? "spin" : undefined} />
              <span>{loading ? "Refreshing" : "Refresh"}</span>
            </button>
            <button className="secondary-button" type="button" onClick={() => onSwitch("setup")}>
              <Settings size={17} aria-hidden="true" />
              <span>Setup</span>
            </button>
          </div>
          <div className="runtime-strip" aria-label="Connected local server">
            <span>Local server</span>
            <code>{baseUrl}</code>
          </div>
          {canRun ? (
            <div className="fleet-quick-panel">
              <div>
                <span>Fleet controls</span>
                <strong>Scheduled firings</strong>
              </div>
              <div className="fleet-quick-actions">
                <button
                  className="secondary-button"
                  type="button"
                  disabled={nativeBusy === "pause:all"}
                  onClick={() => setPendingAll("pause")}
                >
                  <Pause size={17} aria-hidden="true" />
                  <span>{nativeBusy === "pause:all" ? "Pausing" : "Pause all"}</span>
                </button>
                <button
                  className="warn-button"
                  type="button"
                  disabled={nativeBusy === "resume:all"}
                  onClick={() => setPendingAll("resume")}
                >
                  <Play size={17} aria-hidden="true" />
                  <span>{nativeBusy === "resume:all" ? "Resuming" : "Resume all"}</span>
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </section>

      {pendingAll ? (
        <section className="confirm-bar" role="alertdialog" aria-modal="true">
          <span>
            {pendingAll === "pause" ? "Pause" : "Resume"} scheduled firings for{" "}
            <strong>every agent</strong>?
          </span>
          <div className="confirm-bar__actions">
            <button className="danger-button" type="button" onClick={confirmAll} autoFocus>
              <span>Yes, {pendingAll}</span>
            </button>
            <button className="secondary-button" type="button" onClick={() => setPendingAll(null)}>
              <span>Cancel</span>
            </button>
          </div>
        </section>
      ) : null}

      <section className="metric-grid" aria-label="Summary metrics">
        {stats.map((stat) => {
          const target = METRIC_TAB[stat.label];
          if (!target) {
            return (
              <div className="metric-card" key={stat.label}>
                <span>{stat.label}</span>
                <strong>{stat.value}</strong>
                <small>{stat.detail}</small>
              </div>
            );
          }
          return (
            <button
              className="metric-card metric-card--link"
              type="button"
              key={stat.label}
              onClick={() => onSwitch(target)}
              aria-label={`${stat.label}: ${stat.value}. Open ${stat.label}.`}
            >
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
              <small>{stat.detail}</small>
            </button>
          );
        })}
      </section>

      <section className="dashboard-grid">
        <div className="panel panel--wide">
          <PanelHeader
            eyebrow="Decision queue"
            title="Needs attention"
            actionLabel="Draft work"
            onAction={() => onSwitch("compose")}
          />
          {attention.length ? (
            <div className="attention-list">
              {attention.map((item) => (
                <AttentionCard
                  key={item.id}
                  item={item}
                  onNavigate={(target) => {
                    if (target) onSwitch(target);
                  }}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              title="No human decision waiting."
              body="Alfred did not surface blocked plans, stale workers, or memory review candidates in the latest snapshot."
              tone="ok"
            />
          )}
        </div>

        <div className="panel">
          <PanelHeader
            eyebrow="Planning"
            title="Recent plans"
            actionLabel="Plans"
            onAction={() => onSwitch("plans")}
          />
          <CompactPlanList plans={plans} onOpen={() => onSwitch("plans")} />
        </div>

        <div className="panel">
          <PanelHeader
            eyebrow="Runtime"
            title={errorCount ? `${errorCount} agent errors` : "Recent runs"}
            actionLabel="Logs"
            onAction={() => onSwitch("logs")}
          />
          <CompactRunList firings={firings} onOpen={() => onSwitch("logs")} />
        </div>

        <div className="panel">
          <PanelHeader
            eyebrow="Memory"
            title={memoryErrorEntries.length ? "Memory needs repair" : "Review candidates"}
            actionLabel="Memory"
            onAction={() => onSwitch("memory")}
          />
          {memoryErrorEntries.length ? (
            <dl className="health-list">
              {memoryErrorEntries.map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          ) : memoryCandidates.length ? (
            <div className="compact-list">
              {memoryCandidates.slice(0, 3).map((candidate) => (
                <button key={candidate.id} type="button" onClick={() => onSwitch("memory")}>
                  <span>{candidate.repo}</span>
                  <strong>{candidate.body}</strong>
                  <small>{candidate.source}</small>
                </button>
              ))}
            </div>
          ) : suggestions.length ? (
            <div className="attention-list">
              {suggestions.slice(0, 3).map((signal, index) => (
                <SignalCard
                  key={`${signal.title || signal.message || "memory"}-${index}`}
                  signal={signal}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              title="No memory candidates surfaced."
              body="Slack-curated and fleet-brain suggestions will appear here when they need review."
              icon={Brain}
              compact
              tone="ok"
            />
          )}
        </div>
      </section>
    </div>
  );
}
