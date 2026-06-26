import { AlertTriangle, ArrowRight, ArrowUp, RotateCcw, Sparkles, Square } from "lucide-react";
import { useCallback, useEffect } from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ThreadPrimitive,
  useThreadRuntime,
} from "@assistant-ui/react";

import type { TabKey } from "../../lib/uiTypes";
import { AskSurfaceProvider } from "./AskContext";
import { AskAssistantMessage, AskUserMessage, type AskMessageContext } from "./AskMessage";
import { RecentThreads } from "./RecentThreads";
import { useAskThread } from "./useAskThread";

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

// One chat surface, now built on assistant-ui's ExternalStoreRuntime. Whether a
// live engine is configured or not, the operator sees a single ChatGPT-grade
// conversation: a full-height thread, a markdown reply rendered incrementally,
// and a bottom composer where Enter sends. When a turn produces a ready draft it
// appears inline as a lifecycle card (a custom assistant-ui message part) with
// the one File issue action (one-gate). Off Tauri (browser preview) or when the
// server has no live engine, the same turn routes through the reliable draft
// endpoint, so the surface never breaks. State (chatHistory, last-5
// conversations) is owned here; assistant-ui renders it.
export function ComposeView({
  baseUrl,
  selectedRepos = [],
  onSwitch,
}: {
  baseUrl: string;
  selectedRepos?: string[];
  onSwitch: (tab: TabKey) => void;
}) {
  const ask = useAskThread({ baseUrl, selectedRepos });

  const onOpenWork = useCallback(() => onSwitch("pipeline"), [onSwitch]);

  const messageContext: AskMessageContext = {
    busy: ask.busy,
    canRetry: ask.canRetry,
    onRetry: ask.retry,
    lastReplyId: ask.lastReplyId,
  };

  // A failed turn keeps the person's message on screen and offers Retry, so a
  // dropped stream is one click from recovery rather than a retype.
  const errorNotice = ask.error ? (
    <div className="ask__error inline-notice inline-notice--error" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <span>{ask.error}</span>
      {ask.canRetry ? (
        <button type="button" className="ask__retry" onClick={ask.retry}>
          <RotateCcw size={13} aria-hidden="true" />
          <span>Retry</span>
        </button>
      ) : null}
    </div>
  ) : null;

  const composer = (
    <div className="ask__composer-wrap">
      <ComposerPrimitive.Root className="ask__composer">
        <label htmlFor="ask-input" className="visually-hidden">
          Your message to Alfred
        </label>
        <ComposerPrimitive.Input
          id="ask-input"
          className="ask__input"
          // ChatGPT-style: Enter sends, Shift+Enter inserts a newline.
          submitMode="enter"
          rows={1}
          spellCheck
          placeholder={ask.started ? "Reply to Alfred, or add detail." : PLACEHOLDER}
        />
        {ask.busy ? (
          // While a turn streams, the primary control stops generation. The
          // partial reply is kept on screen when stopped.
          <ComposerPrimitive.Cancel
            className="ask__send ask__send--stop"
            aria-label="Stop generating"
            title="Stop generating"
          >
            <Square size={16} aria-hidden="true" />
          </ComposerPrimitive.Cancel>
        ) : (
          <ComposerPrimitive.Send
            className="ask__send"
            aria-label="Send message"
            title="Send"
          >
            <ArrowUp size={18} aria-hidden="true" />
          </ComposerPrimitive.Send>
        )}
      </ComposerPrimitive.Root>
      <small className="ask__hint">
        Enter to send, Shift + Enter for a new line. When you are planning work, Alfred shapes a plan as the chat gets clearer.
      </small>
    </div>
  );

  const starters = (
    <div className="ask__starters" aria-label="Starter prompts">
      {STARTERS.map((starter) => (
        <StarterChip key={starter.label} text={starter.text} label={starter.label} busy={ask.busy} />
      ))}
    </div>
  );

  return (
    <AssistantRuntimeProvider runtime={ask.runtime}>
      <RestoreTextBridge register={ask.registerRestoreText} />
      <AskSurfaceProvider
        value={{
          fileBusyId: ask.fileBusyId,
          fileNotices: ask.fileNotices,
          onFile: ask.fileIssue,
          onOpenWork,
        }}
      >
        <section className={`ask${ask.started ? "" : " ask--empty"}`} aria-label="Ask Alfred">
          {ask.started ? (
            <header className="ask__head">
              <span className="ask__eyebrow">Ask Alfred</span>
              <div className="ask__head-controls">
                <RecentThreads threads={ask.recentThreads} onResume={ask.resumeConversation} />
                <button
                  className="ghost-button ask__reset"
                  type="button"
                  disabled={ask.busy}
                  onClick={ask.newChat}
                >
                  <Sparkles size={14} aria-hidden="true" />
                  <span>New chat</span>
                </button>
              </div>
            </header>
          ) : null}

          {ask.started ? (
            <ThreadPrimitive.Root className="ask__thread-root">
              <ThreadPrimitive.Viewport
                className="ask__thread"
                autoScroll
                role="log"
                aria-label="Conversation with Alfred"
                // Politely announce the assistant's reply to assistive tech as
                // it streams in, without interrupting the person mid-sentence.
                aria-live="polite"
                aria-relevant="additions text"
                aria-busy={ask.busy}
              >
                <div className="ask__turns">
                  <ThreadPrimitive.Messages
                    components={{
                      UserMessage: AskUserMessage,
                      AssistantMessage: () => <AskAssistantMessage context={messageContext} />,
                    }}
                  />
                </div>
              </ThreadPrimitive.Viewport>
            </ThreadPrimitive.Root>
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

          {ask.started ? (
            <>
              {errorNotice}
              {composer}
            </>
          ) : null}
        </section>
      </AskSurfaceProvider>
    </AssistantRuntimeProvider>
  );
}

// Bridges the hook's composer text-restore callback to the runtime composer.
// Mounted inside the AssistantRuntimeProvider so useThreadRuntime resolves; on a
// failed first hop the hook calls register's fn to put the person's text back.
function RestoreTextBridge({
  register,
}: {
  register: (fn: ((text: string) => void) | null) => void;
}) {
  const thread = useThreadRuntime();
  useEffect(() => {
    register((text: string) => {
      thread.composer.setText(text);
    });
    return () => register(null);
  }, [register, thread]);
  return null;
}

// A starter chip seeds the composer with a prompt and focuses it. It writes the
// runtime composer's text directly so the seeded prompt lands in the same input
// the person then sends.
function StarterChip({ text, label, busy }: { text: string; label: string; busy: boolean }) {
  const thread = useThreadRuntime();
  return (
    <button
      type="button"
      className="ask__starter"
      disabled={busy}
      onClick={() => {
        thread.composer.setText(text);
        // Focus the composer input so the seeded prompt is ready to send.
        const input = document.getElementById("ask-input");
        if (input instanceof HTMLTextAreaElement) input.focus();
      }}
    >
      <span>{label}</span>
      <ArrowRight size={13} aria-hidden="true" />
    </button>
  );
}
