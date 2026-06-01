import { CheckCircle2, DatabaseZap, MemoryStick, TerminalSquare, XCircle } from "lucide-react";

import { exactTime, friendlyTime, titleCase } from "../format";
import { supportsNativeActions } from "../api";
import type { MemoryCandidate, Snapshot } from "../types";
import type { ActionNotice, NativeActionRequest } from "../lib/uiTypes";
import { EmptyState, PanelHeader, SignalCard } from "./atoms";

export function MemoryView({
  snapshot,
  actionNotice,
  busyMemoryAction,
  nativeBusy,
  onMemoryCandidateAction,
  onRunLocalAction,
}: {
  snapshot: Snapshot | null;
  actionNotice: ActionNotice;
  busyMemoryAction: string | null;
  nativeBusy: string | null;
  onMemoryCandidateAction: (candidateId: string, action: "promote" | "reject") => void;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const candidates = snapshot?.memoryCandidates.rows || [];
  const suggestions = snapshot?.actions.promotion_suggestions || [];
  const errors = {
    ...(snapshot?.actions.errors || {}),
    ...(snapshot?.memoryCandidates.error ? { candidates: snapshot.memoryCandidates.error } : {}),
  };
  const canRun = supportsNativeActions();

  return (
    <section className="dashboard-grid dashboard-grid--memory">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Memory" title="Review queue" />
        {actionNotice ? (
          <div className={`inline-notice inline-notice--${actionNotice.tone}`}>
            {actionNotice.tone === "ok" ? (
              <CheckCircle2 size={18} aria-hidden="true" />
            ) : (
              <XCircle size={18} aria-hidden="true" />
            )}
            <span>{actionNotice.message}</span>
          </div>
        ) : null}
        {candidates.length ? (
          <div className="memory-candidate-list">
            {candidates.map((candidate) => (
              <MemoryCandidateCard
                key={candidate.id}
                candidate={candidate}
                busyMemoryAction={busyMemoryAction}
                onMemoryCandidateAction={onMemoryCandidateAction}
              />
            ))}
          </div>
        ) : suggestions.length ? (
          <div className="attention-list">
            {suggestions.map((signal, index) => (
              <SignalCard
                key={`${signal.title || signal.message || "memory"}-${index}`}
                signal={signal}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No memory candidates surfaced."
            body="Slack-curated, planning, and repeated-failure candidates appear here before they can enter recall."
            tone="ok"
          />
        )}
      </div>
      <div className="panel">
        <PanelHeader eyebrow="Checks" title="Memory health" />
        {Object.keys(errors).length ? (
          <dl className="health-list">
            {Object.entries(errors).map(([key, value]) => (
              <div key={key}>
                <dt>{titleCase(key)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <EmptyState
            title="No memory errors reported."
            body="Use the action below for a deeper local memory health report."
            tone="ok"
          />
        )}
        <div className="button-stack">
          {canRun ? (
            <>
              <button
                className="icon-button"
                type="button"
                disabled={nativeBusy === "brain_doctor:fleet"}
                onClick={() => onRunLocalAction({ action: "brain_doctor" })}
              >
                <TerminalSquare size={16} aria-hidden="true" />
                <span>{nativeBusy === "brain_doctor:fleet" ? "Checking" : "Run memory check"}</span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={nativeBusy === "redis_status:fleet"}
                onClick={() => onRunLocalAction({ action: "redis_status" })}
              >
                <MemoryStick size={16} aria-hidden="true" />
                <span>
                  {nativeBusy === "redis_status:fleet" ? "Checking" : "Check Redis memory"}
                </span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={nativeBusy === "redis_sync_preview:fleet"}
                onClick={() => onRunLocalAction({ action: "redis_sync_preview" })}
              >
                <DatabaseZap size={16} aria-hidden="true" />
                <span>
                  {nativeBusy === "redis_sync_preview:fleet"
                    ? "Checking"
                    : "Preview Redis sync"}
                </span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={nativeBusy === "memory_harvest:fleet"}
                onClick={() => onRunLocalAction({ action: "memory_harvest", refreshAfter: true })}
              >
                <MemoryStick size={16} aria-hidden="true" />
                <span>
                  {nativeBusy === "memory_harvest:fleet"
                    ? "Harvesting"
                    : "Queue failure lessons"}
                </span>
              </button>
            </>
          ) : (
            <p className="console-note">
              Memory actions run inside the desktop app. Browser preview stays read-only.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function MemoryCandidateCard({
  candidate,
  busyMemoryAction,
  onMemoryCandidateAction,
}: {
  candidate: MemoryCandidate;
  busyMemoryAction: string | null;
  onMemoryCandidateAction: (candidateId: string, action: "promote" | "reject") => void;
}) {
  const isPromoting = busyMemoryAction === `${candidate.id}:promote`;
  const isRejecting = busyMemoryAction === `${candidate.id}:reject`;
  const evidence = evidencePreview(candidate.evidence);
  return (
    <article className="memory-candidate">
      <div className="memory-candidate__body">
        <div className="plan-card__meta">
          <span>{candidate.severity}</span>
          <span>{Math.round(Number(candidate.confidence || 0) * 100)}%</span>
          <span>{candidate.source}</span>
        </div>
        <h2>{candidate.body}</h2>
        <dl className="compact-meta">
          <div>
            <dt>Repo</dt>
            <dd>{candidate.repo}</dd>
          </div>
          <div>
            <dt>Agent</dt>
            <dd>{candidate.codename}</dd>
          </div>
          <div>
            <dt>Queued</dt>
            <dd title={exactTime(candidate.created_at)}>{friendlyTime(candidate.created_at)}</dd>
          </div>
        </dl>
        {candidate.tags.length ? (
          <div className="tag-row">
            {candidate.tags.map((tag) => (
              <span key={tag}>{tag}</span>
            ))}
          </div>
        ) : null}
        {evidence ? (
          <details className="memory-evidence">
            <summary>Evidence</summary>
            <pre>{evidence}</pre>
          </details>
        ) : null}
      </div>
      <div className="card-actions">
        <button
          className="icon-button"
          type="button"
          disabled={Boolean(busyMemoryAction)}
          onClick={() => onMemoryCandidateAction(candidate.id, "promote")}
        >
          <CheckCircle2 size={16} aria-hidden="true" />
          <span>{isPromoting ? "Promoting" : "Promote"}</span>
        </button>
        <button
          className="secondary-button"
          type="button"
          disabled={Boolean(busyMemoryAction)}
          onClick={() => onMemoryCandidateAction(candidate.id, "reject")}
        >
          <XCircle size={16} aria-hidden="true" />
          <span>{isRejecting ? "Rejecting" : "Reject"}</span>
        </button>
      </div>
    </article>
  );
}

function evidencePreview(value: string): string {
  const raw = value.trim();
  if (!raw) return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
