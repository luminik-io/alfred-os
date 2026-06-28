import {
  AlertTriangle,
  BookOpen,
  Check,
  DatabaseZap,
  MemoryStick,
  MessageSquare,
  Repeat,
  Sparkles,
  TerminalSquare,
  Wand2,
  X,
} from "lucide-react";

import { friendlyTime, titleCase } from "../format";
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
  const candidatesError = snapshot?.memoryCandidates.error || null;
  const activeLessons = snapshot?.memoryLessons?.rows || [];

  return (
    <section className="panel animate-rise">
      <PanelHeader eyebrow="Learnings" title="What Alfred has learned" />
      <p className="panel-intro">
        When Alfred notices something worth remembering, it writes a short learning here for you to
        confirm. Keep the ones that look right and Alfred will use them next time. Dismiss the rest.
      </p>

      {actionNotice ? (
        <div className={`inline-notice inline-notice--${actionNotice.tone}`}>
          {actionNotice.tone === "ok" ? (
            <Check size={18} aria-hidden="true" />
          ) : (
            <X size={18} aria-hidden="true" />
          )}
          <span>{actionNotice.message}</span>
        </div>
      ) : null}

      {candidatesError ? (
        <EmptyState
          title="Alfred could not load its lessons right now."
          body="The connection to Alfred's memory was interrupted. This usually clears on the next refresh. The technical detail is in Advanced below."
          tone="error"
        />
      ) : candidates.length ? (
        <div className="lesson-list">
          {candidates.map((candidate) => (
            <LessonCard
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
          title="Alfred has not learned anything new yet."
          body="As Alfred works on your projects, anything worth remembering shows up here for you to confirm. Nothing needs your attention right now."
          tone="ok"
        />
      )}

      {activeLessons.length ? (
        <section className="lessons-active" aria-label="Lessons Alfred is using">
          <h3 className="subsection-title">Lessons Alfred is using</h3>
          <p className="lessons-active__intro">
            Confirmed lessons Alfred now applies as it works, including ones you kept and ones it
            accepted on its own.
          </p>
          <ul className="active-lesson-list">
            {activeLessons.map((lesson) => (
              <li key={lesson.id} className="active-lesson">
                <span className="active-lesson__what">{lesson.body}</span>
                <span className="active-lesson__where">
                  {prettyAgent(lesson.codename)}
                  {lesson.repo ? ` · ${lesson.repo}` : ""} · {friendlyTime(lesson.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <AdvancedPanel
        snapshot={snapshot}
        nativeBusy={nativeBusy}
        onRunLocalAction={onRunLocalAction}
      />
    </section>
  );
}

// Where a lesson came from, in words the designer recognises. The server
// emits machine source strings; we map the known ones to a plain sentence and
// an icon, and fall back to a calm generic for anything new.
type LessonOrigin = { label: string; icon: typeof MessageSquare };

function lessonOrigin(source: string): LessonOrigin {
  const key = source.toLowerCase();
  if (key.startsWith("slack")) {
    return { label: "From a Slack conversation", icon: MessageSquare };
  }
  if (key.startsWith("planning") || key.includes("plan")) {
    return { label: "From planning a request", icon: Wand2 };
  }
  if (key.includes("failure") || key.includes("harvest") || key === "memory_candidate") {
    return { label: "From a repeated problem Alfred hit", icon: Repeat };
  }
  return { label: "From Alfred's work", icon: Sparkles };
}

// Severity describes how strongly Alfred wants to remember a lesson. The raw
// values (info / warning / blocker) read like log levels, so we say it plainly.
function whyItMatters(severity: string): string | null {
  const key = severity.toLowerCase();
  if (key === "blocker") return "Worth remembering: this caused something to get stuck.";
  if (key === "warning") return "Worth a look: this caused trouble before.";
  if (key === "info") return null;
  return null;
}

// Confidence is a 0-1 score. Turn it into a one-word steer instead of a
// percentage a non-developer has to interpret.
function confidenceWord(value: number): string {
  const pct = Number(value || 0);
  if (pct >= 0.8) return "Alfred is fairly sure about this";
  if (pct >= 0.5) return "Alfred is moderately sure about this";
  return "Alfred is unsure about this";
}

function LessonCard({
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
  // Only THIS card is busy while it acts. The old `Boolean(busyMemoryAction)`
  // disabled every card's buttons whenever any single candidate was acting.
  const busy = isPromoting || isRejecting;
  const origin = lessonOrigin(candidate.source);
  const OriginIcon = origin.icon;
  const matters = whyItMatters(candidate.severity);
  const evidence = evidencePreview(candidate.evidence);
  const where = candidate.repo ? `about ${candidate.repo}` : null;

  return (
    <article className="lesson-card">
      <div className="lesson-card__body">
        <div className="lesson-card__origin">
          <OriginIcon size={15} aria-hidden="true" />
          <span>{origin.label}</span>
          {where ? <span className="lesson-card__where">{where}</span> : null}
        </div>

        <h3 className="lesson-card__what">
          {(candidate.statement || "").trim() || candidate.body}
        </h3>

        {matters ? <p className="lesson-card__matters">{matters}</p> : null}

        <p className="lesson-card__provenance">
          Noticed by {prettyAgent(candidate.codename)} {friendlyTime(candidate.created_at)}.{" "}
          {confidenceWord(candidate.confidence)}.
        </p>

        {candidate.tags.length ? (
          <div className="tag-row">
            {candidate.tags.map((tag) => (
              <span key={tag}>{tag}</span>
            ))}
          </div>
        ) : null}

        {evidence ? (
          <details className="notice-details">
            <summary>Technical detail</summary>
            <pre>{evidence}</pre>
          </details>
        ) : null}
      </div>

      <div className="lesson-card__decide">
        <p className="lesson-card__hint">
          Keeping a lesson lets Alfred use it the next time it works on your projects.
        </p>
        <div className="card-actions">
          <button
            className="icon-button"
            type="button"
            disabled={busy}
            aria-busy={isPromoting}
            onClick={() => onMemoryCandidateAction(candidate.id, "promote")}
          >
            <Check size={16} aria-hidden="true" />
            <span>{isPromoting ? "Keeping" : "Keep this lesson"}</span>
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={busy}
            aria-busy={isRejecting}
            onClick={() => onMemoryCandidateAction(candidate.id, "reject")}
          >
            <X size={16} aria-hidden="true" />
            <span>{isRejecting ? "Dismissing" : "Dismiss"}</span>
          </button>
        </div>
      </div>
    </article>
  );
}

// The Redis / harvest plumbing is real and stays available, but it is operator
// depth, not the main surface. Tuck it under a single closed disclosure so the
// page leads with the lessons themselves.
function AdvancedPanel({
  snapshot,
  nativeBusy,
  onRunLocalAction,
}: {
  snapshot: Snapshot | null;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const canRun = supportsNativeActions();
  const errors = {
    ...(snapshot?.actions.errors || {}),
    ...(snapshot?.memoryCandidates.error
      ? { candidates: snapshot.memoryCandidates.error }
      : {}),
  };
  const errorCount = Object.keys(errors).length;

  return (
    <details className="advanced-panel">
      <summary>
        <TerminalSquare size={15} aria-hidden="true" />
        <span>Advanced (technical detail)</span>
      </summary>
      <div className="advanced-panel__body">
        <p className="advanced-panel__intro">
          Tools for inspecting where Alfred stores its memory. You do not need these to keep or
          dismiss lessons.
        </p>

        <h4 className="subsection-title">Memory health</h4>
        {errorCount ? (
          <dl className="health-list">
            {Object.entries(errors).map(([key, value]) => (
              <div key={key}>
                <dt>{titleCase(key)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="advanced-panel__ok">No memory errors reported.</p>
        )}

        {canRun ? (
          <div className="button-stack">
            <button
              className="secondary-button"
              type="button"
              disabled={nativeBusy === "brain_doctor:fleet"}
              onClick={() => onRunLocalAction({ action: "brain_doctor" })}
            >
              <BookOpen size={16} aria-hidden="true" />
              <span>{nativeBusy === "brain_doctor:fleet" ? "Checking" : "Run memory check"}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={nativeBusy === "redis_status:fleet"}
              onClick={() => onRunLocalAction({ action: "redis_status" })}
            >
              <MemoryStick size={16} aria-hidden="true" />
              <span>{nativeBusy === "redis_status:fleet" ? "Checking" : "Check Redis memory"}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={nativeBusy === "redis_sync_preview:fleet"}
              onClick={() => onRunLocalAction({ action: "redis_sync_preview" })}
            >
              <DatabaseZap size={16} aria-hidden="true" />
              <span>
                {nativeBusy === "redis_sync_preview:fleet" ? "Checking" : "Preview Redis sync"}
              </span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={nativeBusy === "memory_harvest:fleet"}
              onClick={() => onRunLocalAction({ action: "memory_harvest", refreshAfter: true })}
            >
              <Repeat size={16} aria-hidden="true" />
              <span>
                {nativeBusy === "memory_harvest:fleet" ? "Harvesting" : "Queue failure lessons"}
              </span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={nativeBusy === "memory_auto_promote:fleet"}
              onClick={() =>
                onRunLocalAction({ action: "memory_auto_promote", refreshAfter: true })
              }
            >
              <Sparkles size={16} aria-hidden="true" />
              <span>
                {nativeBusy === "memory_auto_promote:fleet"
                  ? "Judging"
                  : "Save judged lessons"}
              </span>
            </button>
          </div>
        ) : (
          <p className="console-note console-note--inline">
            <AlertTriangle size={15} aria-hidden="true" />
            <span>
              These tools run inside the desktop app. The browser preview stays read-only.
            </span>
          </p>
        )}
      </div>
    </details>
  );
}

// Agent codenames are an internal roster. Title-case them so they read as a
// name ("Lucius") rather than a slug, and fall back to "Alfred" when missing.
function prettyAgent(codename: string): string {
  const clean = (codename || "").trim();
  if (!clean) return "Alfred";
  return titleCase(clean);
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
