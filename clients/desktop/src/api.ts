import { invoke } from "@tauri-apps/api/core";

import type {
  ActionsResponse,
  AssignmentTargetAgent,
  ComposeDraftRequest,
  ComposeDraftResponse,
  ConversationControlRequest,
  ConversationControlResponse,
  ConverseRequest,
  ConverseResponse,
  DiscardPlanResponse,
  FilePlanIssueResponse,
  FiringRecord,
  FiringsResponse,
  FollowupActionResponse,
  MemoryCandidateActionResponse,
  MemoryCandidatesResponse,
  MemoryLessonsResponse,
  NativeAction,
  NativeCommandResult,
  PlanDecision,
  PlanDecisionResponse,
  PlansResponse,
  QueueAction,
  QueueActionResponse,
  ScheduleResponse,
  SetupDemoResponse,
  SetupPlaybookComposeResponse,
  SetupPlaybooksResponse,
  SetupReposResponse,
  SetupSelectReposResponse,
  SetupStatus,
  ShippedBoard,
  Snapshot,
  StatusResponse,
  TrustedSlackUsersResponse,
  UsageResponse,
} from "./types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:7010";
const BASE_URL_KEY = "alfred-desktop.base-url";
const DEV_BASE_URL = import.meta.env.VITE_ALFRED_BASE_URL?.trim();

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

// An error that carries a plain-language `message` for the UI plus the raw
// `detail` string (status line, stderr, stack) so panels can hide the technical
// text behind a "Details" disclosure instead of leading with it.
export class ApiError extends Error {
  readonly detail: string | null;
  constructor(message: string, detail: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.detail = detail;
  }
}

// Pull the raw technical text out of any thrown value for the Details panel.
export function errorDetail(err: unknown): string | null {
  if (err instanceof ApiError) {
    return err.detail;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return null;
}

// Map an HTTP error status to plain-language guidance. Returns the operator-
// facing message; the raw status line + body stay available on ApiError.detail.
function humanizeFetchError(status: number, serverMessage?: string | null): string {
  const genericAuthBody =
    status === 401 || status === 403
      ? ["forbidden", "unauthorized", "auth required"].includes(
          (serverMessage || "").trim().toLowerCase(),
        )
      : false;
  if (serverMessage && status >= 400 && status < 500 && !genericAuthBody) {
    return serverMessage;
  }
  if (status === 401 || status === 403) {
    if (!isTauri()) {
      return "This action needs the Alfred desktop app so it can attach the launch token. Open the desktop app, then retry.";
    }
    return "Alfred serve is running but rejected this client (auth token mismatch). Restart the runtime or check your token.";
  }
  if (status === 404) {
    return "Alfred serve answered, but this endpoint is missing. Restart the runtime and check the local server logs.";
  }
  if (status === 502 || status === 503 || status === 504) {
    return "Alfred serve is reachable but not ready yet. Give the runtime a moment, then refresh.";
  }
  if (status >= 500) {
    return "Alfred serve hit an internal error handling this request. Check the runtime logs.";
  }
  return `Alfred serve returned an unexpected ${status} response. See details below.`;
}

function serverErrorMessage(text: string): string | null {
  if (!text.trim()) {
    return null;
  }
  try {
    const payload = JSON.parse(text) as unknown;
    if (payload && typeof payload === "object") {
      const record = payload as Record<string, unknown>;
      for (const key of ["error", "message", "detail"]) {
        const value = record[key];
        if (typeof value === "string" && value.trim()) {
          return value.trim();
        }
      }
    }
  } catch {
    return null;
  }
  return null;
}

// Map a transport-level failure (no HTTP response at all) to plain language.
function humanizeTransportError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  const lower = raw.toLowerCase();
  if (
    lower.includes("connection refused") ||
    lower.includes("econnrefused") ||
    lower.includes("failed to fetch") ||
    lower.includes("load failed") ||
    lower.includes("networkerror")
  ) {
    return "Could not reach Alfred serve. Start the runtime, or point this client at the URL where alfred serve is listening.";
  }
  if (lower.includes("timeout") || lower.includes("timed out")) {
    return "Alfred serve did not respond in time. The runtime may be busy or stuck; check it, then refresh.";
  }
  return raw;
}

