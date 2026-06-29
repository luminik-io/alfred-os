// Persisted multi-turn Ask history. Conversations survive reloads and app
// restarts so a person can close the window mid-thread and pick it back up.
//
// History model (v2): we keep the LAST 5 conversations as a bounded local list
// (operator-approved convenience history), each capped at MAX_PERSISTED_TURNS so
// the payload stays small. The durable artifacts (issues/specs) remain the real
// output; this list is only a "resume where I left off" convenience and never
// leaves the machine (localStorage, no cloud).
//
// Storage is best-effort: any failure (private mode, quota, or torn JSON)
// degrades to an empty history rather than throwing into the view.

import type { ComposeDraftFields, ConverseIntent } from "../types";

const STORAGE_KEY = "alfred.ask.history.v2";

// The most conversations we keep on disk. Older ones fall off the end.
export const MAX_PERSISTED_CONVERSATIONS = 5;

// The most turns we keep per conversation. A long thread still renders fully
// in-session; this only bounds what we persist so the blob never grows without
// limit.
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

// One persisted conversation. `id` is a stable local key so the recent-threads
// switcher can resume a specific entry; `updatedAt` orders the list (newest
// first) and `title` is a short, human label derived from the first user turn.
export type PersistedConversation = {
  id: string;
  // The live draft id the composer keeps refining, if any.
  draftId?: string;
  // The accumulating structured draft, carried so a reloaded thread can still
  // file the plan without re-deriving it from scratch.
  draft?: ComposeDraftFields;
  turns: PersistedTurn[];
  updatedAt: number;
  // A short label for the recent-threads switcher (first user turn, trimmed).
  title: string;
};

// The on-disk v2 envelope: a versioned, newest-first list of conversations.
type PersistedHistoryV2 = {
  version: 2;
  conversations: PersistedConversation[];
};

// A new-conversation save: everything except the bookkeeping the store owns
// (id/updatedAt/title are derived or carried by the caller).
export type ConversationDraft = {
  id: string;
  draftId?: string;
  draft?: ComposeDraftFields;
  turns: PersistedTurn[];
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

// Derive a short, human label for the recent-threads switcher from the first
// user turn. Falls back to a neutral label when a conversation somehow has no
// user text (e.g. a control-command-only thread).
export function conversationTitle(turns: PersistedTurn[]): string {
  for (const turn of turns) {
    if (turn.kind === "message" && turn.role === "user") {
      const text = turn.content.trim().replace(/\s+/g, " ");
      if (text) return text.length > 60 ? `${text.slice(0, 57)}...` : text;
    }
  }
  return "New chat";
}

// A best-effort unique id for a conversation. Prefers the platform UUID; falls
// back to a timestamp + random suffix when crypto is unavailable (older webview
// or a test env), which is unique enough for a local, bounded 5-entry list.
export function newConversationId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return uuid;
  } catch {
    // fall through to the timestamp form
  }
  return `c-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function coerceConversation(value: unknown): PersistedConversation | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if (!Array.isArray(data.turns)) return null;
  const turns = data.turns.filter(isPersistedTurn);
  if (!turns.length) return null;
  const trimmed = turns.slice(-MAX_PERSISTED_TURNS);
  return {
    id: typeof data.id === "string" && data.id ? data.id : newConversationId(),
    draftId: typeof data.draftId === "string" ? data.draftId : undefined,
    draft: (data.draft as ComposeDraftFields | undefined) ?? undefined,
    turns: trimmed,
    updatedAt: typeof data.updatedAt === "number" ? data.updatedAt : 0,
    title:
      typeof data.title === "string" && data.title.trim()
        ? data.title
        : conversationTitle(trimmed),
  };
}

// Read and validate the v2 store. Never throws; a torn blob degrades to an empty
// list.
function readRawV2Conversations(): PersistedConversation[] {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return [];
  }
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      const data = parsed as Record<string, unknown>;
      if (data.version === 2 && Array.isArray(data.conversations)) {
        return data.conversations
          .map(coerceConversation)
          .filter((c): c is PersistedConversation => c !== null);
      }
    }
  } catch {
    return [];
  }
  return [];
}

// Load the full v2 history (newest first). Never throws.
export function loadConversations(): PersistedConversation[] {
  return readRawV2Conversations()
    .slice()
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_PERSISTED_CONVERSATIONS);
}

// The single most-recent conversation, or null. The view rehydrates this on
// open so a closed window picks back up where it left off.
export function loadConversation(): PersistedConversation | null {
  const all = loadConversations();
  return all.length ? all[0] : null;
}

// Persist one conversation, upserting it by id into the bounded last-5 list and
// trimming its turns. Best-effort: a quota or serialization error is swallowed
// so saving can never break a send. The saved conversation becomes the most
// recent (it sorts to the front by updatedAt).
export function saveConversation(conversation: ConversationDraft): void {
  const turns = conversation.turns.slice(-MAX_PERSISTED_TURNS);
  if (!turns.length) {
    // An empty conversation is a no-op: nothing worth keeping yet.
    return;
  }
  const entry: PersistedConversation = {
    id: conversation.id,
    draftId: conversation.draftId,
    draft: conversation.draft,
    turns,
    updatedAt: Date.now(),
    title: conversationTitle(turns),
  };

  const existing = readRawV2Conversations().filter((c) => c.id !== entry.id);
  const next = [entry, ...existing]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_PERSISTED_CONVERSATIONS);

  const payload: PersistedHistoryV2 = { version: 2, conversations: next };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // best-effort: storage blocked or full
  }
}

// Drop one conversation by id from the persisted list. Used when a person
// clears the active thread (New chat) so it does not linger in the switcher.
export function deleteConversation(id: string): void {
  const next = readRawV2Conversations().filter((c) => c.id !== id);
  const payload: PersistedHistoryV2 = { version: 2, conversations: next };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // best-effort
  }
}

// Drop the entire persisted history (every conversation). Best-effort.
export function clearConversations(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // best-effort
  }
}
