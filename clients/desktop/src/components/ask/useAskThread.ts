import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";

import {
  composeConverse,
  composeDraft,
  conversationControl,
  filePlanIssue,
  isLiveSessionUnavailable,
  streamComposeConverse,
  supportsNativeActions,
} from "../../api";
import {
  clearConversations,
  conversationTitle,
  deleteConversation,
  loadConversations,
  newConversationId,
  saveConversation,
  type PersistedConversation,
} from "../../lib/chatHistory";
import type {
  ComposeDraftFields,
  ComposeDraftResponse,
  ConversationControlResponse,
  ConverseMessage,
  ConverseResponse,
  FilePlanIssueResponse,
} from "../../types";
import {
  cleanRepos,
  draftCardFrom,
  draftFallbackReply,
  draftHasSubstance,
  DRAFT_TOOL_NAME,
  fromPersistedTurn,
  isConversationTurn,
  lastUserTextOf,
  reconnectMessage,
  toPersistedTurn,
  trimToLastUser,
  type ChatTurn,
  type DraftCardModel,
} from "./askModel";

export type FileNotice = {
  tone: "ok" | "error";
  message: string;
  url?: string;
};

// A lightweight entry for the recent-threads switcher.
export type RecentThread = {
  id: string;
  title: string;
  updatedAt: number;
  active: boolean;
};

// Args/result shape on the draft tool-call part the converter emits. The
// renderer reads `args` to draw the lifecycle card.
export type DraftToolArgs = { draft: DraftCardModel };

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

function controlReply(result: ConversationControlResponse): string {
  const body = (result.text || result.detail || "").trim();
  if (body) return body;
  return `Alfred handled ${result.action || "the request"}.`;
}

// Convert one in-view ChatTurn into the assistant-ui message model. A draft
// turn becomes an assistant message carrying a single "alfred-draft" tool-call
// part (assistant-ui's extension point for custom in-thread content); a message
// turn becomes a plain text part. A pending (streaming) assistant turn is marked
// `running` so the surface can show its live caret / typing indicator.
function convertTurn(turn: ChatTurn, index: number): ThreadMessageLike {
  if (turn.kind === "draft") {
    return {
      role: "assistant",
      id: `draft-${index}-${turn.draft.draftId}`,
      content: [
        {
          type: "tool-call",
          toolCallId: `draft-${index}-${turn.draft.draftId}`,
          toolName: DRAFT_TOOL_NAME,
          args: { draft: turn.draft },
          argsText: JSON.stringify({ draft: turn.draft }),
        },
      ],
    };
  }
  // `status` is only valid on assistant messages; a user turn carries none.
  if (turn.role === "user") {
    return {
      role: "user",
      id: `${index}-user`,
      content: [{ type: "text", text: turn.content }],
    };
  }
  return {
    role: "assistant",
    id: `${index}-assistant`,
    content: [{ type: "text", text: turn.content }],
    status: turn.pending ? { type: "running" } : { type: "complete", reason: "stop" },
  };
}

