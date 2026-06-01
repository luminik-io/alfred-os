import { AlertTriangle, CheckCircle2, GitPullRequest, MessageSquare } from "lucide-react";
import { useEffect, useState } from "react";

import type { PlanDraft } from "../types";
import type { ActionNotice, FollowupAction, TabKey } from "../lib/uiTypes";
import { exactTime, friendlyTime } from "../format";
import { firstLink, isSafeExternalUrl } from "../lib/links";
import { EmptyState, ExternalButton, PanelHeader, PlanCard } from "./atoms";

export function PlansView({
  plans,
  actionNotice,
  busyPlanAction,
  onFollowupAction,
  onSwitch,
}: {
  plans: PlanDraft[];
  actionNotice: ActionNotice;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
  onSwitch: (tab: TabKey) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(plans[0]?.plan_id || null);
  const selectedPlan = plans.find((plan) => plan.plan_id === selectedId) || plans[0] || null;

  useEffect(() => {
    if (!plans.length) {
      setSelectedId(null);
      return;
    }
    if (!plans.some((plan) => plan.plan_id === selectedId)) {
      setSelectedId(plans[0].plan_id);
    }
  }, [plans, selectedId]);

  return (
    <section className="panel">
      <PanelHeader
        eyebrow="Planning"
        title="Saved plans and follow-ups"
        actionLabel="Compose new"
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
        <div className="inspect-layout">
          <div className="plan-grid plan-grid--compact">
            {plans.map((plan) => (
              <PlanCard
                key={plan.plan_id}
                plan={plan}
                busyPlanAction={busyPlanAction}
                onFollowupAction={onFollowupAction}
                selected={plan.plan_id === selectedPlan?.plan_id}
                onSelect={(nextPlan) => setSelectedId(nextPlan.plan_id)}
              />
            ))}
          </div>
          <PlanInspector plan={selectedPlan} />
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

function PlanInspector({ plan }: { plan: PlanDraft | null }) {
  if (!plan) {
    return <EmptyState title="Select a plan." body="Choose a plan to inspect its saved spec." />;
  }
  const parentLink = plan.parent && isSafeExternalUrl(plan.parent) ? plan.parent : null;
  const slackLink = firstLink(plan.content, /slack\.com/i);
  return (
    <aside className="detail-panel" aria-label="Selected plan details">
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
      <div className="card-actions card-actions--start">
        {parentLink ? (
          <ExternalButton label="Open issue" href={parentLink} icon={<GitPullRequest size={16} />} />
        ) : null}
        {slackLink ? (
          <ExternalButton label="Open Slack" href={slackLink} icon={<MessageSquare size={16} />} />
        ) : null}
      </div>
      <pre className="detail-pre">{plan.content || plan.preview || "No saved plan body."}</pre>
    </aside>
  );
}
