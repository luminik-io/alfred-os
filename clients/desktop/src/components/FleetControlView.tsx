import { Pause, Play, RotateCw, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { supportsNativeActions } from "../api";
import { exactTime, friendlyTime } from "../format";
import { buildFleetRows, type FleetControlRow, type FleetServiceState } from "../lib/fleetControl";
import type { NativeActionRequest } from "../lib/uiTypes";
import type { AgentSummary, NativeAction, NativeCommandResult } from "../types";
import { titleCase } from "../format";
import { EmptyState, NativeResultPanel, PanelHeader } from "./atoms";

// State-changing verbs require an explicit confirm; dry-run is side-effect-free
// and runs immediately.
const CONFIRM_VERBS: ReadonlySet<NativeAction> = new Set(["pause", "resume", "run"]);

type PendingAction = { action: NativeAction; codename: string; label: string } | null;

export function FleetControlView({
  agents,
  service,
  nativeBusy,
  nativeResult,
  nativeError,
  nativeErrorRaw,
  onRunLocalAction,
  onRefreshService,
}: {
  agents: AgentSummary[];
  service: FleetServiceState;
  nativeBusy: string | null;
  nativeResult: NativeCommandResult | null;
  nativeError: string | null;
  nativeErrorRaw?: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onRefreshService: () => void;
}) {
  const canRun = supportsNativeActions();
  const rows = buildFleetRows(agents, service);
  const [pending, setPending] = useState<PendingAction>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const affirmRef = useRef<HTMLButtonElement | null>(null);

  const dispatch = (action: NativeAction, codename: string, label: string) => {
    if (CONFIRM_VERBS.has(action)) {
      setPending({ action, codename, label });
      return;
    }
    onRunLocalAction({ action, target: codename, refreshAfter: true });
  };

  const confirm = () => {
    if (!pending) return;
    onRunLocalAction({ action: pending.action, target: pending.codename, refreshAfter: true });
    setPending(null);
  };

  // Confirm-dialog focus + keyboard a11y: move focus to the affirmative button
  // on open, trap Tab inside the dialog, and let Escape cancel. A tiny inline
  // trap keeps the destructive confirm keyboard-operable without a dependency.
  useEffect(() => {
    if (!pending) return;
    affirmRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setPending(null);
        return;
      }
      if (event.key !== "Tab") return;
      const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusables || focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [pending]);

  return (
    <section className="panel">
      <PanelHeader
        eyebrow="Fleet"
        title="Service control"
        actionLabel={canRun ? "Refresh state" : undefined}
        onAction={canRun ? onRefreshService : undefined}
      />
      <p className="panel-intro">
        Pause, resume, run, or dry-run an agent from here. Paused and running state is read from the
        polled <code>/api/status</code> feed; pause and resume change the launchd service.
      </p>

      {pending ? (
        <div
          className="confirm-bar"
          role="alertdialog"
          aria-modal="true"
          aria-label="Confirm fleet action"
          ref={dialogRef}
        >
          <span>
            {confirmCopy(pending.action)} <strong>{pending.codename}</strong>?
          </span>
          <div className="confirm-bar__actions">
            <button
              className="danger-button"
              type="button"
              onClick={confirm}
              ref={affirmRef}
              autoFocus
            >
              <span>Yes, {pending.label.toLowerCase()}</span>
            </button>
            <button className="secondary-button" type="button" onClick={() => setPending(null)}>
              <span>Cancel</span>
            </button>
          </div>
        </div>
      ) : null}

      <NativeResultPanel error={nativeError} errorRaw={nativeErrorRaw} result={nativeResult} />

      {rows.length ? (
        <div className="agent-grid">
          {rows.map((row) => (
            <FleetCard
              key={row.codename}
              row={row}
              canRun={canRun}
              nativeBusy={nativeBusy}
              onDispatch={dispatch}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No agents detected."
          body="Connect to alfred serve to load the fleet from /api/status."
        />
      )}
    </section>
  );
}

function FleetCard({
  row,
  canRun,
  nativeBusy,
  onDispatch,
}: {
  row: FleetControlRow;
  canRun: boolean;
  nativeBusy: string | null;
  onDispatch: (action: NativeAction, codename: string, label: string) => void;
}) {
  const busy = (action: NativeAction) => nativeBusy === `${action}:${row.codename}`;
  return (
    <article className="agent-card">
      <div className="agent-card__head">
        <strong>{row.codename}</strong>
        <ServiceBadge row={row} />
      </div>
      <dl>
        <div>
          <dt>Last run</dt>
          <dd title={exactTime(row.summary?.last_run_at)}>
            {row.summary ? friendlyTime(row.summary.last_run_at) : "—"}
          </dd>
        </div>
        <div>
          <dt>Firings today</dt>
          <dd>{row.summary?.firings_today ?? 0}</dd>
        </div>
      </dl>
      {row.paused && row.pausedSince ? (
        <p>Paused since {friendlyTime(row.pausedSince)}.</p>
      ) : (
        <p>{row.summary?.last_summary || "No firings yet."}</p>
      )}
      {canRun ? (
        <div className="card-actions card-actions--start">
          {row.paused ? (
            <button
              className="warn-button"
              type="button"
              disabled={busy("resume")}
              onClick={() => onDispatch("resume", row.codename, "Resume")}
            >
              <Play size={16} aria-hidden="true" />
              <span>{busy("resume") ? "Resuming" : "Resume"}</span>
            </button>
          ) : (
            <button
              className="secondary-button"
              type="button"
              disabled={busy("pause")}
              onClick={() => onDispatch("pause", row.codename, "Pause")}
            >
              <Pause size={16} aria-hidden="true" />
              <span>{busy("pause") ? "Pausing" : "Pause"}</span>
            </button>
          )}
          <button
            className="secondary-button"
            type="button"
            disabled={busy("run")}
            onClick={() => onDispatch("run", row.codename, "Run")}
          >
            <RotateCw size={16} aria-hidden="true" />
            <span>{busy("run") ? "Running" : "Run once"}</span>
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={busy("dry_run")}
            onClick={() => onDispatch("dry_run", row.codename, "Dry-run")}
          >
            <Square size={16} aria-hidden="true" />
            <span>{busy("dry_run") ? "Running" : "Dry-run"}</span>
          </button>
        </div>
      ) : null}
    </article>
  );
}

function ServiceBadge({ row }: { row: FleetControlRow }) {
  const { tone, label } = serviceTone(row);
  return (
    <span className={`dot-label dot-label--${tone}`}>
      <span aria-hidden="true" />
      {label}
    </span>
  );
}

function serviceTone(row: FleetControlRow): {
  tone: "ok" | "warn" | "error" | "idle";
  label: string;
} {
  if (row.summary?.status === "error" || row.consecutiveFailures >= 2) {
    return { tone: "error", label: "Error" };
  }
  if (row.service === "paused") {
    return { tone: "warn", label: "Paused" };
  }
  if (row.service === "stopped") {
    return { tone: "warn", label: "Stopped" };
  }
  if (row.service === "running") {
    return { tone: "ok", label: "Running" };
  }
  // Unknown service state: fall back to the polled liveness label.
  return { tone: "idle", label: titleCase(row.summary?.status || "idle") };
}

function confirmCopy(action: NativeAction): string {
  if (action === "pause") return "Pause scheduled firings for";
  if (action === "resume") return "Resume scheduled firings for";
  return "Run a one-shot firing of";
}
