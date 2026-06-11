import { Check, ChevronRight, ExternalLink, Loader2 } from "lucide-react";

import { openExternal } from "../lib/links";
import type { RequestThreadModel, RequestThreadStep } from "../lib/uiTypes";

/**
 * The request lifecycle thread: a horizontal stepper showing a single request's
 * journey Intake -> Plan (needs you) -> Queued -> Building -> Shipped. Each step
 * is lit or dimmed by which data the snapshot actually exposes; a step the
 * backend cannot yet confirm renders as "missing" rather than inventing state.
 *
 * Compose renders its result as one of these cards (replacing the dead-end
 * "Review in Plans" jump), and the Review board / Needs-you cards open the same
 * thread. Correlation across stages is best-effort (issue ref + draft_id); when
 * no stable cross-stage id exists the card says so via `correlationApproximate`.
 */
export function RequestThread({
  thread,
  onOpenPlan,
}: {
  thread: RequestThreadModel;
  /** Open the plan/spec sign-off (the one client-owned approval). */
  onOpenPlan?: (thread: RequestThreadModel) => void;
}) {
  const planStep = thread.steps.find((step) => step.key === "plan");
  const needsSignOff = planStep?.state === "active";
  const ref = thread.issueNumber ? `#${thread.issueNumber}` : null;
  const context = repoContext(thread.repos || (thread.repo ? [thread.repo] : []), ref);

  return (
    <article className="request-thread">
      <header className="request-thread__head">
        <div className="request-thread__title">
          <strong>{thread.title}</strong>
          {context ? (
            <span className="request-thread__repo" title={context.title}>
              {context.label}
            </span>
          ) : null}
        </div>
        <div className="request-thread__actions">
          {needsSignOff && onOpenPlan ? (
            <button
              className="secondary-button"
              type="button"
              onClick={() => onOpenPlan(thread)}
            >
              <span>Review plan</span>
              <ChevronRight size={15} aria-hidden="true" />
            </button>
          ) : null}
          {thread.url ? (
            <button
              className="ghost-button"
              type="button"
              onClick={() => void openExternal(thread.url as string)}
            >
              <ExternalLink size={15} aria-hidden="true" />
              <span>GitHub</span>
            </button>
          ) : null}
        </div>
      </header>

      <ol className="request-stepper" aria-label="Request lifecycle">
        {thread.steps.map((step, index) => (
          <Step key={step.key} step={step} isLast={index === thread.steps.length - 1} />
        ))}
      </ol>

      {thread.correlationApproximate ? (
        <p className="request-thread__note">
          Later steps stay dim until Alfred sees GitHub evidence for this
          request. No guessing, no fake progress.
        </p>
      ) : null}
    </article>
  );
}

function Step({ step, isLast }: { step: RequestThreadStep; isLast: boolean }) {
  return (
    <li className={`request-step request-step--${step.state}`}>
      <span className="request-step__marker" aria-hidden="true">
        {step.state === "done" ? (
          <Check size={12} />
        ) : step.state === "active" ? (
          <Loader2 size={12} className="spin" />
        ) : null}
      </span>
      <span className="request-step__label">
        {step.label}
        {step.state === "missing" ? (
          <span className="request-step__missing"> · waiting for evidence</span>
        ) : null}
      </span>
      {step.detail ? <span className="request-step__detail">{step.detail}</span> : null}
      {!isLast ? <span className="request-step__rail" aria-hidden="true" /> : null}
    </li>
  );
}

function shortRepo(repo: string): string {
  const slash = repo.lastIndexOf("/");
  return slash >= 0 ? repo.slice(slash + 1) : repo;
}

function repoContext(repos: string[], ref: string | null): { label: string; title: string } | null {
  const clean = Array.from(
    new Map(
      repos
        .map((repo) => repo.trim())
        .filter(Boolean)
        .map((repo) => [repo.toLowerCase(), repo] as const),
    ).values(),
  );
  if (clean.length === 0) return null;
  if (clean.length === 1) {
    return {
      label: `${shortRepo(clean[0])}${ref ? ` ${ref}` : ""}`,
      title: clean[0],
    };
  }
  return {
    label: `${clean.length} codebases in scope`,
    title: clean.join(", "),
  };
}
