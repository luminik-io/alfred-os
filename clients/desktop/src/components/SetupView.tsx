import {
  CheckCircle2,
  ExternalLink,
  GitPullRequest,
  ListChecks,
  MemoryStick,
  Play,
  Radio,
  Server,
  TerminalSquare,
} from "lucide-react";
import { useState } from "react";

import { supportsNativeActions } from "../api";
import { localUrl } from "../lib/links";
import type { NativeActionRequest } from "../lib/uiTypes";
import { ExternalButton, PanelHeader } from "./atoms";

export function SetupView({
  baseUrl,
  nativeBusy,
  onRunLocalAction,
  onStartRuntime,
}: {
  baseUrl: string;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onStartRuntime: () => void;
}) {
  const canRun = supportsNativeActions();
  const [consoleAgent, setConsoleAgent] = useState("lucius");

  return (
    <section className="dashboard-grid">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Setup" title="Action console" />
        <p className="panel-intro">
          The client is the friendly path. Slack remains the collaboration UI, and the CLI remains
          the inspectable runtime underneath. These buttons run Alfred actions locally and show the
          terminal-style result in this app.
        </p>
        <div className="console-panel" aria-label="Local Alfred command console">
          <div className="console-panel__actions">
            <button
              className="icon-button"
              type="button"
              disabled={!canRun || nativeBusy === "runtime:start"}
              onClick={onStartRuntime}
            >
              <Play size={16} aria-hidden="true" />
              <span>{nativeBusy === "runtime:start" ? "Starting" : "Start runtime"}</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "status:fleet"}
              onClick={() => onRunLocalAction({ action: "status", refreshAfter: true })}
            >
              <TerminalSquare size={16} aria-hidden="true" />
              <span>Fleet status</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "auth_status:fleet"}
              onClick={() => onRunLocalAction({ action: "auth_status" })}
            >
              <CheckCircle2 size={16} aria-hidden="true" />
              <span>Auth check</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "agents:fleet"}
              onClick={() => onRunLocalAction({ action: "agents", refreshAfter: true })}
            >
              <Server size={16} aria-hidden="true" />
              <span>Agents</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "brain_doctor:fleet"}
              onClick={() => onRunLocalAction({ action: "brain_doctor" })}
            >
              <MemoryStick size={16} aria-hidden="true" />
              <span>Memory</span>
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={!canRun || nativeBusy === "redis_status:fleet"}
              onClick={() => onRunLocalAction({ action: "redis_status" })}
            >
              <Radio size={16} aria-hidden="true" />
              <span>Redis</span>
            </button>
          </div>
          <div className="console-agent-row">
            <label htmlFor="dry-run-agent">Dry-run agent</label>
            <input
              id="dry-run-agent"
              value={consoleAgent}
              onChange={(event) => setConsoleAgent(event.currentTarget.value)}
              spellCheck={false}
            />
            <button
              className="icon-button"
              type="button"
              disabled={!canRun || nativeBusy === `dry_run:${consoleAgent.trim()}`}
              onClick={() =>
                onRunLocalAction({
                  action: "dry_run",
                  target: consoleAgent.trim(),
                  refreshAfter: true,
                })
              }
            >
              <Play size={16} aria-hidden="true" />
              <span>Run dry-run</span>
            </button>
          </div>
          {!canRun ? (
            <p className="console-note">
              Native actions appear here in the desktop app. Browser preview stays read-only.
            </p>
          ) : null}
        </div>
        <details className="cli-fallback">
          <summary>
            <strong>What runs underneath</strong>
            <span>Transparent previews for the curated local actions.</span>
          </summary>
          <p>
            Alfred does not expose an arbitrary shell here. Each button maps to a narrow local
            action, then the result panel shows the command, exit status, stdout, and stderr.
          </p>
          <div className="cli-chip-list">
            <code>alfred serve --port 7000 --no-browser</code>
            <code>alfred status --json</code>
            <code>alfred auth status</code>
            <code>alfred agents</code>
            <code>alfred brain status --json</code>
            <code>alfred brain redis-status --json</code>
            <code>alfred dry-run &lt;codename&gt;</code>
            <code>alfred pause &lt;codename&gt;</code>
            <code>alfred resume &lt;codename&gt;</code>
            <code>alfred run &lt;codename&gt;</code>
          </div>
        </details>
      </div>
      <div className="panel">
        <PanelHeader eyebrow="Links" title="Open locally" />
        <div className="link-stack">
          <ExternalButton label="Open serve" href={baseUrl} icon={<ExternalLink size={16} />} />
          <ExternalButton
            label="Open plans"
            href={localUrl(baseUrl, "/plans")}
            icon={<ListChecks size={16} />}
          />
          <ExternalButton
            label="Open GitHub"
            href="https://github.com/luminik-io/alfred-os"
            icon={<GitPullRequest size={16} />}
          />
        </div>
      </div>
    </section>
  );
}