export function clientBaseUrl(value?: string | null): string {
  const trimmed = value?.trim() || DEFAULT_BASE_URL;
  if (shouldNormalizeDevPreviewBaseUrl(trimmed)) {
    return DEFAULT_BASE_URL;
  }
  return trimmed;
}

export function initialBaseUrl(): string {
  return clientBaseUrl(DEV_BASE_URL || window.localStorage.getItem(BASE_URL_KEY));
}

export function rememberBaseUrl(value: string): void {
  window.localStorage.setItem(BASE_URL_KEY, clientBaseUrl(value));
}

// True once Alfred has successfully connected at least once on this machine.
// `rememberBaseUrl` persists the connected URL, so a stored value is a durable
// proxy for "this is a returning user" that survives app restarts. Used to keep
// first-run onboarding from re-firing on every cold start for established users.
export function hasStoredBaseUrl(): boolean {
  return Boolean(window.localStorage.getItem(BASE_URL_KEY));
}

// The dashboard reads several independent endpoints. A failure on any one of them
// should not blank the whole view, so we settle each request and render what
// resolved, marking the missing sections as degraded. /api/status is the spine
// (it carries fleet liveness and the reliability rollup): if it fails the whole
// snapshot is genuinely unusable, so that one rejection still surfaces as the
// connection error the banner shows.
export async function loadSnapshot(baseUrl: string): Promise<Snapshot> {
  const [status, actions, memoryCandidates, memoryLessons, firings, plans, trustedSlack, schedule] =
    await Promise.allSettled([
      readAlfredJson<StatusResponse>(baseUrl, "/api/status"),
      readAlfredJson<ActionsResponse>(baseUrl, "/api/actions"),
      readAlfredJson<MemoryCandidatesResponse>(baseUrl, "/api/memory/candidates?limit=20"),
      readAlfredJson<MemoryLessonsResponse>(baseUrl, "/api/memory/lessons?limit=30"),
      readAlfredJson<FiringsResponse>(baseUrl, "/api/firings?limit=14"),
      readAlfredJson<PlansResponse>(baseUrl, "/api/plans?limit=14"),
      readAlfredJson<TrustedSlackUsersResponse>(baseUrl, "/api/slack/trusted-users"),
      readAlfredJson<ScheduleResponse>(baseUrl, "/api/schedule"),
    ]);

  if (status.status === "rejected") {
    throw status.reason instanceof Error ? status.reason : new Error(String(status.reason));
  }

  const degraded: NonNullable<Snapshot["degraded"]> = {};
  if (actions.status === "rejected") degraded.actions = settledError(actions.reason);
  if (memoryCandidates.status === "rejected") {
    degraded.memoryCandidates = settledError(memoryCandidates.reason);
  }
  if (firings.status === "rejected") degraded.firings = settledError(firings.reason);
  if (plans.status === "rejected") degraded.plans = settledError(plans.reason);
  if (trustedSlack.status === "rejected") degraded.trustedSlack = settledError(trustedSlack.reason);
  if (schedule.status === "rejected") degraded.schedule = settledError(schedule.reason);

  return {
    loadedAt: new Date(),
    status: status.value,
    actions:
      actions.status === "fulfilled"
        ? actions.value
        : {
            status: "degraded",
            actions: [],
            failure_patterns: [],
            stale_workers: [],
            promotion_suggestions: [],
          },
    memoryCandidates:
      memoryCandidates.status === "fulfilled"
        ? { rows: memoryCandidates.value.rows || [], error: memoryCandidates.value.error }
        : { rows: [] },
    memoryLessons:
      memoryLessons.status === "fulfilled"
        ? { rows: memoryLessons.value.rows || [], error: memoryLessons.value.error }
        : { rows: [] },
    firings: firings.status === "fulfilled" ? firings.value.rows || [] : [],
    plans: plans.status === "fulfilled" ? plans.value.rows || [] : [],
    trustedSlack: trustedSlack.status === "fulfilled" ? trustedSlack.value : null,
    // Upcoming scheduled runs (agents.conf). A rejection degrades to an empty
    // lane, never a blanked view.
    schedule: schedule.status === "fulfilled" ? schedule.value.runs || [] : [],
    // The Kanban board is fetched separately (loadShipped) so its slower
    // multi-repo gh scan never gates the core snapshot.
    shipped: null,
    degraded: Object.keys(degraded).length ? degraded : undefined,
  };
}

