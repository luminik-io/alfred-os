import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  Copy,
  ExternalLink,
  FileCheck2,
  GitBranch,
  MessagesSquare,
  Send,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  composeConverse,
  composeDraft,
  conversationControl,
  isLiveSessionUnavailable,
  streamComposeConverse,
  supportsNativeActions,
} from "../api";
import { threadForCompose } from "../lib/derive";
import { isSafeExternalUrl, openExternal } from "../lib/links";
import type { TabKey } from "../lib/uiTypes";
import type {
  ComposeDraftFields,
  ComposeDraftResponse,
  ConversationControlResponse,
  ConverseMessage,
  ConverseReadiness,
  ConverseResponse,
} from "../types";
import { EmptyState, PanelHeader } from "./atoms";
import { RequestThread } from "./RequestThread";
import { Switch } from "./ui";

// Plain-language prompt for a non-developer. The old DSL (title:/problem:/repo:)
// is gone: a designer should be able to type in their own words. The server's
// planning assistant still produces the same structured draft; the optional
// ALFRED_INTAKE_PROFILE=plain server env makes the assistant ask plain
// questions and hide jargon in its summary (see intake_profiles.py).
const PLACEHOLDER = "Describe the outcome, who needs it, and any limits Alfred should respect.";

const CHAT_OPENER =
  "Start in plain language. Alfred asks only what is missing, then turns the request into a reviewable plan.";

const STARTERS = [
  {
    label: "Ship a feature",
    text: "Add a CSV export to the attendees table so sales can download the filtered rows they see.",
    hint: "Useful when you know the user outcome.",
  },
  {
    label: "Fix a workflow",
    text: "The review queue is hard to scan at small window sizes. Make it usable without hiding important decisions.",
    hint: "Useful when something feels slow or confusing.",
  },
  {
    label: "Polish a screen",
    text: "Improve the setup screen so a non-developer can connect Alfred and understand what is ready.",
    hint: "Useful for UX, copy, and visual quality.",
  },
];

export function ComposeView({
  baseUrl,
  intakeProfile,
  selectedRepos = [],
  onSwitch,
}: {
  baseUrl: string;
  // The active server-side intake profile from /api/status. "plain" makes the
  // refiner ask plain questions and hide jargon, so Compose confirms the mode
  // and softens its copy. Undefined on older servers; treated as technical.
  intakeProfile?: string;
  selectedRepos?: string[];
  onSwitch: (tab: TabKey) => void;
}) {
  // The guided chat needs the native bridge (a live Claude/Codex session). Off
  // Tauri (browser preview) we keep the existing one-shot rubric form so the
  // preview never breaks. The chat can also fall back to one-shot mid-session
  // if the server reports no live engine is configured.
  const [oneShot, setOneShot] = useState(!supportsNativeActions());

  if (oneShot) {
    return (
      <OneShotCompose
        baseUrl={baseUrl}
        intakeProfile={intakeProfile}
        selectedRepos={selectedRepos}
        onSwitch={onSwitch}
      />
    );
  }
  return (
    <ConversationalCompose
      baseUrl={baseUrl}
      intakeProfile={intakeProfile}
      selectedRepos={selectedRepos}
      onSwitch={onSwitch}
      onDegrade={() => setOneShot(true)}
    />
  );
}

// ------------------------------------------------------------------------- //
// Conversational spec-builder (native): a guided chat with a readiness meter.
// ------------------------------------------------------------------------- //

type ChatTurn = ConverseMessage & { pending?: boolean };

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

