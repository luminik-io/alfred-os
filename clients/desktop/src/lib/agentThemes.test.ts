import { describe, expect, it } from "vitest";

import {
  deriveAgentRole,
  scheduleRoleLabelForEditor,
  type WorkflowRole,
} from "./agentRoster";
import {
  editableAgents,
  isRosterThemeId,
  resolveThemedIdentity,
  ROSTER_THEME_IDS,
  rosterThemeFor,
} from "./agentThemes";

describe("deriveAgentRole", () => {
  it("places known default-fleet codenames by the hint table", () => {
    expect(deriveAgentRole({ codename: "batman" })).toBe("architect");
    expect(deriveAgentRole({ codename: "rasalghul" })).toBe("review");
    expect(deriveAgentRole({ codename: "automerge" })).toBe("ship");
    expect(deriveAgentRole({ codename: "fleet-doctor" })).toBe("ops");
  });

  it("tolerates fully-qualified codenames", () => {
    expect(deriveAgentRole({ codename: "fleet.local.lucius" })).toBe("implement");
  });

  it("infers a role from the reported role title when the codename is unknown", () => {
    expect(deriveAgentRole({ codename: "newbot", roleTitle: "Code Reviewer" })).toBe(
      "review",
    );
    expect(deriveAgentRole({ codename: "newbot", roleTitle: "Senior Developer" })).toBe(
      "implement",
    );
  });

  it("maps generated alfred-init schedule role strings before fuzzy keywords", () => {
    expect(deriveAgentRole({ codename: "q-branch", roleTitle: "feature dev" })).toBe(
      "implement",
    );
    expect(deriveAgentRole({ codename: "repo-cartographer", roleTitle: "code map refresh" })).toBe(
      "ops",
    );
    expect(deriveAgentRole({ codename: "merge-bot", roleTitle: "PR automerge" })).toBe(
      "ship",
    );
  });

  it("falls back to ops for a wholly unknown agent rather than dropping it", () => {
    expect(deriveAgentRole({ codename: "totally-unknown" })).toBe("ops");
  });

  it("reproduces the prior shipped roster lanes exactly (no visible change by default)", () => {
    // Lanes from the pre-refactor WORKFLOW_LANES mapping. The data-driven
    // derivation must place every default codename in the same lane it had
    // before, so the default Batman roster renders identically. huntress in
    // particular stays in ops (it must not drift into review).
    const PRIOR_LANES: Record<string, WorkflowRole> = {
      robin: "triage",
      drake: "triage",
      damian: "triage",
      batman: "architect",
      lucius: "implement",
      bane: "implement",
      nightwing: "implement",
      rasalghul: "review",
      automerge: "ship",
      gordon: "ops",
      "fleet-doctor": "ops",
      huntress: "ops",
      "agent-cleanup": "ops",
      "memory-harvest": "ops",
      "memory-auto-promote": "ops",
      "code-map-refresh": "ops",
      "agent-morning-brief": "ops",
      "fleet-recap-morning": "ops",
      "fleet-recap-evening": "ops",
      "shipped-summary-daily": "ship",
      "shipped-summary-weekly": "ship",
      "proof-telemetry": "ops",
    };
    for (const [codename, lane] of Object.entries(PRIOR_LANES)) {
      expect(deriveAgentRole({ codename })).toBe(lane);
    }
  });
});

describe("scheduleRoleLabelForEditor", () => {
  it("does not surface generated schedule roles as labels for known shipped agents", () => {
    expect(
      scheduleRoleLabelForEditor({
        codename: "lucius",
        role: "feature dev",
        roleTitle: null,
      }),
    ).toBeNull();
  });

  it("preserves agents.conf descriptors for schedule-only custom agents", () => {
    expect(
      scheduleRoleLabelForEditor({
        codename: "release-captain",
        role: "Release conductor",
        roleTitle: null,
      }),
    ).toBe("Release conductor");
  });

  it("prefers explicit role_title when the server has profile metadata", () => {
    expect(
      scheduleRoleLabelForEditor({
        codename: "release-captain",
        role: "Release conductor",
        roleTitle: "Launch lead",
      }),
    ).toBe("Launch lead");
  });
});

describe("resolveThemedIdentity", () => {
  it("keeps the shipped names under the default Batman theme", () => {
    const id = resolveThemedIdentity({ codename: "batman" }, "batman");
    expect(id.name).toBe("Batman");
    expect(id.role).toBe("architect");
    expect(id.roleLabel).toBe("Architect");
  });

  it("re-skins the architect lead under Transformers without changing the role", () => {
    const id = resolveThemedIdentity({ codename: "batman" }, "transformers");
    expect(id.name).toBe("Optimus Prime");
    expect(id.role).toBe("architect");
    // The plain role label is preserved across themes.
    expect(id.roleLabel).toBe("Architect");
  });

  it("re-skins under Justice League", () => {
    const id = resolveThemedIdentity({ codename: "rasalghul" }, "justice-league");
    expect(id.name).toBe("Wonder Woman");
    expect(id.roleLabel).toBe("Reviewer");
  });

  it("never returns a blank name for an unknown agent in any theme", () => {
    for (const themeId of ROSTER_THEME_IDS) {
      const id = resolveThemedIdentity({ codename: "mystery-bot-7" }, themeId);
      expect(id.name.length).toBeGreaterThan(0);
      expect(id.roleLabel.length).toBeGreaterThan(0);
    }
  });
});

