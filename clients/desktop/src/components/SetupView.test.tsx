import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import type { SetupStatus } from "../types";
import { SetupView } from "./SetupView";

function setupStatus(home: string): SetupStatus {
  return {
    github: { ok: true, account: "octocat", detail: "Signed in to GitHub as octocat." },
    engines: [{ name: "claude", installed: true, path: "/opt/homebrew/bin/claude" }],
    engine_ready: true,
    repos: { selected: ["octocat/web"], count: 1, keys: ["ALFRED_QUEUE_REPOS"] },
    demo: { present: false },
    ready: true,
    install: {
      alfred_home: home,
      env_path: `${home}/.env`,
      env_present: true,
      server_token_present: true,
      agents_conf_path: `${home}/launchd/agents.conf`,
      agents_conf_present: true,
      scheduled_runs: 1,
      selected_repos_env_present: true,
      slack_configured: false,
      memory_configured: false,
      initialized: true,
      items: [
        {
          key: "home",
          label: "Runtime home",
          ok: true,
          detail: `Found ${home}`,
          path: home,
        },
        {
          key: "env",
          label: "Configuration file",
          ok: true,
          detail: `Found ${home}/.env`,
          path: `${home}/.env`,
        },
      ],
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function renderSetup(baseUrl: string, props: Partial<React.ComponentProps<typeof SetupView>> = {}) {
  return (
    <SetupView
      baseUrl={baseUrl}
      loading={false}
      connected
      actionNotice={null}
      trustedSlack={null}
      busyTrustedUser={null}
      nativeBusy={null}
      onAddTrustedUser={vi.fn()}
      onRemoveTrustedUser={vi.fn()}
      onRunLocalAction={vi.fn()}
      onStartRuntime={vi.fn()}
      onConnectServer={vi.fn()}
      {...props}
    />
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SetupView", () => {
  it("ignores stale setup inventory reads after the server URL changes", async () => {
    const oldRequest = deferred<SetupStatus>();
    const newRequest = deferred<SetupStatus>();
    vi.spyOn(api, "supportsNativeActions").mockReturnValue(true);
    vi.spyOn(api, "loadSetupStatus")
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);

    const view = render(renderSetup("http://127.0.0.1:7010"));
    view.rerender(renderSetup("http://127.0.0.1:7011"));

    newRequest.resolve(setupStatus("/tmp/new-alfred-home"));
    expect((await screen.findAllByText("/tmp/new-alfred-home")).length).toBeGreaterThan(0);

    oldRequest.resolve(setupStatus("/tmp/old-alfred-home"));
    await waitFor(() => {
      expect(screen.queryByText("/tmp/old-alfred-home")).not.toBeInTheDocument();
    });
  });

  it("ignores stale setup inventory after a same-url disconnect and reconnect", async () => {
    const staleRequest = deferred<SetupStatus>();
    const loadStatus = vi
      .spyOn(api, "loadSetupStatus")
      .mockReturnValueOnce(staleRequest.promise)
      .mockResolvedValue(setupStatus("/tmp/reconnected-alfred-home"));
    vi.spyOn(api, "supportsNativeActions").mockReturnValue(true);

    const view = render(renderSetup("http://127.0.0.1:7010"));
    await waitFor(() => expect(loadStatus).toHaveBeenCalledTimes(1));

    view.rerender(renderSetup("http://127.0.0.1:7010", { connected: false }));
    view.rerender(renderSetup("http://127.0.0.1:7010", { connected: true }));

    expect((await screen.findAllByText("/tmp/reconnected-alfred-home")).length).toBeGreaterThan(
      0,
    );
    staleRequest.resolve(setupStatus("/tmp/stale-alfred-home"));

    await waitFor(() => {
      expect(screen.queryByText("/tmp/stale-alfred-home")).not.toBeInTheDocument();
    });
  });
});
