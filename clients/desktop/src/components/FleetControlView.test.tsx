import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FleetControlView } from "./FleetControlView";
import { parseFleetServiceState } from "../lib/fleetControl";
import type { AgentSummary, NativeCommandResult, ScheduledRun } from "../types";

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

const SCHEDULE: ScheduledRun[] = [
  {
    codename: "lucius",
    role: "Engineer",
    kind: "interval",
    cadence: "every 10m",
    next_fire_at: null,
    raw_schedule: "interval:600",
  },
  {
    codename: "bane",
    role: "Reviewer",
    kind: "cron-daily",
    cadence: "daily at 08:00",
    next_fire_at: "2026-06-08T08:00:00+02:00",
    raw_schedule: "cron:8:00",
  },
];

function renderView(onRunLocalAction = vi.fn(), schedule: ScheduledRun[] = SCHEDULE) {
  render(
    <FleetControlView
      agents={[agent("lucius"), agent("bane")]}
      schedule={schedule}
      service={SERVICE}
      nativeBusy={null}
      onRunLocalAction={onRunLocalAction}
      onViewLogs={vi.fn()}
    />,
  );
  return onRunLocalAction;
}

describe("FleetControlView", () => {
  beforeEach(() => {
    // Roster view mode persists in localStorage; reset so each test starts on
    // the workflow default regardless of order.
    try {
      window.localStorage.clear();
    } catch {
      // jsdom without storage: nothing to reset.
    }
  });

  it("defaults to the workflow view and toggles to the dense list", async () => {
    renderView();
    const user = userEvent.setup();
    const workflow = screen.getByRole("button", { name: /workflow view/i });
    const list = screen.getByRole("button", { name: /list view/i });
    expect(workflow).toHaveAttribute("aria-pressed", "true");
    expect(list).toHaveAttribute("aria-pressed", "false");

    await user.click(list);
    expect(list).toHaveAttribute("aria-pressed", "true");
    expect(workflow).toHaveAttribute("aria-pressed", "false");
    // Selecting an agent works in the list view.
    await user.click(screen.getByRole("button", { name: /select bane/i }));
    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
  });

  it("shows selected-agent controls and switches to a paused agent", async () => {
    renderView();
    const user = userEvent.setup();

    // The roster selects the running agent first, so only that agent's controls show.
    expect(screen.getByRole("button", { name: /^Pause$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Resume$/i })).not.toBeInTheDocument();

    // Switch to the list to pick a specific agent (the graph renders no
    // measurable nodes in jsdom), then select the paused one.
    await user.click(screen.getByRole("button", { name: /list view/i }));
    await user.click(screen.getByRole("button", { name: /select bane/i }));
    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
    // The list row and the inspector both surface the paused state.
    expect(screen.getAllByText(/paused since/i).length).toBeGreaterThan(0);
  });

  it("opens on an llm-error agent before a running agent", () => {
    render(
      <FleetControlView
        agents={[agent("lucius", { status: "live" }), agent("bane", { status: "llm-error" })]}
        schedule={SCHEDULE}
        service={SERVICE}
        nativeBusy={null}
        onRunLocalAction={vi.fn()}
        onViewLogs={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Pause$/i })).not.toBeInTheDocument();
  });

  it("reads paused state from the polled summary without a CLI service map", async () => {
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
        schedule={[]}
        service={{}}
        nativeBusy={null}
        onRunLocalAction={vi.fn()}
        onViewLogs={vi.fn()}
      />,
    );
    const user = userEvent.setup();

    expect(screen.getByRole("button", { name: /^Pause$/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /list view/i }));
    await user.click(screen.getByRole("button", { name: /select bane/i }));
    expect(screen.getByRole("button", { name: /^Resume$/i })).toBeInTheDocument();
    // The list row and the inspector both surface the paused state.
    expect(screen.getAllByText(/paused since/i).length).toBeGreaterThan(0);
  });

  it("renders the human agent role and purpose above the runtime codename", () => {
    render(
      <FleetControlView
        agents={[
          agent("lucius", {
            display_name: "Lucius",
            role_title: "Senior Developer",
            purpose: "Ships scoped implementation issues as pull requests.",
          }),
        ]}
        schedule={[]}
        service={{}}
        nativeBusy={null}
        onRunLocalAction={vi.fn()}
        onViewLogs={vi.fn()}
      />,
    );

    expect(screen.getAllByText("Lucius · Senior Developer").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText("Ships scoped implementation issues as pull requests.").length,
    ).toBeGreaterThan(0);
    expect(screen.getByTitle("Runtime codename: lucius")).toHaveTextContent("lucius");
  });

  it("runs dry-run immediately without confirmation", async () => {
    const onRun = renderView();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: /Dry-run/i })[0]);
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ action: "dry_run", refreshAfter: true }),
    );
  });

  it("sets an agent schedule from a cadence menu", async () => {
    const onRun = renderView();
    const user = userEvent.setup();

    await user.click(screen.getByRole("combobox", { name: /schedule lucius/i }));
    await user.click(screen.getByRole("option", { name: /every 20 min/i }));
    await user.click(screen.getByRole("button", { name: /set lucius schedule/i }));

    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "schedule",
        target: "lucius",
        cadence: "20m",
        refreshAfter: true,
      }),
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

  it("deep-links to that agent's logs from its card", async () => {
    const onViewLogs = vi.fn();
    render(
      <FleetControlView
        agents={[agent("lucius")]}
        schedule={[]}
        service={{}}
        nativeBusy={null}
        onRunLocalAction={vi.fn()}
        onViewLogs={onViewLogs}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Logs$/i }));
    expect(onViewLogs).toHaveBeenCalledWith("lucius");
  });
});