function ConversationalCompose({
  baseUrl,
  intakeProfile,
  selectedRepos,
  onSwitch,
  onDegrade,
}: {
  baseUrl: string;
  intakeProfile?: string;
  selectedRepos: string[];
  onSwitch: (tab: TabKey) => void;
  onDegrade: () => void;
}) {
  // Plain mode is a per-request toggle, seeded from the server's
  // ALFRED_INTAKE_PROFILE default but flippable in-app: a non-developer can
  // turn jargon-free coaching on/off without restarting the runtime. The chosen
  // value rides each /api/compose/converse call as `plain`.
  const serverPlainDefault = intakeProfile === "plain";
  const [isPlainMode, setIsPlainMode] = useState(serverPlainDefault);
  // Seed from the server's ALFRED_INTAKE_PROFILE default, but keep following it
  // until the operator flips the switch: Compose can mount before the first
  // /api/status arrives (intakeProfile undefined), and a one-time initializer
  // would then lock the toggle off and override a server plain default.
  const [plainPinned, setPlainPinned] = useState(false);
  useEffect(() => {
    if (!plainPinned) setIsPlainMode(serverPlainDefault);
  }, [serverPlainDefault, plainPinned]);
  const onPlainToggle = (next: boolean) => {
    setPlainPinned(true);
    setIsPlainMode(next);
  };
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConverseResponse | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  // Tracks the in-flight converse run so reset() and unmount can cancel it.
  // Without this, a slow stream that resolves AFTER the chat was cleared (or the
  // view unmounted) would resurrect a cleared transcript by committing stale
  // state. We abort the controller and ignore any result whose controller is no
  // longer the current one.
  const abortRef = useRef<AbortController | null>(null);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    if (turns.length === 0) return;
    const el = transcriptRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [turns]);

  // Cancel any in-flight stream when the view unmounts so a late resolve never
  // touches state on a torn-down component.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    // Abort any prior in-flight run and start a fresh controller for this turn.
    // `isCurrent()` lets every state commit below bail if reset()/unmount (or a
    // newer send) replaced this controller while the turn was still resolving.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const isCurrent = () => abortRef.current === controller && !controller.signal.aborted;
    // Echo the user's turn immediately, plus a pending Alfred placeholder.
    const nextTurns: ChatTurn[] = [...turns, { role: "user", content: text }];
    setTurns([...nextTurns, { role: "assistant", content: "", pending: true }]);
    setInput("");

    // Send the full transcript (server caps + wraps it as untrusted). Only real
    // turns go on the wire; the pending placeholder is UI-only.
    const wire: ConverseMessage[] = nextTurns.map((turn) => ({
      role: turn.role,
      content: turn.content,
    }));
    const request = {
      messages: wire,
      draft_id: result?.draft_id,
      repos: contextReposForPlanning(result, selectedRepos),
      context_repos: contextReposForPlanning(result, selectedRepos),
      plain: isPlainMode,
    };

    try {
      const control = await conversationControl(baseUrl, { text }, controller.signal);
      if (!isCurrent()) {
        return;
      }
      if (control.handled) {
        setTurns([
          ...nextTurns,
          { role: "assistant", content: controlReply(control) },
        ]);
        return;
      }
    } catch (err) {
      if (controller.signal.aborted) {
        return;
      }
      setTurns(turns);
      setInput(text);
      setError(err instanceof Error ? err.message : String(err));
      return;
    }

    // Stream tokens into the pending bubble as the model writes them (#36), then
    // reconcile to the final ConverseResponse. If streaming is unavailable or
    // errors mid-flight we fall back to the existing one-shot converse, which
    // returns the same shape; the live engine still ran the turn either way, so
    // the fallback only changes how the reply is delivered, never the result.
    const renderToken = (fragment: string) => {
      // Drop tokens that arrive after this run was cancelled/superseded.
      if (!isCurrent()) return;
      setTurns((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant" && last.pending) {
          next[next.length - 1] = { ...last, content: last.content + fragment };
        }
        return next;
      });
    };

    try {
      let reply: ConverseResponse;
      try {
        reply = await streamComposeConverse(baseUrl, request, renderToken, controller.signal);
      } catch (streamErr) {
        // A cancelled run (reset/unmount) must not fall back or surface an
        // error; it simply stops touching state.
        if (controller.signal.aborted) {
          return;
        }
        // The live-session degrade (no engine) must propagate to the one-shot
        // fallback, not silently retry; any other streaming failure falls back
        // to the non-streaming converse (a buffered request that still works
        // when only the streaming transport is the problem).
        if (isLiveSessionUnavailable(streamErr)) {
          throw streamErr;
        }
        // Reset the pending bubble to empty so a partial stream does not leave
        // half a reply before the buffered call replaces it.
        renderToken("");
        if (isCurrent()) {
          setTurns([...nextTurns, { role: "assistant", content: "", pending: true }]);
        }
        reply = await composeConverse(baseUrl, request, controller.signal);
      }
      // A late resolve after reset()/unmount must not resurrect a cleared chat.
      if (!isCurrent()) {
        return;
      }
      setResult(reply);
      setTurns([...nextTurns, { role: "assistant", content: reply.reply }]);
    } catch (err) {
      // Swallow a cancellation: the run was intentionally discarded.
      if (controller.signal.aborted) {
        return;
      }
      // No live engine configured server-side: still create the plan through
      // the reliable draft endpoint, then render the saved draft inside this
      // chat surface. Pressing Start should always produce a visible saved
      // plan when the runtime itself is reachable.
      if (isLiveSessionUnavailable(err)) {
        try {
          const draft = await composeDraft(baseUrl, {
            text,
            draft_id: result?.draft_id,
            draft: draftWithRepoContext(result, selectedRepos),
            context_repos: contextReposForPlanning(result, selectedRepos),
          });
          if (!isCurrent()) {
            return;
          }
          const fallback = converseFromDraft(draft);
          setResult(fallback);
          setTurns([...nextTurns, { role: "assistant", content: fallback.reply }]);
          return;
        } catch (draftErr) {
          if (controller.signal.aborted) {
            return;
          }
          // If even the reliable endpoint is unavailable, fall back to the
          // explicit one-shot form with the user's text restored.
          onDegrade();
          setInput(text);
          setError(draftErr instanceof Error ? draftErr.message : String(draftErr));
          return;
        }
      }
      // A real failure: revert to the pre-send transcript (drop both the
      // pending bubble AND the just-echoed user turn) and put the text back in
      // the input, so a retry re-sends it once instead of duplicating the turn.
      setTurns(turns);
      setInput(text);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      // Only the still-current run owns the busy flag; a superseded run must not
      // clear busy out from under its replacement.
      if (abortRef.current === controller) {
        setBusy(false);
      }
    }
  }, [baseUrl, busy, input, isPlainMode, onDegrade, result, selectedRepos, turns]);

  const reset = useCallback(() => {
    // Cancel any in-flight stream so a late resolve cannot repopulate the chat
    // we are clearing here.
    abortRef.current?.abort();
    abortRef.current = null;
    setTurns([]);
    setResult(null);
    setError(null);
    setInput("");
    setBusy(false);
  }, []);

  const started = turns.length > 0;
  const readiness = result?.readiness ?? null;

  return (
    <section className="compose-stack">
      <div className={`panel panel--wide compose-chat-panel${result ? " compose-chat-panel--with-result" : ""}`}>
        <div className="compose-chat-panel__head">
          <div className="compose-head">
            {/* Stable eyebrow: the Plain/Technical mode is already shown by the
                toggle beside it, so the eyebrow does not flip between the two
                (which read as a jumping label). It states the intent instead. */}
            <PanelHeader eyebrow="New request" title="Ask Alfred" />
            <PlainModeToggle checked={isPlainMode} onChange={onPlainToggle} />
          </div>
          <p className="panel-intro">
            {isPlainMode
              ? "Say what you want done. Alfred asks follow-ups, drafts the plan, and waits for approval."
              : "Describe the outcome, repos, and constraints. Alfred plans, labels, and prepares the GitHub handoff."}
          </p>
          <div className="compose-session-bar" aria-label="Request status">
            <span>
              <MessagesSquare size={14} aria-hidden="true" />
              Request
            </span>
            <span>
              <FileCheck2 size={14} aria-hidden="true" />
              Plan
            </span>
            <span>
              <CheckCircle2 size={14} aria-hidden="true" />
              Approve
            </span>
          </div>
          {isPlainMode ? (
            <p className="compose-mode-note" role="note">
              Plain answers are on.
            </p>
          ) : null}
        </div>

        <div className={`compose-chat compose-chat--mission${result ? " compose-chat--with-result" : " compose-chat--empty"}`}>
          <div className="compose-chat__transcript" ref={transcriptRef} role="log" aria-label="Conversation with Alfred">
            {started ? (
              turns.map((turn, index) => (
                <ChatBubble key={`${index}-${turn.role}`} turn={turn} />
              ))
            ) : (
              <ComposeWelcome onPick={setInput} />
            )}
          </div>

          {error ? (
            <div className="inline-notice inline-notice--error">
              <AlertTriangle size={18} aria-hidden="true" />
              <span>{error}</span>
            </div>
          ) : null}

          <div className="compose-chat__composer">
            <label htmlFor="compose-chat-input" className="visually-hidden">
              Your message to Alfred
            </label>
            <textarea
              id="compose-chat-input"
              value={input}
              placeholder={started ? "Reply to Alfred, or add detail." : PLACEHOLDER}
              rows={started ? 3 : 3}
              spellCheck
              disabled={busy}
              onChange={(event) => setInput(event.currentTarget.value)}
              onKeyDown={(event) => {
                // Chat-style: Enter sends, Shift+Enter inserts a newline.
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (!busy && input.trim()) void send();
                }
              }}
            />
            <div className="compose-chat__composer-actions">
              <small className="compose-hint">Alfred saves the plan as the conversation gets clearer.</small>
              {started ? (
                <button className="compose-chat__reset" type="button" disabled={busy} onClick={reset}>
                  <Sparkles size={14} aria-hidden="true" />
                  <span>Start over</span>
                </button>
              ) : null}
              <button
                className="compose-chat__send"
                type="button"
                disabled={busy || !input.trim()}
                onClick={() => void send()}
                aria-label={busy ? "Alfred is thinking" : started ? "Send" : "Start plan"}
                title={busy ? "Alfred is thinking" : started ? "Send" : "Start"}
              >
                <Send size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
          <ConversationalPlanTray
            result={result}
            readiness={readiness}
            isPlainMode={isPlainMode}
            selectedRepos={selectedRepos}
            onOpenPlans={() => onSwitch("plans")}
          />
        </div>
      </div>
    </section>
  );
}

