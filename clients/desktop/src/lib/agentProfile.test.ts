import { describe, expect, it } from "vitest";

import { agentProfile } from "./agentProfile";
import type { FleetControlRow } from "./fleetControl";
import type { AgentSummary } from "../types";

function summary(codename: string, overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    codename,
    last_firing_id: null,
    last_run_at: "2026-05-30T10:00:00Z",
    status: "live",
    last_summary: "ok",
    firings_today: 1,
    ...overrides,
  };
}

function row(codename: string, summaryOverrides: Partial<AgentSummary> = {}): FleetControlRow {
  return {
    codename,
    summary: summary(codename, summaryOverrides),
    paused: false,
    pausedSince: null,
    loaded: true,
    consecutiveFailures: 0,
    service: "running",
  };
}

describe("agentProfile under a custom theme", () => {
  // Thread: "Custom Theme Hides Runtime Labels". A custom theme that does not
  // override THIS agent must keep the runtime's display_name / role_title rather
  // than replace it with a Batman default or a titleized codename.
  it("keeps runtime labels for an agent with no custom override", () => {
    const profile = agentProfile(
      row("lucius", { display_name: "Q Branch", role_title: "Gadget lead" }),
      undefined,
      "custom",
      { names: { batman: "Sherlock" }, roles: { batman: "Lead detective" } },
    );
    // lucius was not named in the custom map, so the server labels still win.
    expect(profile.name).toBe("Q Branch");
    expect(profile.roleLabel).toBe("Gadget lead");
  });

  it("applies the custom override only to the named agent", () => {
    const custom = {
      names: { batman: "Sherlock" },
      roles: { batman: "Lead detective" },
    };
    const batman = agentProfile(
      row("batman", { display_name: "Server Batman", role_title: "Server role" }),
      undefined,
      "custom",
      custom,
    );
    // The named agent takes the operator's authored name and role label, which
    // override even a server-provided label.
    expect(batman.name).toBe("Sherlock");
    expect(batman.roleLabel).toBe("Lead detective");
  });

  it("keeps the runtime name when only the role is customized", () => {
    const profile = agentProfile(
      row("lucius", { display_name: "Q Branch", role_title: "Gadget lead" }),
      undefined,
      "custom",
      { names: {}, roles: { lucius: "Quartermaster" } },
    );
    // Only the role was overridden, so the runtime name is preserved.
    expect(profile.name).toBe("Q Branch");
    expect(profile.roleLabel).toBe("Quartermaster");
  });

  it("still lets a preset keep the runtime labels", () => {
    const profile = agentProfile(
      row("lucius", { display_name: "Q Branch", role_title: "Gadget lead" }),
      undefined,
      "transformers",
    );
    expect(profile.name).toBe("Q Branch");
    expect(profile.roleLabel).toBe("Gadget lead");
  });
});