// The orchestration hook for the Ask surface. Owns the conversation state, the
// converse/draft/control state machine (ported from the prior ComposeView), the
// last-5 persistence, the inline draft/file lifecycle, and the assistant-ui
// ExternalStore runtime that renders it all. The view is a thin shell around the
// runtime plus the draft/recent affordances this returns.
export function useAskThread({
  baseUrl,
  selectedRepos,
}: {
  baseUrl: string;
  selectedRepos: string[];
}) {
  // Whether a live engine is reachable. Starts true on native, false in the
  // browser; flips to false the first time the server reports no engine so we
  // stop attempting the streaming path.
  const [hasEngine, setHasEngine] = useState(supportsNativeActions());

  // Rehydrate the most recent conversation so a closed window picks back up
  // where it left off. Best-effort: a missing or malformed store reads empty.
  const restored = useMemo(() => loadConversations(), []);
  const initial = restored[0];

  const [conversationId, setConversationId] = useState<string>(
    () => initial?.id ?? newConversationId(),
  );
  // Always-current conversation id, so an async file resolution can tell
  // whether the operator switched chats and should no longer apply its result.
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  const [turns, setTurns] = useState<ChatTurn[]>(
    () => initial?.turns.map(fromPersistedTurn) ?? [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The most recent saved draft, carried across turns so the same request is
  // refined (draft_id) and the inline card can file it.
  const [result, setResult] = useState<ConverseResponse | ComposeDraftResponse | null>(
    () =>
      initial?.draftId && initial.draft
        ? ({ draft_id: initial.draftId, draft: initial.draft } as unknown as ConverseResponse)
        : null,
  );
  // The draft currently being filed (its draftId), or null when idle. Scoping
  // this to a specific draft keeps a second card's File button live while one
  // file is in flight, and lets each card show its own spinner.
  const [fileBusyId, setFileBusyId] = useState<string | null>(null);
  // File results keyed by the draftId they belong to, so a "Filed" confirmation
  // rides the exact card whose plan was filed rather than a single global notice
  // that drifts onto whichever card is currently last.
  const [fileNotices, setFileNotices] = useState<Record<string, FileNotice>>({});
  // The recent-threads list (newest first), so the switcher can resume any of
  // the last 5. Reloaded from storage after every settle.
  const [recent, setRecent] = useState<PersistedConversation[]>(restored);

  // The last user message, kept so retry/regenerate can replay the turn after a
  // stop or a dropped stream without the person retyping.
  const lastUserTextRef = useRef<string>(lastUserTextOf(initial?.turns.map(fromPersistedTurn) ?? []));
  // Tracks the in-flight run so reset() and unmount can cancel it; a late
  // resolve must never resurrect a cleared transcript.
  const abortRef = useRef<AbortController | null>(null);
  // Synchronous in-flight guard: `busy` state lags a render, so two submits
  // landing in one batch could both pass the busy check. This ref flips
  // immediately so only the first run proceeds.
  const busyRef = useRef(false);
  // The assistant-ui composer clears its input on submit. When a turn fails
  // before it produces anything (a control or draft error on the first hop), we
  // restore the person's text into the composer so they do not lose it; the view
  // wires this ref to the runtime composer's setText.
  const restoreTextRef = useRef<((text: string) => void) | null>(null);

  // Persist the conversation whenever a turn settles, so it survives a reload or
  // restart. We never persist an in-flight (pending/streaming) turn: while busy
  // the transcript is mid-stream and a half-written reply is not worth keeping.
  useEffect(() => {
    if (busy) return;
    if (turns.length === 0) {
      deleteConversation(conversationId);
      setRecent(loadConversations());
      return;
    }
    // saveConversation upserts by id and is best-effort, so a write hiccup can
    // never break a send.
    saveConversation({
      id: conversationId,
      draftId: result?.draft_id,
      draft: result?.draft,
      turns: turns.map(toPersistedTurn),
    });
    setRecent(loadConversations());
  }, [turns, busy, result, conversationId]);

  // Cancel any in-flight stream when the view unmounts.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

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
        next.push({ kind: "message", role: "assistant", content: message });
      }
      if (isConversationTurn(reply)) {
        // A plain conversational reply (e.g. "thanks") must not wipe a "Filed"
        // confirmation the person is still reading: leave the file notice as-is.
        setTurns(next);
        return;
      }
      // Per-draft notices: a new build turn adds its own draft with an empty
      // notice, and earlier filed drafts keep their Filed chip, so there is
      // nothing to clear here (clearing all would drop a prior draft's result).
      setResult(reply);
      if (draftHasSubstance(reply.draft)) {
        next.push({ kind: "draft", role: "assistant", draft: draftCardFrom(reply) });
      }
      setTurns(next);
    },
    [],
  );

  // Run one conversational turn for `text`, appended after `baseTurns`. Factored
  // so retry/regenerate can replay the same turn against a trimmed transcript
  // without the person retyping.
  const runTurn = useCallback(
    async (text: string, baseTurns: ChatTurn[]) => {
      if (!text || busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      setError(null);
      lastUserTextRef.current = text;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const isCurrent = () => abortRef.current === controller && !controller.signal.aborted;
      const finishIfCurrent = () => {
        if (abortRef.current === controller) {
          busyRef.current = false;
          busyRef.current = false;
      busyRef.current = false;
    setBusy(false);
        }
      };

      const priorMessages: ChatTurn[] = baseTurns;
      const nextTurns: ChatTurn[] = [
        ...baseTurns,
        { kind: "message", role: "user", content: text },
      ];
      setTurns([
        ...nextTurns,
        { kind: "message", role: "assistant", content: "", pending: true },
      ]);

      const wire: ConverseMessage[] = nextTurns
        .filter((turn): turn is ChatTurn & { kind: "message" } => turn.kind === "message")
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
          setTurns([
            ...nextTurns,
            { kind: "message", role: "assistant", content: controlReply(control) },
          ]);
          finishIfCurrent();
          return;
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        setTurns(priorMessages);
        restoreTextRef.current?.(text);
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
          restoreTextRef.current?.(text);
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
          if (last && last.kind === "message" && last.role === "assistant" && last.pending) {
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
            setTurns([
              ...nextTurns,
              { kind: "message", role: "assistant", content: "", pending: true },
            ]);
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
        // turn intact, surface a plain, recoverable notice, and offer Retry.
        setTurns(nextTurns);
        lastUserTextRef.current = text;
        setError(reconnectMessage(err));
      } finally {
        finishIfCurrent();
      }
    },
    [baseUrl, commitTurn, hasEngine, result, selectedRepos],
  );

  // Stop the in-flight generation. The partial assistant reply stays on screen
  // (finalized, no longer pending) so the person keeps whatever streamed in.
  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    busyRef.current = false;
    setBusy(false);
    setTurns((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.kind === "message" && last.role === "assistant" && last.pending) {
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
  // assistant turn(s) for the last user message and replays that message.
  const retry = useCallback(() => {
    if (busy) return;
    const { text, baseTurns } = trimToLastUser(turns, lastUserTextRef.current);
    if (!text) return;
    void runTurn(text, baseTurns);
  }, [busy, runTurn, turns]);

  // Start a fresh, empty conversation. The active thread is already persisted
  // (the last-5 list keeps it), so this just opens a new id and clears state.
  const newChat = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setConversationId(newConversationId());
    setTurns([]);
    setResult(null);
    setError(null);
    busyRef.current = false;
    setBusy(false);
    setFileNotices({});
    setFileBusyId(null);
    lastUserTextRef.current = "";
    setRecent(loadConversations());
  }, []);

  // Resume a stored conversation by id, making it the active thread.
  const resumeConversation = useCallback(
    (id: string) => {
      if (id === conversationId) return;
      // Persist the conversation being left BEFORE swapping. The settle effect
      // skips saving while busy, and the swap below replaces `turns`, so without
      // this a switch mid-stream drops the active turn. A trailing in-flight
      // reply is dropped (a half-written reply is not worth keeping), but the
      // operator's message and any settled turns are preserved and stay in
      // Recent.
      const lastIdx = turns.length - 1;
      const settled = turns.filter(
        (t, i) =>
          !(i === lastIdx && t.kind === "message" && t.role === "assistant" && t.pending),
      );
      if (settled.length) {
        saveConversation({
          id: conversationId,
          draftId: result?.draft_id,
          draft: result?.draft,
          turns: settled.map(toPersistedTurn),
        });
      }
      abortRef.current?.abort();
      abortRef.current = null;
      const target = loadConversations().find((c) => c.id === id);
      if (!target) return;
      setConversationId(target.id);
      setTurns(target.turns.map(fromPersistedTurn));
      setResult(
        target.draftId && target.draft
          ? ({ draft_id: target.draftId, draft: target.draft } as unknown as ConverseResponse)
          : null,
      );
      setError(null);
      busyRef.current = false;
    setBusy(false);
      setFileNotices({});
      setFileBusyId(null);
      lastUserTextRef.current = lastUserTextOf(target.turns.map(fromPersistedTurn));
      setRecent(loadConversations());
    },
    [conversationId, turns, result],
  );

  // Drop every stored conversation and start clean.
  const clearAll = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearConversations();
    setConversationId(newConversationId());
    setTurns([]);
    setResult(null);
    setError(null);
    busyRef.current = false;
    setBusy(false);
    setFileNotices({});
    setFileBusyId(null);
    lastUserTextRef.current = "";
    setRecent([]);
  }, []);

  // One-gate: file the issue straight from the inline card. The server still
  // enforces readiness and repo allowlisting; there is no client approval step.
  const fileIssue = useCallback(
    async (draftId: string) => {
      if (fileBusyId) return;
      const startedIn = conversationIdRef.current;
      setFileBusyId(draftId);
      // Drop only this draft's prior notice; other cards keep theirs.
      setFileNotices((prev) => {
        if (!(draftId in prev)) return prev;
        const next = { ...prev };
        delete next[draftId];
        return next;
      });
      try {
        const res: FilePlanIssueResponse = await filePlanIssue(baseUrl, draftId);
        // Ignore if the operator switched chats while this was in flight: the
        // result belongs to the conversation that started it, not the current one.
        if (conversationIdRef.current !== startedIn) return;
        setFileNotices((prev) => ({
          ...prev,
          [draftId]: {
            tone: "ok",
            message:
              res.status === "already_filed"
                ? "Already filed."
                : `Filed with ${res.label || "agent:implement"}.`,
            url: res.issue_url,
          },
        }));
      } catch (err) {
        if (conversationIdRef.current !== startedIn) return;
        setFileNotices((prev) => ({
          ...prev,
          [draftId]: {
            tone: "error",
            message: err instanceof Error ? err.message : String(err),
          },
        }));
      } finally {
        // Only clear the busy flag for the conversation that started the file;
        // a switch already reset it for the new chat.
        if (conversationIdRef.current === startedIn) setFileBusyId(null);
      }
    },
    [baseUrl, fileBusyId],
  );

  // The id of the last assistant TEXT reply (convertTurn ids text turns as
  // `${index}-assistant`). The regenerate control gates on this so it still
  // shows on the reply when a draft card trails it as a separate message.
  const lastReplyId = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const turn = turns[i];
      if (turn.kind === "message" && turn.role === "assistant") return `${i}-assistant`;
    }
    return null;
  }, [turns]);

  // The assistant-ui ExternalStore runtime. We own the state (turns); assistant-
  // ui renders it. `onNew` routes a composer submission through the same turn
  // machine; `onCancel` stops the in-flight stream. `isRunning` keeps the thread
  // in its running state across the buffered fallback hop even though the last
  // assistant message may momentarily complete.
  const runtime = useExternalStoreRuntime({
    isRunning: busy,
    messages: turns,
    convertMessage: convertTurn,
    onNew: async (message: AppendMessage) => {
      const text = message.content
        .filter((part): part is { type: "text"; text: string } => part.type === "text")
        .map((part) => part.text)
        .join("")
        .trim();
      if (!text) return;
      await runTurn(text, turns);
    },
    onCancel: async () => {
      stop();
    },
  });

  const recentThreads: RecentThread[] = useMemo(
    () =>
      recent.map((c) => ({
        id: c.id,
        title: c.title || conversationTitle(c.turns),
        updatedAt: c.updatedAt,
        active: c.id === conversationId,
      })),
    [recent, conversationId],
  );

  return {
    runtime,
    // Surface state for the view shell.
    started: turns.length > 0,
    busy,
    error,
    fileBusyId,
    fileNotices,
    lastReplyId,
    recentThreads,
    // Actions.
    retry,
    stop,
    newChat,
    resumeConversation,
    clearAll,
    fileIssue,
    canRetry: !busy && lastUserTextRef.current.length > 0 && turns.length > 0,
    // The view registers a composer text-restore callback so a turn that fails
    // on its first hop can put the person's text back into the composer.
    registerRestoreText: (fn: ((text: string) => void) | null) => {
      restoreTextRef.current = fn;
    },
  };
}