function ConversationalPlanTray({
  result,
  readiness,
  isPlainMode,
  selectedRepos,
  onOpenPlans,
}: {
  result: ConverseResponse | null;
  readiness: ConverseReadiness | null;
  isPlainMode: boolean;
  selectedRepos: string[];
  onOpenPlans: () => void;
}) {
  if (!result) {
    return (
      <MissionContractTray
        selectedRepos={selectedRepos}
        onOpenPlans={onOpenPlans}
      />
    );
  }
  return (
    <div className="compose-plan-tray">
      <PanelHeader eyebrow="Plan" title="Plan status" />
      {readiness && !isPlainMode ? <ReadinessMeter readiness={readiness} /> : null}
      <RequestThread
        thread={threadForCompose({
          draftId: result.draft_id,
          title: result.draft.title,
          repos: result.draft.repos,
          ready: result.readiness.ready,
        })}
        onOpenPlan={onOpenPlans}
      />
      <HandoffPanel result={result} isPlainMode={isPlainMode} onOpenPlans={onOpenPlans} />
    </div>
  );
}

function ComposeWelcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="compose-welcome">
      <div className="compose-welcome__hero">
        <span className="compose-welcome__mark" aria-hidden="true">
          <Sparkles size={20} />
        </span>
        <h2>Describe the work.</h2>
        <p>{CHAT_OPENER}</p>
      </div>
      <div className="compose-prompts" aria-label="Starter prompts">
        {STARTERS.map((starter) => (
          <button
            key={starter.label}
            type="button"
            className="compose-prompt"
            onClick={() => onPick(starter.text)}
          >
            <span>
              {starter.label}
              <ArrowRight size={14} aria-hidden="true" />
            </span>
            <small>{starter.text}</small>
            <em>{starter.hint}</em>
          </button>
        ))}
      </div>
    </div>
  );
}

