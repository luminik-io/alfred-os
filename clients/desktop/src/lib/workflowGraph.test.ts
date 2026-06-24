import { describe, expect, it } from "vitest";

import type { WorkflowRole } from "./agentRoster";
import {
  buildWorkflowGraph,
  type WorkflowNodeInput,
} from "./workflowGraph";

function input(codename: string, role: WorkflowRole): WorkflowNodeInput {
  return {
    codename,
    role,
    label: codename,
    roleLabel: "role",
    accent: "#fff",
    tone: "ok",
    statusLabel: "Resting",
    runsToday: 0,
  };
}

// A small representative roster spanning every lane.
const ROSTER: WorkflowNodeInput[] = [
  input("robin", "triage"),
  input("batman", "architect"),
  input("lucius", "implement"),
  input("bane", "implement"),
  input("rasalghul", "review"),
  input("automerge", "ship"),
  input("gordon", "ops"),
];

describe("buildWorkflowGraph", () => {
  it("builds an agent node for every input plus a lane label per present lane", () => {
    const { nodes } = buildWorkflowGraph(ROSTER, null);

    const agentNodes = nodes.filter((n) => n.type === "agent");
    const laneNodes = nodes.filter((n) => n.type === "lane");
    // Every reported agent is placed (no hardcoded subset filter).
    expect(agentNodes).toHaveLength(ROSTER.length);
    expect(agentNodes.map((n) => n.id).sort()).toEqual(
      ROSTER.map((r) => r.codename).sort(),
    );
    // One lane label per distinct role present (six here).
    expect(laneNodes).toHaveLength(6);
  });

  it("places an unknown-role agent in its given lane and never drops it", () => {
    // An agent the runtime reports with the fallback role still appears.
    const { nodes } = buildWorkflowGraph(
      [input("batman", "architect"), input("mystery-bot", "ops")],
      null,
    );
    const agentIds = nodes.filter((n) => n.type === "agent").map((n) => n.id);
    expect(agentIds.sort()).toEqual(["batman", "mystery-bot"]);
  });

  it("wires handoff edges between present lanes and drops edges to absent lanes", () => {
    // Only architect + implement present: the architect->implement edge
    // survives; edges to missing lanes (e.g. implement->review) do not.
    const { nodes, edges } = buildWorkflowGraph(
      [input("batman", "architect"), input("lucius", "implement")],
      null,
    );
    const agentIds = nodes.filter((n) => n.type === "agent").map((n) => n.id);
    expect(agentIds.sort()).toEqual(["batman", "lucius"]);
    expect(edges.map((e) => e.id)).toContain("batman->lucius");
    expect(
      edges.every((e) => agentIds.includes(e.source) && agentIds.includes(e.target)),
    ).toBe(true);
  });

  it("marks the selected node and animates its incident edges", () => {
    const { nodes, edges } = buildWorkflowGraph(
      [input("lucius", "implement"), input("rasalghul", "review")],
      "rasalghul",
    );
    const selected = nodes.find((n) => n.id === "rasalghul");
    expect((selected?.data as { selected: boolean }).selected).toBe(true);
    expect(edges.find((e) => e.id === "lucius->rasalghul")?.animated).toBe(true);
  });
});
