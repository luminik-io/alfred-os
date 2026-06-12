import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { OnboardingView } from "./OnboardingView";
import type {
  SetupPlaybooksResponse,
  SetupReposResponse,
  SetupStatus,
} from "../types";

function makeStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    github: { ok: true, account: "octocat", detail: "Signed in to GitHub as octocat." },
    engines: [
      { name: "claude", installed: true, path: "/opt/homebrew/bin/claude" },
      { name: "codex", installed: false, path: null },
    ],
    engine_ready: true,
    repos: { selected: [], count: 0, keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"] },
    demo: { present: false },
    ready: false,
    ...overrides,
  };
}

const REPOS: SetupReposResponse = {
  repos: [
    {
      name_with_owner: "octocat/web",
      description: "The web app",
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
      onRunLocalAction={vi.fn()}
      onOpenConnection={vi.fn()}
      onSwitch={vi.fn()}
      {...props}
    />,
  );
}

async function openSetupStep(label: RegExp | string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: label }));
  return user;
}

beforeEach(() => {
  // Default to the native-capable path so mutating controls render enabled.
  vi.spyOn(api, "supportsNativeActions").mockReturnValue(true);
  vi.spyOn(api, "loadSetupStatus").mockResolvedValue(makeStatus());
  vi.spyOn(api, "loadSetupRepos").mockResolvedValue(REPOS);
  vi.spyOn(api, "loadSetupPlaybooks").mockResolvedValue(PLAYBOOKS);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OnboardingView", () => {
  it("leads with the no-API-keys CLI framing and short setup path", async () => {
    renderOnboarding();
    await openSetupStep(/tools/i);
    expect(screen.getByText(/use the tools already on this mac/i)).toBeInTheDocument();
    expect(screen.getAllByText(/no api keys/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/no cloud dashboard or token paste/i)).toBeInTheDocument();
  });

  it("renders every step as functional, with no COMING placeholders", async () => {
    renderOnboarding();
    // The three formerly-dead steps now have real progress entries and
    // keyboard/click reachable controls.
    expect(screen.getByRole("button", { name: /repositories/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /first plan/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /work preview/i })).toBeInTheDocument();
    await openSetupStep(/repositories/i);
    expect(screen.getByText(/choose repositories alfred can work in/i)).toBeInTheDocument();
    await openSetupStep(/first plan/i);
    expect(screen.getByText(/draft the first plan from a spec/i)).toBeInTheDocument();
    await openSetupStep(/work preview/i);
    expect(screen.getByRole("button", { name: /seed work preview/i })).toBeInTheDocument();
    // No "coming" tag anywhere.
    expect(screen.queryByText(/^coming$/i)).not.toBeInTheDocument();
    // The real engine probe surfaces the installed/not-found list.
    await openSetupStep(/tools/i);
    await waitFor(() => expect(screen.getAllByText(/installed/i).length).toBeGreaterThan(0));
  });

  it("never asks for a token paste on the GitHub step", async () => {
    renderOnboarding();
    await openSetupStep(/github/i);
    expect(screen.getByText(/reuses your github cli sign-in/i)).toBeInTheDocument();
  });

  it("loads, picks, and saves repositories", async () => {
    const save = vi
      .spyOn(api, "saveSetupRepos")
      .mockResolvedValue({
        ok: true,
        repos: ["octocat/web"],
        env_path: "/home/.alfred/.env",
        keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"],
      });
    renderOnboarding();
    const user = userEvent.setup();

    // The repo step unlocks once the async setup status confirms GitHub.
    const loadButton = await screen.findByRole("button", { name: /load my repositories/i });
    await user.click(loadButton);
    await waitFor(() => expect(screen.getByText("octocat/web")).toBeInTheDocument());

    await user.click(screen.getByRole("checkbox", { name: /octocat\/web/i }));
    await user.click(screen.getByRole("button", { name: /save 1 repository/i }));

    await waitFor(() => expect(save).toHaveBeenCalledWith("http://127.0.0.1:7010", ["octocat/web"]));
    await waitFor(() =>
      expect(screen.getByText(/saved 1 repository alfred can work in/i)).toBeInTheDocument(),
    );
  });

  it("keeps already selected repositories that are missing from the visible gh list", async () => {
    vi.spyOn(api, "loadSetupRepos").mockResolvedValue({
      repos: [REPOS.repos[0]],
      selected: ["example-org/alfred"],
    });
    const save = vi.spyOn(api, "saveSetupRepos").mockResolvedValue({
      ok: true,
      repos: ["example-org/alfred", "octocat/web"],
      env_path: "/home/.alfred/.env",
      keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"],
    });
    renderOnboarding();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /load my repositories/i }));
    await waitFor(() => expect(screen.getByText("octocat/web")).toBeInTheDocument());

    await user.click(screen.getByRole("checkbox", { name: /octocat\/web/i }));
    await user.click(screen.getByRole("button", { name: /save 2 repositories/i }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        "http://127.0.0.1:7010",
        expect.arrayContaining(["example-org/alfred", "octocat/web"]),
      ),
    );
  });

  it("composes a starter spec into a real first request", async () => {
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

    await user.click(await screen.findByRole("button", { name: /first plan/i }));
    await waitFor(() => expect(screen.getByText(/triage open prs every night/i)).toBeInTheDocument());
    const card = screen.getByText(/triage open prs every night/i).closest("[data-slot='card']");
    await user.click(within(card as HTMLElement).getByRole("button", { name: /use this/i }));

    await waitFor(() => expect(compose).toHaveBeenCalledWith("http://127.0.0.1:7010", "triage-prs"));
    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith("compose"));
  });

  it("seeds and clears the Work preview", async () => {
    const seed = vi.spyOn(api, "seedSetupDemo").mockResolvedValue({ seeded: true });
    const clear = vi.spyOn(api, "clearSetupDemo").mockResolvedValue({ cleared: true });
    // After seeding, status reports the demo present so the Clear control appears.
    vi.spyOn(api, "loadSetupStatus")
      .mockResolvedValueOnce(makeStatus())
      .mockResolvedValueOnce(makeStatus({ demo: { present: true } }))
      .mockResolvedValue(makeStatus({ demo: { present: false } }));
    const onSwitch = vi.fn();
    const onRefreshBoard = vi.fn(async () => undefined);
    renderOnboarding({ onSwitch, onRefreshBoard });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /work preview/i }));
    await user.click(screen.getByRole("button", { name: /seed work preview/i }));
    await waitFor(() => expect(seed).toHaveBeenCalledWith("http://127.0.0.1:7010"));
    await waitFor(() => expect(onRefreshBoard).toHaveBeenCalledWith({ demo: true }));
    await waitFor(() => expect(onSwitch).toHaveBeenCalledWith("pipeline"));
    await user.click(await screen.findByRole("button", { name: /work preview/i }));
    const clearButton = await screen.findByRole("button", { name: /clear demo/i });
    await user.click(clearButton);
    await waitFor(() => expect(clear).toHaveBeenCalledWith("http://127.0.0.1:7010"));
    await waitFor(() => expect(onRefreshBoard).toHaveBeenCalledWith({ demo: false }));
  });

  it("does not mark the first plan done just because demo cards exist", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({
        repos: {
          selected: ["octocat/web"],
          count: 1,
          keys: ["ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS"],
        },
        demo: { present: true },
        ready: true,
      }),
    );
    renderOnboarding();

    await waitFor(() => expect(screen.getByText(/ready to plan/i)).toBeInTheDocument());
    await openSetupStep(/first plan/i);
    expect(screen.getByText(/draft the first plan from a spec/i)).toBeInTheDocument();
    expect(screen.getByText(/recommended next/i)).toBeInTheDocument();
    expect(screen.queryByText(/starter plan drafted/i)).not.toBeInTheDocument();
  });

  it("detects CLIs via a native auth probe", async () => {
    const onRunLocalAction = vi.fn();
    renderOnboarding({ onRunLocalAction });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /tools/i }));
    await user.click(screen.getByRole("button", { name: /check my tools/i }));
    expect(onRunLocalAction).toHaveBeenCalledWith({ action: "auth_status" });
  });

  it("opens the diagnostics surface from the header", async () => {
    const onOpenConnection = vi.fn();
    renderOnboarding({ onOpenConnection });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /diagnostics/i }));
    expect(onOpenConnection).toHaveBeenCalledTimes(1);
  });

  it("only offers to start the runtime when the local server is disconnected", async () => {
    const connectedRender = renderOnboarding({ connected: true, canRun: true });
    await waitFor(() => expect(screen.getAllByText(/signed in to github/i).length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: /start runtime/i })).not.toBeInTheDocument();
    connectedRender.unmount();

    renderOnboarding({ connected: false, canRun: true });
    await openSetupStep(/github/i);
    expect(screen.getByRole("button", { name: /start runtime/i })).toBeInTheDocument();
  });

  it("degrades the mutating steps gracefully off-Tauri (read-only, clear note)", async () => {
    vi.spyOn(api, "supportsNativeActions").mockReturnValue(false);
    renderOnboarding({ canRun: false });
    // The CLI list still renders (server-side probe), but mutating controls are
    // disabled with a clear desktop-mode note.
    await openSetupStep(/first plan/i);
    await waitFor(() => expect(screen.getByText(/triage open prs every night/i)).toBeInTheDocument());
    expect(screen.getAllByText(/desktop mode can/i).length).toBeGreaterThan(0);
    await openSetupStep(/work preview/i);
    expect(screen.getByRole("button", { name: /seed work preview/i })).toBeDisabled();
  });

  it("shows the gh sign-in fallback when GitHub is not connected", async () => {
    vi.spyOn(api, "loadSetupStatus").mockResolvedValue(
      makeStatus({ github: { ok: false, account: null, detail: "Not signed in to GitHub. Run gh auth login once." } }),
    );
    renderOnboarding();
    await waitFor(() => expect(screen.getByText(/not signed in to github/i)).toBeInTheDocument());
    // The repo step blocks until GitHub is connected.
    await openSetupStep(/repositories/i);
    expect(screen.getByText(/connect github first/i)).toBeInTheDocument();
  });
});