// The Kanban board scans many repos via gh and is the page's centerpiece, so it
// is fetched on its own (decoupled from loadSnapshot) with a generous timeout
// and its own loading/error state. The rest of the dashboard never waits on it.
export async function loadShipped(
  baseUrl: string,
  days = 14,
  options: { demo?: boolean } = {},
): Promise<ShippedBoard> {
  const params = new URLSearchParams({ days: String(days) });
  if (options.demo) params.set("demo", "1");
  const board = await withTimeout(
    readAlfredJson<ShippedBoard>(baseUrl, `/api/shipped?${params.toString()}`),
    20000,
    "/api/shipped",
  );
  return normalizeShippedBoard(board);
}

// Real subscription-usage headroom from GET /api/usage. The server reads local
// Claude/Codex logs with a bounded native reader, so it is fetched separately
// and never gates the core snapshot.
export async function loadUsage(baseUrl: string): Promise<UsageResponse> {
  return withTimeout(
    readAlfredJson<UsageResponse>(baseUrl, "/api/usage"),
    12000,
    "/api/usage",
  );
}

// Fetch one agent's own firing history. The Logs live tail uses this when a
// quieter agent has been pushed out of the limited global /api/firings feed, so
// "View logs" still surfaces real runs instead of an empty state.
export async function loadAgentFirings(
  baseUrl: string,
  codename: string,
  limit = 20,
): Promise<FiringRecord[]> {
  const params = new URLSearchParams({ codename, limit: String(limit) });
  const response = await readAlfredJson<FiringsResponse>(
    baseUrl,
    `/api/firings?${params.toString()}`,
  );
  return response.rows || [];
}

function settledError(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function normalizeShippedBoard(board: ShippedBoard): ShippedBoard {
  if (board.error) return board;
  const errors = board.errors || [];
  if (!errors.length) return board;
  const totalCards = board.counts.queued + board.counts.in_progress + board.counts.shipped;
  if (totalCards > 0) return board;
  const watchedRepos = new Set(board.repos.filter(Boolean));
  const erroredRepos = new Set(errors);
  if (
    !watchedRepos.size ||
    !Array.from(watchedRepos).every((repo) => erroredRepos.has(repo))
  ) {
    return board;
  }
  const shown = errors.slice(0, 3).join(", ");
  const more = errors.length > 3 ? `, +${errors.length - 3} more` : "";
  const repoLabel = errors.length === 1 ? "repo" : "repos";
  return {
    ...board,
    error: `GitHub data unavailable for ${errors.length} watched ${repoLabel}: ${shown}${more}`,
  };
}

// Bound a fetch so a slow optional endpoint cannot stall the snapshot batch.
// The underlying request is not aborted (it rides a Tauri invoke), but the
// client stops waiting and the caller treats the timeout as a rejection.
function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new ApiError(`${label} timed out after ${ms}ms`, "timeout")),
      ms,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

export async function convertFollowupToDraft(
  baseUrl: string,
  planId: string,
): Promise<FollowupActionResponse> {
  return writeAlfredJson(baseUrl, `/api/plans/${planPathSegment(planId)}/convert-followup`);
}

export async function markFollowupHandled(
  baseUrl: string,
  planId: string,
): Promise<FollowupActionResponse> {
  return writeAlfredJson(baseUrl, `/api/plans/${planPathSegment(planId)}/mark-handled`);
}

