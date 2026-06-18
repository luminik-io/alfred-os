import { describe, expect, it } from "vitest";

import {
  buildFleetRows,
  deriveFleetHealth,
  lookupServiceState,
  parseFleetServiceState,
} from "./fleetControl";
import type { AgentSummary, NativeCommandResult } from "../types";

function nativeResult(overrides: Partial<NativeCommandResult> = {}): NativeCommandResult {
  return {
    command: ["alfred", "status", "--json"],
    stdout: "",
    stderr: "",
    status: 0,
    success: true,
    pid: null,
    message: null,
    ...overrides,
  };
}

function agent(codename: string, overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    codename,
    last_firing_id: null,
    last_run_at: "2026-05-30T10:00:00Z",
    status: "live",
    last_summary: "ok",
    firings_today: 2,
    ...overrides,
  };
}

const STATUS_JSON = JSON.stringify({
  ts: "2026-05-30T12:00:00Z",
  agents: [
    { agent: "lucius", loaded: true, paused: false, paused_since: null, today_consecutive_failures: 0 },
    {
      agent: "bane",
      loaded: false,
      paused: true,
      paused_since: "2026-05-30T09:00:00Z",
      today_consecutive_failures: 0,
    },
    {
      agent: "fleet.local.robin",
      loaded: false,
      paused: false,
      paused_since: null,
      today_consecutive_failures: 3,
    },
  ],
});

describe("parseFleetServiceState", () => {
  it("returns an empty map for failed or empty native results", () => {
    expect(parseFleetServiceState(null)).toEqual({});
    expect(parseFleetServiceState(nativeResult({ success: false, stdout: STATUS_JSON }))).toEqual({});
    expect(parseFleetServiceState(nativeResult({ stdout: "" }))).toEqual({});
  });

  it("ignores malformed JSON without throwing", () => {
    expect(parseFleetServiceState(nativeResult({ stdout: "{not json" }))).toEqual({});
    expect(parseFleetServiceState(nativeResult({ stdout: '{"agents": "nope"}' }))).toEqual({});
  });

  it("keys the agents by codename", () => {
    const map = parseFleetServiceState(nativeResult({ stdout: STATUS_JSON }));
    expect(Object.keys(map).sort()).toEqual(["bane", "fleet.local.robin", "lucius"]);
    expect(map.bane.paused).toBe(true);
  });
});

describe("lookupServiceState", () => {
  it("matches a short codename against a fully-qualified label", () => {
    const map = parseFleetServiceState(nativeResult({ stdout: STATUS_JSON }));
    const found = lookupServiceState(map, "robin");
    expect(found?.agent).toBe("fleet.local.robin");
  });
});

describe("buildFleetRows", () => {
  it("joins polled summaries with service state and derives a service label", () => {
    const map = parseFleetServiceState(nativeResult({ stdout: STATUS_JSON }));
    const rows = buildFleetRows([agent("lucius"), agent("bane")], map);
    const lucius = rows.find((row) => row.codename === "lucius");
    const bane = rows.find((row) => row.codename === "bane");
    expect(lucius?.service).toBe("running");
    expect(bane?.service).toBe("paused");
    expect(bane?.pausedSince).toBe("2026-05-30T09:00:00Z");
  });

  it("surfaces service-only agents (resumable even if never polled)", () => {
    const map = parseFleetServiceState(nativeResult({ stdout: STATUS_JSON }));
    const rows = buildFleetRows([agent("lucius")], map);
    // robin appears only in the status JSON; it should still get a row.
    expect(rows.map((row) => row.codename)).toContain("robin");
    const robin = rows.find((row) => row.codename === "robin");
    expect(robin?.summary).toBeNull();
    expect(robin?.consecutiveFailures).toBe(3);
  });

  it("marks agents with no service state as unknown", () => {
    const rows = buildFleetRows([agent("lucius")], {});
    expect(rows[0].service).toBe("unknown");
  });

  it("derives paused/running from the polled summary without a service map", () => {
    const rows = buildFleetRows(
      [
        agent("lucius", { paused: false, loaded: true }),
        agent("bane", { paused: true, loaded: false, paused_since: "2026-05-30T09:00:00Z" }),
      ],
      {},
    );
    const lucius = rows.find((row) => row.codename === "lucius");
    const bane = rows.find((row) => row.codename === "bane");
    // No `alfred status --json` map supplied; state comes straight from /api/status.
    expect(lucius?.service).toBe("running");
    expect(bane?.service).toBe("paused");
    expect(bane?.pausedSince).toBe("2026-05-30T09:00:00Z");
  });

  it("infers loaded from paused when the summary omits loaded", () => {
    const rows = buildFleetRows([agent("bane", { paused: true })], {});
    expect(rows[0].service).toBe("paused");
    expect(rows[0].loaded).toBe(false);
  });

  it("prefers the polled summary over the CLI service map", () => {
    // /api/status says paused; a stale CLI map says running. Summary wins.
    const map = parseFleetServiceState(
      nativeResult({
        stdout: JSON.stringify({
          agents: [{ agent: "bane", loaded: true, paused: false, paused_since: null }],
        }),
      }),
    );
    const rows = buildFleetRows(
      [agent("bane", { paused: true, loaded: false, paused_since: "2026-05-30T09:00:00Z" })],
      map,
    );
    expect(rows[0].service).toBe("paused");
    expect(rows[0].pausedSince).toBe("2026-05-30T09:00:00Z");
  });
});

describe("deriveFleetHealth", () => {
  it("is unknown with no rows", () => {
    expect(deriveFleetHealth([]).level).toBe("unknown");
  });

  it("is error when an agent is errored or fail-streaking", () => {
    const map = parseFleetServiceState(nativeResult({ stdout: STATUS_JSON }));
    // robin has 3 consecutive failures -> error.
    const rows = buildFleetRows([agent("lucius")], map);
    expect(deriveFleetHealth(rows).level).toBe("error");
  });

  it("is warn when an agent is paused or stopped but none erroring", () => {
    const map = parseFleetServiceState(
      nativeResult({
        stdout: JSON.stringify({
          agents: [
            { agent: "lucius", loaded: true, paused: false, paused_since: null },
            { agent: "bane", loaded: false, paused: true, paused_since: null },
          ],
        }),
      }),
    );
    const rows = buildFleetRows([agent("lucius"), agent("bane")], map);
    const health = deriveFleetHealth(rows);
    expect(health.level).toBe("warn");
    expect(health.summary).toContain("paused");
  });

  it("is ok when everything is running", () => {
    const map = parseFleetServiceState(
      nativeResult({
        stdout: JSON.stringify({
          agents: [{ agent: "lucius", loaded: true, paused: false, paused_since: null }],
        }),
      }),
    );
    const rows = buildFleetRows([agent("lucius")], map);
    expect(deriveFleetHealth(rows).level).toBe("ok");
  });
});
