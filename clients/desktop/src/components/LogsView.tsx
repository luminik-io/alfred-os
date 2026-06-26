import {
  Activity,
  AlertTriangle,
  ChevronRight,
  CircleDot,
  ScrollText,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { loadAgentFirings, streamFiringTail } from "../api";
import { exactTime, friendlyTime } from "../format";
import { formatTranscriptLine } from "../lib/transcript";
import type { FeedItem, FeedTarget } from "../lib/notifications";
import type { FiringRecord, TimelineStep } from "../types";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "./ui/collapsible";
import { Switch } from "./ui/switch";
import { EmptyState, PanelHeader } from "./atoms";
import { NotificationsView } from "./NotificationsView";
import { Tabs, type TabItem } from "./Tabs";

type LogsSubtab = "activity" | "live";

/**
 * The Activity destination splits into two tabs so the page is never one long
 * scroll of two unrelated panels:
 *  - Activity: the cross-agent, newest-first feed of what just happened.
 *  - Latest run: pick one agent and read its most recent run's events. This
 *    refreshes on the dashboard's poll, so it is the newest captured run, not
 *    a live byte-by-byte stream.
 *
 * `focus` lets another surface (e.g. an agent card) deep-link straight
 * into the latest-run view for a specific agent. Each focus carries a nonce so
 * the same agent can be re-focused.
 */
export function LogsView({
  baseUrl,
  feed,
  unseen,
  seen,
  onMarkAllSeen,
  onOpenMemory,
  firings,
  focus,
}: {
  baseUrl: string;
  feed: FeedItem[];
  unseen: number;
  seen: Set<string>;
  onMarkAllSeen: () => void;
  /** A memory-suggestion feed row jumps to the Lessons review queue. */
  onOpenMemory?: () => void;
  firings: FiringRecord[];
  focus: { agent: string | null; nonce: number };
}) {
  const [subtab, setSubtab] = useState<LogsSubtab>("activity");
  const [selectedAgent, setSelectedAgent] = useState<string | null>(focus.agent);

  // A feed row leads somewhere: an agent row opens that agent's latest run in
  // place; a lesson-suggestion row hands off to the Lessons subtab.
  const openFeedTarget = (target: FeedTarget) => {
    if (target.type === "agent") {
      setSelectedAgent(target.codename);
      setSubtab("live");
      return;
    }
    onOpenMemory?.();
  };

  // An agent "View logs" deep-link jumps straight to the latest run for one agent.
  useEffect(() => {
    if (focus.nonce === 0) return;
    setSubtab("live");
    setSelectedAgent(focus.agent);
  }, [focus.nonce, focus.agent]);

  const tabs: TabItem<LogsSubtab>[] = [
    { key: "activity", label: "Activity", icon: Activity, badge: unseen },
    { key: "live", label: "Latest run", icon: ScrollText },
  ];

  return (
    <section className="panel logs-view animate-rise">
      <PanelHeader
        eyebrow="Activity"
        title="Agent runs"
        actionLabel={subtab === "activity" && unseen ? "Mark all read" : undefined}
        onAction={subtab === "activity" && unseen ? onMarkAllSeen : undefined}
      />
      <Tabs
        tabs={tabs}
        active={subtab}
        onChange={setSubtab}
        idBase="logs"
        ariaLabel="Activity sections"
      />
      <div id="logs-panel" role="tabpanel" className="subtab-panel">
        {subtab === "activity" ? (
          <NotificationsView
            feed={feed}
            unseen={unseen}
            seen={seen}
            onMarkAllSeen={onMarkAllSeen}
            onOpenTarget={openFeedTarget}
            embedded
          />
        ) : (
          <LiveTailView
            baseUrl={baseUrl}
            firings={firings}
            selectedAgent={selectedAgent}
            onSelectAgent={setSelectedAgent}
          />
        )}
      </div>
    </section>
  );
}

type AgentLane = {
  codename: string;
  firings: FiringRecord[];
  latestAt: string | null;
  status: string;
};

function buildAgentLanes(firings: FiringRecord[]): AgentLane[] {
  const byAgent = new Map<string, FiringRecord[]>();
  for (const firing of firings) {
    const list = byAgent.get(firing.codename) || [];
    list.push(firing);
    byAgent.set(firing.codename, list);
  }
  const lanes: AgentLane[] = [];
  for (const [codename, list] of byAgent) {
    // Firings arrive newest-first from the API; keep that order per agent.
    lanes.push({
      codename,
      firings: list,
      latestAt: list[0]?.started_at ?? null,
      status: list[0]?.status ?? "unknown",
    });
  }
  // Most-recently-active agent first.
  lanes.sort((a, b) => (b.latestAt || "").localeCompare(a.latestAt || ""));
  return lanes;
}

/**
 * The right-hand console reframed as a scannable list of run cards. Each run
 * defaults to a single honest headline (outcome + key result); a chevron
 * expands it to the full step timeline (pr picked, engine + turns, fallback,
 * PR opened / reviewed). Idle / no-work runs stay quiet; failures are loud and
 * surface the honestly-classified cause (authentication, rate_limit, timeout).
 * An "Errors only" switch hides everything that does not need attention.
 */
function LiveTailView({
  baseUrl,
  firings,
  selectedAgent,
  onSelectAgent,
}: {
  baseUrl: string;
  firings: FiringRecord[];
  selectedAgent: string | null;
  onSelectAgent: (agent: string) => void;
}) {
  // The global feed (/api/firings?limit=14) can push a quieter agent's last
  // run out of view. When a deep-link focuses such an agent, fetch its own
  // history by codename instead of claiming it has no logs.
  const inGlobalFeed = selectedAgent
    ? firings.some((f) => f.codename === selectedAgent)
    : true;
  const [fetched, setFetched] = useState<{ agent: string; rows: FiringRecord[] } | null>(null);
  const [fetching, setFetching] = useState(false);
  const [errorsOnly, setErrorsOnly] = useState(false);

  useEffect(() => {
    if (!selectedAgent || inGlobalFeed) return;
    if (fetched?.agent === selectedAgent) return;
    let cancelled = false;
    setFetching(true);
    loadAgentFirings(baseUrl, selectedAgent)
      .then((rows) => {
        if (!cancelled) setFetched({ agent: selectedAgent, rows });
      })
      .catch(() => {
        if (!cancelled) setFetched({ agent: selectedAgent, rows: [] });
      })
      .finally(() => {
        if (!cancelled) setFetching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAgent, inGlobalFeed, baseUrl, fetched?.agent]);

  const lanes = useMemo(() => {
    const built = buildAgentLanes(firings);
    // Surface the focused agent even when it is not in the global feed, using
    // its own fetched history so the switcher honors the deep-link.
    if (selectedAgent && !built.some((l) => l.codename === selectedAgent)) {
      const rows = fetched?.agent === selectedAgent ? fetched.rows : [];
      built.unshift({
        codename: selectedAgent,
        firings: rows,
        latestAt: rows[0]?.started_at ?? null,
        status: rows[0]?.status ?? "idle",
      });
    }
    return built;
  }, [firings, selectedAgent, fetched]);
  const activeAgent = lanes.find((l) => l.codename === selectedAgent) || lanes[0] || null;

  // The "Errors only" filter is per-agent: switching agents should land on the
  // new agent's full run list, not inherit the previous agent's filter and show
  // a misleading "No runs need attention" empty state for a clean agent.
  const activeCodename = activeAgent?.codename ?? null;
  useEffect(() => {
    setErrorsOnly(false);
  }, [activeCodename]);

  const allRuns = useMemo(() => activeAgent?.firings ?? [], [activeAgent]);
  const errorCount = useMemo(
    () => allRuns.filter((f) => isErrorFiring(f)).length,
    [allRuns],
  );
  const visibleRuns = useMemo(
    () => (errorsOnly ? allRuns.filter((f) => isErrorFiring(f)) : allRuns),
    [allRuns, errorsOnly],
  );

  if (!lanes.length) {
    return (
      <EmptyState
        title="No runs captured yet."
        body="Once an agent fires, pick it here to read its most recent run."
      />
    );
  }

  return (
    <div className="tail-layout">
      <div className="tail-agents" role="tablist" aria-label="Agents">
        {lanes.map((lane) => {
          const isActive = lane.codename === activeAgent?.codename;
          const laneErrors = lane.firings.filter((f) => isErrorFiring(f)).length;
          return (
            <button
              key={lane.codename}
              className={isActive ? "tail-agent tail-agent--active" : "tail-agent"}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => onSelectAgent(lane.codename)}
            >
              <span className={`tail-agent__dot tail-agent__dot--${toneFor(lane.status)}`} aria-hidden="true" />
              <span className="tail-agent__name">{lane.codename}</span>
              <span className="tail-agent__meta">
                {lane.firings.length} run{lane.firings.length === 1 ? "" : "s"}
                {lane.latestAt ? ` · ${friendlyTime(lane.latestAt)}` : ""}
              </span>
              {laneErrors > 0 ? (
                <span className="tail-agent__errors" title={`${laneErrors} run(s) need attention`}>
                  {laneErrors}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="tail-console">
        <header className="tail-console__head">
          <div className="tail-console__title">
            <strong>{activeAgent?.codename}</strong>
            <span className="tail-console__count">
              {allRuns.length} run{allRuns.length === 1 ? "" : "s"}
              {errorCount > 0 ? ` · ${errorCount} need attention` : ""}
            </span>
          </div>
          <label className="tail-filter" title="Show only runs that ended in an error">
            <Switch
              checked={errorsOnly}
              onCheckedChange={setErrorsOnly}
              aria-label="Show errors only"
              disabled={errorCount === 0 && !errorsOnly}
            />
            <span>Errors only</span>
          </label>
        </header>

        {allRuns.length === 0 ? (
          fetching ? (
            <EmptyState
              title={`Loading runs for ${activeAgent?.codename ?? "this agent"}...`}
              body="Fetching this agent's recent firings."
              compact
            />
          ) : (
            <EmptyState
              title={`No runs captured for ${activeAgent?.codename ?? "this agent"}.`}
              body="This agent has not fired recently. Trigger a run from Agents, or pick another agent on the left."
              compact
            />
          )
        ) : visibleRuns.length === 0 ? (
          <EmptyState
            title="No runs need attention."
            body="Every recent run for this agent finished cleanly. Toggle off to see them all."
            compact
          />
        ) : (
          <ol className="run-list" aria-label={`Runs for ${activeAgent?.codename ?? "agent"}`}>
            {visibleRuns.map((firing, index) => (
              <RunCard
                key={firing.firing_id}
                firing={firing}
                baseUrl={baseUrl}
                // Default-expand the newest run so the page is useful at a glance,
                // and always expand a failure so the cause is never a click away.
                defaultOpen={index === 0 || isErrorFiring(firing)}
                live={index === 0 && firing.status === "running"}
              />
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

/** A run ended in an honest failure (server severity, or a legacy error status). */
function isErrorFiring(firing: FiringRecord): boolean {
  if (firing.timeline?.severity === "error") return true;
  // Legacy servers that predate the distilled timeline: fall back to status.
  return !firing.timeline && firing.status === "error";
}

function runHeadline(firing: FiringRecord): string {
  if (firing.timeline?.headline) return firing.timeline.headline;
  if (firing.summary && firing.summary !== "(no summary)") return firing.summary;
  if (firing.status === "running") return "Running";
  return "No summary captured";
}

const ERROR_CAUSE_LABEL: Record<string, string> = {
  authentication: "Authentication",
  rate_limit: "Rate limit",
  budget: "Usage budget",
  overloaded: "Provider overloaded",
  timeout: "Timeout",
  max_turns: "Max turns",
  api_error: "API error",
  checks_failed: "Pre-push checks",
  validation_failed: "Workflow validation",
  failed: "Failed",
};

/**
 * One collapsible run. Collapsed: a severity dot, the honest one-line headline,
 * and the run time. Expanded: the step timeline plus, for the running newest
 * run, the live transcript tail.
 */
function RunCard({
  firing,
  baseUrl,
  defaultOpen,
  live,
}: {
  firing: FiringRecord;
  baseUrl: string;
  defaultOpen: boolean;
  live: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const severity = firing.timeline?.severity ?? (firing.status === "error" ? "error" : "ok");
  const isError = severity === "error";
  const cause = firing.timeline?.error ?? null;
  const steps = firing.timeline?.steps ?? [];
  const headline = runHeadline(firing);

  return (
    <li
      className={`run-card run-card--${severity}${open ? " run-card--open" : ""}`}
      data-severity={severity}
    >
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger className="run-card__head" aria-label={`Toggle run ${firing.firing_id}`}>
          <ChevronRight className="run-card__chevron" aria-hidden="true" />
          <span className={`run-card__dot run-card__dot--${severity}`} aria-hidden="true">
            {isError ? <AlertTriangle size={12} /> : <CircleDot size={12} />}
          </span>
          <span className="run-card__headline">{headline}</span>
          {isError && cause ? (
            <span className="run-card__cause">{ERROR_CAUSE_LABEL[cause] ?? cause}</span>
          ) : null}
          {live ? <span className="run-card__live">live</span> : null}
          <time className="run-card__time" title={exactTime(firing.started_at)}>
            {firing.started_at ? friendlyTime(firing.started_at) : "not started"}
          </time>
        </CollapsibleTrigger>
        <CollapsibleContent className="run-card__body">
          {isError ? (
            <p className="run-card__alert">
              <AlertTriangle size={14} aria-hidden="true" />
              <span>
                This run failed{cause ? `: ${ERROR_CAUSE_LABEL[cause] ?? cause}` : ""}.
                {firing.timeline?.outcome ? (
                  <code className="run-card__outcome">{firing.timeline.outcome}</code>
                ) : null}
              </span>
            </p>
          ) : null}

          {steps.length > 0 ? (
            <ol className="run-steps" aria-label="Run steps">
              {steps.map((step, index) => (
                <RunStep key={`${step.kind}-${index}`} step={step} />
              ))}
            </ol>
          ) : (
            <p className="run-steps__empty">No structured steps captured for this run.</p>
          )}

          {live ? <LiveTranscript firing={firing} baseUrl={baseUrl} /> : null}
        </CollapsibleContent>
      </Collapsible>
    </li>
  );
}

function RunStep({ step }: { step: TimelineStep }) {
  return (
    <li className={`run-step run-step--${step.tone}`}>
      <span className={`run-step__dot run-step__dot--${step.tone}`} aria-hidden="true" />
      <span className="run-step__label">{step.label}</span>
      {step.detail ? <span className="run-step__detail">{step.detail}</span> : null}
      {step.ts ? (
        <time className="run-step__ts" title={exactTime(step.ts)}>
          {shortTime(step.ts)}
        </time>
      ) : null}
    </li>
  );
}

/**
 * Progressive live transcript for the running newest run only (#41). Layered on
 * top of the static step timeline: if the stream errors or is unavailable, this
 * stays empty and the 60s poll keeps the timeline fresh, so it degrades cleanly.
 */
function LiveTranscript({ firing, baseUrl }: { firing: FiringRecord; baseUrl: string }) {
  const [liveLines, setLiveLines] = useState<{ ts: string | null; text: string }[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLiveLines([]);
    if (firing.status !== "running") return;
    const dispose = streamFiringTail(baseUrl, firing.firing_id, {
      onLines: (raw) => {
        const formatted = raw
          .map(formatTranscriptLine)
          .filter((line): line is { ts: string | null; text: string } => line !== null);
        if (formatted.length) {
          setLiveLines((prev) => [...prev, ...formatted]);
        }
      },
      onError: () => {},
    });
    return dispose;
  }, [baseUrl, firing.firing_id, firing.status]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [liveLines]);

  if (liveLines.length === 0) {
    return (
      <p className="run-live__empty">Waiting for the live transcript to start streaming...</p>
    );
  }

  return (
    <div className="run-live" role="log" ref={scrollRef}>
      <ol className="tail-lines tail-lines--live" aria-label="Live transcript">
        {liveLines.map((line, index) => (
          <li key={`live-${index}`} className="tail-line tail-line--live">
            {line.ts ? <span className="tail-line__ts">{line.ts}</span> : null}
            <span className="tail-line__text">{line.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function shortTime(iso: string): string {
  // Just the wall-clock HH:MM:SS for a step; the full timestamp is the title.
  const match = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : iso;
}

function toneFor(status: string): "ok" | "warn" | "error" | "idle" {
  if (status === "error") return "error";
  if (status === "running") return "warn";
  if (status === "ok") return "ok";
  return "idle";
}
