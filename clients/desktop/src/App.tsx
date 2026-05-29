import {
  Activity,
  AlertTriangle,
  Archive,
  ArrowRight,
  CheckCircle2,
  Clipboard,
  ExternalLink,
  FilePlus2,
  GitPullRequest,
  ListChecks,
  MemoryStick,
  MessageSquare,
  Play,
  Radio,
  RefreshCw,
  Server,
  Settings,
  TerminalSquare,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";

import "./App.css";
import {
  FALLBACK_BASE_URL,
  convertFollowupToDraft,
  initialBaseUrl,
  isDefaultBaseUrl,
  loadSnapshot,
  markFollowupHandled,
  rememberBaseUrl,
  runNativeAction,
  startLocalRuntime,
  supportsNativeActions,
} from "./api";
import { exactTime, friendlyTime, plural, shortId, titleCase } from "./format";
import type {
  AgentSummary,
  FiringRecord,
  NativeAction,
  NativeCommandResult,
  PlanDraft,
  ReliabilitySignal,
  Snapshot,
} from "./types";

type TabKey = "now" | "plans" | "runs" | "agents" | "memory" | "setup";
type FollowupAction = "convert" | "handled";
type ActionNotice = { tone: "ok" | "error"; message: string } | null;
type NativeActionRequest = { action: NativeAction; target?: string; refreshAfter?: boolean };

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
  const [serverInput, setServerInput] = useState(initialBaseUrl);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<TabKey>("now");
  const [busyPlanAction, setBusyPlanAction] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [nativeBusy, setNativeBusy] = useState<string | null>(null);
  const [nativeResult, setNativeResult] = useState<NativeCommandResult | null>(null);
  const [nativeError, setNativeError] = useState<string | null>(null);

  const refresh = useCallback(async (nextBaseUrl = baseUrl) => {
    const targetBaseUrl = nextBaseUrl.trim();
    setLoading(true);
    setError(null);
    try {
      try {
        setSnapshot(await loadSnapshot(targetBaseUrl));
        setBaseUrl(targetBaseUrl);
        setServerInput(targetBaseUrl);
        rememberBaseUrl(targetBaseUrl);
      } catch (firstErr) {
        if (isDefaultBaseUrl(targetBaseUrl)) {
          setSnapshot(await loadSnapshot(FALLBACK_BASE_URL));
          setBaseUrl(FALLBACK_BASE_URL);
          setServerInput(FALLBACK_BASE_URL);
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

  const runFollowupAction = useCallback(
    async (plan: PlanDraft, action: FollowupAction) => {
      const key = `${plan.plan_id}:${action}`;
      setBusyPlanAction(key);
      setActionNotice(null);
      try {
        const result =
          action === "convert"
            ? await convertFollowupToDraft(baseUrl, plan.plan_id)
            : await markFollowupHandled(baseUrl, plan.plan_id);
        const message =
          action === "convert"
            ? `Created planning draft ${result.draft_id || "for the next pass"}.`
            : "Marked the follow-up handled and moved it out of the inbox.";
        setActionNotice({ tone: "ok", message });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh],
  );

  const runLocalAction = useCallback(
    async ({ action, target, refreshAfter = false }: NativeActionRequest) => {
      const key = `${action}:${target || "fleet"}`;
      setNativeBusy(key);
      setNativeError(null);
      setNativeResult(null);
      try {
        const result = await runNativeAction(action, target);
        setNativeResult(result);
        if (refreshAfter) {
          await refresh(baseUrl);
        }
      } catch (err) {
        setNativeError(err instanceof Error ? err.message : String(err));
      } finally {
        setNativeBusy(null);
      }
    },
    [baseUrl, refresh],
  );

  const startRuntime = useCallback(async () => {
    setNativeBusy("runtime:start");
    setNativeError(null);
    setNativeResult(null);
    try {
      const result = await startLocalRuntime();
      setNativeResult(result);
      window.setTimeout(() => void refresh("http://127.0.0.1:7000"), 900);
    } catch (err) {
      setNativeError(err instanceof Error ? err.message : String(err));
    } finally {
      setNativeBusy(null);
    }
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
              value={serverInput}
              onChange={(event) => setServerInput(event.currentTarget.value)}
              onBlur={() => setServerInput(serverInput.trim())}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void refresh(serverInput);
                }
              }}
              spellCheck={false}
            />
            <button
              className="icon-button"
              type="button"
              onClick={() => void refresh(serverInput)}
              disabled={loading}
            >
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

      <NativeResultPanel error={nativeError} result={nativeResult} />

      {tab === "now" ? (
        <NowView snapshot={snapshot} attention={attention} baseUrl={baseUrl} onSwitch={setTab} />
      ) : null}
      {tab === "plans" ? (
        <PlansView
          actionNotice={actionNotice}
          busyPlanAction={busyPlanAction}
          plans={snapshot?.plans || []}
          baseUrl={baseUrl}
          onFollowupAction={runFollowupAction}
        />
      ) : null}
      {tab === "runs" ? <RunsView firings={snapshot?.firings || []} baseUrl={baseUrl} /> : null}
      {tab === "agents" ? (
        <AgentsView
          agents={snapshot?.status.agents || []}
          nativeBusy={nativeBusy}
          onRunLocalAction={runLocalAction}
        />
      ) : null}
      {tab === "memory" ? (
        <MemoryView
          snapshot={snapshot}
          nativeBusy={nativeBusy}
          onRunLocalAction={runLocalAction}
        />
      ) : null}
      {tab === "setup" ? (
        <SetupView
          baseUrl={baseUrl}
          nativeBusy={nativeBusy}
          onRunLocalAction={runLocalAction}
          onStartRuntime={startRuntime}
        />
      ) : null}
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

function PlansView({
  plans,
  baseUrl,
  actionNotice,
  busyPlanAction,
  onFollowupAction,
}: {
  plans: PlanDraft[];
  baseUrl: string;
  actionNotice: ActionNotice;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
}) {
  return (
    <section className="panel">
      <PanelHeader eyebrow="Planning" title="Saved plans and follow-ups" />
      {actionNotice ? (
        <div className={`inline-notice inline-notice--${actionNotice.tone}`}>
          {actionNotice.tone === "ok" ? (
            <CheckCircle2 size={18} aria-hidden="true" />
          ) : (
            <AlertTriangle size={18} aria-hidden="true" />
          )}
          <span>{actionNotice.message}</span>
        </div>
      ) : null}
      {plans.length ? (
        <div className="plan-grid">
          {plans.map((plan) => (
            <PlanCard
              key={plan.plan_id}
              plan={plan}
              baseUrl={baseUrl}
              busyPlanAction={busyPlanAction}
              onFollowupAction={onFollowupAction}
            />
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

function AgentsView({
  agents,
  nativeBusy,
  onRunLocalAction,
}: {
  agents: AgentSummary[];
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const canRun = supportsNativeActions();
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
              <div className="card-actions card-actions--start">
                {canRun ? (
                  <button
                    className="icon-button"
                    type="button"
                    disabled={nativeBusy === `dry_run:${agent.codename}`}
                    onClick={() =>
                      onRunLocalAction({
                        action: "dry_run",
                        target: agent.codename,
                        refreshAfter: true,
                      })
                    }
                  >
                    <Play size={16} aria-hidden="true" />
                    <span>
                      {nativeBusy === `dry_run:${agent.codename}` ? "Running" : "Dry-run"}
                    </span>
                  </button>
                ) : (
                  <CopyButton
                    label="Copy dry-run"
                    value={`alfred dry-run ${agent.codename}`}
                    icon={<Play size={16} aria-hidden="true" />}
                  />
                )}
              </div>
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

function MemoryView({
  snapshot,
  nativeBusy,
  onRunLocalAction,
}: {
  snapshot: Snapshot | null;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const suggestions = snapshot?.actions.promotion_suggestions || [];
  const errors = snapshot?.actions.errors || {};
  const canRun = supportsNativeActions();

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
          body="Use the command below for a deeper local memory health report."
          />
        )}
        <div className="button-stack">
          {canRun ? (
            <>
              <button
                className="icon-button"
                type="button"
                disabled={nativeBusy === "brain_doctor:fleet"}
                onClick={() => onRunLocalAction({ action: "brain_doctor" })}
              >
                <TerminalSquare size={16} aria-hidden="true" />
                <span>{nativeBusy === "brain_doctor:fleet" ? "Checking" : "Run memory check"}</span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={nativeBusy === "redis_status:fleet"}
                onClick={() => onRunLocalAction({ action: "redis_status" })}
              >
                <MemoryStick size={16} aria-hidden="true" />
                <span>
                  {nativeBusy === "redis_status:fleet" ? "Checking" : "Check Redis memory"}
                </span>
              </button>
            </>
          ) : (
            <>
              <CopyButton label="Copy memory command" value="alfred brain doctor --json" />
              <CopyButton label="Copy Redis check" value="alfred brain redis-status --json" />
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function SetupView({
  baseUrl,
  nativeBusy,
  onRunLocalAction,
  onStartRuntime,
}: {
  baseUrl: string;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onStartRuntime: () => void;
}) {
  const canRun = supportsNativeActions();
  const commands = [
    {
      title: "Install or repair Alfred",
      detail: "Use this when the CLI is missing or this machine needs the base setup.",
      command: "bash install.sh",
      action: "copy" as const,
    },
    {
      title: "Start local runtime",
      detail: "Launches Alfred's local API on this machine.",
      command: "alfred serve --no-browser",
      action: "start" as const,
    },
    {
      title: "Check auth",
      detail: "Verifies the local engine authentication Alfred depends on.",
      command: "alfred auth status",
      action: "auth_status" as const,
    },
    {
      title: "Read fleet status",
      detail: "Checks whether configured agents and recent runs are visible.",
      command: "alfred status --json",
      action: "status" as const,
    },
    {
      title: "Check memory health",
      detail: "Verifies fleet-brain and memory review counters.",
      command: "alfred brain doctor --json",
      action: "brain_doctor" as const,
    },
    {
      title: "Dry-run an agent",
      detail: "Runs a no-side-effect simulation for one codename.",
      command: "alfred dry-run lucius",
      action: "dry_run" as const,
    },
  ];
  const [consoleAgent, setConsoleAgent] = useState("lucius");

  return (
    <section className="dashboard-grid">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Setup" title="Command console" />
        <p className="panel-intro">
          The client is the friendly path. Slack remains the collaboration UI, and the CLI remains
          the inspectable runtime underneath. These buttons run Alfred actions locally and show the
          terminal-style result in this app.
        </p>
        <div className="console-panel" aria-label="Local Alfred command console">
          <div className="console-panel__actions">
            <button
              className="icon-button"
              type="button"
              disabled={!canRun || nativeBusy === "runtime:start"}
              onClick={onStartRuntime}
            >
              <Play size={16} aria-hidden="true" />
              <span>{nativeBusy === "runtime:start" ? "Starting" : "Start runtime"}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "status:fleet"}
              onClick={() => onRunLocalAction({ action: "status", refreshAfter: true })}
            >
              <TerminalSquare size={16} aria-hidden="true" />
              <span>Fleet status</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "auth_status:fleet"}
              onClick={() => onRunLocalAction({ action: "auth_status" })}
            >
              <CheckCircle2 size={16} aria-hidden="true" />
              <span>Auth check</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "agents:fleet"}
              onClick={() => onRunLocalAction({ action: "agents", refreshAfter: true })}
            >
              <Server size={16} aria-hidden="true" />
              <span>Agents</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "brain_doctor:fleet"}
              onClick={() => onRunLocalAction({ action: "brain_doctor" })}
            >
              <MemoryStick size={16} aria-hidden="true" />
              <span>Memory</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "redis_status:fleet"}
              onClick={() => onRunLocalAction({ action: "redis_status" })}
            >
              <Radio size={16} aria-hidden="true" />
              <span>Redis</span>
            </button>
          </div>
          <div className="console-agent-row">
            <label htmlFor="dry-run-agent">Dry-run agent</label>
            <input
              id="dry-run-agent"
              value={consoleAgent}
              onChange={(event) => setConsoleAgent(event.currentTarget.value)}
              spellCheck={false}
            />
            <button
              className="icon-button"
              type="button"
              disabled={!canRun || nativeBusy === `dry_run:${consoleAgent.trim()}`}
              onClick={() =>
                onRunLocalAction({
                  action: "dry_run",
                  target: consoleAgent.trim(),
                  refreshAfter: true,
                })
              }
            >
              <Play size={16} aria-hidden="true" />
              <span>Run dry-run</span>
            </button>
          </div>
          {!canRun ? (
            <p className="console-note">
              Native command execution appears here in the desktop app. Browser preview keeps
              commands copyable only.
            </p>
          ) : null}
        </div>
        <div className="cli-fallback">
          <div>
            <strong>CLI fallback</strong>
            <p>The same actions stay available in a terminal when the client is not running.</p>
          </div>
          <div className="cli-chip-list">
            {commands.map((item) => (
              <CopyButton key={item.command} label={item.title} value={item.command} />
            ))}
          </div>
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

function NativeResultPanel({
  error,
  result,
}: {
  error: string | null;
  result: NativeCommandResult | null;
}) {
  if (!error && !result) return null;
  return (
    <div className={`command-result ${error || result?.success === false ? "command-result--error" : ""}`}>
      <div className="command-result__head">
        <TerminalSquare size={18} aria-hidden="true" />
        <strong>{error ? "Action failed" : result?.message || "Action complete"}</strong>
      </div>
      {error ? <p>{error}</p> : null}
      {result ? (
        <>
          <code>{result.command.join(" ")}</code>
          {result.pid ? <p>Process {result.pid} is running in the background.</p> : null}
          {result.status !== null ? <p>Exit status: {result.status}</p> : null}
          {result.stdout ? <pre>{result.stdout}</pre> : null}
          {result.stderr ? <pre>{result.stderr}</pre> : null}
        </>
      ) : null}
    </div>
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

function PlanCard({
  plan,
  baseUrl,
  busyPlanAction,
  onFollowupAction,
}: {
  plan: PlanDraft;
  baseUrl: string;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
}) {
  const slackLink = firstLink(plan.content, /slack\.com/i);
  const parentLink = plan.parent && isSafeExternalUrl(plan.parent) ? plan.parent : null;
  const isFollowup = plan.source === "followup";
  const actionBusy = busyPlanAction?.startsWith(`${plan.plan_id}:`) || false;
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
        {isFollowup ? (
          <>
            <button
              className="icon-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onFollowupAction(plan, "convert")}
            >
              <FilePlus2 size={16} aria-hidden="true" />
              <span>Plan next pass</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onFollowupAction(plan, "handled")}
            >
              <Archive size={16} aria-hidden="true" />
              <span>Mark handled</span>
            </button>
          </>
        ) : null}
        <ExternalButton
          label="Local detail"
          href={localUrl(baseUrl, `/plans/${plan.plan_id}`)}
          icon={<ExternalLink size={16} />}
        />
        {parentLink ? (
          <ExternalButton label="Issue" href={parentLink} icon={<GitPullRequest size={16} />} />
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
  if (!isSafeExternalUrl(href)) {
    return;
  }
  if (window.__TAURI_INTERNALS__) {
    await openUrl(href);
    return;
  }
  window.open(href, "_blank", "noopener,noreferrer");
}

function isSafeExternalUrl(href: string): boolean {
  try {
    const url = new URL(href);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export default App;
