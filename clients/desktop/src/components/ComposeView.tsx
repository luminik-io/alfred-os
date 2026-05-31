import { AlertTriangle, CheckCircle2, ListChecks, Send, Sparkles } from "lucide-react";
import { useCallback, useState } from "react";

import { composeDraft } from "../api";
import { localUrl } from "../lib/links";
import type { ComposeDraftResponse } from "../types";
import { EmptyState, ExternalButton, PanelHeader } from "./atoms";

const PLACEHOLDER = `Describe the work in plain language. For example:

title: Add CSV export to the attendees table
problem: Sales reps need to pull attendee lists into their own CRM but the app only shows them on screen.
desired: A download button exports the current filtered table as CSV.
repo: your-org/frontend
acceptance: Clicking export downloads a CSV of the visible rows.
test: Vitest covers the export helper; manually verify the download in the app.`;

export function ComposeView({ baseUrl }: { baseUrl: string }) {
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ComposeDraftResponse | null>(null);

  const submit = useCallback(async () => {
    const text = intent.trim();
    if (!text || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await composeDraft(baseUrl, {
        text,
        draft_id: result?.draft_id,
      });
      setResult(next);
      setIntent("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, busy, intent, result?.draft_id]);

  const reset = useCallback(() => {
    setResult(null);
    setError(null);
    setIntent("");
  }, []);

  const iterating = result !== null;
  const score = result?.readiness.score ?? null;
  const tone =
    score === null ? "info" : result?.readiness.ok ? "ok" : score >= 60 ? "warn" : "error";

  return (
    <section className="dashboard-grid dashboard-grid--memory">
      <div className="panel panel--wide">
        <PanelHeader
          eyebrow="Compose"
          title={iterating ? "Refine this draft" : "Describe the work"}
        />
        <p className="panel-intro">
          Write what you want in plain language. Alfred&apos;s planning assistant scores how ready
          the work is to run, asks the clarifying questions that are still open, and saves a draft
          you can keep refining. Each submission refines the same draft.
        </p>
        <div className="compose-form">
          <label htmlFor="compose-intent">
            {iterating ? "Add detail or answer a question" : "What should Alfred build?"}
          </label>
          <textarea
            id="compose-intent"
            value={intent}
            placeholder={PLACEHOLDER}
            rows={iterating ? 5 : 12}
            spellCheck
            disabled={busy}
            onChange={(event) => setIntent(event.currentTarget.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                void submit();
              }
            }}
          />
          <div className="compose-form__actions">
            <button
              className="icon-button"
              type="button"
              disabled={busy || !intent.trim()}
              onClick={() => void submit()}
            >
              <Send size={16} aria-hidden="true" />
              <span>{busy ? "Drafting" : iterating ? "Refine draft" : "Draft it"}</span>
            </button>
            {iterating ? (
              <button className="secondary-button" type="button" disabled={busy} onClick={reset}>
                <Sparkles size={16} aria-hidden="true" />
                <span>Start new draft</span>
              </button>
            ) : null}
            <small className="compose-hint">Cmd/Ctrl + Enter to submit</small>
          </div>
        </div>

        {error ? (
          <div className="inline-notice inline-notice--error">
            <AlertTriangle size={18} aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        {result ? (
          <div className="compose-questions">
            <PanelHeader eyebrow="Clarify" title="Questions to resolve" />
            {result.questions.length ? (
              <ul className="compose-question-list">
                {result.questions.map((question, index) => (
                  <li key={`${index}-${question.slice(0, 24)}`}>
                    <span aria-hidden="true">?</span>
                    {question}
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                title="No open questions."
                body="The planning assistant did not flag anything blocking. Review the spec, then save or run it."
                compact
              />
            )}
          </div>
        ) : null}
      </div>

      <div className="panel">
        <PanelHeader eyebrow="Readiness" title="Draft status" />
        {result ? (
          <>
            <div className={`compose-score compose-score--${tone}`}>
              <strong>{score}</strong>
              <span>/ 100</span>
              <small>{result.readiness.ok ? "Ready to run" : "Needs scope"}</small>
            </div>
            <p className="compose-summary">{result.summary}</p>
            <dl className="compact-meta">
              <div>
                <dt>Title</dt>
                <dd>{result.title || "Untitled"}</dd>
              </div>
              <div>
                <dt>Revisions</dt>
                <dd>{result.revision_count}</dd>
              </div>
              {result.draft.repos.length ? (
                <div>
                  <dt>Repos</dt>
                  <dd>{result.draft.repos.join(", ")}</dd>
                </div>
              ) : null}
            </dl>
            {result.findings.length ? (
              <div className="compose-findings">
                <PanelHeader eyebrow="Findings" title="What still needs work" />
                <ul>
                  {result.findings.map((finding) => (
                    <li key={finding.code} className={`compose-finding compose-finding--${finding.severity}`}>
                      {finding.severity === "error" ? (
                        <AlertTriangle size={15} aria-hidden="true" />
                      ) : (
                        <CheckCircle2 size={15} aria-hidden="true" />
                      )}
                      <span>{finding.message}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="link-stack">
              <ExternalButton
                label="Open saved draft"
                href={localUrl(baseUrl, `/plans/${result.draft_id}`)}
                icon={<ListChecks size={16} />}
              />
            </div>
          </>
        ) : (
          <EmptyState
            title="No draft yet."
            body="Describe the work on the left and Alfred scores how ready it is, then saves it to your Plans inbox."
          />
        )}
      </div>
    </section>
  );
}
