import {
  AlertTriangle,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  Copy,
  ExternalLink,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  composeConverse,
  composeDraft,
  conversationControl,
  filePlanIssue,
  isLiveSessionUnavailable,
  streamComposeConverse,
  supportsNativeActions,
} from "../api";
import { repoShortName } from "../lib/chips";
import { isSafeExternalUrl, openExternal } from "../lib/links";
import type { TabKey } from "../lib/uiTypes";
import type {
  ComposeDraftFields,
  ComposeDraftResponse,
  ConversationControlResponse,
  ConverseMessage,
  ConverseResponse,
  FilePlanIssueResponse,
} from "../types";
import { LifecycleCard, type RepoChip } from "./LifecycleCard";

// Plain-language prompt for a non-developer. Compose always speaks plain: a
// person describes the outcome in their own words and Alfred asks only for what
// is missing, then produces the structured draft. There is no plain/technical
// toggle; every converse call sends plain.
const PLACEHOLDER = "Describe the outcome, who needs it, and any limits Alfred should respect.";

const STARTERS = [
  {
    label: "Ship a feature",
    text: "Add a CSV export to the attendees table so sales can download the filtered rows they see.",
  },
  {
    label: "Fix a workflow",
    text: "The review queue is hard to scan at small window sizes. Make it usable without hiding important decisions.",
  },
  {
    label: "Polish a screen",
    text: "Improve the setup screen so a non-developer can connect Alfred and understand what is ready.",
  },
];

// The composer textarea grows with content up to this many rows, then scrolls.
const MAX_COMPOSER_ROWS = 8;

type MessageTurn = ConverseMessage & { pending?: boolean; kind?: "message" };

type ChatTurn =
  | MessageTurn
  // A draft turn renders the inline lifecycle card with the one-gate File issue
  // action. It is produced when an assistant turn returns a saved draft.
  | { role: "assistant"; kind: "draft"; draft: DraftCardModel };

type DraftCardModel = {
  draftId: string;
  title: string;
  repos: string[];
  ready: boolean;
  questions: string[];
};

function cleanRepos(repos: string[] | undefined): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const repo of repos || []) {
    const clean = repo.trim();
    const key = clean.toLowerCase();
    if (clean && !seen.has(key)) {
      seen.add(key);
      result.push(clean);
    }
  }
  return result;
}

function contextReposForPlanning(
  result: { draft?: Pick<ComposeDraftFields, "repos"> } | null,
  selectedRepos: string[],
): string[] {
  const draftRepos = cleanRepos(result?.draft?.repos);
  return draftRepos.length ? draftRepos : cleanRepos(selectedRepos);
}

function scopeReposForPlanning(
  result: { draft?: Pick<ComposeDraftFields, "repos"> } | null,
  selectedRepos: string[],
): string[] {
  const draftRepos = cleanRepos(result?.draft?.repos);
  if (draftRepos.length) return draftRepos;
  const selected = cleanRepos(selectedRepos);
  return selected.length === 1 ? selected : [];
}

function draftWithRepoContext(
  result: { draft?: ComposeDraftFields } | null,
  selectedRepos: string[],
): Partial<ComposeDraftFields> | undefined {
  const repos = scopeReposForPlanning(result, selectedRepos);
  if (result?.draft) {
    return { ...result.draft, repos };
  }
  return repos.length ? { repos } : undefined;
}

function repoChipsFor(repos: string[]): RepoChip[] {
  return cleanRepos(repos).map((repo) => ({ short: repoShortName(repo), full: repo }));
}

function draftCardFrom(result: ConverseResponse | ComposeDraftResponse): DraftCardModel {
  // ConverseResponse uses readiness.ready; ComposeDraftResponse uses readiness.ok
  // and questions[]. Normalize both into the inline card model.
  const ready =
    "ready" in result.readiness ? result.readiness.ready : result.readiness.ok;
  const questions =
    "missing" in result.readiness
      ? result.readiness.missing
      : (result as ComposeDraftResponse).questions || [];
  return {
    draftId: result.draft_id,
    title: result.draft.title || ("title" in result ? (result as ComposeDraftResponse).title : "New request"),
    repos: cleanRepos(result.draft.repos),
    ready: Boolean(ready),
    questions: questions || [],
  };
}

