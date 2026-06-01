import { useEffect, useState } from "react";

import type { FiringRecord } from "../types";
import { exactTime, friendlyTime } from "../format";
import { EmptyState, PanelHeader, RunCard } from "./atoms";

export function RunsView({ firings }: { firings: FiringRecord[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(firings[0]?.firing_id || null);
  const selectedFiring =
    firings.find((firing) => firing.firing_id === selectedId) || firings[0] || null;

  useEffect(() => {
    if (!firings.length) {
      setSelectedId(null);
      return;
    }
    if (!firings.some((firing) => firing.firing_id === selectedId)) {
      setSelectedId(firings[0].firing_id);
    }
  }, [firings, selectedId]);

  return (
    <section className="panel">
      <PanelHeader eyebrow="Runtime" title="Recent firings" />
      {firings.length ? (
        <div className="inspect-layout">
          <div className="run-list">
            {firings.map((firing) => (
              <RunCard
                key={firing.firing_id}
                firing={firing}
                selected={firing.firing_id === selectedFiring?.firing_id}
                onSelect={(nextFiring) => setSelectedId(nextFiring.firing_id)}
              />
            ))}
          </div>
          <RunInspector firing={selectedFiring} />
        </div>
      ) : (
        <EmptyState
          title="No runs found."
          body="Run an agent or check that this client points at the same ALFRED_HOME as alfred serve."
        />
      )}
    </section>
  );
}

function RunInspector({ firing }: { firing: FiringRecord | null }) {
  if (!firing) {
    return <EmptyState title="Select a run." body="Choose a firing to inspect its local trace." />;
  }
  return (
    <aside className="detail-panel" aria-label="Selected firing details">
      <div className="detail-panel__head">
        <span>{firing.codename}</span>
        <h3>{firing.summary || firing.firing_id}</h3>
      </div>
      <dl className="compact-meta">
        <div>
          <dt>Status</dt>
          <dd>{firing.status}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd title={exactTime(firing.started_at)}>{friendlyTime(firing.started_at)}</dd>
        </div>
        {firing.ended_at ? (
          <div>
            <dt>Ended</dt>
            <dd title={exactTime(firing.ended_at)}>{friendlyTime(firing.ended_at)}</dd>
          </div>
        ) : null}
        {firing.transcript_path ? (
          <div>
            <dt>Transcript</dt>
            <dd>{firing.transcript_path}</dd>
          </div>
        ) : null}
      </dl>
      <pre className="detail-pre">
        {JSON.stringify(
          {
            firing_id: firing.firing_id,
            events_path: firing.events_path,
            events: firing.raw_events || [],
          },
          null,
          2,
        )}
      </pre>
    </aside>
  );
}
