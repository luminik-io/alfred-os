import { invoke } from "@tauri-apps/api/core";

import type {
  ActionsResponse,
  ComposeDraftRequest,
  ComposeDraftResponse,
  FiringsResponse,
  FollowupActionResponse,
  MemoryCandidateActionResponse,
  MemoryCandidatesResponse,
  NativeAction,
  NativeCommandResult,
  PlansResponse,
  Snapshot,
  StatusResponse,
  TrustedSlackUsersResponse,
} from "./types";

const DEFAULT_BASE_URL = "http://127.0.0.1:7010";
export const FALLBACK_BASE_URL = "http://127.0.0.1:7000";
const BASE_URL_KEY = "alfred-desktop.base-url";

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
  if (serverMessage && status >= 400 && status < 500) {
    return serverMessage;
  }
  if (status === 401 || status === 403) {
    return "Alfred serve is running but rejected this client (auth token mismatch). Restart the runtime or check your token.";
  }
  if (status === 404) {
    return "Alfred serve answered, but this endpoint is missing. The runtime may be an older build than this client expects.";
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

export function initialBaseUrl(): string {
  return window.localStorage.getItem(BASE_URL_KEY) || DEFAULT_BASE_URL;
}

export function rememberBaseUrl(value: string): void {
  window.localStorage.setItem(BASE_URL_KEY, value);
}

export function isDefaultBaseUrl(value: string): boolean {
  try {
    return normalizedBaseUrl(value) === `${DEFAULT_BASE_URL}/`;
  } catch {
    return value.trim() === DEFAULT_BASE_URL;
  }
}

// The dashboard reads several independent endpoints. A failure on any one of them
// should not blank the whole view, so we settle each request and render what
// resolved, marking the missing sections as degraded. /api/status is the spine
// (it carries fleet liveness and the reliability rollup): if it fails the whole
// snapshot is genuinely unusable, so that one rejection still surfaces as the
// connection error the banner shows.
export async function loadSnapshot(baseUrl: string): Promise<Snapshot> {
  const [status, actions, memoryCandidates, firings, plans, trustedSlack] =
    await Promise.allSettled([
    readAlfredJson<StatusResponse>(baseUrl, "/api/status"),
    readAlfredJson<ActionsResponse>(baseUrl, "/api/actions"),
    readAlfredJson<MemoryCandidatesResponse>(baseUrl, "/api/memory/candidates?limit=20"),
    readAlfredJson<FiringsResponse>(baseUrl, "/api/firings?limit=14"),
    readAlfredJson<PlansResponse>(baseUrl, "/api/plans?limit=14"),
    readAlfredJson<TrustedSlackUsersResponse>(baseUrl, "/api/slack/trusted-users"),
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
    firings: firings.status === "fulfilled" ? firings.value.rows || [] : [],
    plans: plans.status === "fulfilled" ? plans.value.rows || [] : [],
    trustedSlack: trustedSlack.status === "fulfilled" ? trustedSlack.value : null,
    degraded: Object.keys(degraded).length ? degraded : undefined,
  };
}

function settledError(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
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

export async function composeDraft(
  baseUrl: string,
  request: ComposeDraftRequest,
): Promise<ComposeDraftResponse> {
  return writeAlfredJson(baseUrl, "/api/plans/draft", request);
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

export function supportsNativeActions(): boolean {
  return isTauri();
}

export async function runNativeAction(
  action: NativeAction,
  target?: string,
): Promise<NativeCommandResult> {
  if (!isTauri()) {
    throw new Error("Native Alfred actions are available in the desktop app.");
  }
  return invoke<NativeCommandResult>("run_alfred_action", { action, target });
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
  const text = isTauri()
    ? await invokeAlfredJson("fetch_alfred_json", { baseUrl, path })
    : await browserFetch(baseUrl, path, "GET");
  return JSON.parse(text) as T;
}

async function writeAlfredJson<T>(
  baseUrl: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const payload = body === undefined ? undefined : JSON.stringify(body);
  const text = isTauri()
    ? await invokeAlfredJson("post_alfred_json", { baseUrl, path, body: payload })
    : await browserFetch(baseUrl, path, "POST", payload);
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
): Promise<string> {
  const url = new URL(path, normalizedBaseUrl(baseUrl));
  const devProxyPath = shouldUseDevProxy(url) ? `/alfred-api${path}` : url.toString();
  let response: Response;
  try {
    response = await fetch(devProxyPath, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body,
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
