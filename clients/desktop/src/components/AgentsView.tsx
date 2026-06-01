import { Play } from "lucide-react";

import { exactTime, friendlyTime } from "../format";
import { supportsNativeActions } from "../api";
import type { AgentSummary } from "../types";
import type { NativeActionRequest } from "../lib/uiTypes";
import { EmptyState, PanelHeader, StatusDot } from "./atoms";

export function AgentsView({
  agents,
  nativeBusy,
  onRunLocalAction,
}: {
  agents: AgentSummary[];
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
}) {
  const canRun = supportsNativeActions();
  return (
    <section className="panel">
      <PanelHeader eyebrow="Fleet" title="Agents" />
      {agents.length ? (
        <div className="agent-grid">
          {agents.map((agent) => (
            <article className="agent-card" key={agent.codename}>
              <div className="agent-card__head">
                <strong>{agent.codename}</strong>
                <StatusDot status={agent.status} />
              </div>
              <dl>
                <div>
                  <dt>Last run</dt>
                  <dd title={exactTime(agent.last_run_at)}>{friendlyTime(agent.last_run_at)}</dd>
                </div>
                <div>
                  <dt>Firings today</dt>
                  <dd>{agent.firings_today}</dd>
                </div>
              </dl>
              <p>{agent.last_summary}</p>
              {canRun ? (
                <div className="card-actions card-actions--start">
                  <button
                    className="icon-button"
                    type="button"
                    disabled={nativeBusy === `dry_run:${agent.codename}`}
                    onClick={() =>
                      onRunLocalAction({
                        action: "dry_run",
                        target: agent.codename,
                        refreshAfter: true,
                      })
                    }
                  >
                    <Play size={16} aria-hidden="true" />
                    <span>
                      {nativeBusy === `dry_run:${agent.codename}` ? "Running" : "Dry-run"}
                    </span>
                  </button>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No agents detected."
          body="The local server did not find per-codename state under ALFRED_HOME/state."
        />
      )}
    </section>
  );
}