// Record a real go/no-go on a genuine Batman plan. The server writes the same
// `{issue_num}.approved` / `.rejected` marker Batman's file-poll fallback
// watches, so an approve here starts that exact scope and a decline stops it,
// with no Slack round-trip. Token-gated server-side via _authorized_mutation.
export async function decidePlan(
  baseUrl: string,
  planId: string,
  decision: PlanDecision,
  reason?: string,
): Promise<PlanDecisionResponse> {
  const body: { decision: PlanDecision; reason?: string } = { decision };
  if (reason && reason.trim()) body.reason = reason.trim();
  return writeAlfredJson(
    baseUrl,
    `/api/plans/${planPathSegment(planId)}/decision`,
    body,
  );
}

export async function filePlanIssue(
  baseUrl: string,
  planId: string,
): Promise<FilePlanIssueResponse> {
  return writeAlfredJson(baseUrl, `/api/plans/${planPathSegment(planId)}/file-issue`);
}

// The persisted roster theme shared across surfaces. The desktop reads this on
// connect so its picker reflects the choice the runtime already holds (which
// the Slack path also honors), and writes it back when the operator picks a
// theme or edits a custom name. `custom_names` / `custom_roles` carry the
// operator-authored `custom` theme; presets leave them empty. Mirrors
// `lib/roster_theme_store.py:RosterThemeState.to_dict`.
export type RosterThemeResponse = {
  theme: string;
  custom_names: Record<string, string>;
  custom_roles: Record<string, string>;
  updated_at: string | null;
};

export type RosterThemeWrite = {
  theme: string;
  custom_names?: Record<string, string>;
  custom_roles?: Record<string, string>;
};

// Read-only GET, no token: any surface may learn the active cast.
export async function loadRosterTheme(baseUrl: string): Promise<RosterThemeResponse> {
  return readAlfredJson<RosterThemeResponse>(baseUrl, "/api/roster-theme");
}

// Persist the chosen theme + custom name/role maps. Token-gated server-side via
// _authorized_mutation; the native bridge attaches the per-launch token.
export async function saveRosterTheme(
  baseUrl: string,
  body: RosterThemeWrite,
): Promise<RosterThemeResponse> {
  return writeAlfredJson<RosterThemeResponse>(baseUrl, "/api/roster-theme", body);
}

// Discard a local planning draft (issue 314). The server archives the draft
// JSON rather than hard-deleting it, and is idempotent, so a double click is
// safe. Token-gated server-side via _authorized_mutation.
export async function discardPlan(
  baseUrl: string,
  planId: string,
): Promise<DiscardPlanResponse> {
  return writeAlfredJson(baseUrl, `/api/plans/${planPathSegment(planId)}/discard`);
}

export async function composeDraft(
  baseUrl: string,
  request: ComposeDraftRequest,
): Promise<ComposeDraftResponse> {
  return writeAlfredJson(baseUrl, "/api/plans/draft", request);
}

export async function conversationControl(
  baseUrl: string,
  request: ConversationControlRequest,
  signal?: AbortSignal,
): Promise<ConversationControlResponse> {
  return writeAlfredJson(baseUrl, "/api/conversation/control", request, signal);
}

// One turn of the conversational, repo-grounded spec-builder. The server runs a
// single live interrogator turn and returns the reply + accumulating spec +
// readiness. When no live engine is configured the server returns a 503 with
// `error: "live_session_unavailable"`; the caller catches that (via
// isLiveSessionUnavailable) and degrades to the one-shot `composeDraft` form.
export async function composeConverse(
  baseUrl: string,
  request: ConverseRequest,
  signal?: AbortSignal,
): Promise<ConverseResponse> {
  return writeAlfredJson(baseUrl, "/api/compose/converse", request, signal);
}

// True when a thrown error is the server's "no live session" degrade signal, so
// Compose can quietly fall back to the one-shot rubric form instead of showing
// a scary error. Matches either the structured error code carried on the raw
// detail or the 503 status line.
export function isLiveSessionUnavailable(err: unknown): boolean {
  const detail = errorDetail(err);
  if (detail && detail.includes("live_session_unavailable")) {
    return true;
  }
  return Boolean(detail && /\b503\b/.test(detail));
}

