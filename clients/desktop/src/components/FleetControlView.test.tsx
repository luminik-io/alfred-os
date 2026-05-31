import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FleetControlView } from "./FleetControlView";
import { parseFleetServiceState } from "../lib/fleetControl";
import type { AgentSummary, NativeCommandResult } from "../types";

// Render in the desktop-capable mode so the control buttons appear.
vi.mock("../api", () => ({
  supportsNativeActions: () => true,
}));

function statusResult(stdout: string): NativeCommandResult {
  return {
    command: ["alfred", "status", "--json"],
    stdout,
    stderr: "",
    status: 0,
    success: true,
    pid: null,
    message: null,
  };
}

function agent(codename: string, overrides: Partial<AgentSummary> = {}): AgentSummary {
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

const SERVICE = parseFleetServiceState(
  statusResult(
    JSON.stringify({
      agents: [
        { agent: "lucius", loaded: true, paused: false, paused_since: null },
        { agent: "bane", loaded: false, paused: true, paused_since: "2026-05-30T09:00:00Z" },
      ],
    }),
  ),
);

function renderView(onRunLocalAction = vi.fn()) {
  render(
    <FleetControlView
      agents={[agent("lucius"), agent("bane")]}
      service={SERVICE}
      nativeBusy={null}
      nativeResult={null}
      nativeError={null}
      onRunLocalAction={onRunLocalAction}
      onRefreshService={vi.fn()}
    />,
  );
  return onRunLocalAction;
}

describe("FleetControlView", () => {
  it("shows Pause for a running agent and Resume for a paused one", () => {
    renderView();
    // lucius is running -> Pause offered; bane is paused -> Resume offered.
    expect(screen.getByRole("button", { name: /^Pause$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
    // Paused-since detail is surfaced.
    expect(screen.getByText(/paused since/i)).toBeInTheDocument();
  });

  it("reads paused state from the polled summary without a CLI service map", () => {
    render(
      <FleetControlView
        agents={[
          agent("lucius", { paused: false, loaded: true }),
          agent("bane", {
            paused: true,
            loaded: false,
            paused_since: "2026-05-30T09:00:00Z",
          }),
        ]}
        service={{}}
        nativeBusy={null}
        nativeResult={null}
        nativeError={null}
        onRunLocalAction={vi.fn()}
        onRefreshService={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /^Pause$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
    expect(screen.getByText(/paused since/i)).toBeInTheDocument();
  });

  it("runs dry-run immediately without confirmation", async () => {
    const onRun = renderView();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: /Dry-run/i })[0]);
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ action: "dry_run", refreshAfter: true }),
    );
  });

  it("requires confirmation before a state-changing pause", async () => {
    const onRun = renderView();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /^Pause$/i }));
    // Nothing dispatched yet; a confirm dialog appears instead.
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /yes, pause/i }));
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ action: "pause", target: "lucius", refreshAfter: true }),
    );
  });

  it("cancels a pending action without dispatching", async () => {
    const onRun = renderView();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /^Pause$/i }));
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("moves focus to the affirmative button and closes on Escape", async () => {
    const onRun = renderView();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /^Pause$/i }));
    // Focus lands on the destructive affirmative so the confirm is keyboard-ready.
    const affirm = screen.getByRole("button", { name: /yes, pause/i });
    expect(affirm).toHaveFocus();

    // Escape cancels without dispatching.
    await user.keyboard("{Escape}");
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
