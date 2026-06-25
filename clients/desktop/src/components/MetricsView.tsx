import type { MetricsResponse } from "../types";
import { EmptyState, PanelHeader } from "./atoms";

/**
 * MetricsView surfaces the self-benchmark report from GET /api/metrics: the
 * four metric families (throughput / quality / reliability / efficiency) plus
 * the subscription-quota cost framing (percent of a plan's daily turn budget
 * per PR, never dollars). Stub: filled in by the implementation pass.
 */
export function MetricsView({
  metrics,
  state,
}: {
  metrics: MetricsResponse | null;
  state: "idle" | "loading" | "error";
}) {
  return (
    <section className="metrics-view" aria-label="Self-benchmark metrics">
      <PanelHeader eyebrow="Self-benchmark" title="Metrics" />
      <EmptyState
        tone="neutral"
        title="Metrics coming online"
        body={state === "error" ? "Could not load metrics." : "Benchmark view under construction."}
      />
    </section>
  );
}
