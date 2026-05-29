import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clipboard,
  ExternalLink,
  GitPullRequest,
  ListChecks,
  MemoryStick,
  MessageSquare,
  Play,
  Radio,
  RefreshCw,
  Server,
  Settings,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";

import "./App.css";
import {
  FALLBACK_BASE_URL,
  initialBaseUrl,
  isDefaultBaseUrl,
  loadSnapshot,
  rememberBaseUrl,
} from "./api";
import { exactTime, friendlyTime, plural, shortId, titleCase } from "./format";
import type { AgentSummary, FiringRecord, PlanDraft, ReliabilitySignal, Snapshot } from "./types";

type TabKey = "now" | "plans" | "runs" | "agents" | "memory" | "setup";

type AttentionItem = {
  id: string;
  label: string;
  title: string;
  detail: string;
  tone: "ok" | "warn" | "error" | "info";
  command?: string;
  href?: string;
  icon: "plan" | "run" | "memory" | "setup";
};

const tabs: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: "now", label: "Now", icon: Activity },
  { key: "plans", label: "Plans", icon: ListChecks },
  { key: "runs", label: "Runs", icon: Radio },
  { key: "agents", label: "Agents", icon: Server },
  { key: "memory", label: "Memory", icon: MemoryStick },
  { key: "setup", label: "Setup", icon: Settings },
];

function App() {
  const [baseUrl, setBaseUrl] = useState(initialBaseUrl);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<TabKey>("now");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      rememberBaseUrl(baseUrl);
      try {
        setSnapshot(await loadSnapshot(baseUrl));
      } catch (firstErr) {
        if (isDefaultBaseUrl(baseUrl)) {
          setSnapshot(await loadSnapshot(FALLBACK_BASE_URL));
          setBaseUrl(FALLBACK_BASE_URL);
          rememberBaseUrl(FALLBACK_BASE_URL);
        } else {
          throw firstErr;
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const attention = useMemo(() => buildAttention(snapshot, baseUrl), [snapshot, baseUrl]);
  const stats = useMemo(() => buildStats(snapshot), [snapshot]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label="Alfred home" onClick={() => setTab("now")}>
          <img src="/brand/alfred-logo-transparent.png" alt="" />
          <span>Alfred</span>
        </a>
        <nav className="topnav" aria-label="Primary">
          {tabs.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={tab === item.key ? "nav-button nav-button--active" : "nav-button"}
                type="button"
                onClick={() => setTab(item.key)}
              >
                <Icon size={17} aria-hidden="true" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
      </header>

      <section className="hero-strip" aria-label="Fleet status">
        <div>
          <StatusPill snapshot={snapshot} error={error} />
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
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.currentTarget.value)}
              onBlur={() => rememberBaseUrl(baseUrl)}
              spellCheck={false}
            />
            <button className="icon-button" type="button" onClick={refresh} disabled={loading}>
              <RefreshCw size={18} aria-hidden="true" />
              <span>{loading ? "Checking" : "Refresh"}</span>
            </button>
          </div>
          <small>
            Run <code>alfred serve --no-browser</code>, then keep this URL on
            <code>127.0.0.1</code>.
          </small>
        </div>
      </section>

      {error ? <ConnectionBanner error={error} /> : null}

      <section className="metric-grid" aria-label="Summary metrics">
        {stats.map((stat) => (
          <div className="metric-card" key={stat.label}>
            <span>{stat.label}</span>
            <strong>{stat.value}</strong>
            <small>{stat.detail}</small>
          </div>
        ))}
      </section>

      {tab === "now" ? (
        <NowView snapshot={snapshot} attention={attention} baseUrl={baseUrl} onSwitch={setTab} />
      ) : null}
      {tab === "plans" ? <PlansView plans={snapshot?.plans || []} baseUrl={baseUrl} /> : null}
      {tab === "runs" ? <RunsView firings={snapshot?.firings || []} baseUrl={baseUrl} /> : null}
      {tab === "agents" ? <AgentsView agents={snapshot?.status.agents || []} /> : null}
      {tab === "memory" ? <MemoryView snapshot={snapshot} /> : null}
      {tab === "setup" ? <SetupView baseUrl={baseUrl} /> : null}
    </main>
  );
}

function StatusPill({ snapshot, error }: { snapshot: Snapshot | null; error: string | null }) {
  const status = error ? "offline" : snapshot?.status.reliability.status || "checking";
  const tone = error ? "error" : status === "ok" ? "ok" : status === "checking" ? "info" : "warn";
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <span aria-hidden="true" />
      {titleCase(status)}
    </span>
  );
}

