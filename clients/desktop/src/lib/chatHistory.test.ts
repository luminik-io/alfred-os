import { beforeEach, describe, expect, it } from "vitest";

import {
  MAX_PERSISTED_CONVERSATIONS,
  clearConversations,
  conversationTitle,
  deleteConversation,
  loadConversation,
  loadConversations,
  newConversationId,
  saveConversation,
  type PersistedTurn,
} from "./chatHistory";

const STORAGE_KEY = "alfred.ask.history.v2";
const LEGACY_STORAGE_KEY = "alfred.ask.history.v1";

function messageTurn(role: "user" | "assistant", content: string): PersistedTurn {
  return { kind: "message", role, content };
}

function draftTurn(draftId: string, title: string): PersistedTurn {
  return {
    kind: "draft",
    role: "assistant",
    draft: { draftId, title, repos: ["your-org/frontend"], ready: true, questions: [] },
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("chatHistory last-5 persistence", () => {
  it("saves a conversation and reads it back as the most recent", () => {
    const id = newConversationId();
    saveConversation({
      id,
      draftId: "compose-1",
      draft: undefined,
      turns: [messageTurn("user", "Add a CSV export"), messageTurn("assistant", "Which repo?")],
    });

    const latest = loadConversation();
    expect(latest).not.toBeNull();
    expect(latest?.id).toBe(id);
    expect(latest?.draftId).toBe("compose-1");
    expect(latest?.turns).toHaveLength(2);
    // The title is derived from the first user turn.
    expect(latest?.title).toBe("Add a CSV export");
  });

  it("caps the stored history at the last 5 conversations, newest first", () => {
    const ids: string[] = [];
    for (let i = 0; i < 8; i += 1) {
      const id = `conv-${i}`;
      ids.push(id);
      saveConversation({ id, turns: [messageTurn("user", `request ${i}`)] });
    }

    const all = loadConversations();
    expect(all).toHaveLength(MAX_PERSISTED_CONVERSATIONS);
    // The five most-recent ids survive; the three oldest fell off the end.
    expect(all.map((c) => c.id)).toEqual(["conv-7", "conv-6", "conv-5", "conv-4", "conv-3"]);
    // Persisted blob itself is bounded too.
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) as string) as {
      conversations: unknown[];
    };
    expect(raw.conversations).toHaveLength(MAX_PERSISTED_CONVERSATIONS);
  });

  it("upserts an existing conversation by id rather than duplicating it", () => {
    const id = newConversationId();
    saveConversation({ id, turns: [messageTurn("user", "first")] });
    saveConversation({
      id,
      turns: [messageTurn("user", "first"), messageTurn("assistant", "reply"), draftTurn("d1", "Plan")],
    });

    const all = loadConversations();
    expect(all).toHaveLength(1);
    expect(all[0].id).toBe(id);
    expect(all[0].turns).toHaveLength(3);
  });

  it("resumes any of the stored conversations by id", () => {
    saveConversation({ id: "a", turns: [messageTurn("user", "alpha question")] });
    saveConversation({ id: "b", turns: [messageTurn("user", "beta question")] });

    const all = loadConversations();
    const beta = all.find((c) => c.id === "b");
    const alpha = all.find((c) => c.id === "a");
    expect(beta?.turns[0]).toMatchObject({ content: "beta question" });
    expect(alpha?.turns[0]).toMatchObject({ content: "alpha question" });
  });

  it("migrates the legacy v1 single-conversation blob forward as the most recent", () => {
    // Seed a v1 payload as an older build would have written it.
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        draftId: "legacy-draft",
        draft: { title: "Legacy plan", repos: ["your-org/frontend"] },
        turns: [messageTurn("user", "legacy request"), messageTurn("assistant", "legacy reply")],
        updatedAt: 1000,
      }),
    );
    // No v2 store yet.
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();

    const latest = loadConversation();
    expect(latest).not.toBeNull();
    expect(latest?.draftId).toBe("legacy-draft");
    expect(latest?.title).toBe("legacy request");
    expect(latest?.turns).toHaveLength(2);

    // Saving a new conversation keeps the migrated legacy entry in the list.
    saveConversation({ id: "new", turns: [messageTurn("user", "new request")] });
    const all = loadConversations();
    expect(all.map((c) => c.turns[0]).map((t) => (t.kind === "message" ? t.content : ""))).toContain(
      "legacy request",
    );
  });

  it("folds the legacy v1 thread into Recent even when v2 already has chats", () => {
    // A v2 store already has a conversation...
    saveConversation({ id: "v2-a", turns: [messageTurn("user", "v2 request")] });
    // ...and a valid legacy v1 blob still exists alongside it.
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        draftId: "legacy-draft",
        turns: [messageTurn("user", "legacy request")],
        updatedAt: 500,
      }),
    );

    const titles = loadConversations().map((c) => c.title);
    expect(titles).toContain("v2 request");
    expect(titles).toContain("legacy request");
  });

  it("does not duplicate the legacy thread across repeated loads", () => {
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({ version: 1, turns: [messageTurn("user", "legacy request")], updatedAt: 500 }),
    );
    // Loading twice (stable legacy id) must not accumulate copies.
    loadConversations();
    const second = loadConversations();
    expect(second.filter((c) => c.title === "legacy request")).toHaveLength(1);
  });

  it("a stampless legacy thread does not evict a full set of v2 chats", () => {
    for (let i = 0; i < 5; i += 1) {
      saveConversation({ id: `v2-${i}`, turns: [messageTurn("user", `v2 ${i}`)] });
    }
    // A legacy v1 blob with NO updatedAt (the eviction case).
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({ version: 1, turns: [messageTurn("user", "legacy")] }),
    );
    const all = loadConversations();
    // Still capped at 5, and every real v2 chat survives (legacy did not evict).
    expect(all).toHaveLength(MAX_PERSISTED_CONVERSATIONS);
    expect(all.every((c) => c.title.startsWith("v2 "))).toBe(true);
  });

  it("a timestamped legacy thread cannot evict a real v2 chat on save", () => {
    // Four real v2 chats...
    for (let i = 0; i < 4; i += 1) {
      saveConversation({ id: `v2-${i}`, turns: [messageTurn("user", `v2 ${i}`)] });
    }
    // ...and a legacy v1 blob with a RECENT stamp (the eviction case: a fold
    // that would sort ahead of a real chat under the cap).
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        turns: [messageTurn("user", "legacy request")],
        updatedAt: 9_999_999_999_999,
      }),
    );
    // Saving a fifth real chat must keep all five real chats; the legacy fold
    // is read-only and must never be persisted into v2 to push one off.
    saveConversation({ id: "v2-4", turns: [messageTurn("user", "v2 4")] });
    const all = loadConversations();
    expect(all).toHaveLength(MAX_PERSISTED_CONVERSATIONS);
    expect(all.every((c) => c.title.startsWith("v2 "))).toBe(true);
    expect(all.map((c) => c.id).sort()).toEqual(["v2-0", "v2-1", "v2-2", "v2-3", "v2-4"]);
  });

  it("clears the v1 key when the migrated legacy thread is deleted so it cannot return", () => {
    window.localStorage.setItem(
      LEGACY_STORAGE_KEY,
      JSON.stringify({ version: 1, turns: [messageTurn("user", "legacy request")], updatedAt: 500 }),
    );
    const legacy = loadConversations().find((c) => c.title === "legacy request");
    expect(legacy).toBeDefined();

    deleteConversation(legacy!.id);

    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
    expect(loadConversations().some((c) => c.title === "legacy request")).toBe(false);
  });

  it("ignores a torn or malformed v2 blob and degrades to empty", () => {
    window.localStorage.setItem(STORAGE_KEY, "{ not json");
    expect(loadConversations()).toEqual([]);
    expect(loadConversation()).toBeNull();
  });

  it("deletes a single conversation by id", () => {
    saveConversation({ id: "keep", turns: [messageTurn("user", "keep me")] });
    saveConversation({ id: "drop", turns: [messageTurn("user", "drop me")] });

    deleteConversation("drop");
    const all = loadConversations();
    expect(all.map((c) => c.id)).toEqual(["keep"]);
  });

  it("clears the entire history including the legacy key", () => {
    window.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify({ version: 1, turns: [] }));
    saveConversation({ id: "x", turns: [messageTurn("user", "hi")] });

    clearConversations();
    expect(loadConversations()).toEqual([]);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
  });

  it("does not persist an empty conversation", () => {
    saveConversation({ id: "empty", turns: [] });
    expect(loadConversations()).toEqual([]);
  });

  it("derives a trimmed title from the first user turn", () => {
    const longText = "x".repeat(120);
    expect(conversationTitle([messageTurn("user", longText)]).length).toBeLessThanOrEqual(60);
    expect(conversationTitle([draftTurn("d", "no user turn")])).toBe("New chat");
  });

  it("caps persisted turns per conversation", () => {
    const many: PersistedTurn[] = Array.from({ length: 250 }, (_unused, i) =>
      messageTurn(i % 2 === 0 ? "user" : "assistant", `turn ${i}`),
    );
    saveConversation({ id: "long", turns: many });
    const latest = loadConversation();
    // The per-conversation cap (100) bounds what is stored.
    expect(latest?.turns.length).toBeLessThanOrEqual(100);
  });
});
