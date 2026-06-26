// Shared model types and pure helpers for the Ask surface. Kept apart from the
// React hook and the assistant-ui wiring so they are easy to reason about and
// unit-test in isolation: they translate between Alfred's converse/draft
// responses, the in-view ChatTurn shape, and the plain persisted form.

import type { ComposeDraftResponse, ConverseResponse } from "../../types";
import type { PersistedTurn } from "../../lib/chatHistory";

// The inline draft/plan card model. Carries just enough to render the lifecycle
// card offer and file the plan by id.
export type DraftCardModel = {
  draftId: string;
  title: string;
  repos: string[];
  ready: boolean;
  questions: string[];
};

// One in-view turn. A message turn is a chat bubble (user or assistant); a
// draft turn renders the inline lifecycle card. `pending` marks an assistant
// message whose tokens are still streaming in (never persisted).
export type MessageTurn = {
  kind: "message";
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
};

export type DraftTurn = {
  kind: "draft";
  role: "assistant";
  draft: DraftCardModel;
};

export type ChatTurn = MessageTurn | DraftTurn;

// The custom assistant-ui message part that carries an Alfred draft card. We
// render the draft as a tool-call part (assistant-ui's first-class extension
// point for custom in-thread content) named "alfred-draft"; a custom tools
// component renders it as the lifecycle card.
export const DRAFT_TOOL_NAME = "alfred-draft";

export function cleanRepos(repos: string[] | undefined): string[] {
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

// A converse turn is conversational (just a chat answer) unless the server
// marks it "build". Servers that predate intent omit the field; we treat those
// as build so the plan surface never silently disappears. ComposeDraftResponse
// (the no-engine one-shot fallback) is always a plan, so it has no intent.
export function isConversationTurn(
  result: ConverseResponse | ComposeDraftResponse,
): boolean {
  return "intent" in result && result.intent === "conversation";
}

// Whether a draft carries enough to be worth showing as a plan card. A bare
// title with nothing else is not a plan yet; we only surface the card once the
// turn has actually started building something.
export function draftHasSubstance(
  draft: ConverseResponse["draft"] | undefined,
): boolean {
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
export function draftFallbackReply(draft: ComposeDraftResponse): string {
  const summary =
    (draft.summary || "").trim() ||
    "I saved a plan. Review it below, then file it as an issue when you are ready.";
  const questions = (draft.questions || [])
    .filter((q) => Boolean(q && q.trim()))
    .slice(0, 4);
  if (!questions.length) return summary;
  const list = questions.map((q) => `- ${q.trim()}`).join("\n");
  return `${summary}\n\nTo firm it up:\n\n${list}`;
}

export function draftCardFrom(
  result: ConverseResponse | ComposeDraftResponse,
): DraftCardModel {
  // ConverseResponse uses readiness.ready; ComposeDraftResponse uses
  // readiness.ok and questions[]. Normalize both into the inline card model.
  const ready =
    "ready" in result.readiness ? result.readiness.ready : result.readiness.ok;
  const questions =
    "missing" in result.readiness
      ? result.readiness.missing
      : (result as ComposeDraftResponse).questions || [];
  return {
    draftId: result.draft_id,
    title:
      result.draft.title ||
      ("title" in result ? (result as ComposeDraftResponse).title : "New request"),
    repos: cleanRepos(result.draft.repos),
    ready: Boolean(ready),
    questions: questions || [],
  };
}

// Translate between the in-view ChatTurn shape and the plain persisted form.
export function toPersistedTurn(turn: ChatTurn): PersistedTurn {
  if (turn.kind === "draft") {
    return { kind: "draft", role: "assistant", draft: turn.draft };
  }
  return { kind: "message", role: turn.role, content: turn.content };
}

export function fromPersistedTurn(turn: PersistedTurn): ChatTurn {
  if (turn.kind === "draft") {
    return { kind: "draft", role: "assistant", draft: turn.draft };
  }
  return { kind: "message", role: turn.role, content: turn.content };
}

export function lastUserTextOf(turns: ChatTurn[]): string {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    if (turn.kind === "message" && turn.role === "user") return turn.content;
  }
  return "";
}

// Trim the transcript back to (but not including) the most recent user turn,
// returning that user's text plus the turns that preceded it. Replaying that
// text reruns the turn, so retry/regenerate drops the stale assistant reply
// (and any draft card it produced) and asks again.
export function trimToLastUser(
  turns: ChatTurn[],
  fallback: string,
): { text: string; baseTurns: ChatTurn[] } {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    if (turn.kind === "message" && turn.role === "user") {
      return { text: turn.content, baseTurns: turns.slice(0, i) };
    }
  }
  return { text: fallback, baseTurns: [] };
}

// A plain, recoverable message for a dropped or failed stream. Keeps the cause
// out of the person's way while making clear the turn can be retried.
export function reconnectMessage(err: unknown): string {
  const detail = err instanceof Error ? err.message : "";
  if (/abort/i.test(detail)) {
    return "That reply was interrupted. Retry to ask again.";
  }
  return "The connection to Alfred dropped before the reply finished. Retry to ask again.";
}
