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

  it("clears the entire history", () => {
    saveConversation({ id: "x", turns: [messageTurn("user", "hi")] });

    clearConversations();
    expect(loadConversations()).toEqual([]);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
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
