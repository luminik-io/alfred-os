import {
  AlertTriangle,
  Ban,
  Check,
  CheckCircle2,
  CircleDot,
  GitPullRequest,
  PenLine,
  Plus,
  RefreshCw,
  Settings,
  ShieldCheck,
} from "lucide-react";
import { useState } from "react";

import { supportsNativeActions } from "../api";
import { exactTime, friendlyTime } from "../format";
import { parseIssueRef } from "../lib/links";
import type { ActionNotice, TabKey } from "../lib/uiTypes";
import type { QueueAction, ShippedBoard, ShippedCard } from "../types";
import {
  Badge,
  Button,
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Skeleton,
  TabsList,
  TabsRoot,
  TabsTrigger,
} from "./ui";
import { cn } from "@/lib/utils";

type QueueActionHandler = (
  repo: string,
  issueNumber: number,
  action: QueueAction,
) => void | Promise<boolean>;

type ColumnKey = "queued" | "in_progress" | "shipped";

const COLUMNS: Array<{
  key: ColumnKey;
  label: string;
  tabLabel: string;
  hint: string;
  icon: typeof CircleDot;
}> = [
  {
    key: "queued",
    label: "Ready",
    tabLabel: "Ready",
    hint: "Issues Alfred can pick up",
    icon: CircleDot,
  },
  {
    key: "in_progress",
    label: "Building",
    tabLabel: "Build",
    hint: "Open PRs with Alfred evidence",
    icon: GitPullRequest,
  },
  {
    key: "shipped",
    label: "Shipped",
    tabLabel: "Done",
    hint: "Merged with Alfred evidence",
    icon: CheckCircle2,
  },
];

const COLUMN_EMPTY: Record<ColumnKey, string> = {
  queued: "No pickup-ready issues.",
  in_progress: "No Alfred PRs in flight.",
  shipped: "No Alfred merges in the lookback.",
};

// Cards shown per column before "Show more". Real pagination instead of a
// hard truncation, so no card is ever unreachable.
const PAGE_SIZE = 12;

function repoLabel(repo: string): string {
  const slash = repo.lastIndexOf("/");
  return slash >= 0 ? repo.slice(slash + 1) : repo;
}

function evidenceLabel(card: ShippedCard): string | null {
  if (card.demo) return "demo card";
  if (
    card.kind === "issue" &&
    (card.labels || []).some((label) =>
      ["agent:implement", "agent:large-feature"].includes(label.toLowerCase()),
    )
  ) {
    return "pickup label";
  }
  const evidence = card.agent_evidence || [];
  if (!evidence.length) return null;
  if (evidence.some((item) => item.startsWith("label:agent:"))) return "agent label";
  if (evidence.some((item) => item.startsWith("branch:"))) return "agent branch";
  if (evidence.some((item) => item.startsWith("author:"))) return "agent author";
  return "agent evidence";
}

