import { describe, expect, it } from "vitest";

import { deriveAgentRole } from "./agentRoster";
import { resolveThemedIdentity, ROSTER_THEME_IDS } from "./agentThemes";

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

  it("falls back to ops for a wholly unknown agent rather than dropping it", () => {
    expect(deriveAgentRole({ codename: "totally-unknown" })).toBe("ops");
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