// --------------------------------------------------------------------------- //
// Real-time streaming (#41 live log tail, #36 compose token stream)
//
// These ride the webview's own fetch / EventSource against the localhost server
// directly, NOT the buffered Tauri JSON bridge (the Rust `reqwest` path calls
// `.text()` and so cannot stream a body incrementally). The buffered helpers
// above stay the canonical path for every request/response endpoint; streaming
// is a progressive enhancement that always has a non-streaming fallback.
// --------------------------------------------------------------------------- //

// Resolve the URL for a streaming request. In dev/browser we go through the
// Vite `/alfred-api` proxy so the request is same-origin (and the proxy injects
// the Origin header the server's same-origin check wants); native and prod hit
// the localhost server directly.
function streamingUrl(baseUrl: string, path: string): string {
  const url = new URL(path, normalizedBaseUrl(baseUrl));
  if (!isLocalAlfredUrl(url)) {
    throw new ApiError(
      "Streaming is only available against a local Alfred runtime.",
      "streaming target must be http localhost, 127.0.0.1, or ::1",
    );
  }
  return shouldUseDevProxy(url) ? `/alfred-api${path}` : url.toString();
}

// One frame parsed out of a text/event-stream body.
type SseFrame = { event: string; data: unknown };

// A minimal SSE parser over a fetch ReadableStream. Yields one frame per
// `event:`/`data:` block. Used for the converse POST stream (EventSource is
// GET-only and cannot send the token header); the log tail uses EventSource
// directly since it is an open GET.
async function* readSseStream(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SseFrame> {
  const body = response.body;
  if (!body) {
    return;
  }
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      if (signal?.aborted) {
        return;
      }
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const frame = parseSseBlock(block);
        if (frame) {
          yield frame;
        }
        sep = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseBlock(block: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    }
  }
  if (!dataLines.length) {
    return null;
  }
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: dataLines.join("\n") };
  }
}

// Handlers for a live log tail. `onLines` fires with each new batch of whole
// transcript lines (raw JSONL strings); `onDone` fires once the firing ends or
// the stream closes; `onError` fires on a transport error so the caller can
// fall back to its existing poll. Returns a disposer that closes the stream.
export type LogTailHandlers = {
  onLines: (lines: string[]) => void;
  onDone?: (reason: string) => void;
  onError?: (err: unknown) => void;
};

// Live-tail a running firing's transcript over Server-Sent-Events (#41). This
// is an OPEN GET, so it rides EventSource directly with no token. The caller
// keeps its 60s firing poll as the fallback: if EventSource is unavailable or
// errors, `onError` fires and the caller simply leans on the poll. Returns a
// disposer; call it on unmount or when switching firings.
export function streamFiringTail(
  baseUrl: string,
  firingId: string,
  handlers: LogTailHandlers,
): () => void {
  if (typeof EventSource === "undefined") {
    handlers.onError?.(new Error("EventSource unavailable"));
    return () => {};
  }
  const url = streamingUrl(baseUrl, `/api/firings/${encodeURIComponent(firingId)}/tail`);
  let source: EventSource;
  try {
    source = new EventSource(url);
  } catch (err) {
    handlers.onError?.(err);
    return () => {};
  }
  let closed = false;
  const close = () => {
    if (!closed) {
      closed = true;
      source.close();
    }
  };
  source.addEventListener("append", (event) => {
    try {
      const payload = JSON.parse((event as MessageEvent).data) as { lines?: string[] };
      if (Array.isArray(payload.lines) && payload.lines.length) {
        handlers.onLines(payload.lines);
      }
    } catch {
      // A torn frame is harmless; the next append carries the lines whole.
    }
  });
  source.addEventListener("done", (event) => {
    let reason = "complete";
    try {
      reason = (JSON.parse((event as MessageEvent).data) as { reason?: string }).reason ?? reason;
    } catch {
      // keep default reason
    }
    close();
    handlers.onDone?.(reason);
  });
  source.onerror = () => {
    // EventSource auto-reconnects, but for a localhost runtime a hard error
    // usually means the route is missing or the runtime is down. Close and let
    // the caller fall back to its poll rather than spin.
    close();
    handlers.onError?.(new Error("log tail stream error"));
  };
  return close;
}