describe("isRosterThemeId", () => {
  it("accepts every id in ROSTER_THEME_IDS and rejects unknown or null", () => {
    // Derived from ROSTER_THEME_IDS so adding a theme can't desync the guard.
    for (const themeId of ROSTER_THEME_IDS) {
      expect(isRosterThemeId(themeId)).toBe(true);
    }
    expect(isRosterThemeId("nope")).toBe(false);
    expect(isRosterThemeId(null)).toBe(false);
  });
});

describe("custom roster theme", () => {
  it("applies operator names and role labels over the Batman base", () => {
    const id = resolveThemedIdentity({ codename: "batman" }, "custom", {
      names: { batman: "Sherlock" },
      roles: { batman: "Lead detective" },
    });
    expect(id.name).toBe("Sherlock");
    expect(id.role).toBe("architect");
    expect(id.roleLabel).toBe("Lead detective");
  });

  it("keeps a custom role label scoped to the named codename only", () => {
    // lucius, bane, and nightwing all share the canonical `implement` role.
    // Renaming lucius's role must NOT relabel bane (which would happen if the
    // override were folded into the role-wide labels), matching the Slack path
    // where role_label_for is keyed by codename.
    const custom = {
      names: {},
      roles: { lucius: "Quartermaster" },
    };
    const lucius = resolveThemedIdentity({ codename: "lucius" }, "custom", custom);
    const bane = resolveThemedIdentity({ codename: "bane" }, "custom", custom);
    expect(lucius.roleLabel).toBe("Quartermaster");
    // bane keeps the canonical implement label, not lucius's custom one.
    expect(bane.roleLabel).toBe("Senior developer");
  });

  it("falls back to the Batman name when an agent is not customized", () => {
    const id = resolveThemedIdentity({ codename: "lucius" }, "custom", {
      names: { batman: "Sherlock" },
      roles: {},
    });
    // lucius was not renamed, so it keeps its shipped Batman-base name.
    expect(id.name).toBe("Lucius");
    expect(id.roleLabel).toBe("Senior developer");
  });

  it("ignores blank custom names rather than rendering an empty label", () => {
    const id = resolveThemedIdentity({ codename: "batman" }, "custom", {
      names: { batman: "   " },
      roles: {},
    });
    expect(id.name).toBe("Batman");
  });

  it("normalizes a dotted codename when building the custom theme", () => {
    const theme = rosterThemeFor("custom", {
      names: { "fleet.local.batman": "Sherlock" },
      roles: {},
    });
    expect(theme.nameByCodename.batman).toBe("Sherlock");
  });

  it("presets ignore custom maps entirely", () => {
    const id = resolveThemedIdentity({ codename: "batman" }, "transformers", {
      names: { batman: "Sherlock" },
      roles: {},
    });
    expect(id.name).toBe("Optimus Prime");
  });

  it("can name a future custom agent without adding it to the preset roster", () => {
    const id = resolveThemedIdentity(
      { codename: "security-scout", roleTitle: "Code Reviewer" },
      "custom",
      {
        names: { "security-scout": "Sentinel" },
        roles: { "security-scout": "Security reviewer" },
      },
    );
    expect(id.role).toBe("review");
    expect(id.name).toBe("Sentinel");
    expect(id.roleLabel).toBe("Security reviewer");
  });
});

describe("editableAgents", () => {
  it("lists the full default roster with a role, name, and role label each", () => {
    const agents = editableAgents();
    expect(agents.length).toBeGreaterThan(0);
    for (const agent of agents) {
      expect(agent.codename.length).toBeGreaterThan(0);
      expect(agent.defaultName.length).toBeGreaterThan(0);
      expect(agent.defaultRoleLabel.length).toBeGreaterThan(0);
    }
    // The obsolete cleanup alias is gone; the canonical scheduled codename is
    // editable because it is what the installer deploys.
    expect(agents.some((a) => a.codename === "cleanup")).toBe(false);
    expect(agents.some((a) => a.codename === "agent-cleanup")).toBe(true);
    expect(agents.some((a) => a.codename === "batman")).toBe(true);
    expect(agents.some((a) => a.codename === "memory-auto-promote")).toBe(true);
    expect(agents.some((a) => a.codename === "shipped-summary-weekly")).toBe(true);
  });

  it("adds live custom agents to the editable roster", () => {
    const agents = editableAgents([
      {
        codename: "security-scout",
        displayName: "Sentinel",
        roleLabel: "Security reviewer",
        roleTitle: "Code Reviewer",
      },
    ]);
    expect(agents).toContainEqual({
      codename: "security-scout",
      role: "review",
      defaultName: "Sentinel",
      defaultRoleLabel: "Security reviewer",
    });
  });

  it("keeps Batman-base placeholders for known agents even when runtime reports a themed name", () => {
    const agents = editableAgents([
      {
        codename: "lucius",
        displayName: "Ironhide",
        roleLabel: "implement",
        roleTitle: "implement",
      },
    ]);
    expect(agents.find((agent) => agent.codename === "lucius")).toMatchObject({
      defaultName: "Lucius",
      defaultRoleLabel: "Senior developer",
    });
  });
});
