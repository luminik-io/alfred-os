import { Activity, ScrollText } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { loadAgentFirings, streamFiringTail } from "../api";
import { exactTime, friendlyTime } from "../format";
import { formatTranscriptLine } from "../lib/transcript";
import type { FeedItem } from "../lib/notifications";
import type { FiringRecord } from "../types";
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
  firings,
  focus,
}: {
  baseUrl: string;
  feed: FeedItem[];
  unseen: number;
  seen: Set<string>;
  onMarkAllSeen: () => void;
  firings: FiringRecord[];
  focus: { agent: string | null; nonce: number };
}) {
  const [subtab, setSubtab] = useState<LogsSubtab>("activity");
  const [selectedAgent, setSelectedAgent] = useState<string | null>(focus.agent);

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
          <NotificationsView feed={feed} unseen={unseen} seen={seen} onMarkAllSeen={onMarkAllSeen} embedded />
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
  const [firingId, setFiringId] = useState<string | null>(null);

  const activeFiring =
    activeAgent?.firings.find((f) => f.firing_id === firingId) || activeAgent?.firings[0] || null;

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
          return (
            <button
              key={lane.codename}
              className={isActive ? "tail-agent tail-agent--active" : "tail-agent"}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => {
                onSelectAgent(lane.codename);
                setFiringId(null);
              }}
            >
              <span className={`tail-agent__dot tail-agent__dot--${toneFor(lane.status)}`} aria-hidden="true" />
              <span className="tail-agent__name">{lane.codename}</span>
              <span className="tail-agent__meta">
                {lane.firings.length} run{lane.firings.length === 1 ? "" : "s"}
                {lane.latestAt ? ` · ${friendlyTime(lane.latestAt)}` : ""}
              </span>
            </button>
          );
        })}
      </div>

      <div className="tail-console">
        {activeFiring ? (
          <>
            <header className="tail-console__head">
              <div>
                <strong>{activeAgent?.codename}</strong>
                <span className={`tail-status tail-status--${toneFor(activeFiring.status)}`}>
                  {activeFiring.status}
                </span>
              </div>
              <small title={exactTime(activeFiring.started_at)}>
                {activeFiring.started_at ? friendlyTime(activeFiring.started_at) : "not started"}
                {activeFiring.ended_at ? ` → ${friendlyTime(activeFiring.ended_at)}` : " · running"}
              </small>
            </header>

            {activeAgent && activeAgent.firings.length > 1 ? (
              <div className="tail-runs" aria-label={`Recent runs for ${activeAgent.codename}`}>
                {activeAgent.firings.slice(0, 8).map((f) => (
                  <button
                    key={f.firing_id}
                    className={
                      f.firing_id === activeFiring.firing_id ? "tail-run tail-run--active" : "tail-run"
                    }
                    type="button"
                    onClick={() => setFiringId(f.firing_id)}
                    title={f.summary || f.firing_id}
                  >
                    {f.started_at ? friendlyTime(f.started_at) : f.firing_id}
                  </button>
                ))}
              </div>
            ) : null}

            <EventTail
              firing={activeFiring}
              baseUrl={baseUrl}
              // Only tail the live stream for the agent's newest run: an older
              // run picked from the history strip is already complete on disk,
              // so re-tailing it would just replay a finished transcript.
              live={activeFiring.firing_id === activeAgent?.firings[0]?.firing_id}
            />
          </>
        ) : fetching ? (
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
        )}
      </div>
    </div>
  );
}

function EventTail({
  firing,
  baseUrl,
  live,
}: {
  firing: FiringRecord;
  baseUrl: string;
  live: boolean;
}) {
  const lines = useMemo(() => (firing.raw_events || []).map(formatEvent), [firing.raw_events]);

  // Live-append the running firing's transcript as it grows (#41). This is a
  // progressive enhancement layered ON TOP of the static, poll-derived events:
  // if the stream is unavailable or errors, `liveLines` stays empty and the
  // view is exactly the pre-streaming poll behavior. We reset the buffer per
  // firing so switching runs never bleeds one transcript into another.
  const [liveLines, setLiveLines] = useState<{ ts: string | null; text: string }[]>([]);
  const liveScrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLiveLines([]);
    // Only the currently-running newest run is worth tailing; a completed run is
    // fully captured by the poll, so streaming it would only replay the file.
    if (!live || firing.status !== "running") {
      return;
    }
    const dispose = streamFiringTail(baseUrl, firing.firing_id, {
      onLines: (raw) => {
        const formatted = raw
          .map(formatTranscriptLine)
          .filter((line): line is { ts: string | null; text: string } => line !== null);
        if (formatted.length) {
          setLiveLines((prev) => [...prev, ...formatted]);
        }
      },
      // On error we simply stop appending; the 60s poll keeps the view fresh,
      // so the live tail degrades to the existing behavior with no regression.
      onError: () => {},
    });
    return dispose;
  }, [baseUrl, firing.firing_id, firing.status, live]);

  // Keep the newest live line in view as the transcript grows.
  useEffect(() => {
    const el = liveScrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [liveLines]);

  const hasStatic = lines.length > 0;
  const hasLive = liveLines.length > 0;

  return (
    <div className="tail-stream" aria-label="Run events" role="log" ref={liveScrollRef}>
      {firing.summary ? <p className="tail-stream__summary">{firing.summary}</p> : null}
      {hasStatic ? (
        <ol className="tail-lines">
          {lines.map((line, index) => (
            <li key={index} className="tail-line">
              {line.ts ? <span className="tail-line__ts">{line.ts}</span> : null}
              <span className="tail-line__text">{line.text}</span>
            </li>
          ))}
        </ol>
      ) : !hasLive ? (
        <p className="tail-stream__empty">
          {live && firing.status === "running"
            ? "Waiting for the live transcript to start streaming..."
            : "No structured events captured for this run."}
          {!live && firing.transcript_path ? " A full transcript is saved on disk." : ""}
        </p>
      ) : null}
      {hasLive ? (
        <ol className="tail-lines tail-lines--live" aria-label="Live transcript">
          {liveLines.map((line, index) => (
            <li key={`live-${index}`} className="tail-line tail-line--live">
              {line.ts ? <span className="tail-line__ts">{line.ts}</span> : null}
              <span className="tail-line__text">{line.text}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function formatEvent(event: unknown): { ts: string | null; text: string } {
  if (event && typeof event === "object") {
    const record = event as Record<string, unknown>;
    const ts = typeof record.ts === "string" ? shortTime(record.ts) : null;
    const name = typeof record.event === "string" ? record.event : null;
    // Surface the most useful extra field without dumping the whole object.
    const detail =
      pickString(record, ["summary", "message", "detail", "text", "error"]) ?? null;
    const text = [name, detail].filter(Boolean).join("  ·  ") || JSON.stringify(record);
    return { ts, text };
  }
  return { ts: null, text: String(event) };
}

function pickString(record: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function shortTime(iso: string): string {
  // Just the wall-clock HH:MM:SS for a tail; the full timestamp is the title.
  const match = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : iso;
}

function toneFor(status: string): "ok" | "warn" | "error" | "idle" {
  if (status === "error") return "error";
  if (status === "running") return "warn";
  if (status === "ok") return "ok";
  return "idle";
}
