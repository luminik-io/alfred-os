import {
  AlertTriangle,
  Archive,
  ArrowRight,
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
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { exactTime, friendlyTime, shortId, titleCase } from "../format";
import { firstLink, isSafeExternalUrl, localUrl, openExternal } from "../lib/links";
import type { AttentionItem, FollowupAction } from "../lib/uiTypes";
import { supportsNativeActions } from "../api";
import type {
  FiringRecord,
  NativeCommandResult,
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
  const status = error ? "offline" : snapshot?.status.reliability.status || "checking";
  const tone = error ? "error" : status === "ok" ? "ok" : status === "checking" ? "info" : "warn";
  return (
    <span
      className={`status-pill status-pill--${tone}`}
      role="status"
      aria-live="polite"
      aria-label={`Connection ${titleCase(status)}`}
    >
      <span aria-hidden="true" />
      {titleCase(status)}
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
}: {
  error: string | null;
  errorRaw?: string | null;
  result: NativeCommandResult | null;
}) {
  if (!error && !result) return null;
  const isError = Boolean(error) || result?.success === false;
  const showDetails = Boolean(error && errorRaw && errorRaw !== error);
  return (
    <div
      className={`command-result ${isError ? "command-result--error" : ""}`}
      // Announce action outcomes to screen readers: failures assertively,
      // successful output politely so it does not interrupt.
      aria-live={isError ? "assertive" : "polite"}
    >
      <div className="command-result__head">
        <TerminalSquare size={18} aria-hidden="true" />
        <strong>{error ? "Action failed" : result?.message || "Local action output"}</strong>
      </div>
      {error ? <p>{error}</p> : null}
      {showDetails ? (
        <details className="notice-details">
          <summary>Details</summary>
          <pre>{errorRaw}</pre>
        </details>
      ) : null}
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

export function AttentionCard({ item }: { item: AttentionItem }) {
  const Icon =
    item.icon === "memory"
      ? MemoryStick
      : item.icon === "run"
        ? Radio
        : item.icon === "setup"
          ? Settings
          : ListChecks;
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

export function RunCard({ firing, baseUrl }: { firing: FiringRecord; baseUrl: string }) {
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

export function CompactPlanList({ plans, baseUrl }: { plans: PlanDraft[]; baseUrl: string }) {
  if (!plans.length) {
    return <EmptyState title="No plans yet." body="Planning drafts will appear here." compact />;
  }
  return (
    <div className="compact-list">
      {plans.map((plan) => (
        <button
          key={plan.plan_id}
          type="button"
          onClick={() => void openExternal(localUrl(baseUrl, `/plans/${plan.plan_id}`))}
        >
          <span>{plan.status}</span>
          <strong>{plan.title}</strong>
          <small>{friendlyTime(plan.updated_at)}</small>
        </button>
      ))}
    </div>
  );
}

export function CompactRunList({ firings, baseUrl }: { firings: FiringRecord[]; baseUrl: string }) {
  if (!firings.length) {
    return <EmptyState title="No runs yet." body="Recent firing traces will appear here." compact />;
  }
  return (
    <div className="compact-list">
      {firings.map((firing) => (
        <button
          key={firing.firing_id}
          type="button"
          onClick={() => void openExternal(localUrl(baseUrl, `/firings/${firing.firing_id}`))}
        >
          <span>{firing.codename}</span>
          <strong>{firing.summary}</strong>
          <small>{friendlyTime(firing.started_at)}</small>
        </button>
      ))}
    </div>
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

export function StatusDot({ status }: { status: string }) {
  // "running" is in-progress, not a problem: give it its own blue + pulse
  // treatment rather than the amber "warn" that reads as a fault.
  const tone =
    status === "live" || status === "ok"
      ? "ok"
      : status === "error"
        ? "error"
        : status === "running"
          ? "running"
          : "idle";
  return (
    <span className={`dot-label dot-label--${tone}`}>
      <span aria-hidden="true" />
      {titleCase(status)}
    </span>
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
