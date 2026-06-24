import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { AgentDetailDrawer } from "./AgentDetailDrawer";
import type { FleetControlRow } from "../lib/fleetControl";

function makeRow(codename: string): FleetControlRow {
  return {
    codename,
    summary: null,
    paused: false,
    pausedSince: null,
    loaded: true,
    consecutiveFailures: 0,
    service: "running",
  };
}

const OPTIONS = [
  { value: "10m", label: "Every 10 min" },
  { value: "20m", label: "Every 20 min" },
];

// A tiny host that owns the selected agent and keeps the drawer open across
// switches, mirroring the non-modal behavior where the canvas/list reselects
// without closing the sheet.
function DrawerHost() {
  const [codename, setCodename] = useState("lucius");
  return (
    <>
      <button type="button" onClick={() => setCodename("bane")}>
        switch to bane
      </button>
      <AgentDetailDrawer
        row={makeRow(codename)}
        open
        onOpenChange={() => {}}
        canRun
        nativeBusy={null}
        serviceTone={() => ({ tone: "ok", label: "Running" })}
        agentProfile={() => ({
          name: codename,
          roleLabel: "Engineer",
          label: codename,
          purpose: "purpose",
          themeAccent: "#fff",
        })}
        agentActionCue={() => "cue"}
        scheduleCopy={() => "every 10m"}
        // Both agents share the same base cadence, so the in-place sync effect is
        // a no-op on switch: only a fresh per-agent mount can clear an edited
        // draft. This isolates the keying fix from the effect path.
        editableScheduleValue={() => "10m"}
        scheduleOptions={() => OPTIONS}
        onDispatch={vi.fn()}
        onViewLogs={vi.fn()}
      />
    </>
  );
}

describe("AgentDetailDrawer", () => {
  it("resets the cadence draft per agent when switching with the drawer open", async () => {
    render(<DrawerHost />);
    const user = userEvent.setup();

    // Lucius starts on its base cadence.
    const lucius = screen.getByRole("combobox", { name: /schedule lucius/i });
    expect(lucius).toHaveTextContent(/every 10 min/i);

    // Edit lucius's draft without saving.
    await user.click(lucius);
    await user.click(screen.getByRole("option", { name: /every 20 min/i }));
    expect(screen.getByRole("combobox", { name: /schedule lucius/i })).toHaveTextContent(
      /every 20 min/i,
    );

    // Switch to bane while the drawer stays open. The body is keyed by codename,
    // so it remounts and the cadence reflects bane's own value, not lucius's
    // unsaved "every 20 min" draft.
    await user.click(screen.getByRole("button", { name: /switch to bane/i }));
    const bane = screen.getByRole("combobox", { name: /schedule bane/i });
    expect(bane).toHaveTextContent(/every 10 min/i);
    expect(bane).not.toHaveTextContent(/every 20 min/i);
  });
});
