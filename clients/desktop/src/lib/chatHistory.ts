// Persisted multi-turn Ask history. The conversation survives reloads and app
// restarts so a person can close the window mid-thread and pick it back up. We
// keep a single most-recent conversation (Ask is a focused surface, not a chat
// archive) plus a cap on stored turns so the payload stays small. Storage is
// best-effort: any failure (private mode, quota, a torn JSON blob from an older
// build) degrades to an empty history rather than throwing into the view.

import type { ComposeDraftFields, ConverseIntent } from "../types";

const STORAGE_KEY = "alfred.ask.history.v1";

// The most turns we keep on disk. A long thread still renders fully in-session;
// this only bounds what we persist so the blob never grows without limit.
const MAX_PERSISTED_TURNS = 100;

// A persisted message turn. Mirrors the in-view MessageTurn but is intentionally
// plain data (no `pending`, which is transient): we never persist an in-flight
// streaming turn.
export type PersistedMessageTurn = {
  kind: "message";
  role: "user" | "assistant";
  content: string;
  intent?: ConverseIntent;
};

// A persisted draft/plan card. Carries just enough to re-render the inline
// lifecycle card offer; the live draft itself is reloaded by id on the server
// when the person refines it.
export type PersistedDraftTurn = {
  kind: "draft";
  role: "assistant";
  draft: {
    draftId: string;
    title: string;
    repos: string[];
    ready: boolean;
    questions: string[];
  };
};

export type PersistedTurn = PersistedMessageTurn | PersistedDraftTurn;

export type PersistedConversation = {
  // Schema version, so a future shape change can be migrated or discarded
  // instead of crashing the view.
  version: 1;
  // The live draft id the composer keeps refining, if any.
  draftId?: string;
  // The accumulating structured draft, carried so a reloaded thread can still
  // file the plan without re-deriving it from scratch.
  draft?: ComposeDraftFields;
  turns: PersistedTurn[];
  updatedAt: number;
};

function isPersistedTurn(value: unknown): value is PersistedTurn {
  if (!value || typeof value !== "object") return false;
  const turn = value as Record<string, unknown>;
  if (turn.kind === "message") {
    return (
      (turn.role === "user" || turn.role === "assistant") &&
      typeof turn.content === "string"
    );
  }
  if (turn.kind === "draft") {
    const draft = turn.draft as Record<string, unknown> | undefined;
    return Boolean(
      draft &&
        typeof draft.draftId === "string" &&
        typeof draft.title === "string" &&
        Array.isArray(draft.repos),
    );
  }
  return false;
}

// Load the persisted conversation, or null when there is nothing valid to
// restore. Never throws: a missing, blocked, or malformed store reads as empty.
export function loadConversation(): PersistedConversation | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const data = parsed as Record<string, unknown>;
  if (data.version !== 1 || !Array.isArray(data.turns)) return null;
  const turns = data.turns.filter(isPersistedTurn);
  if (!turns.length) return null;
  return {
    version: 1,
    draftId: typeof data.draftId === "string" ? data.draftId : undefined,
    draft: (data.draft as ComposeDraftFields | undefined) ?? undefined,
    turns,
    updatedAt: typeof data.updatedAt === "number" ? data.updatedAt : Date.now(),
  };
}

// Persist the conversation, trimming to the most recent turns. Best-effort: a
// quota or serialization error is swallowed so saving can never break a send.
export function saveConversation(
  conversation: Omit<PersistedConversation, "version" | "updatedAt">,
): void {
  const turns = conversation.turns.slice(-MAX_PERSISTED_TURNS);
  const payload: PersistedConversation = {
    version: 1,
    draftId: conversation.draftId,
    draft: conversation.draft,
    turns,
    updatedAt: Date.now(),
  };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // best-effort: storage blocked or full
  }
}

// Drop the persisted conversation (new chat). Best-effort.
export function clearConversation(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // best-effort
  }
}