// ------------------------------------------------------------------------- //
// One chat surface. Whether a live engine is configured or not, the operator
// sees a single ChatGPT-grade conversation: a full-height thread, a markdown
// reply rendered incrementally, and a bottom composer where Enter sends. When a
// turn produces a ready draft it appears inline as a lifecycle card with the one
// File issue action (one-gate: file directly, no separate approval step). Off
// Tauri (browser preview) or when the server has no live engine, the same turn
// routes through the reliable draft endpoint, so the surface never breaks.
// ------------------------------------------------------------------------- //

export function ComposeView({
  baseUrl,
  selectedRepos = [],
  onSwitch,
}: {
  baseUrl: string;
  selectedRepos?: string[];
  onSwitch: (tab: TabKey) => void;
}) {
  // Alfred Compose always speaks plain language: the person describes the
  // outcome in their own words and Alfred asks only for what is missing. There
  // is no technical/plain toggle; every /api/compose/converse call sends plain.

  // Whether a live engine is reachable. Starts true on native, false in the
  // browser; flips to false the first time the server reports no engine so we
  // stop attempting the streaming path.
  const [hasEngine, setHasEngine] = useState(supportsNativeActions());

  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The most recent saved draft, carried across turns so the same request is
  // refined (draft_id) and the inline card can file it.
  const [result, setResult] = useState<ConverseResponse | ComposeDraftResponse | null>(null);
  const [fileBusy, setFileBusy] = useState(false);
  const [fileNotice, setFileNotice] = useState<{ tone: "ok" | "error"; message: string; url?: string } | null>(null);

  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // Tracks the in-flight run so reset() and unmount can cancel it; a late
  // resolve must never resurrect a cleared transcript.
  const abortRef = useRef<AbortController | null>(null);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    if (turns.length === 0) return;
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  // Cancel any in-flight stream when the view unmounts.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // Autogrow the composer up to MAX_COMPOSER_ROWS, then scroll. useLayoutEffect
  // so the height is measured before paint and there is no flash.
  useLayoutEffect(() => {
    const el = composerRef.current;
    if (!el) return;
    el.style.height = "auto";
    const styles = window.getComputedStyle(el);
    const lineHeight = parseFloat(styles.lineHeight) || 20;
    const padding =
      parseFloat(styles.paddingTop || "0") + parseFloat(styles.paddingBottom || "0");
    const maxHeight = lineHeight * MAX_COMPOSER_ROWS + padding;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [input]);

  const commitDraftResult = useCallback(
    (
      reply: ConverseResponse | ComposeDraftResponse,
      baseTurns: ChatTurn[],
      message: string,
    ) => {
      setResult(reply);
      setFileNotice(null);
      const next: ChatTurn[] = [...baseTurns];
      if (message.trim()) {
        next.push({ role: "assistant", content: message, kind: "message" });
      }
      next.push({ role: "assistant", kind: "draft", draft: draftCardFrom(reply) });
      setTurns(next);
    },
    [],
  );

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setFileNotice(null);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const isCurrent = () => abortRef.current === controller && !controller.signal.aborted;
    const finishIfCurrent = () => {
      if (abortRef.current === controller) setBusy(false);
    };

    // Only message turns go on the wire; draft cards are UI-only.
    const priorMessages: ChatTurn[] = turns;
    const nextTurns: ChatTurn[] = [...turns, { role: "user", content: text, kind: "message" }];
    setTurns([...nextTurns, { role: "assistant", content: "", pending: true, kind: "message" }]);
    setInput("");

    const wire: ConverseMessage[] = nextTurns
      .filter((turn): turn is ConverseMessage & { kind?: "message" } => turn.kind !== "draft")
      .map((turn) => ({ role: turn.role, content: turn.content }));

    const converseRequest = {
      messages: wire,
      draft_id: result?.draft_id,
      repos: contextReposForPlanning(result, selectedRepos),
      context_repos: contextReposForPlanning(result, selectedRepos),
      plain: true,
    };

    // Control commands (status, run, etc.) short-circuit before any planning.
    try {
      const control = await conversationControl(baseUrl, { text }, controller.signal);
      if (!isCurrent()) return;
      if (control.handled) {
        setTurns([...nextTurns, { role: "assistant", content: controlReply(control), kind: "message" }]);
        finishIfCurrent();
        return;
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      setTurns(priorMessages);
      setInput(text);
      setError(err instanceof Error ? err.message : String(err));
      finishIfCurrent();
      return;
    }

    const runDraftFallback = async (): Promise<boolean> => {
      // The reliable, always-available path: save a draft and render it inline.
      try {
        const draft = await composeDraft(baseUrl, {
          text,
          draft_id: result?.draft_id,
          draft: draftWithRepoContext(result, selectedRepos),
          context_repos: contextReposForPlanning(result, selectedRepos),
        });
        if (!isCurrent()) return true;
        setHasEngine(false);
        commitDraftResult(draft, nextTurns, draft.summary || "I saved a plan. Review it below, then file the issue when it is ready.");
        return true;
      } catch (draftErr) {
        if (controller.signal.aborted) return true;
        setTurns(priorMessages);
        setInput(text);
        setError(draftErr instanceof Error ? draftErr.message : String(draftErr));
        return true;
      }
    };

    // No live engine (browser preview, or server reports none): go straight to
    // the reliable draft endpoint, no streaming attempt.
    if (!hasEngine) {
      await runDraftFallback().finally(() => {
        finishIfCurrent();
      });
      return;
    }

    const renderToken = (fragment: string) => {
      if (!isCurrent()) return;
      setTurns((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.kind !== "draft" && last.role === "assistant" && last.pending) {
          next[next.length - 1] = { ...last, content: last.content + fragment };
        }
        return next;
      });
    };

    try {
      let reply: ConverseResponse;
      try {
        reply = await streamComposeConverse(baseUrl, converseRequest, renderToken, controller.signal);
      } catch (streamErr) {
        if (controller.signal.aborted) return;
        if (isLiveSessionUnavailable(streamErr)) throw streamErr;
        // A transport failure (not a missing engine): retry buffered.
        renderToken("");
        if (isCurrent()) {
          setTurns([...nextTurns, { role: "assistant", content: "", pending: true, kind: "message" }]);
        }
        reply = await composeConverse(baseUrl, converseRequest, controller.signal);
      }
      if (!isCurrent()) return;
      commitDraftResult(reply, nextTurns, reply.reply);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (isLiveSessionUnavailable(err)) {
        await runDraftFallback();
        return;
      }
      setTurns(priorMessages);
      setInput(text);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      finishIfCurrent();
    }
  }, [baseUrl, busy, commitDraftResult, hasEngine, input, result, selectedRepos, turns]);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTurns([]);
    setResult(null);
    setError(null);
    setInput("");
    setBusy(false);
    setFileNotice(null);
    setFileBusy(false);
  }, []);

  // One-gate: file the issue straight from the inline card. The server still
  // enforces readiness and repo allowlisting; there is no client approval step.
  const fileIssue = useCallback(
    async (draftId: string) => {
      if (fileBusy) return;
      setFileBusy(true);
      setFileNotice(null);
      try {
        const res: FilePlanIssueResponse = await filePlanIssue(baseUrl, draftId);
        setFileNotice({
          tone: "ok",
          message:
            res.status === "already_filed"
              ? "Already filed."
              : `Filed with ${res.label || "agent:implement"}.`,
          url: res.issue_url,
        });
      } catch (err) {
        setFileNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setFileBusy(false);
      }
    },
    [baseUrl, fileBusy],
  );

  const started = turns.length > 0;

  // One composer, two layouts. Empty: a single centered hero (one headline, the
  // composer as the focal point, starter chips below) so the screen is not three
  // copies of the same prompt. Started: a compact header, a full-height thread,
  // the composer pinned at the bottom.
  const errorNotice = error ? (
    <div className="ask__error inline-notice inline-notice--error" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <span>{error}</span>
    </div>
  ) : null;

  const composer = (
    <div className="ask__composer-wrap">
      <form
        className="ask__composer"
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy && input.trim()) void send();
        }}
      >
        <label htmlFor="ask-input" className="visually-hidden">
          Your message to Alfred
        </label>
        <textarea
          id="ask-input"
          ref={composerRef}
          className="ask__input"
          value={input}
          placeholder={started ? "Reply to Alfred, or add detail." : PLACEHOLDER}
          rows={1}
          spellCheck
          disabled={busy}
          onChange={(event) => setInput(event.currentTarget.value)}
          onKeyDown={(event) => {
            // ChatGPT-style: Enter sends, Shift+Enter inserts a newline.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (!busy && input.trim()) void send();
            }
          }}
        />
        <button
          className="ask__send"
          type="submit"
          disabled={busy || !input.trim()}
          aria-label={busy ? "Alfred is thinking" : "Send message"}
          title={busy ? "Alfred is thinking" : "Send"}
        >
          <ArrowUp size={18} aria-hidden="true" />
        </button>
      </form>
      <small className="ask__hint">
        Enter to send, Shift + Enter for a new line. Alfred saves the plan as the chat gets clearer.
      </small>
    </div>
  );

  const starters = (
    <div className="ask__starters" aria-label="Starter prompts">
      {STARTERS.map((starter) => (
        <button
          key={starter.label}
          type="button"
          className="ask__starter"
          disabled={busy}
          onClick={() => {
            setInput(starter.text);
            composerRef.current?.focus();
          }}
        >
          <span>{starter.label}</span>
          <ArrowRight size={13} aria-hidden="true" />
        </button>
      ))}
    </div>
  );

  return (
    <section className={`ask${started ? "" : " ask--empty"}`} aria-label="Ask Alfred">
      {started ? (
        <header className="ask__head">
          <span className="ask__eyebrow">Ask Alfred</span>
          <div className="ask__head-controls">
            <button className="ghost-button ask__reset" type="button" disabled={busy} onClick={reset}>
              <Sparkles size={14} aria-hidden="true" />
              <span>New chat</span>
            </button>
          </div>
        </header>
      ) : null}

      {started ? (
        <div className="ask__thread" ref={transcriptRef} role="log" aria-label="Conversation with Alfred">
          <div className="ask__turns">
            {turns.map((turn, index) =>
              turn.kind === "draft" ? (
                <DraftCard
                  key={`draft-${index}-${turn.draft.draftId}`}
                  draft={turn.draft}
                  busy={fileBusy}
                  notice={index === turns.length - 1 ? fileNotice : null}
                  onFile={() => void fileIssue(turn.draft.draftId)}
                  onOpenWork={() => onSwitch("pipeline")}
                />
              ) : (
                <ChatBubble key={`${index}-${turn.role}`} turn={turn} />
              ),
            )}
          </div>
        </div>
      ) : (
        <div className="ask__hero">
          <h1 className="ask__hero-title">What should Alfred do?</h1>
          <p className="ask__hero-sub">
            Say the outcome in your own words. Alfred asks only what is missing, then saves a plan you can file as a GitHub issue.
          </p>
          {errorNotice}
          {composer}
          {starters}
        </div>
      )}

      {started ? (
        <>
          {errorNotice}
          {composer}
        </>
      ) : null}
    </section>
  );
}

