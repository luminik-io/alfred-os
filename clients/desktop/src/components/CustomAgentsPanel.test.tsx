import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CustomAgentsPanel } from "./CustomAgentsPanel";
import type { CustomAgentsResponse } from "../types";

const apiMocks = vi.hoisted(() => ({
  deleteCustomAgent: vi.fn(),
  loadCustomAgents: vi.fn(),
  saveCustomAgent: vi.fn(),
  supportsNativeActions: vi.fn(),
}));

vi.mock("../api", () => ({
  deleteCustomAgent: apiMocks.deleteCustomAgent,
  loadCustomAgents: apiMocks.loadCustomAgents,
  saveCustomAgent: apiMocks.saveCustomAgent,
  supportsNativeActions: apiMocks.supportsNativeActions,
}));

const EMPTY: CustomAgentsResponse = {
  version: 1,
  path: "/tmp/alfred/state/custom-agents/custom-agents.json",
  agents: [],
  count: 0,
  enabled_count: 0,
  disabled_count: 0,
  updated_at: null,
};

const WITH_AGENT: CustomAgentsResponse = {
  version: 1,
  path: "/tmp/alfred/state/custom-agents/custom-agents.json",
  agents: [
    {
      codename: "release-captain",
      display_name: "Release Captain",
      role_title: "Release coordinator",
      purpose: "Checks release readiness before handoff.",
      prompt: "Review release readiness and summarize blockers for the operator.",
      engine: "codex",
      schedule: "cron:9:15",
      repos: ["acme/api"],
      enabled: true,
      created_at: "2026-06-28T09:00:00+00:00",
      updated_at: "2026-06-28T09:00:00+00:00",
    },
  ],
  count: 1,
  enabled_count: 1,
  disabled_count: 0,
  updated_at: "2026-06-28T09:00:00+00:00",
};

function renderPanel(onChanged = vi.fn()) {
  render(<CustomAgentsPanel baseUrl="http://127.0.0.1:7010" onChanged={onChanged} />);
  return onChanged;
}

