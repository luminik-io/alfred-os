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

  it("shows detected roster theme and repo local map state", () => {
    const base = inventory();
    render(
      <InstallInventoryPanel
        inventory={{
          ...base,
          roster_theme: {
            theme: "custom",
            label: "Custom",
            path: "/tmp/alfred-home/state/roster-theme/roster-theme.json",
            custom_names_count: 2,
            custom_roles_count: 1,
            updated_at: "2026-06-30T12:00:00Z",
          },
          repo_local_map: {
            present: true,
            count: 2,
            entries: [
              { repo: "acme/api", path: "/Users/example/api" },
              { repo: "acme/site", path: "../marketing/site" },
            ],
          },
          custom_agents: {
            path: "/tmp/alfred-home/state/custom-agents/custom-agents.json",
            count: 1,
            enabled_count: 1,
            disabled_count: 0,
            agents: [
              {
                codename: "release-captain",
                display_name: "Release Captain",
                role_title: "Release coordinator",
                enabled: true,
                engine: "codex",
                schedule: "interval:1800",
              },
            ],
          },
          items: [
            ...base.items,
            {
              key: "repo-map",
              label: "Repo local map",
              ok: true,
              detail: "2 repo local path mappings configured.",
              path: "/tmp/alfred-home/.env",
              optional: true,
            },
            {
              key: "roster-theme",
              label: "Roster theme",
              ok: true,
              detail: "Custom roster active with 2 names and 1 role label.",
              path: "/tmp/alfred-home/state/roster-theme/roster-theme.json",
            },
            {
              key: "custom-agents",
              label: "Custom agents",
              ok: true,
              detail: "1 custom runtime agent; 1 enabled.",
              path: "/tmp/alfred-home/state/custom-agents/custom-agents.json",
              optional: true,
            },
          ],
        }}
      />,
    );

    expect(screen.getByText(/repo local map/i)).toBeInTheDocument();
    expect(screen.getByText("acme/site")).toBeInTheDocument();
    expect(screen.getByText("../marketing/site")).toBeInTheDocument();
    expect(screen.getByText(/roster theme/i)).toBeInTheDocument();
    expect(screen.getByText(/custom roster active with 2 names and 1 role label/i)).toBeInTheDocument();
    expect(screen.getByText(/custom agents/i)).toBeInTheDocument();
    expect(screen.getByText("Release Captain")).toBeInTheDocument();
    expect(screen.getByText(/release-captain \/ Release coordinator \/ codex \/ interval:1800/i)).toBeInTheDocument();
  });
});