function controlReply(result: ConversationControlResponse): string {
  const body = (result.text || result.detail || "").trim();
  if (body) return body;
  return `Alfred handled ${result.action || "the request"}.`;
}

// The inline lifecycle card rendered when a turn produces a saved draft. One
// primary action: File issue (one-gate). When the draft is not yet ready the
// card stays informational and lists the open questions to answer in chat.
function DraftCard({
  draft,
  busy,
  notice,
  onFile,
  onOpenWork,
}: {
  draft: DraftCardModel;
  busy: boolean;
  notice: { tone: "ok" | "error"; message: string; url?: string } | null;
  onFile: () => void;
  onOpenWork: () => void;
}) {
  const filed = notice?.tone === "ok";
  return (
    <div className="ask-draft" aria-label="Saved plan">
      <LifecycleCard
        chip={
          filed
            ? { label: "Filed", tone: "ok" }
            : draft.ready
              ? { label: "Ready to file", tone: "ok" }
              : { label: "Needs detail", tone: "attention" }
        }
        repos={repoChipsFor(draft.repos)}
        outcome={draft.title}
        attribution={<span>Saved as a plan</span>}
        action={
          filed ? (
            notice?.url ? (
              <button className="secondary-button" type="button" onClick={() => void openExternal(notice.url as string)}>
                <ExternalLink size={15} aria-hidden="true" />
                <span>View issue</span>
              </button>
            ) : (
              <button className="secondary-button" type="button" onClick={onOpenWork}>
                <span>Open Work</span>
              </button>
            )
          ) : (
            <button
              className="icon-button ask-draft__file"
              type="button"
              disabled={busy || !draft.ready}
              onClick={onFile}
              title={draft.ready ? "File this as a GitHub issue" : "Answer the open questions first"}
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              <span>{busy ? "Filing..." : "File issue"}</span>
            </button>
          )
        }
        ariaLabel={`Plan: ${draft.title}`}
      />
      {!draft.ready && draft.questions.length ? (
        <ul className="ask-draft__questions">
          {draft.questions.slice(0, 4).map((question, index) => (
            <li key={`${index}-${question.slice(0, 24)}`}>{question}</li>
          ))}
        </ul>
      ) : null}
      {notice ? (
        <p className={`ask-draft__notice ask-draft__notice--${notice.tone}`} role="status">
          {notice.message}
        </p>
      ) : null}
    </div>
  );
}

