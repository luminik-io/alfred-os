import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SetupInstallInventory, SetupStatus } from "../../types";
import { InstallInventoryPanel } from "./InstallInventoryPanel";

function inventory(overrides: Partial<SetupInstallInventory> = {}): SetupInstallInventory {
  const base: SetupInstallInventory = {
    alfred_home: "/tmp/alfred-home",
    env_path: "/tmp/alfred-home/.env",
    env_present: false,
    server_token_present: true,
    agents_conf_path: "/tmp/alfred-home/launchd/agents.conf",
    agents_conf_present: true,
    scheduled_runs: 2,
    selected_repos_env_present: false,
    slack_configured: false,
    memory_configured: false,
    initialized: true,
    items: [
      {
        key: "home",
        label: "Runtime home",
        ok: true,
        detail: "Found /tmp/alfred-home",
        path: "/tmp/alfred-home",
      },
      {
        key: "env",
        label: "Configuration file",
        ok: false,
        detail: "Not created yet /tmp/alfred-home/.env",
        path: "/tmp/alfred-home/.env",
      },
      {
        key: "agents",
        label: "Scheduled fleet",
        ok: true,
        detail: "2 enabled scheduled runs in agents.conf",
        path: "/tmp/alfred-home/launchd/agents.conf",
      },
      {
        key: "repos",
        label: "Repository scope",
        ok: true,
        detail: "1 selected repos in ALFRED_QUEUE_REPOS, ALFRED_SHIPPED_REPOS",
        path: "/tmp/alfred-home/.env",
      },
      {
        key: "slack",
        label: "Slack approvals",
        ok: false,
        detail: "Optional. Not configured yet.",
        path: null,
        optional: true,
      },
      {
        key: "memory",
        label: "Memory layer",
        ok: true,
        detail: "Using bundled local Redis Agent Memory defaults.",
        path: null,
      },
      {
        key: "token",
        label: "Desktop mutation token",
        ok: true,
        detail: "Runtime token is present for desktop actions.",
        path: "/tmp/alfred-home/state",
      },
    ],
  };
  return { ...base, ...overrides };
}

describe("InstallInventoryPanel", () => {
  it("shows a missing required .env file as a blocking setup item", () => {
    render(<InstallInventoryPanel inventory={inventory()} />);

    expect(screen.getByText(/configuration file/i)).toBeInTheDocument();
    expect(screen.getByText(/not created yet/i)).toBeInTheDocument();
    expect(screen.getByText(/1 to finish/i)).toBeInTheDocument();
    expect(screen.queryByText(/ready to use/i)).not.toBeInTheDocument();
  });

  it("blocks ready state when queue coverage misses selected repos", () => {
    const readyInventory = inventory({
      env_present: true,
      items: inventory().items.map((item) =>
        item.key === "env" ? { ...item, ok: true, detail: "Found /tmp/alfred-home/.env" } : item,
      ),
    });
    const queue: NonNullable<SetupStatus["queue"]> = {
      ready: true,
      count: 1,
      covers_selected: false,
      missing_selected: ["acme/mobile"],
    };

    render(<InstallInventoryPanel inventory={readyInventory} queue={queue} />);

    expect(screen.getByText(/queue coverage/i)).toBeInTheDocument();
    expect(screen.getByText(/missing 1 selected repo/i)).toBeInTheDocument();
    expect(screen.getByText(/1 to finish/i)).toBeInTheDocument();
    expect(screen.queryByText(/ready to use/i)).not.toBeInTheDocument();
  });

  it("blocks ready state when unmanaged scheduler jobs are detected", () => {
    const mixedInventory = inventory({
      env_present: true,
      unmanaged_scheduler_jobs: ["old.agent.batman"],
      unmanaged_scheduler_count: 1,
      items: [
        ...inventory().items.map((item) =>
          item.key === "env"
            ? { ...item, ok: true, detail: "Found /tmp/alfred-home/.env" }
            : item,
        ),
        {
          key: "scheduler_unmanaged",
          label: "Unmanaged scheduler jobs",
          ok: false,
          detail: "1 unmanaged Alfred launchd job found: old.agent.batman.",
          path: "/Users/alice/Library/LaunchAgents",
        },
      ],
    });

    render(<InstallInventoryPanel inventory={mixedInventory} />);

    expect(screen.getByText(/unmanaged scheduler jobs/i)).toBeInTheDocument();
    expect(screen.getByText(/old\.agent\.batman/i)).toBeInTheDocument();
    expect(screen.getByText(/1 to finish/i)).toBeInTheDocument();
    expect(screen.queryByText(/ready to use/i)).not.toBeInTheDocument();
  });
});