// Token-stream one converse turn (#36). EventSource is GET-only and cannot send
// `X-Alfred-Token`, so this uses the webview's `fetch()` with a streamed
// ReadableStream body, which CAN carry the header. `onToken` fires with each
// assistant text fragment as it arrives; the returned promise resolves to the
// final reconciled ConverseResponse. On any streaming failure it REJECTS, so
// the caller can fall back to the non-streaming `composeConverse`. Native only
// (the browser preview stays on the one-shot form), but written transport-
// agnostically so it also works through the dev proxy.
export async function streamComposeConverse(
  baseUrl: string,
  request: ConverseRequest,
  onToken: (text: string) => void,
  signal?: AbortSignal,
): Promise<ConverseResponse> {
  const url = streamingUrl(baseUrl, "/api/compose/converse/stream");
  const headers: Record<string, string> = { "content-type": "application/json" };
  // Native build: attach the per-launch token the Rust side normally injects.
  // The dev proxy path is same-origin and the route still requires the token,
  // so attach it whenever we can read it; a missing token surfaces as the
  // server's 403, which the caller treats as a fallback trigger.
  if (isTauri()) {
    try {
      const token = await invoke<string>("alfred_server_token");
      if (token) {
        headers["X-Alfred-Token"] = token;
      }
    } catch {
      // No token: let the server reject so the caller falls back cleanly.
    }
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(request),
      signal,
    });
  } catch (err) {
    throw new ApiError(humanizeTransportError(err), err instanceof Error ? err.message : String(err));
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    const raw = `alfred serve returned ${response.status}${text ? `: ${text}` : ""}`;
    throw new ApiError(humanizeFetchError(response.status, serverErrorMessage(text)), raw);
  }

  let result: ConverseResponse | null = null;
  let streamError: string | null = null;
  for await (const frame of readSseStream(response, signal)) {
    if (frame.event === "token") {
      const text = (frame.data as { text?: string })?.text;
      if (typeof text === "string" && text) {
        onToken(text);
      }
    } else if (frame.event === "result") {
      result = frame.data as ConverseResponse;
    } else if (frame.event === "error") {
      streamError = (frame.data as { detail?: string })?.detail ?? "stream error";
    }
  }
  if (result) {
    return result;
  }
  // No result event: surface the server's degrade signal so the caller can fall
  // back to non-streaming converse (and then, if needed, the one-shot form).
  const detail = streamError ?? "live_session_unavailable";
  throw new ApiError("The conversational engine did not return a usable turn.", detail);
}

export async function promoteMemoryCandidate(
  baseUrl: string,
  candidateId: string,
): Promise<MemoryCandidateActionResponse> {
  return writeAlfredJson(
    baseUrl,
    `/api/memory/candidates/${memoryPathSegment(candidateId)}/promote`,
    {},
  );
}

export async function rejectMemoryCandidate(
  baseUrl: string,
  candidateId: string,
): Promise<MemoryCandidateActionResponse> {
  return writeAlfredJson(
    baseUrl,
    `/api/memory/candidates/${memoryPathSegment(candidateId)}/reject`,
    {},
  );
}

// Read the current trusted-Slack-approver list on its own (the snapshot batch
// also fetches it, but the onboarding Slack step needs a standalone read so it
// can show who is already trusted without pulling the whole dashboard).
export async function loadTrustedSlackUsers(
  baseUrl: string,
): Promise<TrustedSlackUsersResponse> {
  return readAlfredJson<TrustedSlackUsersResponse>(baseUrl, "/api/slack/trusted-users");
}

export async function addTrustedSlackUser(
  baseUrl: string,
  userId: string,
): Promise<TrustedSlackUsersResponse> {
  return writeAlfredJson(baseUrl, "/api/slack/trusted-users", { user_id: userId });
}