function KanbanCard({
  card,
  onHold,
  holding,
  onDone,
  closing,
  actionsDisabled,
}: {
  card: ShippedCard;
  onHold?: () => void;
  holding?: boolean;
  onDone?: () => void;
  closing?: boolean;
  actionsDisabled?: boolean;
}) {
  const ref = card.number ? `#${card.number}` : card.kind === "issue" ? "issue" : "pr";
  const meta = [card.author, card.timestamp ? friendlyTime(card.timestamp) : null]
    .filter(Boolean)
    .join(" · ");
  const evidence = evidenceLabel(card);
  const disabledReason = actionsDisabled
    ? "Use the native Alfred app to change GitHub issues."
    : undefined;
  const actionable = Boolean(onHold || onDone);
  const content = (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <strong className="line-clamp-2 min-w-0 text-sm font-medium leading-snug text-foreground">
          {card.title || "Untitled"}
        </strong>
        <Badge variant="outline" className="shrink-0 font-mono text-[0.68rem]">
          {ref}
        </Badge>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="font-medium text-foreground/75">{repoLabel(card.repo)}</span>
        {meta ? (
          <small title={exactTime(card.timestamp)} className="text-xs">
            {meta}
          </small>
        ) : null}
      </div>
      {evidence ? (
        <span className="inline-flex w-fit items-center gap-1 rounded-md bg-primary/10 px-2 py-1 text-xs font-medium text-primary">
          <ShieldCheck size={12} aria-hidden="true" />
          {evidence}
        </span>
      ) : null}
    </div>
  );

  return (
    <Card size="sm" className="rounded-lg border-border/70 bg-card/75 shadow-none">
      <CardContent className="px-3">
        {card.url ? (
          <a
            className="block rounded-md outline-none transition-colors hover:text-primary focus-visible:ring-3 focus-visible:ring-ring/45"
            href={card.url}
            target="_blank"
            rel="noreferrer"
          >
            {content}
          </a>
        ) : (
          content
        )}
      </CardContent>
      {actionable ? (
        <CardFooter className="justify-end gap-2 rounded-b-lg border-border/70 bg-muted/35 px-3 py-2">
          {onHold ? (
            <Button
              variant="outline"
              size="xs"
              type="button"
              disabled={holding || actionsDisabled}
              onClick={onHold}
              title={disabledReason || "Remove from Alfred's pickup queue (do-not-pickup)"}
            >
              <Ban size={13} aria-hidden="true" />
              <span>{holding ? "Holding" : "Hold"}</span>
            </Button>
          ) : null}
          {onDone ? (
            <Button
              variant="secondary"
              size="xs"
              type="button"
              disabled={closing || actionsDisabled}
              onClick={onDone}
              title={disabledReason || "Close this issue on GitHub (native closed state)"}
            >
              <Check size={13} aria-hidden="true" />
              <span>{closing ? "Closing" : "Done"}</span>
            </Button>
          ) : null}
        </CardFooter>
      ) : null}
    </Card>
  );
}

