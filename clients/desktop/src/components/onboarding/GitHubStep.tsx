import { CheckCircle2, ExternalLink, KeyRound, Loader2, PlayCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import type { GithubAuthFlow } from "./types";
import type { SetupStatus } from "../../types";
import { Button, Card, CardContent, Input, Label } from "../ui";

/**
 * Step 2: Connect GitHub. Alfred reuses the local gh sign-in. When gh is already
 * signed in the orchestrator auto-advances; this body shows the green
 * "Signed in as @account" confirmation. In the desktop shell, a native action
 * starts `gh auth login --web` and the UI polls until setup status confirms the
 * sign-in. The terminal fallback remains behind Advanced for browser mode.
 *
 * Dev shortcut: paste the server URL + start the runtime, kept from the prior
 * onboarding so a Dev who already runs `alfred serve` connects in one move.
 */
export function GitHubStep({
  baseUrl,
  loading,
  connected,
  github,
  canRun,
  nativeBusy,
  authFlow,
  statusLoading,
  onConnectServer,
  onStartRuntime,
  onStartGithubAuth,
  onRecheck,
}: {
  baseUrl: string;
  loading: boolean;
  connected: boolean;
  github: SetupStatus["github"] | null;
  canRun: boolean;
  nativeBusy: string | null;
  authFlow: GithubAuthFlow;
  statusLoading: boolean;
  onConnectServer: (url: string) => void;
  onStartRuntime: () => void;
  onStartGithubAuth: () => void;
  onRecheck: () => void;
}) {
  const [url, setUrl] = useState(baseUrl);
  useEffect(() => {
    setUrl(baseUrl);
  }, [baseUrl]);

  const signedIn = Boolean(github?.ok);
  const loginBusy =
    nativeBusy === "github_auth_login:fleet" ||
    authFlow.state === "starting" ||
    authFlow.state === "waiting";
  const canStartNativeLogin = canRun && connected && !signedIn;

  return (
    <div className="grid gap-4">
      {signedIn ? (
        <Card
          size="sm"
          className="rounded-lg border-primary/25 bg-primary/10 text-primary shadow-none"
        >
          <CardContent className="flex items-center gap-2 px-3 text-sm">
            <CheckCircle2 size={15} aria-hidden="true" />
            <span>{github?.detail || "Signed in to GitHub."}</span>
          </CardContent>
        </Card>
      ) : (
        <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
          <CardContent className="grid gap-3 px-3 text-sm text-muted-foreground">
            <span className="grid gap-1">
              <strong className="text-foreground">Connect GitHub</strong>
              <span>
                {connected
                  ? github?.detail || "Sign in once so Alfred can read and file issues and pull requests."
                  : "Install or connect the local runtime first, then Alfred can read your GitHub sign-in."}
              </span>
            </span>

            <div className="flex flex-wrap gap-2">
              {canStartNativeLogin ? (
                <Button type="button" onClick={onStartGithubAuth} disabled={loginBusy}>
                  {loginBusy ? (
                    <Loader2 size={15} aria-hidden="true" className="animate-spin" />
                  ) : (
                    <KeyRound size={15} aria-hidden="true" />
                  )}
                  <span>
                    {authFlow.state === "waiting"
                      ? "Waiting for GitHub"
                      : authFlow.state === "starting"
                        ? "Starting"
                        : "Sign in with GitHub"}
                  </span>
                </Button>
              ) : null}
              <Button variant="outline" type="button" onClick={onRecheck} disabled={statusLoading}>
                <RefreshCw
                  size={14}
                  aria-hidden="true"
                  className={statusLoading ? "animate-spin" : undefined}
                />
                <span>Recheck GitHub</span>
              </Button>
            </div>

            {authFlow.state !== "idle" ? <GithubAuthFlowCard authFlow={authFlow} /> : null}

            <span>
              {canRun
                ? "Alfred will continue as soon as GitHub confirms the sign-in."
                : "The browser preview cannot start gh auth. Use the terminal fallback below."}
            </span>
          </CardContent>
        </Card>
      )}

      <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none">
        <CardContent className="px-3">
          <details className="group grid gap-2">
            <summary className="cursor-pointer list-none">
              <span className="grid gap-0.5">
                <strong className="text-sm font-medium">Advanced: terminal fallback</strong>
                <span className="text-xs text-muted-foreground">
                  Manual GitHub sign-in and local runtime controls.
                </span>
              </span>
            </summary>
            <p className="mt-3 text-sm text-muted-foreground">
              {canRun
                ? "Use this if the native sign-in window did not open."
                : "Run this once if GitHub is not connected yet, then press Recheck above."}
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <code className="rounded-md border border-border/70 bg-muted/40 px-2 py-1 font-mono text-xs">
                gh auth login --web
              </code>
              <code className="rounded-md border border-border/70 bg-muted/40 px-2 py-1 font-mono text-xs">
                gh auth status
              </code>
            </div>

            <form
              className="mt-4 grid gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const next = url.trim();
                if (next) onConnectServer(next);
              }}
            >
              <Label htmlFor="onboarding-server-url">Local server URL</Label>
              <div className="grid gap-2 md:grid-cols-[1fr_auto_auto]">
                <Input
                  id="onboarding-server-url"
                  value={url}
                  onChange={(event) => setUrl(event.currentTarget.value)}
                  placeholder="http://127.0.0.1:7010"
                  spellCheck={false}
                />
                <Button variant="outline" type="submit" disabled={loading || !url.trim()}>
                  <span>{loading ? "Checking" : "Connect"}</span>
                </Button>
                {canRun && !connected ? (
                  <Button
                    type="button"
                    disabled={nativeBusy === "runtime:start"}
                    onClick={onStartRuntime}
                  >
                    <PlayCircle size={15} aria-hidden="true" />
                    <span>{nativeBusy === "runtime:start" ? "Starting" : "Start runtime"}</span>
                  </Button>
                ) : null}
              </div>
            </form>
          </details>
        </CardContent>
      </Card>

      {github ? (
        <p className="text-sm text-muted-foreground">
          {signedIn
            ? "GitHub is connected. Continue to choose which repositories Alfred may touch."
            : "Once you sign in, your repositories appear in the next step."}
        </p>
      ) : null}
    </div>
  );
}

function GithubAuthFlowCard({ authFlow }: { authFlow: GithubAuthFlow }) {
  const tone =
    authFlow.state === "error" || authFlow.state === "timeout"
      ? "border-destructive/25 bg-destructive/10 text-destructive"
      : authFlow.state === "success"
        ? "border-primary/25 bg-primary/10 text-primary"
        : "border-border/70 bg-background/70 text-foreground";

  return (
    <div className={`grid gap-2 rounded-lg border px-3 py-2 ${tone}`}>
      {authFlow.message ? <span>{authFlow.message}</span> : null}
      {authFlow.deviceCode ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs uppercase tracking-normal text-muted-foreground">Code</span>
          <code className="rounded-md border border-border/70 bg-background/70 px-2 py-1 font-mono text-sm">
            {authFlow.deviceCode}
          </code>
        </div>
      ) : null}
      {authFlow.deviceUrl ? (
        <a
          className="inline-flex items-center gap-1 text-sm underline underline-offset-2"
          href={authFlow.deviceUrl}
          target="_blank"
          rel="noreferrer"
        >
          <span>Open GitHub sign-in</span>
          <ExternalLink size={13} aria-hidden="true" />
        </a>
      ) : null}
      {authFlow.detail ? <span className="text-xs opacity-80">{authFlow.detail}</span> : null}
    </div>
  );
}