export async function removeTrustedSlackUser(
  baseUrl: string,
  userId: string,
): Promise<TrustedSlackUsersResponse> {
  return writeAlfredJson(
    baseUrl,
    `/api/slack/trusted-users/${encodeURIComponent(userId)}/remove`,
  );
}

// Assign an issue to Batman/Lucius, arm it directly for Lucius pickup
// (`queue` -> agent:implement), hold it (`hold` -> do-not-pickup), or close it
// (`done` -> GitHub's native closed state). Mutating, so it rides the
// token-bearing POST path.
export async function setQueuePickup(
  baseUrl: string,
  repo: string,
  number: number,
  action: QueueAction,
  targetAgent: AssignmentTargetAgent = "auto",
): Promise<QueueActionResponse> {
  const target_agent = action === "assign" && targetAgent !== "auto" ? targetAgent : undefined;
  return writeAlfredJson(baseUrl, "/api/queue", { repo, number, action, target_agent });
}

export async function loadSetupStatus(baseUrl: string): Promise<SetupStatus> {
  return withTimeout(
    readAlfredJson<SetupStatus>(baseUrl, "/api/setup/status"),
    12000,
    "/api/setup/status",
  );
}

export async function loadSetupRepos(
  baseUrl: string,
  limit = 100,
): Promise<SetupReposResponse> {
  return withTimeout(
    readAlfredJson<SetupReposResponse>(baseUrl, `/api/setup/repos?limit=${limit}`),
    20000,
    "/api/setup/repos",
  );
}

export async function saveSetupRepos(
  baseUrl: string,
  repos: string[],
): Promise<SetupSelectReposResponse> {
  return writeAlfredJson(baseUrl, "/api/setup/repos", { repos, queue_repos: repos });
}

export async function loadSetupPlaybooks(
  baseUrl: string,
): Promise<SetupPlaybooksResponse> {
  return readAlfredJson<SetupPlaybooksResponse>(baseUrl, "/api/setup/playbooks");
}

export async function composeSetupPlaybook(
  baseUrl: string,
  key: string,
  repos?: string[],
): Promise<SetupPlaybookComposeResponse> {
  const body = repos?.length ? { key, repos } : { key };
  return writeAlfredJson(baseUrl, "/api/setup/playbook", body);
}

export async function seedSetupDemo(baseUrl: string): Promise<SetupDemoResponse> {
  return writeAlfredJson(baseUrl, "/api/setup/demo", {});
}

export async function clearSetupDemo(baseUrl: string): Promise<SetupDemoResponse> {
  return writeAlfredJson(baseUrl, "/api/setup/demo/clear", {});
}

export function supportsNativeActions(): boolean {
  return isTauri();
}

export async function runNativeAction(
  action: NativeAction,
  target?: string,
  cadence?: string,
): Promise<NativeCommandResult> {
  if (!isTauri()) {
    throw new Error("Native Alfred actions are available in the desktop app.");
  }
  return invoke<NativeCommandResult>("run_alfred_action", { action, target, cadence });
}

export async function startLocalRuntime(port = 7010): Promise<NativeCommandResult> {
  if (!isTauri()) {
    throw new Error("The desktop app is needed to start Alfred locally.");
  }
  return invoke<NativeCommandResult>("start_alfred_runtime", { port });
}

export async function setTrayStatus(
  level: "ok" | "warn" | "error" | "unknown",
  summary?: string,
): Promise<void> {
  if (!isTauri()) {
    return;
  }
  try {
    await invoke("set_tray_status", { level, summary });
  } catch {
    // The tray is best-effort; never let a tray hiccup break the UI.
  }
}

async function readAlfredJson<T>(baseUrl: string, path: string): Promise<T> {
  const resolvedBaseUrl = clientBaseUrl(baseUrl);
  const text = isTauri()
    ? await invokeAlfredJson("fetch_alfred_json", { baseUrl: resolvedBaseUrl, path })
    : await browserFetch(resolvedBaseUrl, path, "GET");
  return JSON.parse(text) as T;
}