function Column({
  columnKey,
  label,
  hint,
  icon: Icon,
  cards,
  loading,
  onQueueAction,
  busyQueue,
  activeMobile,
}: {
  columnKey: ColumnKey;
  label: string;
  hint: string;
  icon: typeof CircleDot;
  cards: ShippedCard[];
  loading: boolean;
  onQueueAction?: QueueActionHandler;
  busyQueue?: string | null;
  activeMobile?: boolean;
}) {
  const [visible, setVisible] = useState(PAGE_SIZE);
  const shown = cards.slice(0, visible);
  const remaining = cards.length - shown.length;
  const actionsDisabled = Boolean(onQueueAction) && !supportsNativeActions();
  const canAct = (card: ShippedCard) =>
    Boolean(onQueueAction) &&
    columnKey === "queued" &&
    card.kind === "issue" &&
    !card.demo &&
    !!card.number;

  return (
    <Card
      size="sm"
      aria-label={`${label} (${cards.length})`}
      className={cn(
        "min-h-[22rem] rounded-lg border-border/70 bg-background/58 shadow-none",
        !activeMobile && "hidden md:flex",
      )}
    >
      <CardHeader className="border-b border-border/70 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Icon size={15} aria-hidden="true" />
              <span>{label}</span>
              <Badge variant="secondary" className="font-mono">
                {cards.length}
              </Badge>
            </CardTitle>
            <CardDescription className="mt-1 text-xs">{hint}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex max-h-[58vh] min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3">
        {loading && !cards.length ? (
          <>
            <Skeleton className="h-24 rounded-lg" />
            <Skeleton className="h-24 rounded-lg" />
            <Skeleton className="h-24 rounded-lg" />
          </>
        ) : shown.length ? (
          <>
            {shown.map((card) => {
              const actionable = canAct(card);
              return (
                <KanbanCard
                  key={`${card.repo}-${card.number ?? card.title}`}
                  card={card}
                  onHold={
                    actionable && card.number
                      ? () => onQueueAction?.(card.repo, card.number as number, "hold")
                      : undefined
                  }
                  holding={busyQueue === `hold:${card.repo}#${card.number}`}
                  onDone={
                    actionable && card.number
                      ? () => onQueueAction?.(card.repo, card.number as number, "done")
                      : undefined
                  }
                  closing={busyQueue === `done:${card.repo}#${card.number}`}
                  actionsDisabled={actionsDisabled}
                />
              );
            })}
            {remaining > 0 ? (
              <Button
                variant="outline"
                size="sm"
                type="button"
                className="mt-1 w-full"
                onClick={() => setVisible((v) => v + PAGE_SIZE)}
              >
                <Plus size={14} aria-hidden="true" />
                <span>Show {Math.min(remaining, PAGE_SIZE)} more</span>
              </Button>
            ) : null}
          </>
        ) : (
          <p className="rounded-lg border border-dashed border-border/80 bg-muted/30 px-3 py-4 text-sm text-muted-foreground">
            {COLUMN_EMPTY[columnKey]}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function QueueComposer({
  onQueueAction,
  busy,
}: {
  onQueueAction: QueueActionHandler;
  busy: boolean;
}) {
  const [value, setValue] = useState("");
  const parsed = parseIssueRef(value);

  return (
    <Card size="sm" className="rounded-lg border-border/70 bg-card/70 shadow-none">
      <CardContent className="px-3">
        <form
          className="grid gap-3 md:grid-cols-[minmax(10rem,1fr)_minmax(16rem,2fr)_auto] md:items-end"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!parsed || busy) return;
            const ok = await onQueueAction(parsed.repo, parsed.number, "assign");
            if (ok !== false) setValue("");
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="queue-issue-ref">Assign an issue</Label>
            <p className="text-xs text-muted-foreground">Paste a GitHub issue URL or repo ref.</p>
          </div>
          <Input
            id="queue-issue-ref"
            value={value}
            onChange={(event) => setValue(event.currentTarget.value)}
            placeholder="owner/repo#123"
            spellCheck={false}
          />
          <Button variant="secondary" type="submit" disabled={!parsed || busy}>
            <Plus size={15} aria-hidden="true" />
            <span>{busy ? "Assigning" : "Assign"}</span>
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export function KanbanBoard({
  board,
  state,
  error,
  onRefresh,
  onQueueAction,
  busyQueue,
  notice,
  onSwitch,
}: {
  board: ShippedBoard | null;
  state: "idle" | "loading" | "error";
  error?: string | null;
  onRefresh?: () => void;
  onQueueAction?: QueueActionHandler;
  busyQueue?: string | null;
  notice?: ActionNotice;
  onSwitch?: (tab: TabKey) => void;
}) {
  const loading = state === "loading";
  const columns = board?.columns;
  const generatedAt = board?.generated_at;
  const hardError = board?.error;
  const loadError = state === "error" ? error || "Work refresh failed." : null;
  const softErrors = board?.errors?.length || 0;
  const hasCards = Boolean(
    columns &&
      (columns.queued.length || columns.in_progress.length || columns.shipped.length),
  );
  const errorPanel = loadError && !columns ? loadError : hardError && !hasCards ? hardError : null;
  const errorIntro =
    loadError && !columns
      ? "Alfred could not load Work"
      : "Alfred reached the runtime but Work failed to build";
  const canAct = Boolean(onQueueAction);
  const canMutate = supportsNativeActions() && Boolean(onQueueAction);
  const [activeColumn, setActiveColumn] = useState<ColumnKey>("queued");
  const status = loadError
    ? columns
      ? "Work refresh failed. Showing last update."
      : "Work failed to load."
    : hardError
      ? "Couldn't reach GitHub. Check gh auth."
      : softErrors
        ? `${softErrors} repo(s) unavailable`
        : generatedAt
          ? `updated ${friendlyTime(generatedAt)}`
          : null;

  return (
    <section className="grid gap-4" aria-label="Work">
      <Card className="rounded-lg border-border/70 bg-card/70 shadow-none">
        <CardHeader className="gap-3 md:grid-cols-[1fr_auto]">
          <div className="min-w-0">
            <CardTitle className="text-xl">Work</CardTitle>
            <CardDescription>
              GitHub issues and PRs Alfred can act on, with shipped evidence.
              {board?.repos?.length ? ` Watching ${board.repos.length} repos.` : ""}
            </CardDescription>
          </div>
          <CardAction className="flex items-center gap-2">
            {status ? (
              <Badge
                variant={loadError || hardError || softErrors ? "destructive" : "outline"}
                title={generatedAt && !loadError && !hardError ? exactTime(generatedAt) : undefined}
                className="max-w-[18rem] justify-start truncate"
              >
                {status}
              </Badge>
            ) : null}
            {onRefresh ? (
              <Button
                variant="ghost"
                size="icon-sm"
                type="button"
                onClick={onRefresh}
                disabled={loading}
                aria-label="Refresh board"
              >
                <RefreshCw
                  size={15}
                  aria-hidden="true"
                  className={loading ? "animate-spin" : undefined}
                />
              </Button>
            ) : null}
          </CardAction>
        </CardHeader>
      </Card>

      {canMutate && onQueueAction ? (
        <QueueComposer onQueueAction={onQueueAction} busy={Boolean(busyQueue)} />
      ) : null}

      {canAct && notice ? (
        <Card
          size="sm"
          className={cn(
            "rounded-lg shadow-none",
            notice.tone === "ok"
              ? "border-primary/25 bg-primary/10 text-primary"
              : "border-destructive/25 bg-destructive/10 text-destructive",
          )}
        >
          <CardContent className="flex items-center gap-2 px-3">
            {notice.tone === "ok" ? (
              <CheckCircle2 size={18} aria-hidden="true" />
            ) : (
              <AlertTriangle size={18} aria-hidden="true" />
            )}
            <span>{notice.message}</span>
          </CardContent>
        </Card>
      ) : null}

      {!loading && !errorPanel && !hasCards ? <BoardEmptyCallout onSwitch={onSwitch} /> : null}

      {errorPanel ? (
        <Card className="rounded-lg border-destructive/30 bg-destructive/10 text-destructive shadow-none">
          <CardContent className="px-4 text-sm">
            {errorIntro} ({errorPanel}). Check <code>gh auth status</code> and the
            watched-repo config.
          </CardContent>
        </Card>
      ) : (
        <TabsRoot
          value={activeColumn}
          onValueChange={(value) => setActiveColumn(value as ColumnKey)}
          className="gap-4"
        >
          <TabsList
            variant="line"
            aria-label="Board lanes"
            className="grid h-auto w-full grid-cols-3 rounded-lg border border-border/70 bg-card/60 p-1 md:hidden"
          >
            {COLUMNS.map((col) => {
              const Icon = col.icon;
              const count = columns?.[col.key]?.length || 0;
              return (
                <TabsTrigger
                  key={col.key}
                  value={col.key}
                  aria-label={`${col.label} lane, ${count} items`}
                  className="h-8 gap-1.5 text-xs"
                >
                  <Icon size={14} aria-hidden="true" />
                  <span>{col.tabLabel}</span>
                  <Badge variant="secondary" className="h-4 px-1.5 text-[0.65rem]">
                    {count}
                  </Badge>
                </TabsTrigger>
              );
            })}
          </TabsList>
          <div className="grid gap-3 md:grid-cols-3">
            {COLUMNS.map((col) => (
              <Column
                key={col.key}
                columnKey={col.key}
                label={col.label}
                hint={col.hint}
                icon={col.icon}
                cards={columns?.[col.key] || []}
                loading={loading}
                onQueueAction={canAct ? onQueueAction : undefined}
                busyQueue={busyQueue}
                activeMobile={activeColumn === col.key}
              />
            ))}
          </div>
        </TabsRoot>
      )}
    </section>
  );
}

function BoardEmptyCallout({ onSwitch }: { onSwitch?: (tab: TabKey) => void }) {
  return (
    <Card size="sm" aria-label="Work empty" className="rounded-lg border-border/70 bg-card/70 shadow-none">
      <CardHeader className="md:grid-cols-[1fr_auto]">
        <div className="min-w-0">
          <Badge variant="outline" className="mb-2">
            Clear board
          </Badge>
          <CardTitle>No work is moving right now.</CardTitle>
          <CardDescription>
            Ask Alfred to file the next labeled issue, or finish Setup so Alfred can discover
            the repos it is allowed to touch.
          </CardDescription>
        </div>
        {onSwitch ? (
          <CardAction className="flex flex-wrap gap-2">
            <Button type="button" onClick={() => onSwitch("compose")}>
              <PenLine size={16} aria-hidden="true" />
              <span>Ask Alfred</span>
            </Button>
            <Button variant="outline" type="button" onClick={() => onSwitch("setup")}>
              <Settings size={16} aria-hidden="true" />
              <span>Choose repos</span>
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
    </Card>
  );
}