function ConnectionBanner({ error }: { error: string }) {
  return (
    <section className="notice-panel notice-panel--warn">
      <AlertTriangle size={20} aria-hidden="true" />
      <div>
        <strong>Alfred serve is not reachable yet.</strong>
        <p>{error}</p>
      </div>
      <CopyButton label="Copy start command" value="alfred serve --no-browser" />
    </section>
  );
}

function NowView({
  snapshot,
  attention,
  baseUrl,
  onSwitch,
}: {
  snapshot: Snapshot | null;
  attention: AttentionItem[];
  baseUrl: string;
  onSwitch: (tab: TabKey) => void;
}) {
  const plans = snapshot?.plans.slice(0, 4) || [];
  const firings = snapshot?.firings.slice(0, 5) || [];

  return (
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
  );
}

function PlansView({ plans, baseUrl }: { plans: PlanDraft[]; baseUrl: string }) {
  return (
    <section className="panel">
      <PanelHeader eyebrow="Planning" title="Saved plans and follow-ups" />
      {plans.length ? (
        <div className="plan-grid">
          {plans.map((plan) => (
            <PlanCard key={plan.plan_id} plan={plan} baseUrl={baseUrl} />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No plans saved yet."
          body="Batman plans, Slack planning drafts, and trusted follow-ups appear here once the listener or planning page writes them."
        />
      )}
    </section>
  );
}

function RunsView({ firings, baseUrl }: { firings: FiringRecord[]; baseUrl: string }) {
  return (
    <section className="panel">
      <PanelHeader eyebrow="Runtime" title="Recent firings" />
      {firings.length ? (
        <div className="run-list">
          {firings.map((firing) => (
            <RunCard key={firing.firing_id} firing={firing} baseUrl={baseUrl} />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No runs found."
          body="Run an agent or check that this client points at the same ALFRED_HOME as alfred serve."
        />
      )}
    </section>
  );
}

function AgentsView({ agents }: { agents: AgentSummary[] }) {
  return (
    <section className="panel">
      <PanelHeader eyebrow="Fleet" title="Agents" />
      {agents.length ? (
        <div className="agent-grid">
          {agents.map((agent) => (
            <article className="agent-card" key={agent.codename}>
              <div className="agent-card__head">
                <strong>{agent.codename}</strong>
                <StatusDot status={agent.status} />
              </div>
              <dl>
                <div>
                  <dt>Last run</dt>
                  <dd title={exactTime(agent.last_run_at)}>{friendlyTime(agent.last_run_at)}</dd>
                </div>
                <div>
                  <dt>Firings today</dt>
                  <dd>{agent.firings_today}</dd>
                </div>
              </dl>
              <p>{agent.last_summary}</p>
              <CopyButton
                label="Copy dry-run"
                value={`alfred dry-run ${agent.codename}`}
                icon={<Play size={16} aria-hidden="true" />}
              />
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No agents detected."
          body="The local server did not find per-codename state under ALFRED_HOME/state."
        />
      )}
    </section>
  );
}

function MemoryView({ snapshot }: { snapshot: Snapshot | null }) {
  const suggestions = snapshot?.actions.promotion_suggestions || [];
  const errors = snapshot?.actions.errors || {};

  return (
    <section className="dashboard-grid dashboard-grid--memory">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Memory" title="Review queue" />
        {suggestions.length ? (
          <div className="attention-list">
            {suggestions.map((signal, index) => (
              <SignalCard key={`${signal.title || signal.message || "memory"}-${index}`} signal={signal} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No memory candidates surfaced."
            body="Promotion suggestions show up here after fleet-brain finds high-confidence lessons with evidence."
          />
        )}
      </div>
      <div className="panel">
        <PanelHeader eyebrow="Checks" title="Memory health" />
        {Object.keys(errors).length ? (
          <dl className="health-list">
            {Object.entries(errors).map(([key, value]) => (
              <div key={key}>
                <dt>{titleCase(key)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <EmptyState
            title="No memory errors reported."
            body="Use the command below for a deeper local memory doctor report."
          />
        )}
        <CopyButton label="Copy doctor command" value="alfred brain doctor --json" />
      </div>
    </section>
  );
}

function SetupView({ baseUrl }: { baseUrl: string }) {
  const commands = [
    {
      title: "Start the local API",
      detail: "Run this once before opening Alfred Desktop.",
      command: "alfred serve --no-browser",
    },
    {
      title: "Use the fallback port",
      detail: "Use this when another local tool already owns port 7000.",
      command: "alfred serve --port 7010 --no-browser",
    },
    {
      title: "Check fleet health",
      detail: "Runs the same preflight used by scheduled agents.",
      command: "bash bin/doctor.sh --dev",
    },
    {
      title: "Dry-run one agent",
      detail: "Use the codename shown on the Agents tab.",
      command: "alfred dry-run lucius",
    },
  ];

  return (
    <section className="dashboard-grid">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Setup" title="Local control center" />
        <div className="setup-stack">
          {commands.map((item) => (
            <div className="command-row" key={item.command}>
              <div>
                <strong>{item.title}</strong>
                <p>{item.detail}</p>
                <code>{item.command}</code>
              </div>
              <CopyButton label="Copy" value={item.command} />
            </div>
          ))}
        </div>
      </div>
      <div className="panel">
        <PanelHeader eyebrow="Links" title="Open locally" />
        <div className="link-stack">
          <ExternalButton label="Open serve" href={baseUrl} icon={<ExternalLink size={16} />} />
          <ExternalButton
            label="Open plans"
            href={localUrl(baseUrl, "/plans")}
            icon={<ListChecks size={16} />}
          />
          <ExternalButton
            label="Open GitHub"
            href="https://github.com/luminik-io/alfred-os"
            icon={<GitPullRequest size={16} />}
          />
        </div>
      </div>
    </section>
  );
}

function AttentionCard({ item }: { item: AttentionItem }) {
  const Icon = item.icon === "memory" ? MemoryStick : item.icon === "run" ? Radio : item.icon === "setup" ? Settings : ListChecks;
  return (
    <article className={`attention-card attention-card--${item.tone}`}>
      <Icon size={20} aria-hidden="true" />
      <div>
        <span>{item.label}</span>
        <strong>{item.title}</strong>
        <p>{item.detail}</p>
        {item.command ? <code>{item.command}</code> : null}
      </div>
      <div className="card-actions">
        {item.href ? <ExternalButton label="Open" href={item.href} icon={<ExternalLink size={16} />} /> : null}
        {item.command ? <CopyButton label="Copy" value={item.command} /> : null}
      </div>
    </article>
  );
}

function SignalCard({ signal }: { signal: ReliabilitySignal }) {
  return (
    <article className="attention-card attention-card--info">
      <MemoryStick size={20} aria-hidden="true" />
      <div>
        <span>{signal.severity || "memory"}</span>
        <strong>{signal.title || signal.action || signal.codename || "Memory candidate"}</strong>
        <p>{signal.message || signal.summary || signal.reason || "Review evidence before promotion."}</p>
      </div>
      {signal.command ? <CopyButton label="Copy" value={signal.command} /> : null}
    </article>
  );
}

function PlanCard({ plan, baseUrl }: { plan: PlanDraft; baseUrl: string }) {
  const slackLink = firstLink(plan.content, /slack\.com/i);
  return (
    <article className="plan-card">
      <div>
        <div className="plan-card__meta">
          <span>{plan.source}</span>
          <span>{plan.status}</span>
          {plan.readiness_score !== null ? <span>{plan.readiness_score}/100</span> : null}
        </div>
        <h2>{plan.title}</h2>
        <p>{plan.preview}</p>
        <dl className="compact-meta">
          {plan.affected_repos ? (
            <div>
              <dt>Repos</dt>
              <dd>{plan.affected_repos}</dd>
            </div>
          ) : null}
          {plan.updated_at ? (
            <div>
              <dt>Updated</dt>
              <dd title={exactTime(plan.updated_at)}>{friendlyTime(plan.updated_at)}</dd>
            </div>
          ) : null}
        </dl>
      </div>
      <div className="card-actions">
        <ExternalButton
          label="Local detail"
          href={localUrl(baseUrl, `/plans/${plan.plan_id}`)}
          icon={<ExternalLink size={16} />}
        />
        {plan.parent ? (
          <ExternalButton label="Issue" href={plan.parent} icon={<GitPullRequest size={16} />} />
        ) : null}
        {slackLink ? (
          <ExternalButton label="Slack" href={slackLink} icon={<MessageSquare size={16} />} />
        ) : null}
      </div>
    </article>
  );
}

function RunCard({ firing, baseUrl }: { firing: FiringRecord; baseUrl: string }) {
  return (
    <article className="run-card">
      <div className="run-card__status">
        <StatusDot status={firing.status} />
      </div>
      <div>
        <div className="run-card__meta">
          <strong>{firing.codename}</strong>
          <code title={firing.firing_id}>{shortId(firing.firing_id)}</code>
          <time title={exactTime(firing.started_at)}>{friendlyTime(firing.started_at)}</time>
        </div>
        <p>{firing.summary}</p>
      </div>
      <ExternalButton
        label="Trace"
        href={localUrl(baseUrl, `/firings/${firing.firing_id}`)}
        icon={<ExternalLink size={16} />}
      />
    </article>
  );
}

function CompactPlanList({ plans, baseUrl }: { plans: PlanDraft[]; baseUrl: string }) {
  if (!plans.length) {
    return <EmptyState title="No plans yet." body="Planning drafts will appear here." compact />;
  }
  return (
    <div className="compact-list">
      {plans.map((plan) => (
        <button key={plan.plan_id} type="button" onClick={() => void openExternal(localUrl(baseUrl, `/plans/${plan.plan_id}`))}>
          <span>{plan.status}</span>
          <strong>{plan.title}</strong>
          <small>{friendlyTime(plan.updated_at)}</small>
        </button>
      ))}
    </div>
  );
}

function CompactRunList({ firings, baseUrl }: { firings: FiringRecord[]; baseUrl: string }) {
  if (!firings.length) {
    return <EmptyState title="No runs yet." body="Recent firing traces will appear here." compact />;
  }
  return (
    <div className="compact-list">
      {firings.map((firing) => (
        <button key={firing.firing_id} type="button" onClick={() => void openExternal(localUrl(baseUrl, `/firings/${firing.firing_id}`))}>
          <span>{firing.codename}</span>
          <strong>{firing.summary}</strong>
          <small>{friendlyTime(firing.started_at)}</small>
        </button>
      ))}
    </div>
  );
}

function PanelHeader({
  eyebrow,
  title,
  actionLabel,
  onAction,
}: {
  eyebrow: string;
  title: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="panel-header">
      <div>
        <span>{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      {actionLabel && onAction ? (
        <button className="text-button" type="button" onClick={onAction}>
          {actionLabel}
          <ArrowRight size={16} aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const tone = status === "live" || status === "ok" ? "ok" : status === "error" ? "error" : status === "running" ? "warn" : "idle";
  return (
    <span className={`dot-label dot-label--${tone}`}>
      <span aria-hidden="true" />
      {titleCase(status)}
    </span>
  );
}

function EmptyState({
  title,
  body,
  compact = false,
}: {
  title: string;
  body: string;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "empty-state empty-state--compact" : "empty-state"}>
      <CheckCircle2 size={20} aria-hidden="true" />
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

function ExternalButton({ label, href, icon }: { label: string; href: string; icon: React.ReactNode }) {
  return (
    <button className="secondary-button" type="button" onClick={() => void openExternal(href)}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

function CopyButton({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon?: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="secondary-button"
      type="button"
      onClick={async () => {
        await navigator.clipboard.writeText(value);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }}
    >
      {icon || <Clipboard size={16} aria-hidden="true" />}
      <span>{copied ? "Copied" : label}</span>
    </button>
  );
}

function buildStats(snapshot: Snapshot | null) {
  const agents = snapshot?.status.agents || [];
  const reliability = snapshot?.actions;
  const live = agents.filter((agent) => agent.status === "live").length;
  const errored = agents.filter((agent) => agent.status === "error").length;
  return [
    {
      label: "Agents",
      value: agents.length ? `${live}/${agents.length}` : "0",
      detail: agents.length ? `${plural(errored, "error")} visible` : "waiting for state",
    },
    {
      label: "Runs today",
      value: String(snapshot?.status.total_today || 0),
      detail: snapshot ? `updated ${friendlyTime(snapshot.loadedAt.toISOString())}` : "not loaded",
    },
    {
      label: "Planning",
      value: String(snapshot?.plans.length || 0),
      detail: "saved plans and follow-ups",
    },
    {
      label: "Memory",
      value: String(reliability?.promotion_suggestions?.length || 0),
      detail: "review candidates",
    },
  ];
}

function buildAttention(snapshot: Snapshot | null, baseUrl: string): AttentionItem[] {
  if (!snapshot) {
    return [
      {
        id: "connect",
        label: "Setup",
        title: "Connect to the local Alfred server",
        detail: "Start alfred serve so the client can read local state.",
        tone: "warn",
        command: "alfred serve --no-browser",
        icon: "setup",
      },
    ];
  }

  const items: AttentionItem[] = [];
  for (const [index, signal] of (snapshot.actions.actions || []).entries()) {
    items.push(signalToAttention(signal, `action-${index}`));
  }
  for (const [index, signal] of (snapshot.actions.stale_workers || []).entries()) {
    items.push(signalToAttention(signal, `stale-${index}`, "run"));
  }
  for (const [index, signal] of (snapshot.actions.failure_patterns || []).entries()) {
    items.push(signalToAttention(signal, `failure-${index}`, "run", "error"));
  }
  for (const plan of snapshot.plans.filter((plan) => planNeedsAttention(plan)).slice(0, 4)) {
    items.push({
      id: `plan-${plan.plan_id}`,
      label: titleCase(plan.status || "plan"),
      title: plan.title,
      detail: plan.preview || plan.affected_repos || "Review plan context before Alfred implements it.",
      tone: plan.status.includes("question") ? "warn" : "info",
      href: localUrl(baseUrl, `/plans/${plan.plan_id}`),
      icon: "plan",
    });
  }
  for (const [index, signal] of (snapshot.actions.promotion_suggestions || []).entries()) {
    items.push(signalToAttention(signal, `memory-${index}`, "memory"));
  }

  return items.slice(0, 8);
}

function signalToAttention(
  signal: ReliabilitySignal,
  id: string,
  icon: AttentionItem["icon"] = "setup",
  tone: AttentionItem["tone"] = "warn",
): AttentionItem {
  return {
    id,
    label: titleCase(signal.severity || signal.codename || "Action"),
    title: titleCase(signal.title || signal.action || signal.codename || "Review Alfred signal"),
    detail: signal.message || signal.summary || signal.reason || "Open the local source before changing state.",
    command: signal.command,
    tone,
    icon,
  };
}

function planNeedsAttention(plan: PlanDraft): boolean {
  const status = plan.status.toLowerCase();
  return (
    status.includes("draft") ||
    status.includes("follow") ||
    status.includes("question") ||
    status.includes("blocked")
  );
}

function localUrl(baseUrl: string, path: string): string {
  try {
    const url = new URL(baseUrl);
    url.pathname = path;
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return path;
  }
}

function firstLink(text: string, matcher: RegExp): string | null {
  const urls = text.match(/https?:\/\/[^\s)]+/g) || [];
  return urls.find((url) => matcher.test(url)) || null;
}

async function openExternal(href: string): Promise<void> {
  if (window.__TAURI_INTERNALS__) {
    await openUrl(href);
    return;
  }
  window.open(href, "_blank", "noopener,noreferrer");
}

export default App;
