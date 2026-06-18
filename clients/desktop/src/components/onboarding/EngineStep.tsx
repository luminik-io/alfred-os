import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";

import type { NativeActionRequest } from "../../lib/uiTypes";
import type { SetupStatus } from "../../types";
import { Badge, Button, Card, CardContent } from "../ui";
import { cn } from "@/lib/utils";

/**
 * Step 1: Connect Claude / Codex. The Maya path is a single "Check my tools"
 * button that runs the native auth_status action and shows a plain result; the
 * raw engine probe table (SetupStatus.engines) is one disclosure away for Dev.
 * No API keys, said explicitly.
 */
export function EngineStep({
  status,
  engineReady,
  canRun,
  nativeBusy,
  statusLoading,
  onRunLocalAction,
  onRecheck,
}: {
  status: SetupStatus | null;
  engineReady: boolean;
  canRun: boolean;
  nativeBusy: string | null;
  statusLoading: boolean;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onRecheck: () => void;
}) {
  const engines = status?.engines ?? [];
  const readyEngine = engines.find((engine) => engine.installed);

  return (
    <div className="grid gap-4">
      {engineReady ? (
        <Card
          size="sm"
          className="rounded-lg border-primary/25 bg-primary/10 text-primary shadow-none"
        >
          <CardContent className="flex items-center gap-2 px-3 text-sm">
            <CheckCircle2 size={15} aria-hidden="true" />
            <span>
              {readyEngine?.name === "codex" ? "Codex" : "Claude Code"} is ready. Alfred can run work
              on this Mac.
            </span>
          </CardContent>
        </Card>
      ) : (
        <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
          <CardContent className="grid gap-2 px-3 text-sm text-muted-foreground">
            <span>
              <strong className="block text-foreground">No engine found yet.</strong>
              Alfred needs Claude Code or Codex installed on this Mac.
            </span>
            <a
              className="inline-flex w-fit min-h-9 items-center gap-1 rounded-md border border-border/70 bg-background/55 px-2.5 py-1.5 text-sm font-medium text-foreground underline-offset-2 hover:bg-muted/45 hover:underline"
              href="https://docs.anthropic.com/en/docs/claude-code/overview"
              target="_blank"
              rel="noreferrer"
            >
              Install Claude Code
            </a>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          disabled={!canRun || nativeBusy === "auth_status:fleet"}
          onClick={() => onRunLocalAction({ action: "auth_status", refreshAfter: true })}
        >
          <CheckCircle2 size={15} aria-hidden="true" />
          <span>{nativeBusy === "auth_status:fleet" ? "Checking" : "Check my tools"}</span>
        </Button>
        <Button variant="outline" type="button" onClick={onRecheck} disabled={statusLoading}>
          <RefreshCw
            size={14}
            aria-hidden="true"
            className={statusLoading ? "animate-spin" : undefined}
          />
          <span>Recheck</span>
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">No API keys needed.</p>

      {engines.length ? (
        <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none">
          <CardContent className="px-3">
            <details className="group grid gap-2">
              <summary className="cursor-pointer list-none">
                <span className="grid gap-0.5">
                  <strong className="text-sm font-medium">Advanced: engine probe</strong>
                  <span className="text-xs text-muted-foreground">
                    What Alfred detected for each CLI.
                  </span>
                </span>
              </summary>
              <ul className="mt-3 grid gap-2" aria-label="Installed developer tools">
                {engines.map((engine) => (
                  <li
                    key={engine.name}
                    className="flex items-center gap-2 rounded-md border border-border/60 bg-card/60 px-2.5 py-2 text-sm"
                  >
                    {engine.installed ? (
                      <CheckCircle2 size={15} aria-hidden="true" className="text-primary" />
                    ) : (
                      <XCircle size={15} aria-hidden="true" className="text-muted-foreground" />
                    )}
                    <code className="font-mono text-xs">{engine.name}</code>
                    <Badge
                      variant={engine.installed ? "secondary" : "outline"}
                      className={cn("ml-auto")}
                    >
                      {engine.installed ? "installed" : "not found"}
                    </Badge>
                  </li>
                ))}
              </ul>
            </details>
          </CardContent>
        </Card>
      ) : null}

      {!canRun ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          The desktop app runs the deeper CLI check. In the browser preview, this step reads the
          server's engine probe only.
        </p>
      ) : null}
    </div>
  );
}
