import type { SetupStatus } from "../types";

export type GithubAuthPollResult = {
  state: "success" | "timeout";
  status: SetupStatus | null;
  attempts: number;
  elapsedMs: number;
  lastError: string | null;
};

type PollOptions = {
  pollIntervalMs?: number | null;
  timeoutMs?: number | null;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
};

const DEFAULT_POLL_INTERVAL_MS = 2_000;
const DEFAULT_TIMEOUT_MS = 120_000;
const MIN_POLL_INTERVAL_MS = 250;
const MIN_TIMEOUT_MS = 1_000;

export async function pollGithubAuthStatus(
  loadStatus: () => Promise<SetupStatus>,
  options: PollOptions = {},
): Promise<GithubAuthPollResult> {
  const now = options.now ?? (() => Date.now());
  const sleep =
    options.sleep ??
    ((ms: number) =>
      new Promise<void>((resolve) => {
        window.setTimeout(resolve, ms);
      }));
  const pollIntervalMs = Math.max(
    MIN_POLL_INTERVAL_MS,
    options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
  );
  const timeoutMs = Math.max(MIN_TIMEOUT_MS, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const startedAt = now();
  let attempts = 0;
  let status: SetupStatus | null = null;
  let lastError: string | null = null;

  while (now() - startedAt < timeoutMs) {
    attempts += 1;
    try {
      status = await loadStatus();
      lastError = null;
      if (status.github.ok) {
        return {
          state: "success",
          status,
          attempts,
          elapsedMs: now() - startedAt,
          lastError,
        };
      }
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    }

    const elapsedMs = now() - startedAt;
    const remainingMs = timeoutMs - elapsedMs;
    if (remainingMs <= 0) {
      break;
    }
    await sleep(Math.min(pollIntervalMs, remainingMs));
  }

  return {
    state: "timeout",
    status,
    attempts,
    elapsedMs: now() - startedAt,
    lastError,
  };
}