describe("CustomAgentsPanel", () => {
  beforeEach(() => {
    apiMocks.deleteCustomAgent.mockReset();
    apiMocks.loadCustomAgents.mockReset();
    apiMocks.saveCustomAgent.mockReset();
    apiMocks.supportsNativeActions.mockReset();
    apiMocks.supportsNativeActions.mockReturnValue(true);
    apiMocks.loadCustomAgents.mockResolvedValue(EMPTY);
  });

  it("loads existing custom agents with editable prompt context", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue(WITH_AGENT);
    renderPanel();

    expect(await screen.findByText("Release Captain")).toBeInTheDocument();
    expect(screen.getByText("Daily 09:15")).toBeInTheDocument();
    expect(apiMocks.loadCustomAgents).toHaveBeenCalledWith("http://127.0.0.1:7010", {
      includePrompt: true,
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /edit release captain/i }));

    expect(screen.getByLabelText("Codename")).toHaveValue("release-captain");
    expect(screen.getByLabelText("Prompt")).toHaveValue(
      "Review release readiness and summarize blockers for the operator.",
    );
    expect(screen.getByLabelText("Schedule")).toHaveValue("daily@09:15");
    expect(screen.getByRole("radio", { name: /codex/i })).toBeChecked();
  });

  it("round-trips weekly cron schedules through editable shortcuts", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue({
      ...WITH_AGENT,
      agents: [{ ...WITH_AGENT.agents[0], schedule: "cron:1:9:05" }],
    });
    renderPanel();

    expect(await screen.findByText("Weekly Mon 09:05")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /edit release captain/i }));

    expect(screen.getByLabelText("Schedule")).toHaveValue("weekly@mon:09:05");
  });

  it("renders day interval schedule labels in human terms", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue({
      ...WITH_AGENT,
      agents: [{ ...WITH_AGENT.agents[0], schedule: "interval:172800" }],
    });
    renderPanel();

    expect(await screen.findByText("Every 2 days")).toBeInTheDocument();
  });

  it("creates a custom agent and parses repo scope", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue(EMPTY);
    apiMocks.saveCustomAgent.mockResolvedValue({
      ok: true,
      agent: WITH_AGENT.agents[0],
      deploy_required: true,
      detail: "Run `bash deploy.sh` from the source checkout.",
    });
    const onChanged = renderPanel();
    const user = userEvent.setup();

    await screen.findByText("No custom agents yet.");
    await user.type(screen.getByLabelText("Codename"), "release-captain");
    await user.type(screen.getByLabelText("Display name"), "Release Captain");
    await user.type(screen.getByLabelText("Role title"), "Release coordinator");
    await user.clear(screen.getByLabelText("Schedule"));
    await user.type(screen.getByLabelText("Schedule"), "daily@09:15");
    await user.click(screen.getByRole("radio", { name: /codex/i }));
    await user.type(screen.getByLabelText("Purpose"), "Checks release readiness before handoff.");
    await user.type(
      screen.getByLabelText("Prompt"),
      "Review release readiness and summarize blockers for the operator.",
    );
    await user.type(screen.getByLabelText("Repo scope"), "acme/api\nacme/web, acme/api");
    await user.click(screen.getByRole("button", { name: /create agent/i }));

    expect(apiMocks.saveCustomAgent).toHaveBeenCalledWith("http://127.0.0.1:7010", {
      codename: "release-captain",
      display_name: "Release Captain",
      role_title: "Release coordinator",
      purpose: "Checks release readiness before handoff.",
      prompt: "Review release readiness and summarize blockers for the operator.",
      engine: "codex",
      schedule: "daily@09:15",
      repos: ["acme/api", "acme/web"],
      enabled: true,
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(screen.getByText(/Run bash deploy.sh/i)).toBeInTheDocument();
  });

  it("keeps browser preview read-only", async () => {
    apiMocks.supportsNativeActions.mockReturnValue(false);
    renderPanel();

    expect(await screen.findByText("No custom agents yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create agent/i })).toBeDisabled();
    expect(screen.getByRole("note")).toHaveTextContent(/packaged desktop app/i);
  });

  it("falls back to prompt-free inventory when browser preview lacks the token", async () => {
    apiMocks.supportsNativeActions.mockReturnValue(false);
    apiMocks.loadCustomAgents
      .mockRejectedValueOnce(new Error("This action needs the Alfred desktop app."))
      .mockResolvedValueOnce({
        ...WITH_AGENT,
        agents: [{ ...WITH_AGENT.agents[0], prompt: undefined }],
      });

    renderPanel();

    expect(await screen.findByText("Release Captain")).toBeInTheDocument();
    expect(apiMocks.loadCustomAgents).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:7010",
      { includePrompt: true },
    );
    expect(apiMocks.loadCustomAgents).toHaveBeenNthCalledWith(2, "http://127.0.0.1:7010");
  });

  it("removes a custom agent after confirmation", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue(WITH_AGENT);
    apiMocks.deleteCustomAgent.mockResolvedValue({
      ok: true,
      removed: true,
      deploy_required: true,
      detail: "Run `bash deploy.sh` from the source checkout.",
    });
    const onChanged = renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /edit release captain/i }));
    await user.click(screen.getByRole("button", { name: /remove/i }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /remove agent/i }));

    expect(apiMocks.deleteCustomAgent).toHaveBeenCalledWith(
      "http://127.0.0.1:7010",
      "release-captain",
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("closes the delete dialog and surfaces failures where the operator can see them", async () => {
    apiMocks.loadCustomAgents.mockResolvedValue(WITH_AGENT);
    apiMocks.deleteCustomAgent.mockRejectedValue(new Error("delete failed"));
    const onChanged = renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /edit release captain/i }));
    await user.click(screen.getByRole("button", { name: /remove/i }));
    await user.click(screen.getByRole("button", { name: /remove agent/i }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("delete failed");
    expect(onChanged).not.toHaveBeenCalled();
  });
});
