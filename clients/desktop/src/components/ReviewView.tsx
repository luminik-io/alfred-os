import {
  Activity,
  ArrowRight,
  Bell,
  Check,
  Clock,
  ExternalLink,
  GitPullRequest,
  ListChecks,
  MemoryStick,
  MessageSquare,
  Radio,
  Settings,
  X,
} from "lucide-react";
import type { CSSProperties } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import { exactTime, friendlyTime } from "../format";
import {
  buildActiveThreads,
  buildCostHealth,
  buildRunning,
  buildShippedDigest,
  isErrorStatus,
  threadForCard,
} from "../lib/derive";
import { openExternal } from "../lib/links";
import type { AttentionItem, RequestThreadModel, TabKey } from "../lib/uiTypes";
import type {
  PlanDecision,
  PlanDraft,
  AgentSummary,
  ShippedBoard,
  ShippedCard,
  Snapshot,
  UsageResponse,
} from "../types";
import { RequestThread } from "./RequestThread";
import { Tabs, type TabItem } from "./Tabs";
import { UsagePanel } from "./UsagePanel";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./ui/card";

const SHIPPED_WINDOWS: Array<{ key: number; label: string }> = [
  { key: 1, label: "24h" },
  { key: 7, label: "7 days" },
  { key: 14, label: "14 days" },
];

const ROUTE_AGENT_PRIORITY = ["batman", "lucius", "drake", "damian", "bane"];

type RouteCard = {
  codename: string;
  displayName: string;
  roleTitle: string;
  purpose: string;
  themeAccent: string;
};

const FALLBACK_ROUTE_CARDS: RouteCard[] = [
  {
    codename: "batman",
    displayName: "Batman",
    roleTitle: "Architect",
    purpose: "Plans and coordinates multi-repo work with approval.",
    themeAccent: "var(--primary)",
  },
  {
    codename: "lucius",
    displayName: "Lucius",
    roleTitle: "Senior Developer",
    purpose: "Ships scoped implementation issues as pull requests.",
    themeAccent: "var(--blue)",
  },
  {
    codename: "drake",
    displayName: "Drake",
    roleTitle: "Spec Planner",
    purpose: "Turns vague work into implementation-ready issues.",
    themeAccent: "var(--accent)",
  },
];

function countBoardLiveWork(board: ShippedBoard | null): number {
  if (!board) return 0;
  const queued = Math.max(board.counts?.queued ?? 0, board.columns.queued.length);
  const inProgress = Math.max(board.counts?.in_progress ?? 0, board.columns.in_progress.length);
  return queued + inProgress;
}

type InboxLane = "needs" | "activity" | "shipped";

