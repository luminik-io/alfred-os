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
  PenLine,
  Radio,
  Settings,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { exactTime, friendlyTime } from "../format";
import {
  buildActiveThreads,
  buildCostHealth,
  buildRunning,
  buildShippedDigest,
  threadForCard,
} from "../lib/derive";
import { openExternal } from "../lib/links";
import type { AttentionItem, RequestThreadModel, TabKey } from "../lib/uiTypes";
import type {
  PlanDecision,
  PlanDraft,
  ShippedBoard,
  ShippedCard,
  Snapshot,
  UsageResponse,
} from "../types";
import { RequestThread } from "./RequestThread";
import { Tabs, type TabItem } from "./Tabs";
import { UsagePanel } from "./UsagePanel";
import { AlfredMetric } from "./ui/alfred";
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
  const decisions = needsYou.length;
  const preferredLane: InboxLane = decisions ? "needs" : "activity";
  const [lane, setLane] = useState<InboxLane>(preferredLane);
  const [lanePinned, setLanePinned] = useState(false);
  const [shippedDays, setShippedDays] = useState<number>(1);

  useEffect(() => {
    if (!lanePinned) setLane(preferredLane);
  }, [preferredLane, lanePinned]);

  const filteredShipped = useMemo<ShippedBoard | null>(() => {
    if (!shipped) return null;
    const within = (card: ShippedCard) => card.age_days == null || card.age_days <= shippedDays;
    return {
      ...shipped,
      columns: { ...shipped.columns, shipped: shipped.columns.shipped.filter(within) },
    };
  }, [shipped, shippedDays]);
  const filteredDigest = useMemo(() => buildShippedDigest(filteredShipped), [filteredShipped]);

  const summary = snapshot
    ? [
        decisions ? `${decisions} waiting` : "Nothing waiting",
        running.running.length ? `${running.running.length} running` : "No active runs",
        filteredDigest.length ? `${filteredDigest.length} shipped` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "Waiting for the local runtime";

  const laneTabs: TabItem<InboxLane>[] = [
    { key: "needs", label: "Needs you", icon: Bell, badge: decisions || null },
    { key: "activity", label: "Activity", icon: Activity, badge: running.running.length || null },
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
      label: "Needs approval",
      value: decisions ? String(decisions) : "0",
      detail: decisions
        ? "Plans, lessons, or blockers are waiting."
        : "Alfred can keep working without a decision.",
    },
    {
      label: "Working now",
      value: running.running.length ? String(running.running.length) : "0",
      detail: running.running.length ? "Open Activity to follow active runs." : "No agent is in flight.",
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

  return (
    <div className="space-y-4" aria-label="Inbox">
      <Card className="border-border/70 bg-card/80 shadow-sm backdrop-blur">
        <CardHeader className="gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-1">
            <CardDescription>Inbox</CardDescription>
            <CardTitle className="font-heading text-2xl">What needs you</CardTitle>
            <p className="text-sm text-muted-foreground">{summary}</p>
          </div>
          <CardAction className="flex gap-2">
            <Button type="button" onClick={() => onSwitch("compose")}>
              <PenLine aria-hidden="true" />
              Ask Alfred
            </Button>
            <Button type="button" variant="outline" onClick={() => onSwitch("setup")}>
              <Settings aria-hidden="true" />
              Setup
            </Button>
          </CardAction>
        </CardHeader>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Card className="min-w-0 border-border/70 bg-card/80 shadow-sm backdrop-blur">
          <CardHeader className="gap-3">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="min-w-fit">
                <CardDescription>Queue</CardDescription>
              </div>
              <Tabs
                tabs={laneTabs}
                active={lane}
                onChange={onLaneChange}
                idBase="review-lane"
                ariaLabel="Inbox sections"
              />
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
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
                onOpenWork={() => onSwitch("board")}
                onOpenThread={
                  onOpenThread
                    ? (card) => onOpenThread(threadForCard(card, filteredShipped))
                    : undefined
                }
              />
            ) : null}
          </CardContent>
        </Card>

        <aside className="space-y-4" aria-label="Review insights">
          <section aria-label="Alfred shift summary">
            <Card className="border-border/70 bg-card/80 shadow-sm backdrop-blur">
              <CardHeader>
                <CardDescription>Agents</CardDescription>
                <CardTitle>Keep agents moving</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-2">
                {overviewCards.map((card) => (
                  <AlfredMetric
                    key={card.label}
                    detail={card.detail}
                    label={card.label}
                    value={card.value}
                  />
                ))}
              </CardContent>
            </Card>
          </section>

          <section aria-label="Capacity and proof">
            <Card className="border-border/70 bg-card/80 shadow-sm backdrop-blur">
              <CardHeader>
                <CardDescription>Capacity</CardDescription>
                <CardTitle>Headroom and proof</CardTitle>
              </CardHeader>
              <CardContent>
                <UsagePanel usage={usage} state={usageState} shipped={shipped} compact />
              </CardContent>
            </Card>
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
        title="No review requests"
        body="Approvals, questions, lessons, and blockers show up here."
        tone="ok"
      />
    );
  }
  return (
    <section className="grid gap-3" aria-label="Needs you">
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
    <Card className="border-border/70 bg-background/35">
      <CardHeader className="gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-accent/30 bg-accent/10 text-accent">
            <Icon className="size-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <CardDescription>{item.label}</CardDescription>
            <CardTitle className="truncate text-base">{item.title}</CardTitle>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">{item.detail}</p>
        {canDecide ? (
          <p className="rounded-lg border border-border/70 bg-muted/25 p-2 text-xs text-muted-foreground" role="note">
            Approving starts this exact scope on Batman's next run. Declining stops it.
          </p>
        ) : null}
        {item.command ? (
          <code className="block truncate rounded-md border border-border/70 bg-muted/30 px-2 py-1 text-xs text-muted-foreground">
            {item.command}
          </code>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {canDecide ? (
            <>
              <Button type="button" disabled={busy} onClick={onApprove}>
                <Check aria-hidden="true" />
                Approve
              </Button>
              <Button type="button" variant="outline" disabled={busy} onClick={onDecline}>
                <X aria-hidden="true" />
                Decline
              </Button>
            </>
          ) : null}
          {item.targetTab ? (
            <Button type="button" variant="outline" onClick={() => onNavigate(item.targetTab)}>
              <ArrowRight aria-hidden="true" />
              {item.icon === "run" ? "Inspect runs" : "Review"}
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
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
              <RequestThread key={thread.id} thread={thread} onOpenPlan={() => onSwitch("plans")} />
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
                {agent ? <Badge variant="secondary">{agent}</Badge> : null}
                <strong className="block min-w-0 truncate">{what}</strong>
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
    <Card className="border-border/70 bg-background/35">
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
