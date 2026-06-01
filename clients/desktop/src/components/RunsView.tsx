import type { FiringRecord } from "../types";
import { EmptyState, PanelHeader, RunCard } from "./atoms";

export function RunsView({ firings, baseUrl }: { firings: FiringRecord[]; baseUrl: string }) {
  return (
    <section className="panel">
      <PanelHeader eyebrow="Runtime" title="Recent firings" />
      {firings.length ? (
        <div className="run-list">
          {firings.map((firing) => (
            <RunCard key={firing.firing_id} firing={firing} baseUrl={baseUrl} />
          ))}
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