async function writeAlfredJson<T>(
  baseUrl: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  // An already-aborted run short-circuits before doing any work. The native
  // Tauri invoke path cannot be cancelled mid-flight (the buffered Rust bridge
  // resolves whole), so callers also guard the resolved value; the browser/dev
  // path threads the signal into fetch so it can abort in-flight.
  if (signal?.aborted) {
    throw new DOMException("Aborted", "AbortError");
  }
  const resolvedBaseUrl = clientBaseUrl(baseUrl);
  const payload = body === undefined ? undefined : JSON.stringify(body);
  const text = isTauri()
    ? await invokeAlfredJson("post_alfred_json", { baseUrl: resolvedBaseUrl, path, body: payload })
    : await browserFetch(resolvedBaseUrl, path, "POST", payload, signal);
  return JSON.parse(text) as T;
}

// The native fetch command surfaces the same auth/transport failures the browser
// path does, just as a Tauri invoke rejection. Humanize those too so the desktop
// build does not leak a raw Rust error string into the connection banner.
async function invokeAlfredJson(
  command: "fetch_alfred_json" | "post_alfred_json",
  args: Record<string, unknown>,
): Promise<string> {
  try {
    return await invoke<string>(command, args);
  } catch (err) {
    const raw = err instanceof Error ? err.message : String(err);
    const statusMatch = raw.match(/\b(40[13]|404|5\d\d)\b/);
    if (statusMatch) {
      throw new ApiError(humanizeFetchError(Number(statusMatch[1])), raw);
    }
    throw new ApiError(humanizeTransportError(err), raw);
  }
}

async function browserFetch(
  baseUrl: string,
  path: string,
  method: "GET" | "POST",
  body?: string,
  signal?: AbortSignal,
): Promise<string> {
  const url = new URL(path, normalizedBaseUrl(baseUrl));
  const devProxyPath = shouldUseDevProxy(url) ? `/alfred-api${path}` : url.toString();
  let response: Response;
  try {
    response = await fetch(devProxyPath, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body,
      signal,
    });
  } catch (err) {
    // No HTTP response at all: connection refused, DNS, timeout, CORS, etc.
    throw new ApiError(humanizeTransportError(err), err instanceof Error ? err.message : String(err));
  }
  const text = await response.text();
  if (!response.ok) {
    const raw = `alfred serve returned ${response.status}${text ? `: ${text}` : ""}`;
    throw new ApiError(humanizeFetchError(response.status, serverErrorMessage(text)), raw);
  }
  return text;
}

function normalizedBaseUrl(baseUrl: string): string {
  const url = new URL(baseUrl);
  url.pathname = "/";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function isTauri(): boolean {
  return Boolean(window.__TAURI_INTERNALS__);
}

function shouldUseDevProxy(url: URL): boolean {
  return (
    import.meta.env.DEV &&
    url.protocol === "http:" &&
    ["127.0.0.1", "localhost"].includes(url.hostname)
  );
}

function shouldNormalizeDevPreviewBaseUrl(value: string): boolean {
  if (!import.meta.env.DEV || isTauri() || DEV_BASE_URL) {
    return false;
  }
  try {
    const url = new URL(value);
    return isLocalAlfredUrl(url);
  } catch {
    return false;
  }
}

function isLocalAlfredUrl(url: URL): boolean {
  return (
    url.protocol === "http:" &&
    ["127.0.0.1", "localhost", "::1", "[::1]"].includes(url.hostname)
  );
}

function planPathSegment(planId: string): string {
  const clean = planId.trim();
  if (!/^[A-Za-z0-9_.-]+$/.test(clean)) {
    throw new Error("Plan id is not safe to send to Alfred serve.");
  }
  return clean;
}

function memoryPathSegment(candidateId: string): string {
  const clean = candidateId.trim();
  if (!/^[A-Za-z0-9:_-]+$/.test(clean)) {
    throw new Error("Memory candidate id is not safe to send to Alfred serve.");
  }
  return clean;
}
