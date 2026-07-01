import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { OnboardingView } from "./OnboardingView";
import type {
  SetupPlaybooksResponse,
  SetupReposResponse,
  SetupStatus,
  TrustedSlackUsersResponse,
} from "../types";

function makeStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    github: { ok: true, account: "octocat", detail: "Signed in to GitHub as octocat." },
    engines: [
      { name: "claude", installed: true, path: "/opt/homebrew/bin/claude" },
      { name: "codex", installed: false, path: null },
    ],
    engine_ready: true,
    code_memory: {
      enabled: true,
      autofetch: true,
      binary: {
        resolved: false,
        path: null,
        source: "none",
        configured: null,
      },
      version_pin: "v0.8.1",
      repo: "DeusData/codebase-memory-mcp",
      index_dir: "/tmp/.alfred/state/code-memory",
      index_present: false,
      repos: { configured: [], count: 0 },
      detail:
        "Code-memory binary is not installed yet; Alfred can fetch the pinned release on first explicit use.",
    },
    repos: { selected: [], count: 0, keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"] },
    demo: { present: false },
    ready: false,
    ...overrides,
  };
}

function makeInstall(overrides: Partial<NonNullable<SetupStatus["install"]>> = {}): NonNullable<SetupStatus["install"]> {
  const base: NonNullable<SetupStatus["install"]> = {
    alfred_home: "/tmp/alfred-home",
    env_path: "/tmp/alfred-home/.env",
    env_present: true,
    server_token_present: true,
    agents_conf_path: "/tmp/alfred-home/launchd/agents.conf",
    agents_conf_present: true,
    scheduled_runs: 3,
    selected_repos_env_present: true,
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
        key: "agents",
        label: "Scheduled fleet",
        ok: true,
        detail: "3 enabled scheduled runs in agents.conf",
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

const REPOS: SetupReposResponse = {
  repos: [
    {
      name_with_owner: "octocat/web",
      description: "The marketing site",
      is_private: false,
      is_fork: false,
      updated_at: "2026-06-01T00:00:00Z",
      selected: false,
    },
    {
      name_with_owner: "octocat/api",
      description: null,
      is_private: true,
      is_fork: false,
      updated_at: "2026-06-02T00:00:00Z",
      selected: false,
    },
  ],
  selected: [],
};

const PLAYBOOKS: SetupPlaybooksResponse = {
  playbooks: [
    { key: "triage-prs", title: "Triage open PRs every night", summary: "Review open PRs nightly." },
    { key: "fix-failing-ci", title: "Fix failing CI", summary: "Diagnose and fix a failing check." },
  ],
};

const TRUSTED_EMPTY: TrustedSlackUsersResponse = {
  operator_user_id: null,
  users: [],
  state_path: "/tmp/trusted.json",
};

function defaultRosterProps() {
  return {
    rosterTheme: "batman" as const,
    customNames: { names: {}, roles: {} },
    rosterSaveError: null,
    onRosterThemeChange: vi.fn(),
    onEditCustomTheme: vi.fn(),
  };
}

function renderOnboarding(props: Partial<React.ComponentProps<typeof OnboardingView>> = {}) {
  return render(
    <OnboardingView
      baseUrl="http://127.0.0.1:7010"
      loading={false}
      connected
      canRun
      nativeBusy={null}
      nativeResult={null}
      onConnectServer={vi.fn()}
      onStartRuntime={vi.fn()}
      onRunLocalAction={vi.fn(async () => null)}
      onOpenConnection={vi.fn()}
      onSwitch={vi.fn()}
      onRefreshBoard={vi.fn(async () => undefined)}
      {...defaultRosterProps()}
      {...props}
    />,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// The rail buttons are the reliable way to reach a given step from any state.
async function gotoStep(user: ReturnType<typeof userEvent.setup>, railName: RegExp) {
  await user.click(await screen.findByRole("button", { name: railName }));
}

beforeEach(() => {
  vi.spyOn(api, "supportsNativeActions").mockReturnValue(true);
  vi.spyOn(api, "loadSetupStatus").mockResolvedValue(makeStatus());
  vi.spyOn(api, "loadSetupRepos").mockResolvedValue(REPOS);
  vi.spyOn(api, "loadSetupPlaybooks").mockResolvedValue(PLAYBOOKS);
  vi.spyOn(api, "loadTrustedSlackUsers").mockResolvedValue(TRUSTED_EMPTY);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OnboardingView seven-step takeover", () => {
  it("opens on the welcome step with the mental model and no-terminal framing", async () => {
    renderOnboarding();
    expect(
      await screen.findByText(/wake up to shipped work you can trust/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/opens pull requests, handles reviews, and reports back in slack/i),
    ).toBeInTheDocument();
    // The trust differentiator is on the first screen, not buried.
    expect(
      screen.getByText(/runs on the claude max and codex pro subscriptions you already pay for/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/you will not need a terminal/i)).toBeInTheDocument();
    // The persistent rail shows all seven steps.
    expect(screen.getByRole("button", { name: /^welcome$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^tools$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^github$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^repositories$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^team$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^slack$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^first request$/i })).toBeInTheDocument();
  });

  it("shows detected existing install inventory on the welcome step", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        install: makeInstall(),
        repos: { selected: ["octocat/web"], count: 1, keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"] },
      }),
    );
    renderOnboarding();

    expect(await screen.findByText(/existing setup/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /review your alfred setup/i })).toBeInTheDocument();
    expect(screen.queryByText(/^first run$/i)).not.toBeInTheDocument();
    expect(await screen.findByText(/found an alfred setup on this mac/i)).toBeInTheDocument();
    expect(screen.getAllByText("/tmp/alfred-home").length).toBeGreaterThan(0);
    expect(screen.getByText(/3 enabled scheduled runs in agents\.conf/i)).toBeInTheDocument();
    expect(screen.getByText(/optional\. not configured yet/i)).toBeInTheDocument();
    expect(screen.getByText(/ready to use/i)).toBeInTheDocument();
  });

  it("uses neutral shell copy while setup inventory is loading", async () => {
    const pending = deferred<SetupStatus>();
    vi.spyOn(api, "loadSetupStatus").mockReturnValue(pending.promise);
    renderOnboarding();

    expect(screen.getByText(/checking setup/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /checking this mac/i })).toBeInTheDocument();
    expect(screen.queryByText(/^first run$/i)).not.toBeInTheDocument();

    pending.resolve(makeStatus());
    await waitFor(() => expect(screen.getByText(/first run/i)).toBeInTheDocument());
  });

  it("does not show checking copy when no server is connected", () => {
    renderOnboarding({ connected: false });

    expect(screen.getByText(/^first run$/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /let's connect alfred/i })).toBeInTheDocument();
    expect(screen.queryByText(/checking setup/i)).not.toBeInTheDocument();
  });

  it("clears displayed welcome inventory while a new server URL is loading", async () => {
    const newRequest = deferred<SetupStatus>();
    vi.spyOn(api, "loadSetupStatus")
      .mockResolvedValueOnce(
        makeStatus({ install: makeInstall({ alfred_home: "/tmp/old-alfred-home" }) }),
      )
      .mockReturnValueOnce(newRequest.promise);

    const view = renderOnboarding({ baseUrl: "http://127.0.0.1:7010" });
    expect(await screen.findByText("/tmp/old-alfred-home")).toBeInTheDocument();

    view.rerender(
      <OnboardingView
        baseUrl="http://127.0.0.1:7011"
        loading={false}
        connected
        canRun
        nativeBusy={null}
        nativeResult={null}
        onConnectServer={vi.fn()}
        onStartRuntime={vi.fn()}
        onRunLocalAction={vi.fn(async () => null)}
        onOpenConnection={vi.fn()}
        onSwitch={vi.fn()}
        onRefreshBoard={vi.fn(async () => undefined)}
        {...defaultRosterProps()}
      />,
    );

    await waitFor(() => {
      expect(screen.queryByText("/tmp/old-alfred-home")).not.toBeInTheDocument();
    });

    newRequest.resolve(
      makeStatus({ install: makeInstall({ alfred_home: "/tmp/new-alfred-home" }) }),
    );
    expect(await screen.findByText("/tmp/new-alfred-home")).toBeInTheDocument();
  });

  it("ignores stale welcome inventory reads after the server URL changes", async () => {
    const oldRequest = deferred<SetupStatus>();
    const newRequest = deferred<SetupStatus>();
    vi.spyOn(api, "loadSetupStatus")
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);

    const view = renderOnboarding({ baseUrl: "http://127.0.0.1:7010" });
    view.rerender(
      <OnboardingView
        baseUrl="http://127.0.0.1:7011"
        loading={false}
        connected
        canRun
        nativeBusy={null}
        nativeResult={null}
        onConnectServer={vi.fn()}
        onStartRuntime={vi.fn()}
        onRunLocalAction={vi.fn(async () => null)}
        onOpenConnection={vi.fn()}
        onSwitch={vi.fn()}
        onRefreshBoard={vi.fn(async () => undefined)}
        {...defaultRosterProps()}
      />,
    );

    newRequest.resolve(
      makeStatus({ install: makeInstall({ alfred_home: "/tmp/new-alfred-home" }) }),
    );
    expect(await screen.findByText("/tmp/new-alfred-home")).toBeInTheDocument();

    oldRequest.resolve(
      makeStatus({ install: makeInstall({ alfred_home: "/tmp/old-alfred-home" }) }),
    );
    await waitFor(() => {
      expect(screen.queryByText("/tmp/old-alfred-home")).not.toBeInTheDocument();
    });
  });

  it("welcome 'Get started' moves to the tools step", async () => {
    // Engine not ready yet, so Tools does not auto-advance and the user sees it.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /get started/i }));
    expect(screen.getByText(/no api keys needed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check my tools/i })).toBeInTheDocument();
  });

  it("welcome dev shortcut 'I have a server running' jumps to GitHub", async () => {
    renderOnboarding({ connected: false });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /i have a server running/i }));
    expect(screen.getAllByText(/connect github/i).length).toBeGreaterThan(0);
  });

  it("detects CLIs via a native auth probe on the tools step", async () => {
    const onRunLocalAction = vi.fn();
    // Keep gh NOT signed in so auto-advance does not skip past Tools immediately.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding({ onRunLocalAction });
    const user = userEvent.setup();
    await gotoStep(user, /^tools$/i);
    await user.click(screen.getByRole("button", { name: /check my tools/i }));
    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "auth_status", refreshAfter: true });
  });

  it("surfaces code-memory readiness on the tools step", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
        code_memory: {
          enabled: true,
          autofetch: true,
          binary: {
            resolved: true,
            path: "/opt/alfred/bin/codebase-memory-mcp",
            source: "cache",
            configured: null,
          },
          version_pin: "v0.8.1",
          repo: "DeusData/codebase-memory-mcp",
          index_dir: "/opt/alfred/state/code-memory",
          index_present: true,
          repos: { configured: ["api", "web"], count: 2 },
          detail: "Code-memory binary and index are present.",
        },
        capability_plane: {
          version: 1,
          summary: { ready: 2, actionable: 1, disabled: 0, total: 3 },
          capabilities: [
            {
              key: "code_graph",
              title: "Code graph memory",
              category: "memory",
              recommended: true,
              state: "ready",
              installed: true,
              enabled: true,
              detail: "Code graph index is ready for selected repos.",
              detected: {},
              install_hint: "Run `alfred code-memory doctor`, then `alfred code-memory index`.",
              source: {
                source: "DeusData/codebase-memory-mcp",
                url: "https://github.com/DeusData/codebase-memory-mcp",
                license: "MIT",
              },
            },
            {
              key: "context_compression",
              title: "Context compression",
              category: "tokens",
              recommended: true,
              state: "available",
              installed: true,
              enabled: false,
              detail: "Headroom CLI is installed; runner integration is not wired yet.",
              detected: {},
              install_hint: "Run `headroom doctor`, then enable ALFRED_CONTEXT_COMPRESSION.",
              source: {
                source: "headroomlabs-ai/headroom",
                url: "https://github.com/headroomlabs-ai/headroom",
                license: "Apache-2.0",
              },
            },
            {
              key: "engineering_skills",
              title: "Engineering skill packs",
              category: "skills",
              recommended: true,
              state: "ready",
              installed: true,
              enabled: true,
              detail: "At least one engineering skill pack is installed.",
              detected: {},
              install_hint: "Install gstack and the Vercel/Addy agent-skill packs.",
              source: { source: "garrytan/gstack", url: "https://github.com/garrytan/gstack" },
            },
          ],
        },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^tools$/i);

    expect(await screen.findByText(/^code memory$/i)).toBeInTheDocument();
    expect(screen.getByText(/code-memory binary and index are present/i)).toBeInTheDocument();
    expect(screen.getByText(/local capabilities/i)).toBeInTheDocument();
    expect(screen.getByText(/2 of 3 ready, 1 to finish/i)).toBeInTheDocument();
    expect(screen.getByText(/code graph index is ready for selected repos/i)).toBeInTheDocument();
    expect(screen.getByText(/headroom cli is installed/i)).toBeInTheDocument();
    expect(screen.getByText(/run `headroom doctor`/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /headroomlabs-ai\/headroom/i })).toHaveAttribute(
      "href",
      "https://github.com/headroomlabs-ai/headroom",
    );
    await user.click(screen.getByText(/advanced: code-memory probe/i));
    expect(screen.getByText(/DeusData\/codebase-memory-mcp@v0.8.1/i)).toBeInTheDocument();
    expect(screen.getByText(/configured repos/i)).toBeInTheDocument();
    expect(screen.getByText(/api, web/i)).toBeInTheDocument();
  });

  it("labels disabled optional capabilities without calling setup ready", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
        capability_plane: {
          version: 1,
          summary: { ready: 0, actionable: 0, disabled: 1, total: 1 },
          capabilities: [
            {
              key: "context_compression",
              title: "Context compression",
              category: "tokens",
              recommended: false,
              state: "disabled",
              installed: false,
              enabled: false,
              detail: "Context compression is optional for this install.",
              detected: {},
              install_hint: "Install Headroom if you want token compression.",
              source: { source: "headroomlabs-ai/headroom" },
            },
          ],
        },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^tools$/i);

    expect(await screen.findByText(/local capabilities/i)).toBeInTheDocument();
    expect(screen.getByText(/0 of 1 ready/i)).toBeInTheDocument();
    expect(screen.getByText(/^optional$/i)).toBeInTheDocument();
    expect(screen.queryByText(/^ready$/i)).not.toBeInTheDocument();
  });

  it("handles older code-memory payloads without repo metadata", async () => {
    const legacyCodeMemory = { ...makeStatus().code_memory! };
    delete legacyCodeMemory.repos;
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
        code_memory: {
          ...legacyCodeMemory,
          binary: {
            resolved: true,
            path: "/opt/alfred/bin/codebase-memory-mcp",
            source: "cache",
            configured: null,
          },
          index_present: true,
          detail: "Code-memory binary and index are present.",
        },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^tools$/i);

    expect(await screen.findByText(/code-memory binary and index are present/i)).toBeInTheDocument();
    await user.click(screen.getByText(/advanced: code-memory probe/i));
    expect(screen.getByText(/auto-discovered repos/i)).toBeInTheDocument();
    expect(screen.getByText(/none found yet/i)).toBeInTheDocument();
  });

  it("shows an honest empty state when no engine is found", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^tools$/i);
    expect(await screen.findByText(/no engine found yet/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /install claude code/i })).toBeInTheDocument();
  });

  it("shows 'Signed in' on the GitHub step and never asks for a token paste", async () => {
    // Opening GitHub deliberately from the rail does not auto-advance away, so a
    // signed-in user can still read the confirmation.
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^github$/i);
    expect(await screen.findByText(/signed in to github as octocat/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
  });

  it("auto-advances through Tools and GitHub from the forward flow when both are detected", async () => {
    // engine_ready true + github ok: Get started lands on Tools, which
    // auto-advances to GitHub, which auto-advances to Repositories.
    renderOnboarding();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /get started/i }));
    expect(
      await screen.findByRole("button", { name: /load my repositories/i }),
    ).toBeInTheDocument();
  });

  it("keeps the user on Tools when local capabilities need attention", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        github: { ok: true, account: "octocat", detail: "Signed in to GitHub as octocat." },
        engine_ready: true,
        capability_plane: {
          version: 1,
          summary: { ready: 1, actionable: 1, disabled: 0, total: 2 },
          capabilities: [
            {
              key: "code_graph",
              title: "Code graph memory",
              category: "memory",
              recommended: true,
              state: "ready",
              installed: true,
              enabled: true,
              detail: "Code graph index is ready.",
              detected: {},
              install_hint: "Run `alfred code-memory doctor`.",
              source: { source: "DeusData/codebase-memory-mcp" },
            },
            {
              key: "context_compression",
              title: "Context compression",
              category: "tokens",
              recommended: true,
              state: "available",
              installed: true,
              enabled: false,
              detail: "Headroom CLI is installed; runner integration is not wired yet.",
              detected: {},
              install_hint: "Run `headroom doctor`, then enable ALFRED_CONTEXT_COMPRESSION.",
              source: { source: "headroomlabs-ai/headroom" },
            },
          ],
        },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /get started/i }));

    expect(await screen.findByText(/local capabilities/i)).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 ready, 1 to finish/i)).toBeInTheDocument();
    expect(screen.getByText(/^needs attention$/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /load my repositories/i })).not.toBeInTheDocument();
  });

  it("starts native GitHub web sign-in and polls until setup reports connected", async () => {
    const pending = makeStatus({
      github: { ok: false, account: null, detail: "Not signed in to GitHub." },
    });
    const loadStatus = vi
      .spyOn(api, "loadSetupStatus")
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce(makeStatus());
    const onRunLocalAction = vi.fn(async () => ({
      command: ["gh", "auth", "login", "--web"],
      stdout: "",
      stderr: "",
      status: null,
      success: true,
      pid: 42,
      message: "GitHub sign-in started. Enter the one-time code in your browser.",
      github_auth: {
        device_url: "https://github.com/login/device",
        device_code: "ABCD-1234",
        poll_interval_ms: 250,
        timeout_ms: 1_000,
      },
    }));
    renderOnboarding({ onRunLocalAction });
    const user = userEvent.setup();
    await gotoStep(user, /^github$/i);

    await user.click(await screen.findByRole("button", { name: /sign in with github/i }));

    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "github_auth_login" });
    await waitFor(() => expect(loadStatus).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByText(/signed in to github as octocat/i)).toBeInTheDocument(),
    );
  });

  it("ignores GitHub auth poll inventory after same-url disconnect and reconnect", async () => {
    const pending = makeStatus({
      github: { ok: false, account: null, detail: "Not signed in to GitHub." },
    });
    const pollStatus = deferred<SetupStatus>();
    const loadStatus = vi
      .spyOn(api, "loadSetupStatus")
      .mockResolvedValue(pending)
      .mockResolvedValueOnce(pending)
      .mockReturnValueOnce(pollStatus.promise);
    const onRunLocalAction = vi.fn(async () => ({
      command: ["gh", "auth", "login", "--web"],
      stdout: "",
      stderr: "",
      status: null,
      success: true,
      pid: 42,
      message: "GitHub sign-in started. Enter the one-time code in your browser.",
      github_auth: {
        device_url: "https://github.com/login/device",
        device_code: "ABCD-1234",
        poll_interval_ms: 250,
        timeout_ms: 1_000,
      },
    }));
    const props = {
      baseUrl: "http://127.0.0.1:7010",
      loading: false,
      canRun: true,
      nativeBusy: null,
      nativeResult: null,
      onConnectServer: vi.fn(),
      onStartRuntime: vi.fn(),
      onRunLocalAction,
      onOpenConnection: vi.fn(),
      onSwitch: vi.fn(),
      onRefreshBoard: vi.fn(async () => undefined),
      ...defaultRosterProps(),
    };
    const view = render(<OnboardingView {...props} connected />);
    const user = userEvent.setup();
    await gotoStep(user, /^github$/i);

    await user.click(await screen.findByRole("button", { name: /sign in with github/i }));
    await waitFor(() => expect(loadStatus).toHaveBeenCalledTimes(2));

    view.rerender(<OnboardingView {...props} connected={false} />);
    view.rerender(<OnboardingView {...props} connected />);
    await waitFor(() => expect(loadStatus).toHaveBeenCalledTimes(3));
    expect(await screen.findByText(/GitHub sign-in was interrupted/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /sign in with github/i })).toBeEnabled(),
    );

    pollStatus.resolve(
      makeStatus({
        install: makeInstall({ alfred_home: "/tmp/stale-alfred-home" }),
      }),
    );

    await waitFor(() => {
      expect(screen.queryByText("/tmp/stale-alfred-home")).not.toBeInTheDocument();
      expect(screen.queryByText(/found an alfred setup on this mac/i)).not.toBeInTheDocument();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /sign in with github/i })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: /sign in with github/i }));
    expect(onRunLocalAction).toHaveBeenCalledTimes(2);
  });

  it("reenables GitHub sign-in when stale native auth resolves after reconnect", async () => {
    const pending = makeStatus({
      github: { ok: false, account: null, detail: "Not signed in to GitHub." },
    });
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(pending);
    const nativeAuth = deferred<Awaited<ReturnType<React.ComponentProps<typeof OnboardingView>["onRunLocalAction"]>>>();
    const onRunLocalAction = vi.fn(() => nativeAuth.promise);
    const props = {
      baseUrl: "http://127.0.0.1:7010",
      loading: false,
      canRun: true,
      nativeBusy: null,
      nativeResult: null,
      onConnectServer: vi.fn(),
      onStartRuntime: vi.fn(),
      onRunLocalAction,
      onOpenConnection: vi.fn(),
      onSwitch: vi.fn(),
      onRefreshBoard: vi.fn(async () => undefined),
      ...defaultRosterProps(),
    };
    const view = render(<OnboardingView {...props} connected />);
    const user = userEvent.setup();
    await gotoStep(user, /^github$/i);

    await user.click(await screen.findByRole("button", { name: /sign in with github/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /starting/i })).toBeDisabled(),
    );

    view.rerender(<OnboardingView {...props} connected={false} />);
    view.rerender(<OnboardingView {...props} connected />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /sign in with github/i })).toBeEnabled(),
    );
    expect(screen.getByText(/GitHub sign-in was interrupted/i)).toBeInTheDocument();

    nativeAuth.resolve({
      command: ["gh", "auth", "login", "--web"],
      stdout: "",
      stderr: "",
      status: null,
      success: true,
      pid: 42,
      message: "GitHub sign-in started. Enter the one-time code in your browser.",
      github_auth: {
        device_url: "https://github.com/login/device",
        device_code: "ABCD-1234",
        poll_interval_ms: 250,
        timeout_ms: 1_000,
      },
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /sign in with github/i })).toBeEnabled(),
    );
    expect(screen.queryByRole("button", { name: /waiting for github/i })).not.toBeInTheDocument();
  });

  it("falls back to copy-paste gh auth + recheck in browser mode", async () => {
    vi.spyOn(api, "supportsNativeActions").mockReturnValue(false);
    const refetch = vi
      .spyOn(api, "loadSetupStatus")
      .mockResolvedValue(
        makeStatus({
          engine_ready: false,
          github: { ok: false, account: null, detail: "Not signed in to GitHub." },
        }),
      );
    renderOnboarding({ canRun: false });
    const user = userEvent.setup();
    await gotoStep(user, /^github$/i);
    expect(screen.queryByRole("button", { name: /sign in with github/i })).not.toBeInTheDocument();
    await user.click(screen.getByText(/advanced: terminal fallback/i));
    expect(screen.getByText("gh auth login --web")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /recheck github/i }));
    await waitFor(() => expect(refetch).toHaveBeenCalled());
  });

  it("loads, picks, and saves repositories leading with name + description", async () => {
    const save = vi.spyOn(api, "saveSetupRepos").mockResolvedValue({
      ok: true,
      repos: ["octocat/web"],
      env_path: "/home/.alfred/.env",
      keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"],
    });
    renderOnboarding();
    const user = userEvent.setup();

    // Forward flow auto-advances Tools + GitHub (both detected) onto Repositories.
    await user.click(await screen.findByRole("button", { name: /get started/i }));
    const loadButton = await screen.findByRole("button", { name: /load my repositories/i });
    await user.click(loadButton);
    // Leads with the short name and the description, with the full slug present.
    await waitFor(() => expect(screen.getByText("web")).toBeInTheDocument());
    expect(screen.getByText(/the marketing site/i)).toBeInTheDocument();
    expect(screen.getByText("octocat/web")).toBeInTheDocument();
    // Private badge on the private repo.
    expect(screen.getByText(/private/i)).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /octocat\/web/i }));
    await user.click(screen.getByRole("button", { name: /save 1 repository/i }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("http://127.0.0.1:7010", ["octocat/web"]),
    );
    await waitFor(() =>
      expect(screen.getByText(/saved 1 repository alfred can work in/i)).toBeInTheDocument(),
    );
  });

  it("blocks the repo step until GitHub is connected", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^repositories$/i);
    expect(screen.getByText(/connect github first/i)).toBeInTheDocument();
  });

  it("lets the user choose the agent naming theme before Slack", async () => {
    const onRosterThemeChange = vi.fn();
    const onEditCustomTheme = vi.fn();
    renderOnboarding({
      rosterTheme: "transformers",
      onRosterThemeChange,
      onEditCustomTheme,
    });
    const user = userEvent.setup();
    await gotoStep(user, /^team$/i);

    const stepper = screen.getByRole("navigation", { name: /onboarding progress/i });
    expect(within(stepper).getByRole("button", { current: "step" })).toHaveAccessibleName(
      /^team$/i,
    );
    expect(
      within(stepper).getByLabelText("3 of 7 onboarding steps complete"),
    ).toBeInTheDocument();
    expect(screen.getByText(/active roster/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /transformers/i })).toBeInTheDocument();
    expect(screen.getByText("Optimus Prime")).toBeInTheDocument();
    expect(
      screen.getByText(/roles, permissions, schedules, labels, worktrees, and merge gates stay unchanged/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /customize/i }));
    expect(onEditCustomTheme).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("combobox", { name: /roster theme/i }));
    await user.click(await screen.findByRole("option", { name: /justice league/i }));
    expect(onRosterThemeChange).toHaveBeenCalledWith("justice-league");
  });

  it("treats Slack as optional and skippable, advancing to the first request", async () => {
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^slack$/i);
    expect(screen.getByText(/want approvals and questions in slack/i)).toBeInTheDocument();
    // Skip is a first-class button, not a tiny link.
    await user.click(screen.getByRole("button", { name: /skip for now/i }));
    await waitFor(() =>
      expect(screen.getByText(/pick something for alfred to do first/i)).toBeInTheDocument(),
    );
  });

  it("lets a Dev add a trusted Slack approver", async () => {
    const add = vi.spyOn(api, "addTrustedSlackUser").mockResolvedValue({
      operator_user_id: null,
      users: [
        { user_id: "U999", sources: ["onboarding"], added_at: null, added_by: null, can_remove: true },
      ],
      state_path: "/tmp/trusted.json",
      added: true,
    });
    renderOnboarding();
    const user = userEvent.setup();
    await gotoStep(user, /^slack$/i);
    await user.click(screen.getByText(/add a slack approver/i));
    await user.type(screen.getByLabelText(/slack user id/i), "U999");
    await user.click(screen.getByRole("button", { name: /^trust$/i }));
    await waitFor(() => expect(add).toHaveBeenCalledWith("http://127.0.0.1:7010", "U999"));
    await waitFor(() => expect(screen.getByText("U999")).toBeInTheDocument());
  });

  it("composes a starter spec into a real first request and lands on Ask", async () => {
    const compose = vi.spyOn(api, "composeSetupPlaybook").mockResolvedValue({
      ok: true,
      playbook: "triage-prs",
      draft_id: "compose-x",
      saved_path: "/p.json",
      title: "Nightly: triage open pull requests",
      repos: ["octocat/web"],
      readiness: { ok: false, score: 0.4 },
    });
    const onSwitch = vi.fn();
    renderOnboarding({ onSwitch });
    const user = userEvent.setup();

    await gotoStep(user, /^first request$/i);
    await waitFor(() =>
      expect(screen.getByText(/triage open prs every night/i)).toBeInTheDocument(),
    );
    const card = screen.getByText(/triage open prs every night/i).closest("[data-slot='card']");
    await user.click(within(card as HTMLElement).getByRole("button", { name: /use this/i }));

    await waitFor(() => expect(compose).toHaveBeenCalledWith("http://127.0.0.1:7010", "triage-prs"));
    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith("compose"));
  });

  it("seeds a labelled demo lifecycle and lands on a populated Inbox", async () => {
    const seed = vi.spyOn(api, "seedSetupDemo").mockResolvedValue({ seeded: true });
    const onSwitch = vi.fn();
    const onRefreshBoard = vi.fn(async () => undefined);
    renderOnboarding({ onSwitch, onRefreshBoard });
    const user = userEvent.setup();

    await gotoStep(user, /^first request$/i);
    await user.click(await screen.findByRole("button", { name: /show me a sample first/i }));
    await waitFor(() => expect(seed).toHaveBeenCalledWith("http://127.0.0.1:7010"));
    await waitFor(() => expect(onRefreshBoard).toHaveBeenCalledWith({ demo: true }));
    // The sample is not a one-way door: an "Open Inbox" control lands the user on
    // the populated board only when they choose to.
    await user.click(await screen.findByRole("button", { name: /open inbox/i }));
    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith("home"));
  });

  it("clears the seeded sample and flips the board back out of demo mode", async () => {
    vi.spyOn(api, "seedSetupDemo").mockResolvedValue({ seeded: true });
    const clear = vi.spyOn(api, "clearSetupDemo").mockResolvedValue({ cleared: true });
    const onRefreshBoard = vi.fn(async () => undefined);
    renderOnboarding({ onRefreshBoard });
    const user = userEvent.setup();

    await gotoStep(user, /^first request$/i);
    await user.click(await screen.findByRole("button", { name: /show me a sample first/i }));
    // Once seeded, the step surfaces a visible clear control instead of stranding
    // the user in demo mode.
    const clearButton = await screen.findByRole("button", { name: /clear sample data/i });
    await user.click(clearButton);
    await waitFor(() => expect(clear).toHaveBeenCalledWith("http://127.0.0.1:7010"));
    await waitFor(() => expect(onRefreshBoard).toHaveBeenCalledWith({ demo: false }));
    // The clear returns the step to its pre-seed offer.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /show me a sample first/i })).toBeInTheDocument(),
    );
  });

  it("shows the clear-sample exit when the server already reports demo present", async () => {
    // Simulate a remount after the sample was seeded in a prior mount (open
    // Inbox, reload, navigate back). The in-component seed flag has reset, but
    // the server still reports demo.present, so the step must derive the
    // "Clear sample data" exit from server truth rather than strand the user.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({ demo: { present: true } }),
    );
    renderOnboarding();
    const user = userEvent.setup();

    await gotoStep(user, /^first request$/i);
    // No seed click in this mount, yet the clear control is present.
    expect(
      await screen.findByRole("button", { name: /clear sample data/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /show me a sample first/i }),
    ).not.toBeInTheDocument();
  });

  it("Enter advances to the next step when focus is not in a field", async () => {
    // Engine + GitHub both not detected so neither step auto-advances and the
    // Enter-driven move from Tools to GitHub is observable.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    // Land on Tools via the rail.
    await gotoStep(user, /^tools$/i);
    expect(screen.getByRole("button", { name: /check my tools/i })).toBeInTheDocument();
    // Press Enter on the takeover section (not a field) to continue to GitHub.
    // The section carries a heading, an element with no input semantics, so the
    // handler treats it as a continue.
    const section = screen.getByLabelText(/set up alfred/i);
    fireEvent.keyDown(section, { key: "Enter" });
    await waitFor(() =>
      expect(screen.getAllByText(/connect github/i).length).toBeGreaterThan(0),
    );
  });

  it("opens the advanced setup handoff from the header", async () => {
    const onOpenConnection = vi.fn();
    renderOnboarding({ onOpenConnection });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /advanced setup/i }));
    expect(onOpenConnection).toHaveBeenCalledTimes(1);
  });

  it("degrades mutating steps gracefully off-Tauri with a clear note", async () => {
    vi.spyOn(api, "supportsNativeActions").mockReturnValue(false);
    renderOnboarding({ canRun: false });
    const user = userEvent.setup();
    await gotoStep(user, /^first request$/i);
    await waitFor(() =>
      expect(screen.getByText(/triage open prs every night/i)).toBeInTheDocument(),
    );
    expect(screen.getAllByText(/desktop app/i).length).toBeGreaterThan(0);
    // The demo seed control is disabled in the browser preview.
    expect(screen.getByRole("button", { name: /show me a sample first/i })).toBeDisabled();
  });

  it("surfaces a setup-status read error without blanking the steps", async () => {
    vi.spyOn(api, "loadSetupStatus").mockRejectedValue(new Error("boom"));
    renderOnboarding();
    expect(await screen.findByText(/manual fallback/i)).toBeInTheDocument();
    // The welcome step still renders.
    expect(screen.getByText(/wake up to shipped work you can trust/i)).toBeInTheDocument();
  });

  it("tracks progress in the rail as steps complete", async () => {
    // gh + engine ready: the forward flow auto-advances through Tools + GitHub,
    // so the progress label reflects real completion.
    renderOnboarding();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /get started/i }));
    expect(
      await screen.findByRole("button", { name: /load my repositories/i }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText(/onboarding steps complete/i)).toBeInTheDocument(),
    );
  });

  it("renders a persistent numbered stepper with current and upcoming states", async () => {
    // Engine + gh not detected so nothing auto-advances and Welcome stays current.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const stepper = await screen.findByRole("navigation", { name: /onboarding progress/i });
    // The active node carries aria-current="step" and is the Welcome node.
    const current = within(stepper).getByRole("button", { current: "step" });
    expect(current).toHaveAccessibleName(/welcome/i);
    // All seven numbered nodes are present and queryable by their bare labels.
    for (const label of [
      /^welcome$/i,
      /^tools$/i,
      /^github$/i,
      /^repositories$/i,
      /^team$/i,
      /^slack$/i,
      /^first request$/i,
    ]) {
      expect(within(stepper).getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("marks detected steps done in the stepper", async () => {
    // engine_ready + github ok: once the forward flow lands on Repositories,
    // Welcome, Tools, and GitHub read as done (aria-current moves to repos).
    renderOnboarding();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /get started/i }));
    await screen.findByRole("button", { name: /load my repositories/i });
    const stepper = screen.getByRole("navigation", { name: /onboarding progress/i });
    const current = within(stepper).getByRole("button", { current: "step" });
    expect(current).toHaveAccessibleName(/repositories/i);
    // The completion count reflects the three detected-done steps.
    expect(within(stepper).getByLabelText(/onboarding steps complete/i)).toHaveTextContent(
      /3 of 7/i,
    );
  });

  it("opens on Welcome at 0 of 7 done even when tools, gh and repos are pre-detected", async () => {
    // Regression for the broken progress logic: on a fresh launch where Claude
    // Code is installed, gh is already signed in, and repos are already saved,
    // the rail used to show "3 of 7 done" while the user was still on step 1
    // (Welcome). The count must reflect where the user actually is, so a step the
    // user has not reached never reads done even when its signal is satisfied.
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: true,
        github: { ok: true, account: "octocat", detail: "Signed in to GitHub as octocat." },
        repos: { selected: ["octocat/web"], count: 1, keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"] },
      }),
    );
    renderOnboarding();
    const stepper = await screen.findByRole("navigation", { name: /onboarding progress/i });
    // The active node is Welcome and nothing reads done.
    const current = within(stepper).getByRole("button", { current: "step" });
    expect(current).toHaveAccessibleName(/welcome/i);
    await waitFor(() =>
      expect(within(stepper).getByLabelText(/onboarding steps complete/i)).toHaveTextContent(
        /0 of 7/i,
      ),
    );
  });

  it("moves Back and Continue through the footer", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        engine_ready: false,
        github: { ok: false, account: null, detail: "Not signed in to GitHub." },
      }),
    );
    renderOnboarding();
    const user = userEvent.setup();
    // From Welcome, Continue advances to Tools.
    await user.click(await screen.findByRole("button", { name: /^continue$/i }));
    expect(screen.getByRole("button", { name: /check my tools/i })).toBeInTheDocument();
    // Back returns to Welcome.
    await user.click(screen.getByRole("button", { name: /^back$/i }));
    expect(screen.getByText(/wake up to shipped work you can trust/i)).toBeInTheDocument();
    // Back is disabled on the first step.
    expect(screen.getByRole("button", { name: /^back$/i })).toBeDisabled();
  });
});
