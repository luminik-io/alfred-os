import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UsagePanel } from "./UsagePanel";
import { formatReset, formatTokens } from "../lib/usageFormat";
import type { ShippedBoard, UsageResponse } from "../types";

function usage(overrides: Partial<UsageResponse> = {}): UsageResponse {
  return {
    available: true,
    kind: "subscription",
    source: "native",
    block: {
      start_at: "2026-06-03T14:00:00Z",
      reset_at: "2026-06-03T19:00:00Z",
      minutes_to_reset: 125,
      is_active: true,
      total_tokens: 142_200_916,
      cost_usd: 109.9,
      entries: 1021,
      token_counts: { input: 1, output: 2, cache_creation: 3, cache_read: 4 },
      projection: { total_tokens: 1_063_656_501, total_cost_usd: 822.07, remaining_minutes: 175 },
      burn_rate: { tokens_per_minute: 3_544_059, cost_per_hour: 164.34 },
      models: ["claude-opus-4-8"],
    },
    codex: {
      latest_day: { date: "2026-06-03", total_tokens: 75_778, cost_usd: 0.32, input_tokens: 62_886, output_tokens: 92 },
      totals: { total_tokens: 4_211_983_837, cost_usd: 3276.2 },
    },
    limits: {
      source: "claude_usage_limits_cache",
      five_hour: {
        utilization: 35,
        remaining_percent: 65,
        resets_at: "2026-06-03T19:00:00Z",
        minutes_to_reset: 125,
      },
      seven_day: {
        utilization: 14.5,
        remaining_percent: 85.5,
        resets_at: "2026-06-08T09:30:00Z",
        minutes_to_reset: 6750,
      },
      seven_day_sonnet: null,
      seven_day_opus: null,
      extra_usage: null,
    },
    weekly: null,
    ...overrides,
  };
}

function shipped(): ShippedBoard {
  return {
    generated_at: "2026-06-03T17:00:00Z",
    lookback_days: 14,
    repos: ["example-org/alfred"],
    columns: { queued: [], in_progress: [], shipped: [] },
    counts: { queued: 0, in_progress: 0, shipped: 12 },
    errors: [],
  };
}

