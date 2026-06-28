import {
  CheckCircle2,
  MemoryStick,
  Play,
  Radio,
  RefreshCw,
  Server,
  TerminalSquare,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { errorDetail, loadSetupStatus, supportsNativeActions } from "../api";
import type { ActionNotice, NativeActionRequest } from "../lib/uiTypes";
import type { SetupStatus, TrustedSlackUsersResponse } from "../types";
import { EmptyState, PanelHeader } from "./atoms";
import { InstallInventoryPanel } from "./onboarding/InstallInventoryPanel";
import { Tabs, type TabItem } from "./Tabs";

type SetupSubtab = "connection" | "collaborators" | "diagnostics";

export function SetupView({
  baseUrl,
  loading,
  connected,
  actionNotice,
  trustedSlack,
  busyTrustedUser,
  nativeBusy,
  onAddTrustedUser,
  onRemoveTrustedUser,
  onRunLocalAction,
  onStartRuntime,
  onConnectServer,
}: {
  baseUrl: string;
  loading: boolean;
  connected: boolean;
  actionNotice: ActionNotice;
  trustedSlack: TrustedSlackUsersResponse | null;
  busyTrustedUser: string | null;
  nativeBusy: string | null;
  onAddTrustedUser: (userId: string) => void;
  onRemoveTrustedUser: (userId: string) => void;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onStartRuntime: () => void;
  onConnectServer: (url: string) => void;
}) {
  const canRun = supportsNativeActions();
  const [consoleAgent, setConsoleAgent] = useState("lucius");
  const [serverUrl, setServerUrl] = useState(baseUrl);
  const [trustedUserId, setTrustedUserId] = useState("");
  const [subtab, setSubtab] = useState<SetupSubtab>("connection");
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [setupLoading, setSetupLoading] = useState(false);
  const setupRequestSeq = useRef(0);
  const baseUrlRef = useRef(baseUrl);
  const connectedRef = useRef(connected);
  const connectionGenerationRef = useRef(0);
  const trustedUsers = trustedSlack?.users || [];
  const canAddTrusted = Boolean(trustedUserId.trim()) && !busyTrustedUser;

  useEffect(() => {
    baseUrlRef.current = baseUrl;
    setServerUrl(baseUrl);
  }, [baseUrl]);

  useEffect(() => {
    connectedRef.current = connected;
    if (!connected) {
      connectionGenerationRef.current += 1;
    }
  }, [connected]);

  const refreshSetupStatus = useCallback(() => {
    if (!connected) {
      setupRequestSeq.current += 1;
      setSetupStatus(null);
      setSetupLoading(false);
      return;
    }
    const requestId = setupRequestSeq.current + 1;
    setupRequestSeq.current = requestId;
    const requestBaseUrl = baseUrl;
    const requestGeneration = connectionGenerationRef.current;
    const requestIsCurrent = () =>
      setupRequestSeq.current === requestId &&
      baseUrlRef.current === requestBaseUrl &&
      connectedRef.current &&
      connectionGenerationRef.current === requestGeneration;
    setSetupLoading(true);
    setSetupError(null);
    void loadSetupStatus(baseUrl)
      .then((next) => {
        if (requestIsCurrent()) {
          setSetupStatus(next);
        }
      })
      .catch((err) => {
        if (requestIsCurrent()) {
          setSetupStatus(null);
          setSetupError(errorDetail(err) || "Could not read setup status.");
        }
      })
      .finally(() => {
        if (requestIsCurrent()) {
          setSetupLoading(false);
        }
      });
  }, [baseUrl, connected]);

  useEffect(() => {
    refreshSetupStatus();
    return () => {
      setupRequestSeq.current += 1;
    };
  }, [refreshSetupStatus]);

  const tabs: TabItem<SetupSubtab>[] = [
    { key: "connection", label: "Connection" },
    { key: "collaborators", label: "Collaborators", badge: trustedUsers.length || null },
    { key: "diagnostics", label: "Diagnostics" },
  ];

  return (
    <section className="panel animate-rise setup-view">
      <PanelHeader eyebrow="Setup" title="Connect and configure Alfred" />
      <Tabs
        tabs={tabs}
        active={subtab}
        onChange={setSubtab}
        idBase="setup"
        ariaLabel="Setup sections"
      />
      <div id="setup-panel" role="tabpanel" className="subtab-panel">
        {subtab === "connection" ? (
          <div className="setup-section">
            <p className="panel-intro">
              Point this client at your local <code>alfred serve</code>. The client is the friendly
              path, Slack stays the collaboration UI, and the CLI is the inspectable runtime
              underneath.
            </p>
            <InstallInventoryPanel inventory={setupStatus?.install ?? null} compact />
            {setupError ? (
              <p className="console-note">
                Setup inventory unavailable: {setupError}
              </p>
            ) : null}
            <form
              className="server-connect-form"
              onSubmit={(event) => {
                event.preventDefault();
                const nextUrl = serverUrl.trim();
                if (nextUrl) onConnectServer(nextUrl);
              }}
            >
              <label htmlFor="server-url">Local server URL</label>
              <div className="server-row">
                <input
                  id="server-url"
                  value={serverUrl}
                  onChange={(event) => setServerUrl(event.currentTarget.value)}
                  placeholder="http://127.0.0.1:7010"
                  spellCheck={false}
                />
                <button
                  className="secondary-button"
                  type="submit"
                  disabled={loading || !serverUrl.trim()}
                >
                  <span>{loading ? "Checking" : "Use URL"}</span>
                </button>
              </div>
            </form>
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
                disabled={!canRun || nativeBusy === "auth_status:fleet"}
                onClick={() => onRunLocalAction({ action: "auth_status" })}
              >
                <CheckCircle2 size={16} aria-hidden="true" />
                <span>Auth check</span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={setupLoading}
                onClick={refreshSetupStatus}
              >
                <RefreshCw
                  size={16}
                  aria-hidden="true"
                  className={setupLoading ? "animate-spin" : undefined}
                />
                <span>{setupLoading ? "Checking" : "Recheck setup"}</span>
              </button>
            </div>
            {!canRun ? (
              <p className="console-note">
                Native actions appear in the desktop app. Browser preview stays read-only.
              </p>
            ) : null}
          </div>
        ) : null}

        {subtab === "collaborators" ? (
          <div className="setup-section">
            <p className="panel-intro">
              Add people who can discuss plans and request drafts in Slack. The final approval gate
              stays with the designated operator.
            </p>
            {actionNotice ? (
              <p className={`inline-notice inline-notice--${actionNotice.tone}`}>
                {actionNotice.message}
              </p>
            ) : null}
            <form
              className="trusted-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!canAddTrusted) return;
                onAddTrustedUser(trustedUserId.trim());
                setTrustedUserId("");
              }}
            >
              <label htmlFor="trusted-user-id">Slack user ID</label>
              <div className="trusted-form__row">
                <input
                  id="trusted-user-id"
                  value={trustedUserId}
                  onChange={(event) => setTrustedUserId(event.currentTarget.value)}
                  placeholder="U0123ABCDEF"
                  spellCheck={false}
                />
                <button className="icon-button" type="submit" disabled={!canAddTrusted}>
                  <UserPlus size={16} aria-hidden="true" />
                  <span>{busyTrustedUser?.startsWith("add:") ? "Adding" : "Trust"}</span>
                </button>
              </div>
            </form>
            <div className="trusted-list" aria-label="Trusted Slack collaborators">
              {trustedUsers.length ? (
                trustedUsers.map((user) => (
                  <div className="trusted-user" key={user.user_id}>
                    <Users size={16} aria-hidden="true" />
                    <div>
                      <strong>{user.user_id}</strong>
                      <span>{user.sources.join(", ")}</span>
                    </div>
                    {user.can_remove ? (
                      <button
                        className="ghost-icon"
                        type="button"
                        aria-label={`Remove ${user.user_id}`}
                        disabled={busyTrustedUser === `remove:${user.user_id}`}
                        onClick={() => onRemoveTrustedUser(user.user_id)}
                      >
                        <X size={15} aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                ))
              ) : (
                <EmptyState
                  title="No collaborators yet."
                  body="Add a Slack user ID above so they can discuss plans with Alfred."
                  compact
                />
              )}
            </div>
          </div>
        ) : null}

        {subtab === "diagnostics" ? (
          <div className="setup-section">
            <p className="panel-intro">
              Raw runtime probes for power users. Output appears in the result panel at the top of
              the app. Per-agent controls live on Agents; memory checks live in Learnings.
            </p>
            <div className="console-panel__actions">
              <button
                className="secondary-button"
                type="button"
                disabled={!canRun || nativeBusy === "status:fleet"}
                onClick={() => onRunLocalAction({ action: "status", refreshAfter: true })}
              >
                <TerminalSquare size={16} aria-hidden="true" />
                <span>Agent status</span>
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
                disabled={!canRun || nativeBusy === "code_memory_status:fleet"}
                onClick={() => onRunLocalAction({ action: "code_memory_status" })}
              >
                <MemoryStick size={16} aria-hidden="true" />
                <span>Code memory</span>
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
                <code>alfred serve --port 7010</code>
                <code>alfred status --json</code>
                <code>alfred auth status</code>
                <code>alfred agents</code>
                <code>alfred brain status --json</code>
                <code>alfred code-memory doctor</code>
                <code>alfred brain redis-status --json</code>
                <code>alfred dry-run &lt;codename&gt;</code>
                <code>alfred pause &lt;codename&gt;</code>
                <code>alfred resume &lt;codename&gt;</code>
                <code>alfred run &lt;codename&gt;</code>
              </div>
            </details>
            {!canRun ? (
              <p className="console-note">
                Native actions appear in the desktop app. Browser preview stays read-only.
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
