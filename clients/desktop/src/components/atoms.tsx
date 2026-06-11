import {
  AlertTriangle,
  Archive,
  ArrowRight,
  Check,
  CheckCircle2,
  ExternalLink,
  FilePlus2,
  GitPullRequest,
  Inbox,
  ListChecks,
  MemoryStick,
  MessageSquare,
  Play,
  Radio,
  Settings,
  TerminalSquare,
  X,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { exactTime, friendlyTime, titleCase } from "../format";
import { planNeedsAttention } from "../lib/derive";
import { firstLink, isSafeExternalUrl, openExternal } from "../lib/links";
import type { AttentionItem, FollowupAction } from "../lib/uiTypes";
import { supportsNativeActions } from "../api";
import type {
  NativeCommandResult,
  PlanDecision,
  PlanDraft,
  ReliabilitySignal,
  Snapshot,
} from "../types";

export function StatusPill({
  snapshot,
  error,
}: {
  snapshot: Snapshot | null;
  error: string | null;
}) {
  // The pill rolls up two distinct facts: whether alfred-serve is reachable
  // (connection) and, when it is, how healthy the fleet is (reliability). Keep
  // them separate so a fleet warning over a perfectly good connection does not
  // read as a connection failure.
  const offline = Boolean(error);
  const health = snapshot?.status.reliability.status || "checking";
  const state = offline ? "offline" : health;
  const tone = offline ? "error" : state === "ok" ? "ok" : state === "checking" ? "info" : "warn";
  const text =
    state === "offline"
      ? "Offline"
      : state === "checking"
        ? "Connecting"
        : state === "ok"
          ? "Live"
          : titleCase(state);
  const label =
    state === "offline"
      ? "Alfred serve offline"
      : state === "checking"
        ? "Connecting to Alfred serve"
        : state === "ok"
          ? "Connected to Alfred serve, fleet healthy"
          : `Connected to Alfred serve, fleet ${titleCase(state)}`;
  return (
    <span
      className={`status-pill status-pill--${tone}`}
      role="status"
      aria-live="polite"
      aria-label={label}
      title={label}
    >
      <span aria-hidden="true" />
      {text}
    </span>
  );
}

export function ConnectionBanner({
  error,
  errorRaw,
  nativeBusy,
  onStartRuntime,
}: {
  error: string;
  errorRaw?: string | null;
  nativeBusy: string | null;
  onStartRuntime: () => void;
}) {
  const canRun = supportsNativeActions();
  // Only surface the raw text in the disclosure when it adds something beyond
  // the plain-language guidance we already lead with.
  const showDetails = Boolean(errorRaw && errorRaw !== error);
  return (
    <section className="notice-panel notice-panel--error" role="alert" aria-live="assertive">
      <AlertTriangle size={20} aria-hidden="true" />
      <div className="notice-panel__body">
        <strong>Alfred serve is not reachable yet.</strong>
        <p>{error}</p>
        {showDetails ? (
          <details className="notice-details">
            <summary>Details</summary>
            <pre>{errorRaw}</pre>
          </details>
        ) : null}
      </div>
      {canRun ? (
        <button
          className="icon-button"
          type="button"
          disabled={nativeBusy === "runtime:start"}
          onClick={onStartRuntime}
        >
          <Play size={16} aria-hidden="true" />
          <span>{nativeBusy === "runtime:start" ? "Starting" : "Start runtime"}</span>
        </button>
      ) : (
        <p className="fallback-note">
          Open Alfred as a desktop app to start the local runtime from here.
        </p>
      )}
    </section>
  );
}

export function NativeResultPanel({
  error,
  errorRaw,
  result,
  onDismiss,
}: {
  error: string | null;
  errorRaw?: string | null;
  result: NativeCommandResult | null;
  /** When provided, renders a dismiss control so the panel never lingers. */
  onDismiss?: () => void;
}) {
  if (!error && !result) return null;
  const isError = Boolean(error) || result?.success === false;
  const showErrorDetails = Boolean(error && errorRaw && errorRaw !== error);
  // Lead with a plain-English headline; tuck the raw command + stdout/stderr
  // behind a disclosure so a successful check is a one-liner, not a wall of
  // JSON pinned to the top of the app.
  const headline = error
    ? "Action failed"
    : result?.message || friendlyActionLabel(result) || "Done";
  const hasRawOutput = Boolean(result && (result.stdout || result.stderr));
  return (
    <div
      className={`command-result ${isError ? "command-result--error" : ""}`}
      // Announce action outcomes to screen readers: failures assertively,
      // successful output politely so it does not interrupt.
      aria-live={isError ? "assertive" : "polite"}
    >
      <div className="command-result__head">
        <TerminalSquare size={18} aria-hidden="true" />
        <strong>{headline}</strong>
        {result && !isError ? <span className="command-result__ok">Success</span> : null}
        {onDismiss ? (
          <button
            className="command-result__dismiss"
            type="button"
            aria-label="Dismiss"
            onClick={onDismiss}
          >
            <X size={15} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {error ? <p>{error}</p> : null}
      {showErrorDetails ? (
        <details className="notice-details">
          <summary>Technical details</summary>
          <pre>{errorRaw}</pre>
        </details>
      ) : null}
      {result ? (
        <details className="notice-details">
          <summary>Technical details</summary>
          <code>{result.command.join(" ")}</code>
          {result.pid ? <p>Process {result.pid} is running in the background.</p> : null}
          {result.status !== null ? <p>Exit status: {result.status}</p> : null}
          {hasRawOutput ? (
            <>
              {result.stdout ? <pre>{result.stdout}</pre> : null}
              {result.stderr ? <pre>{result.stderr}</pre> : null}
            </>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}

// Turn a curated CLI command into a plain-English headline for non-technical
// users (the raw command stays available under "Technical details").
function friendlyActionLabel(result: NativeCommandResult | null): string | null {
  if (!result) return null;
  const joined = result.command.join(" ");
  if (joined.includes("brain status") || joined.includes("brain-doctor")) return "Memory check complete";
  if (joined.includes("redis")) return "Redis memory check complete";
  if (joined.includes("auth status")) return "Auth check complete";
  if (joined.includes("status --json") || /\bstatus\b/.test(joined)) return "Agent status refreshed";
  if (joined.includes("agents")) return "Agents listed";
  if (joined.includes("schedule set")) return "Schedule updated";
  if (joined.includes("dry-run") || joined.includes("dry_run")) return "Dry-run complete";
  return null;
}

export function AttentionCard({
  item,
  onNavigate,
  onDecide,
  busyPlanAction,
}: {
  item: AttentionItem;
  onNavigate?: (tab: AttentionItem["targetTab"]) => void;
  /** Record a go/no-go directly on a single waiting Batman plan. */
  onDecide?: (planId: string, decision: PlanDecision) => void;
  busyPlanAction?: string | null;
}) {
  const Icon =
    item.icon === "memory"
      ? MemoryStick
      : item.icon === "run"
        ? Radio
        : item.icon === "setup"
          ? Settings
          : ListChecks;
  // A single Batman plan awaiting a sign-off can be approved or declined right
  // here, before work starts. derive.ts only sets planId on that case.
  const canDecide = Boolean(item.planId) && Boolean(onDecide);
  const actionBusy = (busyPlanAction && item.planId
    ? busyPlanAction.startsWith(`${item.planId}:`)
    : false) as boolean;
  return (
    <article className={`attention-card attention-card--${item.tone}`}>
      <Icon size={20} aria-hidden="true" />
      <div>
        <span>{item.label}</span>
        <strong>{item.title}</strong>
        <p>{item.detail}</p>
        {canDecide ? (
          <p className="attention-card__decision-note" role="note">
            Approving starts this exact scope on Batman's next run. Declining stops it.
          </p>
        ) : null}
        {item.command ? <code>{item.command}</code> : null}
      </div>
      <div className="card-actions">
        {canDecide ? (
          <>
            <button
              className="approve-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecide?.(item.planId as string, "approve")}
            >
              <Check size={16} aria-hidden="true" />
              <span>Approve</span>
            </button>
            <button
              className="decline-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecide?.(item.planId as string, "decline")}
            >
              <X size={16} aria-hidden="true" />
              <span>Decline</span>
            </button>
          </>
        ) : null}
        {item.targetTab ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() => onNavigate?.(item.targetTab)}
          >
            <ArrowRight size={16} aria-hidden="true" />
            <span>{item.icon === "run" ? "Inspect runs" : "Review"}</span>
          </button>
        ) : null}
        {item.href ? <ExternalButton label="Open external" href={item.href} icon={<ExternalLink size={16} />} /> : null}
      </div>
    </article>
  );
}

export function SignalCard({ signal }: { signal: ReliabilitySignal }) {
  return (
    <article className="attention-card attention-card--info">
      <MemoryStick size={20} aria-hidden="true" />
      <div>
        <span>{signal.severity || "memory"}</span>
        <strong>{signal.title || signal.action || signal.codename || "Memory candidate"}</strong>
        <p>{signal.message || signal.summary || signal.reason || "Review evidence before promotion."}</p>
        {signal.command ? <code>{signal.command}</code> : null}
      </div>
    </article>
  );
}

export function PlanCard({
  plan,
  busyPlanAction,
  onFollowupAction,
  onDecision,
  selected = false,
  onSelect,
}: {
  plan: PlanDraft;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
  /** Record a real go/no-go on a genuine Batman plan (approve starts work). */
  onDecision?: (plan: PlanDraft, decision: PlanDecision) => void;
  selected?: boolean;
  onSelect?: (plan: PlanDraft) => void;
}) {
  const slackLink = firstLink(plan.content, /slack\.com/i);
  const parentLink = plan.parent && isSafeExternalUrl(plan.parent) ? plan.parent : null;
  const isFollowup = plan.source === "followup";
  // Only a genuine Batman go/no-go plan that is still awaiting a sign-off can be
  // approved or declined in-app. planNeedsAttention already encodes both halves
  // (source === "batman" and a waiting status), so a decided plan loses the
  // buttons and reads as approved/declined instead.
  const canDecide = Boolean(onDecision) && planNeedsAttention(plan);
  const actionBusy = busyPlanAction?.startsWith(`${plan.plan_id}:`) || false;
  return (
    <article className={selected ? "plan-card plan-card--selected" : "plan-card"}>
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
        {canDecide ? (
          <p className="plan-card__decision-note" role="note">
            Approving starts this exact scope on Batman's next run. Declining stops
            it. No code or worktrees move until you decide.
          </p>
        ) : null}
      </div>
      <div className="card-actions">
        {canDecide ? (
          <>
            <button
              className="approve-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecision?.(plan, "approve")}
            >
              <Check size={16} aria-hidden="true" />
              <span>Approve</span>
            </button>
            <button
              className="decline-button"
              type="button"
              disabled={actionBusy}
              onClick={() => onDecision?.(plan, "decline")}
            >
              <X size={16} aria-hidden="true" />
              <span>Decline</span>
            </button>
          </>
        ) : null}
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
        {onSelect ? (
          <button className="secondary-button" type="button" onClick={() => onSelect(plan)}>
            <ListChecks size={16} aria-hidden="true" />
            <span>{selected ? "Selected" : "Inspect"}</span>
          </button>
        ) : null}
        {parentLink ? (
          <ExternalButton label="Open issue" href={parentLink} icon={<GitPullRequest size={16} />} />
        ) : null}
        {slackLink ? (
          <ExternalButton label="Open in Slack" href={slackLink} icon={<MessageSquare size={16} />} />
        ) : null}
      </div>
    </article>
  );
}

export function PanelHeader({
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

// Empty states are not all the same shape. "neutral" is the default (nothing
// yet / not connected) and reads as informational, not as success. "ok" is for
// genuinely-good emptiness ("no human decision waiting"). "error" is for
// failure-shaped emptiness ("alfred serve returned 403"). The icon follows the
// tone unless an explicit one is passed.
export type EmptyTone = "neutral" | "ok" | "error";

const EMPTY_TONE_ICON: Record<EmptyTone, LucideIcon> = {
  neutral: Inbox,
  ok: CheckCircle2,
  error: XCircle,
};

export function EmptyState({
  title,
  body,
  compact = false,
  tone = "neutral",
  icon,
}: {
  title: string;
  body: string;
  compact?: boolean;
  tone?: EmptyTone;
  icon?: LucideIcon;
}) {
  const Icon = icon || EMPTY_TONE_ICON[tone];
  const className = [
    "empty-state",
    `empty-state--${tone}`,
    compact ? "empty-state--compact" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={className}>
      <Icon size={22} aria-hidden="true" />
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

export function ExternalButton({
  label,
  href,
  icon,
}: {
  label: string;
  href: string;
  icon: React.ReactNode;
}) {
  return (
    <button className="secondary-button" type="button" onClick={() => void openExternal(href)}>
      {icon}
      <span>{label}</span>
    </button>
  );
}
