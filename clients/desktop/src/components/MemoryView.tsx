import { MemoryStick, TerminalSquare } from "lucide-react";

import { titleCase } from "../format";
import { supportsNativeActions } from "../api";
import type { Snapshot } from "../types";
import type { NativeActionRequest } from "../lib/uiTypes";
import { EmptyState, PanelHeader, SignalCard } from "./atoms";

export function MemoryView({
  snapshot,
  nativeBusy,
  onRunLocalAction,
}: {
  snapshot: Snapshot | null;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const suggestions = snapshot?.actions.promotion_suggestions || [];
  const errors = snapshot?.actions.errors || {};
  const canRun = supportsNativeActions();

  return (
    <section className="dashboard-grid dashboard-grid--memory">
      <div className="panel panel--wide">
        <PanelHeader eyebrow="Memory" title="Review queue" />
        {suggestions.length ? (
          <div className="attention-list">
            {suggestions.map((signal, index) => (
              <SignalCard key={`${signal.title || signal.message || "memory"}-${index}`} signal={signal} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No memory candidates surfaced."
            body="Promotion suggestions show up here after fleet-brain finds high-confidence lessons with evidence."
          />
        )}
      </div>
      <div className="panel">
        <PanelHeader eyebrow="Checks" title="Memory health" />
        {Object.keys(errors).length ? (
          <dl className="health-list">
            {Object.entries(errors).map(([key, value]) => (
              <div key={key}>
                <dt>{titleCase(key)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <EmptyState
            title="No memory errors reported."
            body="Use the action below for a deeper local memory health report."
            tone="ok"
          />
        )}
        <div className="button-stack">
          {canRun ? (
            <>
              <button
                className="icon-button"
                type="button"
                disabled={nativeBusy === "brain_doctor:fleet"}
                onClick={() => onRunLocalAction({ action: "brain_doctor" })}
              >
                <TerminalSquare size={16} aria-hidden="true" />
                <span>{nativeBusy === "brain_doctor:fleet" ? "Checking" : "Run memory check"}</span>
              </button>
              <button
                className="secondary-button"
                type="button"
                disabled={nativeBusy === "redis_status:fleet"}
                onClick={() => onRunLocalAction({ action: "redis_status" })}
              >
                <MemoryStick size={16} aria-hidden="true" />
                <span>
                  {nativeBusy === "redis_status:fleet" ? "Checking" : "Check Redis memory"}
                </span>
              </button>
            </>
          ) : (
            <p className="console-note">
              Memory actions run inside the desktop app. Browser preview stays read-only.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