describe("UsagePanel", () => {
  it("renders quota headroom, local evidence, and shipped output", () => {
    render(<UsagePanel usage={usage()} state="idle" shipped={shipped()} />);
    expect(screen.getByRole("region", { name: /subscription usage/i })).toBeInTheDocument();
    // Real quota cache.
    expect(screen.getByText("65% left")).toBeInTheDocument();
    expect(screen.getByText(/5h window, resets in 2h 5m/i)).toBeInTheDocument();
    expect(screen.getByText("85.5% left")).toBeInTheDocument();
    expect(screen.getByText(/weekly window, resets in 4d 16h/i)).toBeInTheDocument();
    // Local token evidence.
    expect(screen.getByText("142.2M")).toBeInTheDocument();
    expect(screen.getByText(/claude window, codex 75.8K today/i)).toBeInTheDocument();
    // Delivery signal.
    expect(screen.getByText("12 shipped")).toBeInTheDocument();
    expect(screen.getByText(/alfred-evidenced merges in 14 days/i)).toBeInTheDocument();
    // No dollar figure leads the panel.
    const panel = screen.getByRole("region", { name: /subscription usage/i });
    expect(panel.textContent).toMatch(/5h and weekly headroom/i);
    expect(panel.textContent).not.toMatch(/\$109/);
  });

  it("shows 'no active block' when there is no live 5-hour window", () => {
    render(<UsagePanel usage={usage({ block: null, limits: null })} state="idle" />);
    expect(screen.getAllByText(/quota unavailable/i).length).toBeGreaterThan(1);
    expect(screen.getByText(/5h quota not synced/i)).toBeInTheDocument();
    // Codex still contributes to the local evidence row.
    expect(screen.getByText("75.8K")).toBeInTheDocument();
    expect(screen.getByText(/codex local tokens today/i)).toBeInTheDocument();
  });

  it("flags a partial native-read failure instead of rendering it as an empty window", () => {
    render(
      <UsagePanel
        usage={usage({ block: null, errors: { block: "PermissionError: unreadable transcript" } })}
        state="idle"
      />,
    );
    // An unreadable rolling window must not look like a genuinely empty one.
    expect(screen.getByText(/^Read failed$/i)).toBeInTheDocument();
    expect(screen.getByText(/claude window read failed/i)).toBeInTheDocument();
    expect(screen.getByText(/could not read the 5-hour window/i)).toBeInTheDocument();
  });

  it("degrades to a plain 'usage unavailable' state with the error detail", () => {
    render(
      <UsagePanel
        usage={usage({ available: false, block: null, codex: null, error: "usage logs unavailable" })}
        state="error"
      />,
    );
    expect(screen.getByText(/usage unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/usage logs unavailable/i)).toBeInTheDocument();
  });

  it("shows a loading note before the first reading arrives", () => {
    render(<UsagePanel usage={null} state="loading" />);
    expect(screen.getByText(/reading local usage/i)).toBeInTheDocument();
  });

  it("shows a neutral empty state when usage is null and idle", () => {
    render(<UsagePanel usage={null} state="idle" />);
    expect(screen.getByText(/usage will appear here/i)).toBeInTheDocument();
  });

  it("labels Codex as no-data when native usage has no Codex row", () => {
    render(<UsagePanel usage={usage({ codex: null })} state="idle" />);
    expect(screen.getByText(/claude active window/i)).toBeInTheDocument();
  });

  it("renders a Codex quota tile when the rate_limits block is present", () => {
    // Far-future reset so the relative countdown is positive and deterministic
    // enough to assert the percentage label is plain language.
    render(
      <UsagePanel
        usage={usage({
          codex: {
            latest_day: {
              date: "2026-06-03",
              total_tokens: 75_778,
              cost_usd: null,
              input_tokens: 62_886,
              output_tokens: 92,
            },
            totals: { total_tokens: null, cost_usd: null },
            quota: {
              primary: { used_percent: 22.5, resets_at: "2099-06-03T14:00:00Z" },
              secondary: { used_percent: 41, resets_at: "2099-06-08T09:00:00Z" },
              plan_type: "pro",
            },
          },
        })}
        state="idle"
        shipped={shipped()}
      />,
    );
    // 5h primary: 100 - 22.5 = 77.5% left leads the tile value.
    expect(screen.getByText("77.5% left")).toBeInTheDocument();
    // Plain-language label naming both windows.
    expect(screen.getByText(/codex 5h 77.5% left, weekly 59% left/i)).toBeInTheDocument();
  });

  it("omits the Codex quota tile when no rate_limits block is present", () => {
    render(<UsagePanel usage={usage()} state="idle" shipped={shipped()} />);
    expect(screen.queryByText(/codex 5h/i)).not.toBeInTheDocument();
  });

  it("shows the weekly reset + 7d tokens + tier from local state when the quota cache is absent", () => {
    render(
      <UsagePanel
        usage={usage({
          // No OAuth quota cache; weekly comes from the local .claude.json state.
          limits: { ...usage().limits!, seven_day: null },
          weekly: {
            available: true,
            total_tokens: null,
            cost_usd: null,
            utilization: null,
            remaining_percent: null,
            resets_at: "2026-06-08T09:30:00Z",
            minutes_to_reset: 6750,
            source: "claude_local_state",
            used_tokens_7d: 142_200_916,
            tier: "default_claude_max_20x",
            tier_label: "Max 20x",
            unavailable_reason: null,
          },
        })}
        state="idle"
        shipped={shipped()}
      />,
    );
    // The reset countdown leads the tile value (no fabricated "% left").
    expect(screen.getByText(/^resets in 4d 16h$/i)).toBeInTheDocument();
    // The label names the 7-day token total and the plan tier.
    expect(screen.getByText(/weekly window, 142.2M in 7d, Max 20x/i)).toBeInTheDocument();
  });

  it("uses the local-state fallback when the quota cache has an empty seven_day bucket", () => {
    render(
      <UsagePanel
        usage={usage({
          // A malformed/empty usage-limits.json normalizes to an all-null
          // bucket; it must not mask the local-state fallback.
          limits: {
            ...usage().limits!,
            seven_day: {
              utilization: null,
              remaining_percent: null,
              resets_at: null,
              minutes_to_reset: null,
            },
          },
          weekly: {
            available: true,
            total_tokens: null,
            cost_usd: null,
            utilization: null,
            remaining_percent: null,
            resets_at: "2026-06-08T09:30:00Z",
            minutes_to_reset: 6750,
            source: "claude_local_state",
            used_tokens_7d: 142_200_916,
            tier: "default_claude_max_20x",
            tier_label: "Max 20x",
            unavailable_reason: null,
          },
        })}
        state="idle"
        shipped={shipped()}
      />,
    );
    // The empty bucket is ignored; the local-state fallback drives the tile.
    expect(screen.getByText(/^resets in 4d 16h$/i)).toBeInTheDocument();
    expect(screen.getByText(/weekly window, 142.2M in 7d, Max 20x/i)).toBeInTheDocument();
  });

  it("never shows a fabricated weekly percent from local state", () => {
    render(
      <UsagePanel
        usage={usage({
          limits: { ...usage().limits!, seven_day: null },
          weekly: {
            available: true,
            total_tokens: null,
            cost_usd: null,
            utilization: null,
            remaining_percent: null,
            resets_at: "2026-06-08T09:30:00Z",
            minutes_to_reset: 6750,
            source: "claude_local_state",
            used_tokens_7d: 12_750,
            tier: "default_claude_max_20x",
            tier_label: "Max 20x",
            unavailable_reason: null,
          },
        })}
        state="idle"
        shipped={shipped()}
      />,
    );
    // Only the 5h tile (from the cache) carries a "% left"; the weekly tile must
    // not. There should be exactly one "% left" string in the panel.
    expect(screen.getAllByText(/% left/i)).toHaveLength(1);
  });

  it("keeps the honest 'weekly quota not synced' copy when nothing is known", () => {
    render(
      <UsagePanel
        usage={usage({
          limits: { ...usage().limits!, seven_day: null },
          weekly: {
            available: false,
            total_tokens: null,
            cost_usd: null,
            utilization: null,
            remaining_percent: null,
            resets_at: null,
            minutes_to_reset: null,
            source: null,
            used_tokens_7d: null,
            tier: null,
            tier_label: null,
            unavailable_reason: "True weekly Claude quota is unavailable from local logs.",
          },
        })}
        state="idle"
      />,
    );
    expect(screen.getByText(/weekly quota not synced/i)).toBeInTheDocument();
  });

  it("renders the weekly tile from local state in the compact Inbox rail", () => {
    render(
      <UsagePanel
        compact
        usage={usage({
          limits: { ...usage().limits!, seven_day: null },
          weekly: {
            available: true,
            total_tokens: null,
            cost_usd: null,
            utilization: null,
            remaining_percent: null,
            resets_at: "2026-06-08T09:30:00Z",
            minutes_to_reset: 6750,
            source: "claude_local_state",
            used_tokens_7d: 75_778,
            tier: "default_claude_max_20x",
            tier_label: "Max 20x",
            unavailable_reason: null,
          },
        })}
        state="idle"
      />,
    );
    // Compact mode includes the weekly tile, so the reset + tier must show.
    expect(screen.getByText(/^resets in 4d 16h$/i)).toBeInTheDocument();
    expect(screen.getByText(/weekly window, 75.8K in 7d, Max 20x/i)).toBeInTheDocument();
  });
});

describe("formatTokens", () => {
  it("formats counts compactly and handles null", () => {
    expect(formatTokens(null)).toBe("No data");
    expect(formatTokens(900)).toBe("900");
    expect(formatTokens(9_500)).toBe("9.5K");
    expect(formatTokens(75_778)).toBe("75.8K");
    expect(formatTokens(142_200_916)).toBe("142.2M");
    expect(formatTokens(1_063_656_501)).toBe("1.1B");
  });
});

describe("formatReset", () => {
  it("formats minutes to reset, flooring at 'now'", () => {
    expect(formatReset(null)).toBe("No reset");
    expect(formatReset(0)).toBe("now");
    expect(formatReset(45)).toBe("45m");
    expect(formatReset(60)).toBe("1h");
    expect(formatReset(125)).toBe("2h 5m");
    expect(formatReset(6750)).toBe("4d 16h");
  });
});
