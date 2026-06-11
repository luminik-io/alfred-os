import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusPill } from "./atoms";
import type { Snapshot } from "../types";

function snapshot(reliabilityStatus: string): Snapshot {
  return {
    loadedAt: new Date("2026-06-02T12:00:00Z"),
    shipped: null,
    schedule: [],
    status: { agents: [], total_today: 0, reliability: { status: reliabilityStatus } },
    actions: {
      status: reliabilityStatus,
      actions: [],
      failure_patterns: [],
      stale_workers: [],
      promotion_suggestions: [],
    },
    memoryCandidates: { rows: [] },
    firings: [],
    plans: [],
    trustedSlack: null,
  };
}

describe("StatusPill", () => {
  it("shows a connection failure as Offline when serve is unreachable", () => {
    render(<StatusPill snapshot={null} error="connection refused" />);
    const pill = screen.getByRole("status");
    expect(pill).toHaveTextContent("Offline");
    expect(pill).toHaveAttribute("aria-label", "Alfred serve offline");
    expect(pill.className).toContain("status-pill--error");
  });

  it("reads a healthy connected fleet as Live, not as a connection warning", () => {
    render(<StatusPill snapshot={snapshot("ok")} error={null} />);
    const pill = screen.getByRole("status");
    expect(pill).toHaveTextContent("Live");
    expect(pill.className).toContain("status-pill--ok");
  });

  it("attributes a fleet warning to the fleet, not the connection", () => {
    render(<StatusPill snapshot={snapshot("warn")} error={null} />);
    const pill = screen.getByRole("status");
    // The amber state reflects fleet health over a good connection, so the
    // label must not imply the connection itself is degraded.
    expect(pill).toHaveTextContent("Warn");
    expect(pill).toHaveAttribute("aria-label", "Connected to Alfred serve, fleet Warn");
    expect(pill.getAttribute("aria-label")).not.toMatch(/^Connection /);
    expect(pill.className).toContain("status-pill--warn");
  });
});