export function ReviewView({
  snapshot,
  needsYou,
  shipped,
  usage,
  usageState,
  onSwitch,
  onOpenThread,
  onPlanDecision,
  busyPlanAction,
}: {
  snapshot: Snapshot | null;
  needsYou: AttentionItem[];
  shipped: ShippedBoard | null;
  usage: UsageResponse | null;
  usageState: "idle" | "loading" | "error";
  onSwitch: (tab: TabKey) => void;
  onOpenThread?: (thread: RequestThreadModel) => void;
  onPlanDecision?: (plan: PlanDraft, decision: PlanDecision) => void;
  busyPlanAction?: string | null;
}) {
  const running = buildRunning(snapshot);
  const health = buildCostHealth(snapshot);
  const activeThreads = buildActiveThreads(shipped);
  const boardLiveCount = countBoardLiveWork(shipped);
  const decisions = needsYou.length;
  const [shippedDays, setShippedDays] = useState<number>(1);
  const routeCards = useMemo(() => buildRouteCards(snapshot), [snapshot]);
  const filteredShipped = useMemo<ShippedBoard | null>(() => {
    if (!shipped) return null;
    const within = (card: ShippedCard) => card.age_days == null || card.age_days <= shippedDays;
    return {
      ...shipped,
      columns: { ...shipped.columns, shipped: shipped.columns.shipped.filter(within) },
    };
  }, [shipped, shippedDays]);
  const filteredDigest = useMemo(() => buildShippedDigest(filteredShipped), [filteredShipped]);
  const preferredLane: InboxLane = decisions
    ? "needs"
    : running.running.length || boardLiveCount
      ? "activity"
      : filteredDigest.length
        ? "shipped"
        : "activity";
  const [lane, setLane] = useState<InboxLane>(preferredLane);
  const [lanePinned, setLanePinned] = useState(false);
  // The decision queue pane, so the hero CTA can scroll it into focus.
  const queueRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!lanePinned) setLane(preferredLane);
  }, [preferredLane, lanePinned]);

  // Honest morning-after rollup: shipped count, go-aheads waiting, and snags.
  // Errored runs (including llm-error) are counted as snags, never as ok.
  const snagCount = (snapshot?.firings || []).filter((firing) =>
    isErrorStatus(firing.status),
  ).length;
  const runningCount = running.running.length;
  const liveWorkCount = runningCount + boardLiveCount;
  const liveRunSummary = runningCount
    ? `${runningCount} ${runningCount === 1 ? "run is" : "runs are"} active now.`
    : null;
  const activeThreadSummary = boardLiveCount
    ? `${boardLiveCount} ${boardLiveCount === 1 ? "request is" : "requests are"} queued or building.`
    : null;
  const summary = snapshot
    ? [
        filteredDigest.length
          ? `Shipped ${filteredDigest.length} ${filteredDigest.length === 1 ? "thing" : "things"} overnight.`
          : null,
        liveRunSummary,
        activeThreadSummary,
        decisions ? `${decisions} ${decisions === 1 ? "needs" : "need"} your go-ahead.` : null,
        snagCount ? `${snagCount} hit a snag.` : null,
      ]
        .filter(Boolean)
        .join(" ") || "All clear. Nothing needed you."
    : "Waiting for the local runtime";
  const headline = decisions
    ? decisions === 1
      ? "Alfred needs one decision"
      : `Alfred needs ${decisions} decisions`
    : liveWorkCount
      ? "Alfred is working"
      : filteredDigest.length
        ? "Alfred shipped while you were away"
        : "Alfred is clear";

  const laneTabs: TabItem<InboxLane>[] = [
    { key: "needs", label: "Needs you", icon: Bell, badge: decisions || null },
    { key: "activity", label: "Running", icon: Activity, badge: liveWorkCount || null },
    { key: "shipped", label: "Shipped", icon: GitPullRequest, badge: filteredDigest.length || null },
  ];

  const overviewCards = [
    {
      label: "Runs today",
      value: String(health.runsToday),
      detail: health.lastRunByAgent.length
        ? `${health.succeeded} ok, ${health.failed} failed. Last: ${health.lastRunByAgent[0].codename} ${
            health.lastRunByAgent[0].at ? friendlyTime(health.lastRunByAgent[0].at) : ""
          }`.trim()
        : "No runs recorded yet.",
    },
    {
      label: "Decisions",
      value: decisions ? String(decisions) : "0",
      detail: decisions
        ? "Plans, lessons, or blockers are waiting."
        : "Alfred can keep working.",
    },
    {
      label: "Working now",
      value: liveWorkCount ? String(liveWorkCount) : "0",
      detail: liveWorkCount ? "Open Running to follow live work." : "No live work right now.",
    },
    {
      label: `Shipped in ${shippedDays === 1 ? "24h" : `${shippedDays}d`}`,
      value: filteredDigest.length ? String(filteredDigest.length) : "0",
      detail: filteredDigest.length ? "Merged PRs with Alfred evidence." : "No shipped evidence here.",
    },
  ];

  const onLaneChange = (key: InboxLane) => {
    setLanePinned(true);
    setLane(key);
  };
  // The hero CTA is the top decision when one is waiting, otherwise the quiet
  // "Open Pipeline" fallback. Ask lives in the sidebar, so the hero no longer
  // duplicates it: it is a pure morning-after summary plus the next action.
  const decisionCta = decisions
    ? {
        label: decisions === 1 ? "Review the 1 waiting" : `Review the ${decisions} waiting`,
        onClick: () => {
          setLanePinned(true);
          setLane("needs");
          // The decision queue sits below the hero and is often already the
          // active lane (and on a tall window already fully in view), so
          // re-selecting it looks inert. Scroll it into view AND move focus to
          // the first waiting decision's action, so the CTA always does
          // something visible and actionable. Focus only, never auto-click, so
          // we never approve/decline on the operator's behalf.
          requestAnimationFrame(() => {
            const pane = queueRef.current;
            pane?.scrollIntoView?.({ behavior: "smooth", block: "start" });
            pane
              ?.querySelector<HTMLElement>(
                ".command-center__pane-body a[href], .command-center__pane-body button",
              )
              ?.focus?.();
          });
        },
      }
    : { label: "Open Work", onClick: () => onSwitch("pipeline") };

  return (
    <div className="command-center" aria-label="Inbox">
      <header className="command-center__top" aria-label="Inbox summary">
        <div className="command-center__title">
          <p>Inbox</p>
          <h1>{headline}</h1>
          <span>{summary}</span>
        </div>
        <div className="command-center__actions">
          <Button
            type="button"
            variant={decisions ? "default" : "outline"}
            onClick={decisionCta.onClick}
          >
            {decisions ? (
              <Bell aria-hidden="true" />
            ) : (
              <GitPullRequest aria-hidden="true" />
            )}
            {decisionCta.label}
            <ArrowRight aria-hidden="true" />
          </Button>
        </div>
      </header>

      <div className="command-center__grid">
        <section ref={queueRef} className="command-center__pane command-center__pane--main" aria-label="Inbox queue">
          <div className="command-center__pane-head">
            <div>
              <p>{lane === "needs" ? "Decide" : lane === "activity" ? "Watch" : "Proof"}</p>
              <h2>{lane === "needs" ? "Needs you" : lane === "activity" ? "Running now" : "Shipped PRs"}</h2>
            </div>
            <Tabs
              tabs={laneTabs}
              active={lane}
              onChange={onLaneChange}
              idBase="review-lane"
              ariaLabel="Inbox sections"
            />
          </div>
          <div className="command-center__pane-body motion-fade" key={lane}>
            {lane === "needs" ? (
              <NeedsYouLane
                items={needsYou}
                snapshot={snapshot}
                busyPlanAction={busyPlanAction}
                onPlanDecision={onPlanDecision}
                onSwitch={onSwitch}
              />
            ) : null}
            {lane === "activity" ? (
              <ActivityLane running={running} activeThreads={activeThreads} onSwitch={onSwitch} />
            ) : null}
            {lane === "shipped" ? (
              <ShippedLane
                board={filteredShipped}
                digest={filteredDigest}
                shippedDays={shippedDays}
                onSetDays={setShippedDays}
                onOpenWork={() => onSwitch("pipeline")}
                onOpenThread={
                  onOpenThread
                    ? (card) => onOpenThread(threadForCard(card, filteredShipped))
                    : undefined
                }
              />
            ) : null}
          </div>
        </section>

        <aside className="command-center__rail" aria-label="Review insights">
          <section className="command-center__route" aria-label="Agent roles">
            <div className="command-center__rail-head">
              <p>Agent roles</p>
              <Button type="button" variant="ghost" size="sm" onClick={() => onSwitch("fleet")}>
                Manage
              </Button>
            </div>
            <div className="command-center__agent-route motion-rise">
              {routeCards.map((agent) => (
                <article
                  key={agent.codename}
                  style={{ "--agent-accent": agent.themeAccent } as CSSProperties}
                >
                  <span>{agent.displayName}</span>
                  <strong>{agent.roleTitle}</strong>
                  <p>{agent.purpose}</p>
                </article>
              ))}
            </div>
          </section>

          <section className="command-center__pulse" aria-label="Alfred shift summary">
            <div className="command-center__rail-head">
              <p>Shift</p>
            </div>
            <div className="command-center__metrics">
              {overviewCards.map((card) => (
                <article key={card.label} className="command-center__metric">
                  <span>{card.label}</span>
                  <strong>{card.value}</strong>
                  <p>{card.detail}</p>
                </article>
              ))}
            </div>
          </section>

          <section className="command-center__capacity" aria-label="Engine capacity">
            <div className="command-center__rail-head">
              <p>Capacity</p>
            </div>
            <UsagePanel usage={usage} state={usageState} compact />
          </section>
        </aside>
      </div>
    </div>
  );
}

