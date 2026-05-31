import { RefreshCw } from "lucide-react";

import type { Snapshot } from "../types";
import type { AttentionItem, TabKey } from "../lib/uiTypes";
import {
  AttentionCard,
  CompactPlanList,
  CompactRunList,
  EmptyState,
  PanelHeader,
} from "./atoms";

// Each metric card routes to the tab that owns its detail. Labels come from
// buildStats() in lib/derive.ts; keep these keys in sync with that source.
const METRIC_TAB: Record<string, TabKey> = {
  Agents: "agents",
  "Runs today": "runs",
  Planning: "plans",
  Memory: "memory",
};

export type StatCard = { label: string; value: string; detail: string };

export function NowView({
  snapshot,
  attention,
  baseUrl,
  stats,
  serverInput,
  setServerInput,
  loading,
  onRefresh,
  onSwitch,
}: {
  snapshot: Snapshot | null;
  attention: AttentionItem[];
  baseUrl: string;
  stats: StatCard[];
  serverInput: string;
  setServerInput: (value: string) => void;
  loading: boolean;
  onRefresh: (value?: string) => void;
  onSwitch: (tab: TabKey) => void;
}) {
  const plans = snapshot?.plans.slice(0, 4) || [];
  const firings = snapshot?.firings.slice(0, 5) || [];

  return (
    <div className="now-view animate-rise">
      <section className="hero-strip" aria-label="Fleet status">
        <div>
          <h1>What needs attention?</h1>
          <p>
            Slack stays the collaboration surface. This app keeps the local fleet, plans,
            memory, and repairs inspectable on this machine.
          </p>
        </div>
        <div className="connection-panel">
          <label htmlFor="base-url">Local server</label>
          <div className="server-row">
            <input
              id="base-url"
              value={serverInput}
              onChange={(event) => setServerInput(event.currentTarget.value)}
              onBlur={() => setServerInput(serverInput.trim())}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  onRefresh(serverInput);
                }
              }}
              spellCheck={false}
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => onRefresh(serverInput)}
              disabled={loading}
            >
              <RefreshCw
                size={18}
                aria-hidden="true"
                className={loading ? "spin" : undefined}
              />
              <span>{loading ? "Checking" : "Refresh"}</span>
            </button>
          </div>
          <small>
            Use Start runtime in the client, then keep this URL on
            <code>127.0.0.1</code>.
          </small>
        </div>
      </section>

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
            actionLabel="Planning"
            onAction={() => onSwitch("plans")}
          />
          {attention.length ? (
            <div className="attention-list">
              {attention.map((item) => (
                <AttentionCard key={item.id} item={item} />
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
            actionLabel="All plans"
            onAction={() => onSwitch("plans")}
          />
          <CompactPlanList plans={plans} baseUrl={baseUrl} />
        </div>

        <div className="panel">
          <PanelHeader
            eyebrow="Runtime"
            title="Recent runs"
            actionLabel="All runs"
            onAction={() => onSwitch("runs")}
          />
          <CompactRunList firings={firings} baseUrl={baseUrl} />
        </div>
      </section>
    </div>
  );
}
