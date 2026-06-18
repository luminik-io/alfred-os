import type { AgentSummary, AlfredStatusAgent, AlfredStatusJson, NativeCommandResult } from "../types";

// A single agent's row in the Fleet Control panel: the at-a-glance summary the
// client already polls. Paused/running state comes from the polled /api/status
// feed (the server reads the same pause marker the CLI writes); the optional
// `alfred status --json` service map only enriches the CLI-only fail-streak
// counter the read-only API does not expose.
export type FleetControlRow = {
  codename: string;
  summary: AgentSummary | null;
  paused: boolean;
  pausedSince: string | null;
  loaded: boolean;
  // Consecutive failures so far today, surfaced from the status JSON so the
  // health rollup can flag a fail-streaking agent even before it errors hard.
  consecutiveFailures: number;
  // "running" when loaded and not paused, "paused" when a pause marker is set,
  // "stopped" when neither loaded nor paused, "unknown" when no service state
  // has been read yet (the status JSON has not been fetched).
  service: "running" | "paused" | "stopped" | "unknown";
};

// Service-state map keyed by codename, derived from `alfred status --json`.
export type FleetServiceState = Record<string, AlfredStatusAgent>;

/**
 * Parse the stdout of `alfred status --json` into a codename->agent map. Returns
 * an empty map for any output that is missing, malformed, or not the expected
 * shape so a bad payload never throws into the render path.
 */
export function parseFleetServiceState(result: NativeCommandResult | null): FleetServiceState {
  if (!result || !result.success || !result.stdout) {
    return {};
  }
  let parsed: AlfredStatusJson;
  try {
    parsed = JSON.parse(result.stdout) as AlfredStatusJson;
  } catch {
    return {};
  }
  if (!parsed || !Array.isArray(parsed.agents)) {
    return {};
  }
  const map: FleetServiceState = {};
  for (const agent of parsed.agents) {
    if (agent && typeof agent.agent === "string" && agent.agent) {
      map[agent.agent] = agent;
    }
  }
  return map;
}

/**
 * Look up an agent's service entry tolerating the CLI's fully-qualified labels.
 * The /api/status feed reports short codenames (e.g. "lucius") while the status
 * JSON may report "luminik.eng.lucius"; match on either the exact key or the
 * trailing segment.
 */
export function lookupServiceState(
  service: FleetServiceState,
  codename: string,
): AlfredStatusAgent | null {
  if (service[codename]) {
    return service[codename];
  }
  for (const [key, value] of Object.entries(service)) {
    if (key === codename || key.split(".").pop() === codename) {
      return value;
    }
  }
  return null;
}

/**
 * Build the Fleet Control rows by joining the polled agent summaries with the
 * service state. Agents present only in the status JSON (e.g. disabled/never
 * fired) still surface so they can be resumed.
 */
export function buildFleetRows(
  agents: AgentSummary[],
  service: FleetServiceState,
): FleetControlRow[] {
  const rows: FleetControlRow[] = [];
  const seen = new Set<string>();

  for (const summary of agents) {
    const state = lookupServiceState(service, summary.codename);
    rows.push(toRow(summary.codename, summary, state));
    seen.add(summary.codename);
  }

  for (const [key, state] of Object.entries(service)) {
    const short = key.split(".").pop() || key;
    if (seen.has(key) || seen.has(short)) {
      continue;
    }
    rows.push(toRow(short, null, state));
    seen.add(short);
  }

  return rows.sort((a, b) => a.codename.localeCompare(b.codename));
}

function toRow(
  codename: string,
  summary: AgentSummary | null,
  state: AlfredStatusAgent | null,
): FleetControlRow {
  // Fail-streak count is CLI-only; enrich it from the service map when present.
  const consecutiveFailures = state
    ? Math.max(0, Number(state.today_consecutive_failures ?? 0))
    : 0;

  // Paused/running state comes from the polled summary first. The server reads
  // the same pause marker the CLI writes, so the desktop client no longer needs
  // to shell `alfred status --json` for it. The CLI service map is a fallback
  // for older servers (or browser/dev runs) whose /api/status omits the fields.
  const summaryHasService =
    summary != null &&
    (summary.paused !== undefined || summary.loaded !== undefined);

  let paused: boolean;
  let loaded: boolean;
  let pausedSince: string | null;
  let serviceKnown: boolean;

  if (summaryHasService) {
    paused = Boolean(summary!.paused);
    loaded = summary!.loaded ?? !paused;
    pausedSince = summary!.paused_since ?? null;
    serviceKnown = true;
  } else if (state) {
    paused = Boolean(state.paused);
    loaded = Boolean(state.loaded);
    pausedSince = state.paused_since ?? null;
    serviceKnown = true;
  } else {
    paused = false;
    loaded = false;
    pausedSince = null;
    serviceKnown = false;
  }

  return {
    codename,
    summary,
    paused,
    pausedSince,
    loaded,
    consecutiveFailures,
    service: !serviceKnown ? "unknown" : paused ? "paused" : loaded ? "running" : "stopped",
  };
}

/**
 * Reduce the fleet to a single health level for the tray and hero pill:
 *  - "error" if any polled agent is errored or any agent is fail-streaking,
 *  - "warn" if any agent is paused or stopped (service degraded but not broken),
 *  - "ok" otherwise.
 * Returns "unknown" when there is nothing to assess yet.
 */
export function deriveFleetHealth(rows: FleetControlRow[]): {
  level: "ok" | "warn" | "error" | "unknown";
  summary: string;
} {
  if (!rows.length) {
    return { level: "unknown", summary: "no agents detected" };
  }
  const errored = rows.filter(
    (row) => row.summary?.status === "error" || hasFailStreak(row),
  );
  if (errored.length) {
    return {
      level: "error",
      summary: `${errored.length} ${errored.length === 1 ? "agent" : "agents"} erroring`,
    };
  }
  const degraded = rows.filter((row) => row.service === "paused" || row.service === "stopped");
  if (degraded.length) {
    const paused = degraded.filter((row) => row.service === "paused").length;
    const stopped = degraded.length - paused;
    const parts: string[] = [];
    if (paused) parts.push(`${paused} paused`);
    if (stopped) parts.push(`${stopped} stopped`);
    return { level: "warn", summary: parts.join(", ") };
  }
  return { level: "ok", summary: `${rows.length} agents running` };
}

// An agent with two or more consecutive failures today is treated as erroring
// for the health rollup, matching the fleet's own self-pause fail-streak gate.
const FAIL_STREAK_THRESHOLD = 2;

function hasFailStreak(row: FleetControlRow): boolean {
  return row.consecutiveFailures >= FAIL_STREAK_THRESHOLD;
}