function MissionContractTray({
  selectedRepos,
  onOpenPlans,
}: {
  selectedRepos: string[];
  onOpenPlans: () => void;
}) {
  const repos = cleanRepos(selectedRepos);
  const repoPreview =
    repos.length === 0
      ? "Auto-detect"
      : repos.length > 2
        ? `${repos.length} repos selected`
        : repos.length === 1
        ? repos[0]
        : repos.join(", ");
  const repoTitle = repos.length ? repos.join(", ") : repoPreview;
  return (
    <div className="compose-plan-tray compose-plan-tray--contract">
      <PanelHeader eyebrow="Ask" title="Request brief" />
      <div className="mission-contract" aria-label="Request brief">
        <article className="mission-contract__item">
          <GitBranch size={16} aria-hidden="true" />
          <div>
            <span>Context</span>
            <strong title={repoTitle}>{repoPreview}</strong>
            <p>Selected repos shape the draft. Alfred can infer context when none are selected.</p>
          </div>
        </article>
        <article className="mission-contract__item">
          <Bot size={16} aria-hidden="true" />
          <div>
            <span>Routing</span>
            <strong>Lucius or Batman</strong>
            <p>Focused repo work goes to Lucius. Cross-repo work goes to Batman.</p>
          </div>
        </article>
        <article className="mission-contract__item">
          <ShieldCheck size={16} aria-hidden="true" />
          <div>
            <span>Gate</span>
            <strong>Approval gate</strong>
            <p>No issue filing or code execution before the explicit approval step.</p>
          </div>
        </article>
      </div>
      <button className="secondary-button compose-plan-tray__plans" type="button" onClick={onOpenPlans}>
        <FileCheck2 size={16} aria-hidden="true" />
        <span>Open plans</span>
      </button>
    </div>
  );
}

