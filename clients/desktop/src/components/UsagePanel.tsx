import { Activity, CheckCircle2, Cpu, Timer } from "lucide-react";

import { formatReset, formatTokens } from "../lib/usageFormat";
import type { ShippedBoard, UsageLimitBucket, UsageResponse } from "../types";
import { EmptyState, PanelHeader } from "./atoms";

/**
 * UsagePanel shows real subscription headroom from the local usage reader, not the API
 * list-price of tokens. Under a Max/Pro subscription the per-token dollar
 * figure is meaningless (and $0 for Codex), so this replaces the old
 * misleading "$ spend" tile. It reports the current Claude 5-hour rolling
 * window (tokens used, time to reset, and the reader's burn projection) and a Codex
 * row. When usage logs are unavailable it shows a plain "usage unavailable" note
 * instead of a fabricated number.
 */
export function UsagePanel({
  usage,
  state,
  shipped,
  compact = false,
}: {
  usage: UsageResponse | null;
  state: "idle" | "loading" | "error";
  shipped?: ShippedBoard | null;
  compact?: boolean;
}) {
  return (
    <section className={`usage-panel${compact ? " usage-panel--compact" : ""}`} aria-label="Subscription usage">
      {!compact ? (
        <>
          <PanelHeader
            eyebrow="Subscription usage"
            title="Capacity"
          />
          <p className="panel-intro panel-intro--tight">
            5h and weekly headroom, local tokens, and Alfred-evidenced merges.
          </p>
        </>
      ) : null}
      <UsageBody usage={usage} state={state} shipped={shipped} />
    </section>
  );
}

function UsageBody({
  usage,
  state,
  shipped,
}: {
  usage: UsageResponse | null;
  state: "idle" | "loading" | "error";
  shipped?: ShippedBoard | null;
}) {
  if (!usage) {
    if (state === "loading") {
      return <p className="usage-panel__note">Reading local usage&hellip;</p>;
    }
    return (
      <EmptyState
        title="Usage will appear here."
        body="When local logs are readable, Alfred shows remaining headroom, token evidence, and shipped output."
        compact
      />
    );
  }

  if (!usage.available) {
    return (
      <EmptyState
        title="Usage unavailable."
        body={
          usage.error
            ? `Alfred could not read local usage logs (${usage.error}). Check the runtime host and refresh.`
            : "Alfred could not read local usage logs on the runtime host. Check the runtime and refresh."
        }
        compact
        tone="error"
      />
    );
  }

  const block = usage.block;
  const fiveHour = usage.limits?.five_hour ?? null;
  const week = usage.limits?.seven_day ?? null;
  // A partial failure (Codex read but the active 5h block did not) must not
  // render like a genuinely empty window, or the operator is told there is no
  // Claude usage when really the headroom could not be read.
  const blockError = !block ? usage.errors?.block : undefined;
  return (
    <>
      {blockError ? (
        <p className="usage-panel__note usage-panel__note--warn">
          Could not read the 5-hour window ({blockError}). Codex usage below is still current.
        </p>
      ) : null}
      <div className="usage-grid">
        <div className="usage-tile">
          <Timer size={16} aria-hidden="true" />
          <div>
            <strong>{formatQuotaLeft(fiveHour, block ? formatReset(block.minutes_to_reset) : null)}</strong>
            <span>{formatQuotaLabel(fiveHour, "5h window", block ? "local 5h window" : "5h quota not synced")}</span>
          </div>
        </div>
        <div className="usage-tile">
          <Activity size={16} aria-hidden="true" />
          <div>
            <strong>{formatQuotaLeft(week, null)}</strong>
            <span>{formatQuotaLabel(week, "weekly window", "weekly quota not synced")}</span>
          </div>
        </div>
        <div className="usage-tile">
          <Cpu size={16} aria-hidden="true" />
          <div>
            <strong>{formatLocalEvidenceOutput(usage, block, blockError)}</strong>
            <span>{formatLocalEvidenceLabel(usage, block, blockError)}</span>
          </div>
        </div>
        <div className="usage-tile">
          <CheckCircle2 size={16} aria-hidden="true" />
          <div>
            <strong>{formatShippedOutput(shipped)}</strong>
            <span>{formatShippedLabel(shipped)}</span>
          </div>
        </div>
      </div>
    </>
  );
}

function formatQuotaLeft(bucket: UsageLimitBucket | null, fallback: string | null): string {
  if (bucket?.remaining_percent !== null && bucket?.remaining_percent !== undefined) {
    return `${trimPercent(bucket.remaining_percent)}% left`;
  }
  return fallback ?? "Quota unavailable";
}

function formatQuotaLabel(
  bucket: UsageLimitBucket | null,
  syncedLabel: string,
  missingLabel: string,
): string {
  if (!bucket) return missingLabel;
  const reset = formatReset(bucket.minutes_to_reset);
  return reset === "No reset" ? syncedLabel : `${syncedLabel}, resets in ${reset}`;
}

function formatLocalEvidenceOutput(
  usage: UsageResponse,
  block: UsageResponse["block"],
  blockError?: string,
): string {
  if (blockError) return "Read failed";
  if (block) return formatTokens(block.total_tokens);
  return formatCodexTokens(usage);
}

function formatLocalEvidenceLabel(
  usage: UsageResponse,
  block: UsageResponse["block"],
  blockError?: string,
): string {
  if (blockError) return "Claude window read failed";
  const codex = formatCodexTokens(usage);
  if (block && codex !== "No data") return `Claude window, Codex ${codex} today`;
  if (block) return "Claude active window";
  if (codex !== "No data") return "Codex local tokens today";
  return "No local token rows found";
}

function formatShippedOutput(shipped?: ShippedBoard | null): string {
  if (!shipped || shipped.error) return "No board";
  return `${shipped.counts.shipped} shipped`;
}

function formatShippedLabel(shipped?: ShippedBoard | null): string {
  if (!shipped) return "delivery signal not loaded";
  if (shipped.error) return "delivery signal failed";
  const days = shipped.lookback_days;
  return days ? `Alfred-evidenced merges in ${days} days` : "Alfred-evidenced merges";
}

function trimPercent(value: number): string {
  return value.toFixed(1).replace(/\.0$/, "");
}

function formatCodexTokens(usage: UsageResponse): string {
  const latest = usage.codex?.latest_day;
  if (latest && latest.total_tokens !== null) {
    return formatTokens(latest.total_tokens);
  }
  return "No data";
}
