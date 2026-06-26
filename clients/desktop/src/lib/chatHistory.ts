// Persisted multi-turn Ask history. Conversations survive reloads and app
// restarts so a person can close the window mid-thread and pick it back up.
//
// History model (v2): we keep the LAST 5 conversations as a bounded local list
// (operator-approved convenience history), each capped at MAX_PERSISTED_TURNS so
// the payload stays small. The durable artifacts (issues/specs) remain the real
// output; this list is only a "resume where I left off" convenience and never
// leaves the machine (localStorage, no cloud). The v1 single-conversation blob
// is migrated forward as the most-recent entry rather than dropped.
//
// Storage is best-effort: any failure (private mode, quota, a torn JSON blob
// from an older build) degrades to an empty history rather than throwing into
// the view.

import type { ComposeDraftFields, ConverseIntent } from "../types";

// v2 store. The legacy v1 key is read once (for migration) then superseded.
const STORAGE_KEY = "alfred.ask.history.v2";
const LEGACY_STORAGE_KEY = "alfred.ask.history.v1";
// Stable id for the migrated legacy v1 thread so it dedupes across loads.
const LEGACY_CONVERSATION_ID = "legacy-v1";

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
    // A pre-v2 thread is always older than any v2 chat; default a missing
    // stamp to 0 (oldest) so the legacy fold can never evict a real v2 chat.
    updatedAt: typeof data.updatedAt === "number" ? data.updatedAt : 0,
    title:
      typeof data.title === "string" && data.title.trim()
        ? data.title
        : conversationTitle(trimmed),
  };
}

// Read the legacy v1 single-conversation blob, if present, as one conversation
// so an upgrading user keeps their most recent thread. Returns null when there
// is nothing valid to migrate.
function readLegacyConversation(): PersistedConversation | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(LEGACY_STORAGE_KEY);
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
  return coerceConversation({
    // A stable id (not a fresh random one) so the legacy thread dedupes against
    // its own migrated v2 copy across loads instead of being re-folded each time.
    id: LEGACY_CONVERSATION_ID,
    draftId: data.draftId,
    draft: data.draft,
    turns: data.turns,
    updatedAt: typeof data.updatedAt === "number" ? data.updatedAt : 0,
  });
}

// Read and validate the raw v2 store ONLY (no legacy fold). This is the true
// persisted set of real conversations; saves and deletes write from this so the
// read-time legacy fold can never be persisted into v2 (and so evict a real
// chat). Never throws; a torn blob degrades to an empty list.
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

// Load the full v2 history (newest first). Never throws. On first run after the
// v1 -> v2 change, a legacy single-conversation blob is migrated in as the
// most-recent entry and the legacy key is left in place (read-only) so a
// downgrade is non-destructive; the next save writes only the v2 key.
export function loadConversations(): PersistedConversation[] {
  let conversations = readRawV2Conversations();

  // Fold the legacy v1 blob in only when there is ROOM under the cap, so the
  // legacy thread can never evict a real v2 chat (a downgrade could stamp it
  // recent). It shows when fewer than the cap of v2 chats exist; once v2 is
  // full, real chats take precedence. Dedupes by id; deleteConversation clears
  // the v1 key so a removed thread cannot return.
  const legacy = readLegacyConversation();
  if (
    legacy &&
    !conversations.some((c) => c.id === legacy.id) &&
    conversations.length < MAX_PERSISTED_CONVERSATIONS
  ) {
    conversations = [...conversations, legacy];
  }

  return conversations
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

  // Build the write list from the RAW v2 store, never the legacy-folded read:
  // the read-time legacy fold must never be persisted into v2 where it could
  // push a real chat off the end of the cap.
  //
  // A persisted legacy-v1 entry is only DEMOTABLE while it is still the pristine
  // migrated fold: the v1 blob still exists AND the entry has gained no turns
  // beyond it. Such a duplicate may be demoted behind real chats (it re-folds
  // from the v1 key at read time, so nothing is lost). But once the user has
  // CONTINUED the thread (more turns than the blob), or the v1 blob is gone, the
  // entry is the only copy of real conversation data, so it is treated as a
  // first-class chat the cap never silently drops. `entry` itself (a save of the
  // legacy thread, e.g. a continuation) is always real-priority.
  const legacyBlob = readLegacyConversation();
  const isPristineFold = (c: PersistedConversation): boolean =>
    c.id === LEGACY_CONVERSATION_ID &&
    legacyBlob != null &&
    c.turns.length <= legacyBlob.turns.length;
  const existing = readRawV2Conversations().filter((c) => c.id !== entry.id);
  const reals = [entry, ...existing.filter((c) => !isPristineFold(c))].sort(
    (a, b) => b.updatedAt - a.updatedAt,
  );
  const demotable = existing.filter(isPristineFold);
  // Reals first (newest wins the cap), then any pristine legacy fold fills
  // leftover slots only, so a redundant fold never displaces a real chat.
  const next = [...reals, ...demotable].slice(0, MAX_PERSISTED_CONVERSATIONS);

  const payload: PersistedHistoryV2 = { version: 2, conversations: next };
  let saved = false;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    saved = true;
  } catch {
    // best-effort: storage blocked or full
  }

  // Saving the legacy thread itself migrates it forward into v2, so the v1 blob
  // is now redundant: clear it. Without this, a continued legacy thread that
  // later ages off the cap (as any old chat can) would leave the stale one-turn
  // v1 blob behind, and the read-time fold would resurrect that stale version.
  // Only clear it once the v2 write actually SUCCEEDED: if setItem threw (quota
  // or private mode) the v1 blob is still the only copy, so removing it would
  // lose the conversation entirely.
  if (saved && entry.id === LEGACY_CONVERSATION_ID) {
    try {
      window.localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      // best-effort
    }
  }
}

// Drop one conversation by id from the persisted list. Used when a person
// clears the active thread (New chat) so it does not linger in the switcher.
export function deleteConversation(id: string): void {
  // Operate on the RAW v2 store so a delete never persists the legacy fold.
  const next = readRawV2Conversations().filter((c) => c.id !== id);
  // If the deleted thread is the migrated legacy v1 blob, drop the read-only v1
  // key too, otherwise loadConversations would re-fold it on the next load.
  const legacy = readLegacyConversation();
  if (legacy && legacy.id === id) {
    try {
      window.localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      // best-effort
    }
  }
  const payload: PersistedHistoryV2 = { version: 2, conversations: next };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // best-effort
  }
}

// Drop the entire persisted history (every conversation). Best-effort. Also
// removes the legacy v1 key so a full clear leaves nothing behind.
export function clearConversations(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // best-effort
  }
  try {
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // best-effort
  }
}

// Backwards-compatible alias: the old single-conversation API named this
// clearConversation. Kept so callers that meant "drop the active thread" keep
// working; with the last-5 list this clears everything, matching the prior
// single-conversation semantics of "new chat wipes the store".
export function clearConversation(): void {
  clearConversations();
}