function converseFromDraft(result: ComposeDraftResponse): ConverseResponse {
  const rawScore = result.readiness.score;
  const score = rawScore <= 1 ? Math.round(rawScore * 100) : Math.round(rawScore);
  return {
    draft_id: result.draft_id,
    saved_path: result.saved_path,
    reply: oneShotReply(result),
    readiness: {
      score: Math.max(0, Math.min(100, score)),
      ready: result.readiness.ok,
      missing: result.questions,
    },
    done: false,
    draft: result.draft,
  };
}

function controlReply(result: ConversationControlResponse): string {
  const body = (result.text || result.detail || "").trim();
  if (body) {
    return body;
  }
  return `Alfred handled ${result.action || "the request"}.`;
}

// A visible in-app toggle for plain mode. Seeded from the server's
// ALFRED_INTAKE_PROFILE default, it lets a non-developer flip jargon-free
// coaching on/off; the value rides each converse call as `plain`. Rendered as a
// labelled Radix switch so keyboard, pointer, and screen-reader behavior stays
// native while Alfred owns the visual skin.
function PlainModeToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="plain-toggle">
      <Switch
        className="plain-toggle__switch"
        checked={checked}
        onCheckedChange={onChange}
        aria-label="Plain mode"
      />
      <span className="plain-toggle__label">Plain mode</span>
    </label>
  );
}

