import {
  AlertTriangle,
  Ban,
  Check,
  CheckCircle2,
  ChevronDown,
  ExternalLink,
  FilePlus2,
  GitPullRequest,
  MessageSquare,
  RefreshCw,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { supportsNativeActions } from "../api";
import { exactTime, friendlyTime } from "../format";
import {
  agentForShipped,
  boardCardChip,
  planChip,
  repoShortName,
  type BoardColumn,
} from "../lib/chips";
import {
  dedupePlans,
  isLowSignalPlan,
  planNeedsAttention,
} from "../lib/derive";
import { firstLink, isSafeExternalUrl, openExternal, parseIssueRef } from "../lib/links";
import type { ActionNotice, FollowupAction } from "../lib/uiTypes";
import type {
  AssignmentTargetAgent,
  PlanDecision,
  PlanDraft,
  QueueAction,
  ShippedBoard,
  ShippedCard,
} from "../types";
import { EmptyState } from "./atoms";
import { LifecycleCard, type RepoChip } from "./LifecycleCard";
import { Button } from "./ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "./ui/sheet";

type QueueActionHandler = (
  repo: string,
  issueNumber: number,
  action: QueueAction,
  targetAgent?: AssignmentTargetAgent,
) => void | Promise<boolean>;

// A unified selection key so the detail panel can address either a plan or a
// board card without a shared id space.
type Selection =
  | { kind: "plan"; id: string }
  | { kind: "card"; key: string };

const BOARD_COLUMNS: Array<{ key: BoardColumn; label: string }> = [
  { key: "queued", label: "Queued" },
  { key: "in_progress", label: "Working now" },
  { key: "shipped", label: "Shipped" },
];

function repoChips(repos: string[]): RepoChip[] {
  return repos.map((repo) => ({ short: repoShortName(repo), full: repo }));
}

function cardKey(card: ShippedCard): string {
  return `${card.repo}#${card.number ?? card.title}`;
}

// A plain outcome sentence for a board card: strip the conventional-commit
// prefix and present the title as a sentence. A richer summary is a Phase 2
// backend change (flagged in the spec).
function cardOutcome(card: ShippedCard): string {
  // Prefer the server-derived plain-language outcome when present (Phase 2);
  // fall back to a cleaned title for older servers that omit the field.
  const serverOutcome = (card.outcome || "").trim();
  if (serverOutcome) return serverOutcome;
  const title = (card.title || "").trim();
  if (!title) return "Shipped a change to this repo.";
  const cleaned = title.replace(
    /^\s*(feat|fix|chore|docs|refactor|test)(\([^)]*\))?:\s*/i,
    "",
  );
  const sentence = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
  return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`;
}

export function PipelineView({
  board,
  state,
  error,
  plans,
  busyPlanAction,
  busyQueue,
  notice,
  onRefresh,
  onQueueAction,
  onDecision,
  onDiscardPlan,
  onFileIssue,
  onFollowupAction,
}: {
  board: ShippedBoard | null;
  state: "idle" | "loading" | "error";
  error?: string | null;
  plans: PlanDraft[];
  busyPlanAction: string | null;
  busyQueue?: string | null;
  notice?: ActionNotice;
  onRefresh?: () => void;
  onQueueAction?: QueueActionHandler;
  onDecision: (plan: PlanDraft, decision: PlanDecision) => void;
  onDiscardPlan: (plan: PlanDraft) => void;
  onFileIssue: (plan: PlanDraft) => void;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [showLowSignal, setShowLowSignal] = useState(false);

  const loading = state === "loading";
  const columns = board?.columns;
  const hardError = board?.error;
  const loadError = state === "error" ? error || "Work refresh failed." : null;

  // Column 1: plans awaiting you. Dedupe identical drafts (issue 314) and tuck
  // low-signal drafts behind a disclosure so junk never crowds the column.
  const deduped = useMemo(() => dedupePlans(plans), [plans]);
  const visiblePlans = useMemo(
    () => deduped.filter((entry) => !isLowSignalPlan(entry.plan)),
    [deduped],
  );
  const lowSignal = useMemo(
    () => deduped.filter((entry) => isLowSignalPlan(entry.plan)),
    [deduped],
  );

  const selectedPlan =
    selection?.kind === "plan"
      ? plans.find((plan) => plan.plan_id === selection.id) || null
      : null;
  const selectedCard =
    selection?.kind === "card"
      ? [
          ...(columns?.queued || []),
          ...(columns?.in_progress || []),
          ...(columns?.shipped || []),
        ].find((card) => cardKey(card) === selection.key) || null
      : null;

  // Drop a stale selection when the underlying object disappears on refresh.
  useEffect(() => {
    if (selection?.kind === "plan" && !selectedPlan) setSelection(null);
    if (selection?.kind === "card" && !selectedCard) setSelection(null);
  }, [selection, selectedPlan, selectedCard]);

  const hasAnything =
    visiblePlans.length ||
    lowSignal.length ||
    (columns &&
      (columns.queued.length || columns.in_progress.length || columns.shipped.length));

  const canQueue = Boolean(onQueueAction) && supportsNativeActions();
  const generatedAt = board?.generated_at;
  const status = loadError
    ? "Work refresh failed."
    : hardError
      ? "Couldn't reach GitHub. Check gh auth."
      : generatedAt
        ? `updated ${friendlyTime(generatedAt)}`
        : null;

  return (
    <section className="alfred-pipeline" aria-label="Work">
      <section className="alfred-page-hero px-4 py-4" aria-label="Work summary">
        <div className="relative flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0 space-y-1">
            <h1 className="font-heading text-2xl font-medium tracking-normal text-foreground">
              Work
            </h1>
            <p className="max-w-3xl text-sm text-muted-foreground">
              One lifecycle: plans you approve become queued work, then runs in
              flight, then shipped outcomes.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {status ? (
              <span className="text-xs text-muted-foreground" title={generatedAt ? exactTime(generatedAt) : undefined}>
                {status}
              </span>
            ) : null}
            {onRefresh ? (
              <Button
                variant="ghost"
                size="icon-sm"
                type="button"
                onClick={onRefresh}
                disabled={loading}
                aria-label="Refresh pipeline"
              >
                <RefreshCw
                  size={15}
                  aria-hidden="true"
                  className={loading ? "animate-spin" : undefined}
                />
              </Button>
            ) : null}
          </div>
        </div>
      </section>

      {notice ? (
        <div className={`inline-notice inline-notice--${notice.tone}`}>
          {notice.tone === "ok" ? (
            <CheckCircle2 size={18} aria-hidden="true" />
          ) : (
            <AlertTriangle size={18} aria-hidden="true" />
          )}
          <span>{notice.message}</span>
        </div>
      ) : null}

      {canQueue && onQueueAction ? (
        <QueueComposer onQueueAction={onQueueAction} busy={Boolean(busyQueue)} />
      ) : null}

      {hardError && !hasAnything ? (
        <div className="inline-notice inline-notice--error">
          <AlertTriangle size={18} aria-hidden="true" />
          <span>
            Alfred reached the runtime but the pipeline failed to build ({hardError}).
            Check <code>gh auth status</code> and the watched-repo config.
          </span>
        </div>
      ) : null}

      {!loading && !hasAnything && !hardError ? (
        <EmptyState
          title="Nothing in the pipeline yet."
          body="When you ask Alfred for something, it appears here first as a plan for you to approve, then as work in progress, then as shipped."
        />
      ) : (
        <div className="alfred-pipeline__columns motion-rise">
          <PipelineColumn label="Needs your go-ahead" count={visiblePlans.length}>
            {visiblePlans.map((entry) => (
              <PlanLifecycleCard
                key={entry.plan.plan_id}
                plan={entry.plan}
                revisions={entry.revisions}
                busyPlanAction={busyPlanAction}
                selected={selection?.kind === "plan" && selection.id === entry.plan.plan_id}
                onSelect={() => setSelection({ kind: "plan", id: entry.plan.plan_id })}
                onDecision={onDecision}
              />
            ))}
            {lowSignal.length ? (
              <div className="alfred-pipeline__lowsignal">
                <button
                  type="button"
                  className="alfred-pipeline__lowsignal-toggle"
                  aria-expanded={showLowSignal}
                  onClick={() => setShowLowSignal((open) => !open)}
                >
                  <ChevronDown
                    size={14}
                    aria-hidden="true"
                    className={showLowSignal ? "rotate-180 transition-transform" : "transition-transform"}
                  />
                  {showLowSignal ? "Hide low signal" : `${lowSignal.length} low signal`}
                </button>
                {showLowSignal
                  ? lowSignal.map((entry) => (
                      <PlanLifecycleCard
                        key={entry.plan.plan_id}
                        plan={entry.plan}
                        revisions={entry.revisions}
                        busyPlanAction={busyPlanAction}
                        selected={selection?.kind === "plan" && selection.id === entry.plan.plan_id}
                        onSelect={() => setSelection({ kind: "plan", id: entry.plan.plan_id })}
                        onDecision={onDecision}
                      />
                    ))
                  : null}
              </div>
            ) : null}
            {!visiblePlans.length && !lowSignal.length ? (
              <p className="alfred-pipeline__empty">No plans waiting on you.</p>
            ) : null}
          </PipelineColumn>

          {BOARD_COLUMNS.map((col) => {
            const cards = columns?.[col.key] || [];
            return (
              <PipelineColumn key={col.key} label={col.label} count={cards.length}>
                {cards.length ? (
                  cards.map((card) => (
                    <BoardLifecycleCard
                      key={cardKey(card)}
                      card={card}
                      column={col.key}
                      selected={selection?.kind === "card" && selection.key === cardKey(card)}
                      onSelect={() => setSelection({ kind: "card", key: cardKey(card) })}
                    />
                  ))
                ) : (
                  <p className="alfred-pipeline__empty">Nothing here yet.</p>
                )}
              </PipelineColumn>
            );
          })}
        </div>
      )}

      <Sheet
        open={Boolean(selectedPlan || selectedCard)}
        onOpenChange={(open) => {
          if (!open) setSelection(null);
        }}
      >
        <SheetContent className="plan-detail-sheet">
          <SheetHeader>
            <SheetTitle>
              {selectedPlan ? "Review plan" : "Work item"}
            </SheetTitle>
            <SheetDescription>
              {selectedPlan
                ? "Approve, file, or open the GitHub evidence."
                : "Open the GitHub record or change the queue state."}
            </SheetDescription>
          </SheetHeader>
          {selectedPlan ? (
            <PlanInspector
              plan={selectedPlan}
              busyPlanAction={busyPlanAction}
              onDecision={onDecision}
              onDiscardPlan={onDiscardPlan}
              onFileIssue={onFileIssue}
              onFollowupAction={onFollowupAction}
            />
          ) : null}
          {selectedCard ? (
            <CardInspector
              card={selectedCard}
              column={
                (columns?.shipped || []).some((c) => cardKey(c) === cardKey(selectedCard))
                  ? "shipped"
                  : (columns?.in_progress || []).some((c) => cardKey(c) === cardKey(selectedCard))
                    ? "in_progress"
                    : "queued"
              }
              busyQueue={busyQueue}
              canQueue={canQueue}
              onQueueAction={onQueueAction}
            />
          ) : null}
        </SheetContent>
      </Sheet>
    </section>
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
  const [targetAgent, setTargetAgent] = useState<AssignmentTargetAgent>("auto");
  const parsed = parseIssueRef(value);
  const invalid = Boolean(value.trim()) && !parsed;

  return (
    <form
      className="alfred-pipeline__assign"
      aria-label="Assign existing GitHub issue"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!parsed || busy) return;
        const ok = await onQueueAction(parsed.repo, parsed.number, "assign", targetAgent);
        if (ok !== false) setValue("");
      }}
    >
      <div className="alfred-pipeline__assign-label">
        <FilePlus2 size={16} aria-hidden="true" />
        <span>Assign existing issue</span>
        <small>Paste owner/repo#123 or a GitHub issue URL.</small>
      </div>
      <input
        id="pipeline-assign-issue"
        value={value}
        onChange={(event) => setValue(event.currentTarget.value)}
        placeholder="owner/repo#123"
        spellCheck={false}
        aria-invalid={invalid}
        aria-describedby={invalid ? "pipeline-assign-error" : undefined}
      />
      <select
        value={targetAgent}
        onChange={(event) => setTargetAgent(event.currentTarget.value as AssignmentTargetAgent)}
        aria-label="Assignment target"
      >
        <option value="auto">Smart route</option>
        <option value="batman">Batman</option>
        <option value="lucius">Lucius</option>
      </select>
      <button className="secondary-button" type="submit" disabled={!parsed || busy}>
        <FilePlus2 size={16} aria-hidden="true" />
        <span>{busy ? "Routing" : "Route"}</span>
      </button>
      {invalid ? (
        <p id="pipeline-assign-error" className="alfred-pipeline__assign-error">
          Use owner/repo#123 or a GitHub issue URL.
        </p>
      ) : null}
    </form>
  );
}

function PipelineColumn({
  label,
  count,
  children,
}: {
  label: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="alfred-pipeline__column" aria-label={`${label} (${count})`}>
      <div className="alfred-pipeline__column-head">
        <span>{label}</span>
        <small>{count}</small>
      </div>
      <div className="alfred-pipeline__cards motion-rise">{children}</div>
    </section>
  );
}

function PlanLifecycleCard({
  plan,
  revisions,
  busyPlanAction,
  selected,
  onSelect,
  onDecision,
}: {
  plan: PlanDraft;
  revisions: number;
  busyPlanAction: string | null;
  selected: boolean;
  onSelect: () => void;
  onDecision: (plan: PlanDraft, decision: PlanDecision) => void;
}) {
  const canDecide = planNeedsAttention(plan);
  const actionBusy = busyPlanAction?.startsWith(`${plan.plan_id}:`) || false;
  const repos = repoChips(splitReposFull(plan.affected_repos));
  const outcome = revisions > 1 ? `${plan.title} (${revisions} revisions)` : plan.title;
  return (
    <LifecycleCard
      chip={planChip(plan)}
      repos={repos}
      age={plan.updated_at}
      outcome={outcome}
      selected={selected}
      onSelect={onSelect}
      action={
        canDecide ? (
          <button
            className="approve-button"
            type="button"
            disabled={actionBusy}
            onClick={() => onDecision(plan, "approve")}
          >
            <Check size={15} aria-hidden="true" />
            <span>Approve</span>
          </button>
        ) : null
      }
    />
  );
}

function BoardLifecycleCard({
  card,
  column,
  selected,
  onSelect,
}: {
  card: ShippedCard;
  column: BoardColumn;
  selected: boolean;
  onSelect: () => void;
}) {
  const agent = agentForShipped(card);
  const action =
    column === "shipped" && card.url ? (
      <button
        className="secondary-button"
        type="button"
        onClick={() => void openExternal(card.url as string)}
      >
        <ExternalLink size={15} aria-hidden="true" />
        <span>Open PR</span>
      </button>
    ) : null;
  return (
    <LifecycleCard
      chip={boardCardChip(card, column)}
      repos={repoChips([card.repo])}
      age={card.timestamp}
      outcome={cardOutcome(card)}
      attribution={agent ? <span>{agent}</span> : null}
      action={action}
      selected={selected}
      onSelect={onSelect}
    />
  );
}

function PlanInspector({
  plan,
  busyPlanAction,
  onDecision,
  onDiscardPlan,
  onFileIssue,
  onFollowupAction,
}: {
  plan: PlanDraft;
  busyPlanAction: string | null;
  onDecision: (plan: PlanDraft, decision: PlanDecision) => void;
  onDiscardPlan: (plan: PlanDraft) => void;
  onFileIssue: (plan: PlanDraft) => void;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
}) {
  const parentLink = plan.parent && isSafeExternalUrl(plan.parent) ? plan.parent : null;
  const slackLink = firstLink(plan.content, /slack\.com/i);
  const canDecide = planNeedsAttention(plan);
  const canFileIssue =
    !parentLink &&
    plan.readiness_ok === true &&
    (plan.source === "compose" || plan.source === "planning");
  const canDiscardDraft =
    !parentLink &&
    (plan.source === "compose" || plan.source === "planning");
  const isFollowup = plan.source === "followup";
  const actionBusy = busyPlanAction?.startsWith(`${plan.plan_id}:`) || false;
  return (
    <div className="detail-panel detail-panel--sheet" aria-label="Selected plan details">
      <div className="detail-panel__head">
        <span>{plan.status}</span>
        <h3>{plan.title}</h3>
      </div>
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
        {/* Dev-only: the raw readiness number survives in the panel, never on the card face. */}
        {plan.readiness_score !== null ? (
          <div>
            <dt>Readiness</dt>
            <dd>{plan.readiness_score}/100</dd>
          </div>
        ) : null}
        {/* Dev-only: the source is an internal routing detail, shown as origin here only. */}
        <div>
          <dt>Origin</dt>
          <dd>{plan.source}</dd>
        </div>
      </dl>
      {canDecide ? (
        <div className="plan-decision">
          <p className="plan-decision__note" role="note">
            Approving starts this exact scope on Batman's next run. Declining stops
            it. No code or worktrees move until you decide.
          </p>
          <div className="card-actions card-actions--start">
            <button
              className="approve-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecision(plan, "approve")}
            >
              <Check size={16} aria-hidden="true" />
              <span>Approve plan</span>
            </button>
            <button
              className="decline-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecision(plan, "decline")}
            >
              <X size={16} aria-hidden="true" />
              <span>Decline</span>
            </button>
          </div>
        </div>
      ) : null}
      <div className="card-actions card-actions--start">
        {canFileIssue ? (
          <button
            className="approve-button"
            type="button"
            disabled={actionBusy}
            onClick={() => onFileIssue(plan)}
          >
            <FilePlus2 size={16} aria-hidden="true" />
            <span>File GitHub issue</span>
          </button>
        ) : null}
        {canDiscardDraft ? (
          <button
            className="decline-button"
            type="button"
            disabled={actionBusy}
            onClick={() => onDiscardPlan(plan)}
          >
            <X size={16} aria-hidden="true" />
            <span>Discard draft</span>
          </button>
        ) : null}
        {isFollowup ? (
          <>
            <button
              className="approve-button"
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
              <Check size={16} aria-hidden="true" />
              <span>Mark handled</span>
            </button>
          </>
        ) : null}
        {parentLink ? (
          <button className="secondary-button" type="button" onClick={() => void openExternal(parentLink)}>
            <GitPullRequest size={16} aria-hidden="true" />
            <span>Open issue</span>
          </button>
        ) : null}
        {slackLink ? (
          <button className="secondary-button" type="button" onClick={() => void openExternal(slackLink)}>
            <MessageSquare size={16} aria-hidden="true" />
            <span>Open in Slack</span>
          </button>
        ) : null}
      </div>
      <pre className="detail-pre">
        {plan.content || plan.preview || "No plan body saved yet."}
      </pre>
    </div>
  );
}

function CardInspector({
  card,
  column,
  busyQueue,
  canQueue,
  onQueueAction,
}: {
  card: ShippedCard;
  column: BoardColumn;
  busyQueue?: string | null;
  canQueue: boolean;
  onQueueAction?: QueueActionHandler;
}) {
  const actionable =
    canQueue && column === "queued" && card.kind === "issue" && !card.demo && !!card.number;
  const holding = busyQueue === `hold:${card.repo}#${card.number}`;
  const closing = busyQueue === `done:${card.repo}#${card.number}`;
  return (
    <div className="detail-panel detail-panel--sheet" aria-label="Selected pipeline item">
      <div className="detail-panel__head">
        <span>{card.repo}</span>
        <h3>{cardOutcome(card)}</h3>
      </div>
      <dl className="compact-meta">
        {card.timestamp ? (
          <div>
            <dt>Updated</dt>
            <dd title={exactTime(card.timestamp)}>{friendlyTime(card.timestamp)}</dd>
          </div>
        ) : null}
        {card.author ? (
          <div>
            <dt>Author</dt>
            <dd>{card.author}</dd>
          </div>
        ) : null}
      </dl>
      <div className="card-actions card-actions--start">
        {card.url ? (
          <button className="secondary-button" type="button" onClick={() => void openExternal(card.url as string)}>
            <ExternalLink size={16} aria-hidden="true" />
            <span>Open on GitHub</span>
          </button>
        ) : null}
        {actionable && card.number ? (
          <>
            <button
              className="secondary-button"
              type="button"
              disabled={holding}
              onClick={() => onQueueAction?.(card.repo, card.number as number, "hold")}
            >
              <Ban size={16} aria-hidden="true" />
              <span>{holding ? "Holding" : "Hold"}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={closing}
              onClick={() => onQueueAction?.(card.repo, card.number as number, "done")}
            >
              <Check size={16} aria-hidden="true" />
              <span>{closing ? "Closing" : "Mark done"}</span>
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}

// Full repo slugs (for the inspector dd) from the affected-repos string.
function splitReposFull(value: string | null | undefined): string[] {
  if (!value) return [];
  return value
    .split(/[,\s]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}