function ChatBubble({ turn }: { turn: MessageTurn }) {
  const who = turn.role === "user" ? "You" : "Alfred";
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(turn.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    });
  };
  return (
    <div className={`ask-bubble ask-bubble--${turn.role}`}>
      <div className="ask-bubble__head">
        <span className="ask-bubble__who">{who}</span>
        {!turn.pending && turn.content ? (
          <button
            type="button"
            className="ask-bubble__copy"
            onClick={copy}
            aria-label={copied ? "Copied" : "Copy message"}
            title={copied ? "Copied" : "Copy"}
          >
            {copied ? <CheckCircle2 size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          </button>
        ) : null}
      </div>
      {turn.pending ? (
        <span className="ask-bubble__pending" aria-label="Alfred is thinking">
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
        </span>
      ) : turn.role === "assistant" ? (
        // Render Alfred's replies as markdown so headings, lists, tables and
        // fenced code render like a real chat surface. User turns stay plain
        // text (people type prose, and it avoids rendering injected markup).
        <div className="ask-bubble__md">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ href, children }) => (
                <SafeMarkdownLink href={href}>{children}</SafeMarkdownLink>
              ),
            }}
          >
            {turn.content}
          </ReactMarkdown>
        </div>
      ) : (
        <p className="ask-bubble__text">{turn.content}</p>
      )}
    </div>
  );
}

function SafeMarkdownLink({
  href,
  children,
}: {
  href?: string;
  children: ReactNode;
}) {
  if (!href || !isSafeExternalUrl(href)) {
    return <span className="ask-bubble__unsafe-link">{children}</span>;
  }
  return (
    <button
      className="ask-bubble__link"
      type="button"
      onClick={() => void openExternal(href)}
    >
      <span>{children}</span>
      <ExternalLink size={13} aria-hidden="true" />
    </button>
  );
}