function ChatBubble({ turn }: { turn: ChatTurn }) {
  const who = turn.role === "user" ? "You" : "Alfred";
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(turn.content).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    });
  };
  return (
    <div className={`compose-bubble compose-bubble--${turn.role}`}>
      <div className="compose-bubble__head">
        <span className="compose-bubble__who">{who}</span>
        {!turn.pending && turn.content ? (
          <button
            type="button"
            className="compose-bubble__copy"
            onClick={copy}
            aria-label={copied ? "Copied" : "Copy message"}
            title={copied ? "Copied" : "Copy"}
          >
            {copied ? <CheckCircle2 size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          </button>
        ) : null}
      </div>
      {turn.pending ? (
        <span className="compose-bubble__pending" aria-label="Alfred is thinking">
          <span className="compose-bubble__dot" />
          <span className="compose-bubble__dot" />
          <span className="compose-bubble__dot" />
        </span>
      ) : turn.role === "assistant" ? (
        // Render Alfred's replies as markdown so headings, lists, tables and
        // fenced code render like a real chat surface. User turns stay plain
        // text (people type prose, and it avoids rendering injected markup).
        <div className="compose-bubble__md">
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
        <p className="compose-bubble__text">{turn.content}</p>
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
    return <span className="compose-bubble__unsafe-link">{children}</span>;
  }
  return (
    <button
      className="compose-bubble__link"
      type="button"
      onClick={() => void openExternal(href)}
    >
      <span>{children}</span>
      <ExternalLink size={13} aria-hidden="true" />
    </button>
  );
}

// A restrained, enterprise readiness meter: a slim track that fills as the spec
// firms up, plus a quiet "N questions from ready" caption. No badges, no
// celebration, no points. Matches the Wayne theme tokens used across Compose.
function ReadinessMeter({ readiness }: { readiness: ConverseReadiness }) {
  const score = Math.max(0, Math.min(100, Math.round(readiness.score)));
  const tone = readiness.ready ? "ready" : score >= 60 ? "near" : "early";
  const remaining = readiness.missing.length;
  const caption = readiness.ready
    ? "Ready to hand off."
    : remaining === 0
      ? "Almost there."
      : `${remaining} ${remaining === 1 ? "question" : "questions"} from ready`;
  return (
    <div className={`compose-readiness compose-readiness--${tone}`}>
      <div className="compose-readiness__head">
        <span className="compose-readiness__label">Plan readiness</span>
        <span className="compose-readiness__value">{score}%</span>
      </div>
      <div
        className="compose-readiness__track"
        role="progressbar"
        aria-valuenow={score}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Plan readiness"
      >
        <div className="compose-readiness__fill" style={{ width: `${score}%` }} />
      </div>
      <p className="compose-readiness__caption">{caption}</p>
      {remaining > 0 ? (
        <ul className="compose-readiness__missing">
          {readiness.missing.map((item, index) => (
            <li key={`${index}-${item.slice(0, 24)}`}>{item}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// The hand-off: once the spec is ready (model-judged), surface a clear "Open
// Plans" action that opens the saved-plan inbox where the draft lives. Before
// ready, the button stays available so the person can inspect or keep chatting.
function HandoffPanel({
  result,
  isPlainMode,
  onOpenPlans,
}: {
  result: ConverseResponse;
  isPlainMode: boolean;
  onOpenPlans: () => void;
}) {
  const ready = result.readiness.ready;
  return (
    <div className="compose-handoff">
      <p className="compose-handoff__note">
        {ready
          ? isPlainMode
            ? "Ready to review. Open Plans to approve and file the issue."
            : "Ready to review. Open Plans to approve and create the GitHub issue."
          : "Saved as a plan. Add the missing details here, or review it in Plans."}
      </p>
      <button
        className={ready ? "icon-button" : "secondary-button"}
        type="button"
        onClick={onOpenPlans}
      >
        {ready ? <CheckCircle2 size={16} aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}
        <span>Open Plans</span>
      </button>
    </div>
  );
}

// ------------------------------------------------------------------------- //
// One-shot rubric form (browser preview / degrade): the existing behavior.
// ------------------------------------------------------------------------- //

function OneShotCompose({
  baseUrl,
  intakeProfile,
  selectedRepos,
  onSwitch,
}: {
  baseUrl: string;
  intakeProfile?: string;
  selectedRepos: string[];
  onSwitch: (tab: TabKey) => void;
}) {
  const isPlainMode = intakeProfile === "plain";
  const [intent, setIntent] = useState("");
  const [lastIntent, setLastIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ComposeDraftResponse | null>(null);
  const [controlResult, setControlResult] = useState<ConversationControlResponse | null>(null);

  const submit = useCallback(async () => {
    const text = intent.trim();
    if (!text || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const control = await conversationControl(baseUrl, { text });
      if (control.handled) {
        setControlResult(control);
        setResult(null);
        setLastIntent(text);
        setIntent("");
        return;
      }
      setControlResult(null);
      const next = await composeDraft(baseUrl, {
        text,
        draft_id: result?.draft_id,
        draft: draftWithRepoContext(result, selectedRepos),
        context_repos: contextReposForPlanning(result, selectedRepos),
      });
      setResult(next);
      setLastIntent(text);
      setIntent("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, busy, intent, result, selectedRepos]);

  const reset = useCallback(() => {
    setResult(null);
    setControlResult(null);
    setError(null);
    setIntent("");
    setLastIntent("");
  }, []);

  const iterating = result !== null;
  const nextQuestion = result?.questions?.[0] || null;

  return (
    <section className="compose-stack">
      <div className={`panel panel--wide compose-chat-panel${result ? " compose-chat-panel--with-result" : ""}`}>
        <div className="compose-chat-panel__head">
          {/* Stable eyebrow: it does not flip between Plain and Technical (the
              mode is shown elsewhere), so the label stays steady while the
              title carries the new-vs-refine distinction. */}
          <PanelHeader
            eyebrow="New request"
            title={iterating ? "Refine request" : "Ask Alfred"}
          />
          <p className="panel-intro">
            {isPlainMode
              ? "Say what you want done. Alfred asks only what is missing and saves a plain plan for approval."
              : "Describe the change, repo scope, and constraints. Alfred prepares the GitHub handoff."}
          </p>
          <div className="compose-session-bar" aria-label="Request status">
            <span>
              <MessagesSquare size={14} aria-hidden="true" />
              Request
            </span>
            <span>
              <FileCheck2 size={14} aria-hidden="true" />
              Plan
            </span>
            <span>
              <CheckCircle2 size={14} aria-hidden="true" />
              Issue
            </span>
          </div>
          {isPlainMode ? (
            <p className="compose-mode-note" role="note">
              Plain answers are on.
            </p>
          ) : null}
        </div>

        <div className={`compose-chat compose-chat--mission${result ? " compose-chat--with-result" : " compose-chat--empty"}`}>
          <div className="compose-chat__transcript" role="log" aria-label="Conversation with Alfred">
            {result ? (
              <>
                <ChatBubble turn={{ role: "user", content: lastIntent || "Plan this work item." }} />
                <ChatBubble turn={{ role: "assistant", content: oneShotReply(result) }} />
              </>
            ) : controlResult ? (
              <>
                <ChatBubble turn={{ role: "user", content: lastIntent || "Ask Alfred." }} />
                <ChatBubble turn={{ role: "assistant", content: controlReply(controlResult) }} />
              </>
            ) : (
              <ComposeWelcome onPick={setIntent} />
            )}
          </div>

          {error ? (
            <div className="inline-notice inline-notice--error">
              <AlertTriangle size={18} aria-hidden="true" />
              <span>{error}</span>
            </div>
          ) : null}

          <div className="compose-chat__composer">
            <label htmlFor="compose-intent" className="visually-hidden">
              {iterating ? "Add detail or answer a question" : "What should Alfred build?"}
            </label>
            {nextQuestion ? (
              <div className="compose-next-question" role="note">
                <span>Next question</span>
                <strong>{nextQuestion}</strong>
              </div>
            ) : null}
            <textarea
              id="compose-intent"
              value={intent}
              placeholder={nextQuestion || PLACEHOLDER}
              rows={iterating ? 4 : 3}
              spellCheck
              disabled={busy}
              onChange={(event) => setIntent(event.currentTarget.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  void submit();
                }
              }}
            />
            <div className="compose-chat__composer-actions">
              <small className="compose-hint">Alfred saves the plan as the conversation gets clearer.</small>
              {iterating ? (
                <button className="compose-chat__reset" type="button" disabled={busy} onClick={reset}>
                  <Sparkles size={14} aria-hidden="true" />
                  <span>Start a new work item</span>
                </button>
              ) : null}
              <button
                className="compose-chat__send"
                type="button"
                disabled={busy || !intent.trim()}
                onClick={() => void submit()}
                aria-label={busy ? "Alfred is working" : iterating ? "Update plan" : "Create plan"}
                title={busy ? "Alfred is working" : iterating ? "Update plan" : "Create plan"}
              >
                <Send size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
          {result ? (
            <DraftPlanTray result={result} selectedRepos={selectedRepos} onOpenPlans={() => onSwitch("plans")} />
          ) : (
            <MissionContractTray
              selectedRepos={selectedRepos}
              onOpenPlans={() => onSwitch("plans")}
            />
          )}
        </div>
      </div>
    </section>
  );
}

function DraftPlanTray({
  result,
  selectedRepos,
  onOpenPlans,
}: {
  result: ComposeDraftResponse;
  selectedRepos: string[];
  onOpenPlans: () => void;
}) {
  const scopedRepos = cleanRepos(result.draft.repos);
  const contextRepos = scopedRepos.length ? [] : cleanRepos(selectedRepos);
  return (
    <div className="compose-plan-tray">
      <PanelHeader eyebrow="Plan" title="Plan status" />
      <RequestThread
        thread={threadForCompose({
          draftId: result.draft_id,
          title: result.title,
          repos: result.draft.repos,
          ready: result.readiness.ok,
        })}
        onOpenPlan={onOpenPlans}
      />
      {!scopedRepos.length && contextRepos.length > 1 ? (
        <div className="compose-context-note" role="note">
          Alfred already has {contextRepos.length} codebases available for context. Pick the workspace area before filing the issue.
        </div>
      ) : null}
      <div className="compose-handoff">
        <p className="compose-handoff__note">
          {result.readiness.ok
            ? "This plan is saved. Open Plans to approve and create the GitHub issue."
            : "The plan is saved, but Alfred still has questions. Answer them here or open Plans to inspect it."}
        </p>
        <button
          className={result.readiness.ok ? "icon-button" : "secondary-button"}
          type="button"
          onClick={onOpenPlans}
        >
          {result.readiness.ok ? <CheckCircle2 size={16} aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}
          <span>Open Plans</span>
        </button>
      </div>
      <div className="compose-questions">
        <PanelHeader eyebrow="A quick check" title="Anything to clear up" />
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
            title="Nothing unclear."
            body="Review the saved plan, then file the issue when it is ready."
            compact
            tone="ok"
          />
        )}
      </div>
      {result.findings.length ? (
        <div className="compose-findings">
          <PanelHeader eyebrow="Before it ships" title="What still needs a look" />
          <ul>
            {result.findings.map((finding) => (
              <li
                key={finding.code}
                className={`compose-finding compose-finding--${finding.severity}`}
              >
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
    </div>
  );
}

function oneShotReply(result: ComposeDraftResponse): string {
  return result.summary || "I saved a plan. Review the next steps below.";
}