function NeedsYouLane({
  busyPlanAction,
  items,
  onPlanDecision,
  onSwitch,
  snapshot,
}: {
  busyPlanAction?: string | null;
  items: AttentionItem[];
  onPlanDecision?: (plan: PlanDraft, decision: PlanDecision) => void;
  onSwitch: (tab: TabKey) => void;
  snapshot: Snapshot | null;
}) {
  if (!items.length) {
    return (
      <EmptyCard
        title="No decisions waiting"
        body="Plans, questions, lessons, and blockers appear here."
        tone="ok"
      />
    );
  }
  return (
    <section className="grid gap-3 motion-rise" aria-label="Decisions">
      {items.map((item) => {
        const plan = item.planId
          ? snapshot?.plans.find((candidate) => candidate.plan_id === item.planId)
          : null;
        return (
          <DecisionCard
            key={item.id}
            item={item}
            busyPlanAction={busyPlanAction}
            canDecide={Boolean(plan && onPlanDecision)}
            onApprove={() => {
              if (plan) onPlanDecision?.(plan, "approve");
            }}
            onDecline={() => {
              if (plan) onPlanDecision?.(plan, "decline");
            }}
            onNavigate={(target) => {
              if (target) onSwitch(target);
            }}
          />
        );
      })}
    </section>
  );
}

