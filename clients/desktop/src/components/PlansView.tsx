import { AlertTriangle, CheckCircle2 } from "lucide-react";

import type { PlanDraft } from "../types";
import type { ActionNotice, FollowupAction, TabKey } from "../lib/uiTypes";
import { EmptyState, PanelHeader, PlanCard } from "./atoms";

export function PlansView({
  plans,
  baseUrl,
  actionNotice,
  busyPlanAction,
  onFollowupAction,
  onSwitch,
}: {
  plans: PlanDraft[];
  baseUrl: string;
  actionNotice: ActionNotice;
  busyPlanAction: string | null;
  onFollowupAction: (plan: PlanDraft, action: FollowupAction) => void;
  onSwitch: (tab: TabKey) => void;
}) {
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
        <div className="plan-grid">
          {plans.map((plan) => (
            <PlanCard
              key={plan.plan_id}
              plan={plan}
              baseUrl={baseUrl}
              busyPlanAction={busyPlanAction}
              onFollowupAction={onFollowupAction}
            />
          ))}
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
