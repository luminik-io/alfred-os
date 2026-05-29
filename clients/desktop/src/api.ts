import { invoke } from "@tauri-apps/api/core";

import type {
  ActionsResponse,
  FiringsResponse,
  FollowupActionResponse,
  NativeAction,
  NativeCommandResult,
  PlansResponse,
  Snapshot,
  StatusResponse,
} from "./types";

const DEFAULT_BASE_URL = "http://127.0.0.1:7000";
export const FALLBACK_BASE_URL = "http://127.0.0.1:7010";
const BASE_URL_KEY = "alfred-desktop.base-url";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
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

export async function loadSnapshot(baseUrl: string): Promise<Snapshot> {
  const [status, actions, firings, plans] = await Promise.all([
    readAlfredJson<StatusResponse>(baseUrl, "/api/status"),
    readAlfredJson<ActionsResponse>(baseUrl, "/api/actions"),
    readAlfredJson<FiringsResponse>(baseUrl, "/api/firings?limit=14"),
    readAlfredJson<PlansResponse>(baseUrl, "/api/plans?limit=14"),
  ]);

  return {
    loadedAt: new Date(),
    status,
    actions,
    firings: firings.rows || [],
    plans: plans.rows || [],
  };
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

export async function startLocalRuntime(port = 7000): Promise<NativeCommandResult> {
  if (!isTauri()) {
    throw new Error("The desktop app is needed to start Alfred locally.");
  }
  return invoke<NativeCommandResult>("start_alfred_runtime", { port });
}

async function readAlfredJson<T>(baseUrl: string, path: string): Promise<T> {
  const text = isTauri()
    ? await invoke<string>("fetch_alfred_json", { baseUrl, path })
    : await browserFetch(baseUrl, path, "GET");
  return JSON.parse(text) as T;
}

async function writeAlfredJson<T>(baseUrl: string, path: string): Promise<T> {
  const text = isTauri()
    ? await invoke<string>("post_alfred_json", { baseUrl, path })
    : await browserFetch(baseUrl, path, "POST");
  return JSON.parse(text) as T;
}

async function browserFetch(baseUrl: string, path: string, method: "GET" | "POST"): Promise<string> {
  const url = new URL(path, normalizedBaseUrl(baseUrl));
  const devProxyPath = shouldUseDevProxy(url) ? `/alfred-api${path}` : url.toString();
  const response = await fetch(devProxyPath, { method });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`alfred serve returned ${response.status}${text ? `: ${text}` : ""}`);
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