function buildRouteCards(snapshot: Snapshot | null): RouteCard[] {
  const agents = snapshot?.status.agents || [];
  if (!agents.length) return FALLBACK_ROUTE_CARDS;
  const byCodename = new Map(agents.map((agent) => [agent.codename, agent]));
  const prioritized = [
    ...ROUTE_AGENT_PRIORITY.map((codename) => byCodename.get(codename)).filter(
      (agent): agent is AgentSummary => Boolean(agent),
    ),
    ...agents.filter((agent) => !ROUTE_AGENT_PRIORITY.includes(agent.codename)),
  ];
  const cards = prioritized
    .filter((agent) => agent.display_name || agent.role_title || agent.purpose)
    .slice(0, 3)
    .map((agent) => ({
      codename: agent.codename,
      displayName: agent.display_name || titleFromCodename(agent.codename),
      roleTitle: agent.role_title || "Agent",
      purpose: agent.purpose || "Handles scheduled local Alfred work.",
      themeAccent: agent.theme_accent || "var(--primary)",
    }));
  return cards.length ? cards : FALLBACK_ROUTE_CARDS;
}

function DecisionCard({
  busyPlanAction,
  canDecide,
  item,
  onApprove,
  onDecline,
  onNavigate,
}: {
  busyPlanAction?: string | null;
  canDecide: boolean;
  item: AttentionItem;
  onApprove: () => void;
  onDecline: () => void;
  onNavigate: (tab: AttentionItem["targetTab"]) => void;
}) {
  const Icon = item.icon === "memory" ? MemoryStick : item.icon === "run" ? Radio : item.icon === "setup" ? Settings : ListChecks;
  const busy = Boolean(busyPlanAction && item.planId && busyPlanAction.startsWith(`${item.planId}:`));
  return (
    <Card size="sm" className="border-border/70 bg-card/80">
      <CardHeader className="gap-2">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg border border-accent/30 bg-accent/10 text-accent">
            <Icon className="size-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <CardDescription className="text-xs">{item.label}</CardDescription>
            <CardTitle className="text-sm leading-snug">{item.title}</CardTitle>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-sm leading-relaxed text-muted-foreground">{item.detail}</p>
        {item.command ? (
          <code className="block truncate rounded-md border border-border/70 bg-muted/30 px-2 py-1 text-xs text-muted-foreground">
            {item.command}
          </code>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {canDecide ? (
            <>
              <Button type="button" size="sm" disabled={busy} onClick={onApprove}>
                <Check aria-hidden="true" />
                Approve
              </Button>
              <Button type="button" variant="outline" size="sm" disabled={busy} onClick={onDecline}>
                <X aria-hidden="true" />
                Decline
              </Button>
            </>
          ) : null}
          {item.targetTab ? (
            <Button type="button" variant="outline" size="sm" onClick={() => onNavigate(item.targetTab)}>
              <ArrowRight aria-hidden="true" />
              {item.icon === "run" ? "Inspect runs" : "Review"}
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

function titleFromCodename(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

function ActivityLane({
  activeThreads,
  onSwitch,
  running,
}: {
  activeThreads: RequestThreadModel[];
  onSwitch: (tab: TabKey) => void;
  running: ReturnType<typeof buildRunning>;
}) {
  return (
    <section className="space-y-4" aria-label="Running and scheduled">
      {running.running.length ? (
        <div className="grid gap-2">
          {running.running.map((firing) => (
            <Card key={firing.firing_id} className="border-border/70 bg-background/35">
              <CardContent className="flex items-center gap-3 py-3">
                <span className="size-2 rounded-full bg-emerald-500" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <strong className="block truncate">{firing.codename}</strong>
                  <p className="truncate text-sm text-muted-foreground">
                    {firing.summary || "Running now."}
                  </p>
                </div>
                <time className="text-xs text-muted-foreground" title={exactTime(firing.started_at)}>
                  {firing.started_at ? friendlyTime(firing.started_at) : "just now"}
                </time>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyCard
          title="Nothing running right now"
          body="In-flight agent runs show here while they work."
        />
      )}

      {running.upcoming.length ? (
        <ul className="grid gap-2" aria-label="Upcoming scheduled runs">
          {running.upcoming.map((run) => (
            <li key={run.codename} className="flex items-center gap-3 rounded-lg border border-border/70 bg-background/35 p-3 text-sm">
              <Clock className="size-4 text-muted-foreground" aria-hidden="true" />
              <strong>{run.codename}</strong>
              {run.role ? <span className="truncate text-muted-foreground">{run.role}</span> : null}
              <span className="ml-auto whitespace-nowrap text-muted-foreground">
                {run.next_fire_at ? (
                  <time title={exactTime(run.next_fire_at)}>next {friendlyTime(run.next_fire_at)}</time>
                ) : (
                  run.cadence
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex items-start gap-2 rounded-lg border border-border/70 bg-muted/25 p-3 text-sm text-muted-foreground" role="note">
          <Clock className="mt-0.5 size-4" aria-hidden="true" />
          <span>
            No upcoming runs surfaced. Alfred could not read a launchd schedule
            (agents.conf), so upcoming firings cannot be listed here.
          </span>
        </div>
      )}

      {activeThreads.length ? (
        <section className="space-y-3" aria-label="Request threads">
          <div>
            <h2 className="font-heading text-base font-medium">Requests in flight</h2>
            <p className="text-sm text-muted-foreground">Follow a request end to end.</p>
          </div>
          <div className="grid gap-3">
            {activeThreads.map((thread) => (
              <RequestThread key={thread.id} thread={thread} onOpenPlan={() => onSwitch("pipeline")} />
            ))}
          </div>
        </section>
      ) : null}
    </section>
  );
}

function ShippedLane({
  board,
  digest,
  onOpenThread,
  onOpenWork,
  onSetDays,
  shippedDays,
}: {
  board: ShippedBoard | null;
  digest: ReturnType<typeof buildShippedDigest>;
  onOpenThread?: (card: ShippedCard) => void;
  onOpenWork: () => void;
  onSetDays: (days: number) => void;
  shippedDays: number;
}) {
  return (
    <section className="space-y-4" aria-label="Shipped">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-heading text-base font-medium">What Alfred shipped</h2>
          <p className="text-sm text-muted-foreground">Plain-English evidence from merged PRs.</p>
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Filter shipped by time window">
          {SHIPPED_WINDOWS.map((window) => (
            <Button
              key={window.key}
              type="button"
              variant={shippedDays === window.key ? "secondary" : "outline"}
              size="sm"
              aria-pressed={shippedDays === window.key}
              onClick={() => onSetDays(window.key)}
            >
              {window.label}
            </Button>
          ))}
          {board ? (
            <Button type="button" variant="ghost" size="sm" onClick={onOpenWork}>
              Work
            </Button>
          ) : null}
        </div>
      </div>
      <ShippedDigest board={board} digest={digest} onOpenThread={onOpenThread} />
    </section>
  );
}

function ShippedDigest({
  board,
  digest,
  onOpenThread,
}: {
  board: ShippedBoard | null;
  digest: ReturnType<typeof buildShippedDigest>;
  onOpenThread?: (card: ShippedCard) => void;
}) {
  if (board?.error) {
    return (
      <EmptyCard
        title="Couldn't build the shipped list"
        body={`Alfred reached the runtime but Work failed to build (${board.error}). Check GitHub auth and watched repos.`}
        tone="error"
      />
    );
  }
  if (!board) {
    return (
      <EmptyCard
        title="Shipped work will show here"
        body="Once the runtime exposes recent merges, Alfred lists what it shipped in plain words."
      />
    );
  }
  if (!digest.length) {
    return (
      <EmptyCard
        title="Nothing shipped in this window"
        body="Merged pull requests appear here as Alfred ships them."
        tone="ok"
      />
    );
  }
  return (
    <div className="grid gap-3">
      {digest.map(({ agent, card, what, why }) => (
        <Card key={`${card.repo}-${card.number ?? card.title}`} className="border-border/70 bg-background/35">
          <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                {card.demo ? <Badge variant="outline">Sample</Badge> : null}
                {agent ? <Badge variant="secondary">{agent}</Badge> : null}
                <strong className="block min-w-0 leading-snug sm:truncate">{what}</strong>
              </div>
              <p className="text-sm text-muted-foreground">{why}</p>
            </div>
            <div className="flex shrink-0 gap-2">
              {card.url ? (
                <Button type="button" variant="outline" onClick={() => void openExternal(card.url as string)}>
                  <ExternalLink aria-hidden="true" />
                  Open PR
                </Button>
              ) : null}
              {onOpenThread ? (
                <Button type="button" variant="ghost" onClick={() => onOpenThread(card)}>
                  <MessageSquare aria-hidden="true" />
                  Thread
                </Button>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function EmptyCard({
  body,
  title,
  tone,
}: {
  body: string;
  title: string;
  tone?: "ok" | "error";
}) {
  const variant = tone === "error" ? "destructive" : tone === "ok" ? "secondary" : "outline";
  return (
    <Card className="border-border/70 bg-card/80">
      <CardHeader>
        <CardAction>
          {tone ? <Badge variant={variant}>{tone === "ok" ? "Clear" : "Needs attention"}</Badge> : null}
        </CardAction>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{body}</CardDescription>
      </CardHeader>
    </Card>
  );
}
