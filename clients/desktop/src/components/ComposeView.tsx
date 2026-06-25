import {
  AlertTriangle,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  ExternalLink,
  RotateCcw,
  Sparkles,
  Square,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

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
import {
  clearConversation,
  loadConversation,
  saveConversation,
  type PersistedTurn,
} from "../lib/chatHistory";
import { openExternal } from "../lib/links";
import type { TabKey } from "../lib/uiTypes";
import type {
  ComposeDraftFields,
  ComposeDraftResponse,
  ConversationControlResponse,
  ConverseMessage,
  ConverseResponse,
  FilePlanIssueResponse,
} from "../types";
import { ChatMessage } from "./ChatMessage";
import { LifecycleCard, type RepoChip } from "./LifecycleCard";

// Plain-language prompt for a non-developer. Compose always speaks plain: a
// person describes the outcome in their own words and Alfred asks only for what
// is missing, then produces the structured draft. There is no plain/technical
// toggle; every converse call sends plain.
const PLACEHOLDER = "Ask a question, or describe a change you want made.";

const STARTERS = [
  {
    label: "How does Alfred work?",
    text: "How does Alfred work, and what can you do for me?",
  },
  {
    label: "Ship a feature",
    text: "Add a CSV export to the attendees table so sales can download the filtered rows they see.",
  },
  {
    label: "Fix a workflow",
    text: "The review queue is hard to scan at small window sizes. Make it usable without hiding important decisions.",
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

// A converse turn is conversational (just a chat answer) unless the server
// marks it "build". Servers that predate intent omit the field; we treat those
// as build so the plan surface never silently disappears. ComposeDraftResponse
// (the no-engine one-shot fallback) is always a plan, so it has no intent.
function isConversationTurn(
  result: ConverseResponse | ComposeDraftResponse,
): boolean {
  return "intent" in result && result.intent === "conversation";
}

// Whether a draft carries enough to be worth showing as a plan card. A bare
// title with nothing else is not a plan yet; we only surface the card once the
// turn has actually started building something.
function draftHasSubstance(draft: ComposeDraftFields | undefined): boolean {
  if (!draft) return false;
  const scalars = [
    draft.problem,
    draft.desired_behavior,
    draft.current_behavior,
    draft.user,
    draft.test_plan,
  ];
  if (scalars.some((value) => Boolean(value && value.trim()))) return true;
  if (cleanRepos(draft.repos).length) return true;
  if ((draft.acceptance_criteria || []).some((value) => Boolean(value && value.trim())))
    return true;
  return false;
}

// The reply for the no-engine fallback path. There is no live model to ask the
// open questions in prose, so fold them into the message (Markdown list) under
// the saved-plan summary, keeping the inline card a clean offer.
function draftFallbackReply(draft: ComposeDraftResponse): string {
  const summary =
    (draft.summary || "").trim() ||
    "I saved a plan. Review it below, then file it as an issue when you are ready.";
  const questions = (draft.questions || []).filter((q) => Boolean(q && q.trim())).slice(0, 4);
  if (!questions.length) return summary;
  const list = questions.map((q) => `- ${q.trim()}`).join("\n");
  return `${summary}\n\nTo firm it up:\n\n${list}`;
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

  // Rehydrate the most recent conversation so a closed window picks back up
  // where it left off. Best-effort: a missing or malformed store reads empty.
  const restored = useMemo(() => restoreFromStorage(), []);

  const [turns, setTurns] = useState<ChatTurn[]>(restored.turns);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The most recent saved draft, carried across turns so the same request is
  // refined (draft_id) and the inline card can file it.
  const [result, setResult] = useState<ConverseResponse | ComposeDraftResponse | null>(
    restored.result,
  );
  const [fileBusy, setFileBusy] = useState(false);
  const [fileNotice, setFileNotice] = useState<{ tone: "ok" | "error"; message: string; url?: string } | null>(null);
  // The last user message, kept so retry/regenerate can replay the turn after a
  // stop or a dropped stream without the person retyping.
  const lastUserTextRef = useRef<string>(lastUserTextOf(restored.turns));

  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // Whether the user is pinned to the bottom of the transcript. Starts true so
  // the first turns show the latest; flips on scroll (see onScroll below).
  const pinnedRef = useRef(true);
  // Tracks the in-flight run so reset() and unmount can cancel it; a late
  // resolve must never resurrect a cleared transcript.
  const abortRef = useRef<AbortController | null>(null);

  // Keep the newest turn in view as the conversation grows, but only when the
  // user is already pinned to the bottom. Otherwise a new (or streaming) turn
  // would yank them away from earlier content they had scrolled up to read.
  useEffect(() => {
    if (turns.length === 0) return;
    const el = transcriptRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  // Persist the conversation whenever a turn settles, so it survives a reload or
  // restart. We never persist an in-flight (pending/streaming) turn: while busy
  // the transcript is mid-stream and a half-written reply is not worth keeping.
  useEffect(() => {
    if (busy) return;
    if (turns.length === 0) {
      clearConversation();
      return;
    }
    saveConversation({
      draftId: result?.draft_id,
      draft: result?.draft,
      turns: turns.map(toPersistedTurn),
    });
  }, [turns, busy, result]);

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

  // Commit one assistant turn. A conversation turn is just a chat reply: no
  // plan card, and the live plan (result) is left untouched so a "thanks" mid
  // build does not wipe the spec. A build turn renders the reply, then the
  // inline plan card once the draft has real substance, and becomes the live
  // plan the composer keeps refining.
  const commitTurn = useCallback(
    (
      reply: ConverseResponse | ComposeDraftResponse,
      baseTurns: ChatTurn[],
      message: string,
    ) => {
      const next: ChatTurn[] = [...baseTurns];
      if (message.trim()) {
        next.push({ role: "assistant", content: message, kind: "message" });
      }
      if (isConversationTurn(reply)) {
        // A plain conversational reply (e.g. "thanks") must not wipe a "Filed"
        // confirmation the person is still reading: leave the file notice as-is.
        setTurns(next);
        return;
      }
      // A real build turn supersedes any prior file result, so clear the notice.
      setFileNotice(null);
      setResult(reply);
      if (draftHasSubstance(reply.draft)) {
        next.push({ role: "assistant", kind: "draft", draft: draftCardFrom(reply) });
      }
      setTurns(next);
    },
    [],
  );

  // Run one conversational turn for `text`, appended after `baseTurns`. Factored
  // out of send() so retry/regenerate can replay the same turn against a trimmed
  // transcript without the person retyping.
  const runTurn = useCallback(
    async (text: string, baseTurns: ChatTurn[]) => {
      if (!text || busy) return;
      setBusy(true);
      setError(null);
      lastUserTextRef.current = text;
      // The file notice is cleared per-turn inside commitTurn, but only for a real
      // build turn: a conversational follow-up after filing keeps the "Filed"
      // confirmation visible until the person actually plans something new.

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const isCurrent = () => abortRef.current === controller && !controller.signal.aborted;
      const finishIfCurrent = () => {
        if (abortRef.current === controller) setBusy(false);
      };

      // Only message turns go on the wire; draft cards are UI-only.
      const priorMessages: ChatTurn[] = baseTurns;
      const nextTurns: ChatTurn[] = [...baseTurns, { role: "user", content: text, kind: "message" }];
      setTurns([...nextTurns, { role: "assistant", content: "", pending: true, kind: "message" }]);

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
        // The no-engine path has no live model to weave questions into prose,
        // so fold any open questions into the reply itself (the inline card is
        // an offer, not a form, and no longer lists them).
        commitTurn(draft, nextTurns, draftFallbackReply(draft));
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
      commitTurn(reply, nextTurns, reply.reply);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (isLiveSessionUnavailable(err)) {
        await runDraftFallback();
        return;
      }
      // A dropped stream or transport error: keep the person's message and the
      // turn intact, surface a plain, recoverable notice, and offer Retry. The
      // retry control replays this exact turn against the unchanged transcript.
      setTurns(nextTurns);
      lastUserTextRef.current = text;
      setError(reconnectMessage(err));
    } finally {
      finishIfCurrent();
    }
    },
    [baseUrl, busy, commitTurn, hasEngine, result, selectedRepos],
  );

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    void runTurn(text, turns);
  }, [busy, input, runTurn, turns]);

  // Stop the in-flight generation. The partial assistant reply stays on screen
  // (finalized, no longer pending) so the person keeps whatever streamed in.
  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    setTurns((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.kind !== "draft" && last.role === "assistant" && last.pending) {
        if (last.content.trim()) {
          next[next.length - 1] = { ...last, pending: false };
        } else {
          // Nothing streamed yet: drop the empty placeholder turn entirely.
          next.pop();
        }
      }
      return next;
    });
  }, []);

  // Retry / regenerate the most recent assistant turn. Drops the trailing
  // assistant turn(s) for the last user message and replays that message, so a
  // dropped stream or an unsatisfying answer can be re-run with one click.
  const retry = useCallback(() => {
    if (busy) return;
    const { text, baseTurns } = trimToLastUser(turns, lastUserTextRef.current);
    if (!text) return;
    void runTurn(text, baseTurns);
  }, [busy, runTurn, turns]);

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
    lastUserTextRef.current = "";
    clearConversation();
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
  // The index of the most recent draft card: the file notice rides this card so
  // it survives conversational turns that land after the plan was filed.
  const lastDraftIndex = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      if (turns[i].kind === "draft") return i;
    }
    return -1;
  }, [turns]);

  // The index of the most recent settled assistant message turn. Only that turn
  // shows the regenerate control, keeping every earlier reply's bar to copy.
  const lastAssistantIndex = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const turn = turns[i];
      if (turn.kind !== "draft" && turn.role === "assistant" && !turn.pending) return i;
    }
    return -1;
  }, [turns]);

  // One composer, two layouts. Empty: a single centered hero (one headline, the
  // composer as the focal point, starter chips below) so the screen is not three
  // copies of the same prompt. Started: a compact header, a full-height thread,
  // the composer pinned at the bottom.
  // A failed turn keeps the person's message on screen and offers Retry, so a
  // dropped stream is one click from recovery rather than a retype.
  const canRetry = !busy && lastUserTextRef.current.length > 0 && turns.length > 0;
  const errorNotice = error ? (
    <div className="ask__error inline-notice inline-notice--error" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <span>{error}</span>
      {canRetry ? (
        <button type="button" className="ask__retry" onClick={retry}>
          <RotateCcw size={13} aria-hidden="true" />
          <span>Retry</span>
        </button>
      ) : null}
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
          onChange={(event) => setInput(event.currentTarget.value)}
          onKeyDown={(event) => {
            // ChatGPT-style: Enter sends, Shift+Enter inserts a newline.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (!busy && input.trim()) send();
            }
          }}
        />
        {busy ? (
          // While a turn streams, the primary control stops generation. The
          // partial reply is kept on screen when stopped.
          <button
            className="ask__send ask__send--stop"
            type="button"
            onClick={stop}
            aria-label="Stop generating"
            title="Stop generating"
          >
            <Square size={16} aria-hidden="true" />
          </button>
        ) : (
          <button
            className="ask__send"
            type="submit"
            disabled={!input.trim()}
            aria-label="Send message"
            title="Send"
          >
            <ArrowUp size={18} aria-hidden="true" />
          </button>
        )}
      </form>
      <small className="ask__hint">
        Enter to send, Shift + Enter for a new line. When you are planning work, Alfred shapes a plan as the chat gets clearer.
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
        <div
          className="ask__thread"
          ref={transcriptRef}
          role="log"
          aria-label="Conversation with Alfred"
          // Politely announce the assistant's reply to assistive tech as it
          // streams in, without interrupting the person mid-sentence.
          aria-live="polite"
          aria-relevant="additions text"
          aria-busy={busy}
          onScroll={(event) => {
            const el = event.currentTarget;
            pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          <div className="ask__turns">
            {turns.map((turn, index) =>
              turn.kind === "draft" ? (
                <DraftCard
                  key={`draft-${index}-${turn.draft.draftId}`}
                  draft={turn.draft}
                  busy={fileBusy}
                  // Attach the notice to the most recent draft card, not the
                  // last turn overall, so a filed confirmation stays visible
                  // when conversational turns ("thanks") follow the plan.
                  notice={index === lastDraftIndex ? fileNotice : null}
                  onFile={() => void fileIssue(turn.draft.draftId)}
                  onOpenWork={() => onSwitch("pipeline")}
                />
              ) : (
                <ChatMessage
                  key={`${index}-${turn.role}`}
                  role={turn.role}
                  content={turn.content}
                  streaming={Boolean(turn.pending)}
                  canRetry={!busy && index === lastAssistantIndex}
                  onRetry={retry}
                />
              ),
            )}
          </div>
        </div>
      ) : (
        <div className="ask__hero">
          <h1 className="ask__hero-title">Ask Alfred anything</h1>
          <p className="ask__hero-sub">
            Ask a question, or describe a change you want made. Alfred answers, and when you are planning work it shapes a plan you can file as a GitHub issue.
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
  // The card is an OFFER attached to a build turn, not a form. The chat reply
  // already carries Alfred's questions, so the card stays quiet until the plan
  // is ready to file: a neutral "Draft plan" while it firms up, a confident
  // "Ready to file" once it is. File issue is always available (the server is
  // the real gate); when the plan is still firming up it is a quiet secondary.
  return (
    <div className="ask-draft" aria-label="Plan Alfred is shaping">
      <LifecycleCard
        chip={
          filed
            ? { label: "Filed", tone: "ok" }
            : draft.ready
              ? { label: "Ready to file", tone: "ok" }
              : { label: "Draft plan", tone: "idle" }
        }
        repos={repoChipsFor(draft.repos)}
        outcome={draft.title}
        attribution={
          <span>{draft.ready ? "Ready when you are" : "Keep chatting to firm it up"}</span>
        }
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
              className={draft.ready ? "icon-button ask-draft__file" : "secondary-button ask-draft__file"}
              type="button"
              disabled={busy}
              onClick={onFile}
              title={
                draft.ready
                  ? "File this as a GitHub issue"
                  : "File it now, or keep chatting to add detail first"
              }
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              <span>{busy ? "Filing..." : draft.ready ? "File issue" : "File as an issue"}</span>
            </button>
          )
        }
        ariaLabel={`Plan: ${draft.title}`}
      />
      {notice ? (
        <p className={`ask-draft__notice ask-draft__notice--${notice.tone}`} role="status">
          {notice.message}
        </p>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Persistence + retry helpers. Pure functions so they are easy to reason about
// and unit-test: they translate between the in-view ChatTurn shape and the
// plain persisted form, and locate the last user turn for retry/regenerate.
// --------------------------------------------------------------------------- //

function toPersistedTurn(turn: ChatTurn): PersistedTurn {
  if (turn.kind === "draft") {
    return { kind: "draft", role: "assistant", draft: turn.draft };
  }
  return { kind: "message", role: turn.role, content: turn.content };
}

function fromPersistedTurn(turn: PersistedTurn): ChatTurn {
  if (turn.kind === "draft") {
    return { role: "assistant", kind: "draft", draft: turn.draft };
  }
  return { role: turn.role, content: turn.content, kind: "message" };
}

// Read the persisted conversation into view state. A pending/streaming turn is
// never persisted, so nothing here is in flight.
function restoreFromStorage(): {
  turns: ChatTurn[];
  result: ConverseResponse | ComposeDraftResponse | null;
} {
  const saved = loadConversation();
  if (!saved) return { turns: [], result: null };
  const turns = saved.turns.map(fromPersistedTurn);
  // Rebuild just enough of the live result to keep refining the same draft
  // (the composer reads result.draft_id and result.draft).
  const result =
    saved.draftId && saved.draft
      ? ({
          draft_id: saved.draftId,
          draft: saved.draft,
        } as unknown as ConverseResponse)
      : null;
  return { turns, result };
}

function lastUserTextOf(turns: ChatTurn[]): string {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    if (turn.kind !== "draft" && turn.role === "user") return turn.content;
  }
  return "";
}

// Trim the transcript back to (but not including) the most recent user turn,
// returning that user's text plus the turns that preceded it. Replaying that
// text reruns the turn, so retry/regenerate drops the stale assistant reply (and
// any draft card it produced) and asks again.
function trimToLastUser(
  turns: ChatTurn[],
  fallback: string,
): { text: string; baseTurns: ChatTurn[] } {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    if (turn.kind !== "draft" && turn.role === "user") {
      return { text: turn.content, baseTurns: turns.slice(0, i) };
    }
  }
  return { text: fallback, baseTurns: [] };
}

// A plain, recoverable message for a dropped or failed stream. Keeps the cause
// out of the person's way while making clear the turn can be retried.
function reconnectMessage(err: unknown): string {
  const detail = err instanceof Error ? err.message : "";
  if (/abort/i.test(detail)) {
    return "That reply was interrupted. Retry to ask again.";
  }
  return "The connection to Alfred dropped before the reply finished. Retry to ask again.";
}
