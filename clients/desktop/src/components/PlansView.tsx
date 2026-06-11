import {
  AlertTriangle,
  Check,
  CheckCircle2,
  FilePlus2,
  GitPullRequest,
  MessageSquare,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import type { PlanDecision, PlanDraft } from "../types";
import type { ActionNotice, FollowupAction, TabKey } from "../lib/uiTypes";
import { exactTime, friendlyTime } from "../format";
import { planNeedsAttention } from "../lib/derive";
import { firstLink, isSafeExternalUrl } from "../lib/links";
import { EmptyState, ExternalButton, PanelHeader, PlanCard } from "./atoms";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "./ui";

export function PlansView({
  plans,
  actionNotice,
  busyPlanAction,
  onFollowupAction,
  onDecision,
  onFileIssue,
  onSwitch,
}: {
  plans: PlanDraft[];
  actionNotice: ActionNotice;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
  onDecision: (plan: PlanDraft, decision: PlanDecision) => void;
  onFileIssue: (plan: PlanDraft) => void;
  onSwitch: (tab: TabKey) => void;
}) {
  const [detailPlanId, setDetailPlanId] = useState<string | null>(null);
  const detailPlan = plans.find((plan) => plan.plan_id === detailPlanId) || null;

  useEffect(() => {
    if (detailPlanId && !plans.some((plan) => plan.plan_id === detailPlanId)) {
      setDetailPlanId(null);
    }
  }, [plans, detailPlanId]);

  return (
    <section className="panel">
      <PanelHeader
        eyebrow="Plans"
        title="Saved work requests"
        actionLabel="Ask"
        onAction={() => onSwitch("compose")}
      />
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
        <>
          <div className="plans-workspace">
            {plans.map((plan) => (
              <PlanCard
                key={plan.plan_id}
                plan={plan}
                busyPlanAction={busyPlanAction}
                onFollowupAction={onFollowupAction}
                onDecision={onDecision}
                selected={plan.plan_id === detailPlanId}
                onSelect={(nextPlan) => setDetailPlanId(nextPlan.plan_id)}
              />
            ))}
          </div>
          <Sheet
            open={Boolean(detailPlan)}
            onOpenChange={(open) => {
              if (!open) {
                setDetailPlanId(null);
              }
            }}
          >
            <SheetContent
              className="plan-detail-sheet"
            >
              <SheetHeader>
                <SheetTitle>Review plan</SheetTitle>
                <SheetDescription>
                  Approve, file, or open the GitHub evidence.
                </SheetDescription>
              </SheetHeader>
              <PlanInspector
                plan={detailPlan}
                busyPlanAction={busyPlanAction}
                onDecision={onDecision}
                onFileIssue={onFileIssue}
              />
            </SheetContent>
          </Sheet>
        </>
      ) : (
        <EmptyState
          title="No saved plans yet."
          body="Batman plans, Slack planning drafts, and trusted follow-ups appear here once Alfred saves the scope."
        />
      )}
    </section>
  );
}

function PlanInspector({
  plan,
  busyPlanAction,
  onDecision,
  onFileIssue,
}: {
  plan: PlanDraft | null;
  busyPlanAction: string | null;
  onDecision: (plan: PlanDraft, decision: PlanDecision) => void;
  onFileIssue: (plan: PlanDraft) => void;
}) {
  if (!plan) {
    return (
      <EmptyState
        title="Select a plan."
        body="Choose a plan to review its scope, approval state, and GitHub issue."
      />
    );
  }
  const parentLink = plan.parent && isSafeExternalUrl(plan.parent) ? plan.parent : null;
  const slackLink = firstLink(plan.content, /slack\.com/i);
  const canDecide = planNeedsAttention(plan);
  const canFileIssue = planCanFileIssue(plan, parentLink);
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
        {plan.readiness_score !== null ? (
          <div>
            <dt>Readiness</dt>
            <dd>{plan.readiness_score}/100</dd>
          </div>
        ) : null}
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
        {parentLink ? (
          <ExternalButton label="Open issue" href={parentLink} icon={<GitPullRequest size={16} />} />
        ) : null}
        {slackLink ? (
          <ExternalButton label="Open in Slack" href={slackLink} icon={<MessageSquare size={16} />} />
        ) : null}
      </div>
      <pre className="detail-pre">
        {plan.content || plan.preview || "No plan body saved yet."}
      </pre>
    </div>
  );
}

function planCanFileIssue(plan: PlanDraft, issueUrl: string | null): boolean {
  if (issueUrl) {
    return false;
  }
  if (plan.readiness_ok !== true) {
    return false;
  }
  return plan.source === "compose" || plan.source === "planning";
}
