import { describe, expect, it } from "vitest";

import {
  buildWorkflowGraph,
  WORKFLOW_AGENTS,
  type WorkflowNodeInput,
} from "./workflowGraph";

function input(codename: string): WorkflowNodeInput {
  return {
    codename,
    label: codename,
    role: "role",
    accent: "#fff",
    tone: "ok",
    statusLabel: "Resting",
    runsToday: 0,
  };
}

describe("buildWorkflowGraph", () => {
  it("builds an agent node for each present pipeline agent plus its lane label", () => {
    const inputs = WORKFLOW_AGENTS.map(input);
    const { nodes } = buildWorkflowGraph(inputs, null);

    const agentNodes = nodes.filter((n) => n.type === "agent");
    const laneNodes = nodes.filter((n) => n.type === "lane");
    expect(agentNodes).toHaveLength(WORKFLOW_AGENTS.length);
    expect(laneNodes.length).toBeGreaterThan(0);
    // every agent id is one of the known pipeline codenames
    expect(agentNodes.every((n) => WORKFLOW_AGENTS.includes(n.id))).toBe(true);
  });

  it("skips agents that are not in the live roster, and edges that lose an endpoint", () => {
    // Only batman + lucius present: the batman->lucius edge survives, edges to
    // missing endpoints (e.g. lucius->rasalghul) are dropped.
    const { nodes, edges } = buildWorkflowGraph([input("batman"), input("lucius")], null);
    const agentIds = nodes.filter((n) => n.type === "agent").map((n) => n.id);
    expect(agentIds.sort()).toEqual(["batman", "lucius"]);
    expect(edges.map((e) => e.id)).toContain("batman->lucius");
    expect(edges.every((e) => agentIds.includes(e.source) && agentIds.includes(e.target))).toBe(
      true,
    );
  });

  it("marks the selected node and animates its incident edges", () => {
    const { nodes, edges } = buildWorkflowGraph(
      [input("lucius"), input("rasalghul")],
      "rasalghul",
    );
    const selected = nodes.find((n) => n.id === "rasalghul");
    expect((selected?.data as { selected: boolean }).selected).toBe(true);
    expect(edges.find((e) => e.id === "lucius->rasalghul")?.animated).toBe(true);
  });

  it("ignores inputs that are not part of any lane", () => {
    const { nodes } = buildWorkflowGraph([input("batman"), input("echo")], null);
    const agentIds = nodes.filter((n) => n.type === "agent").map((n) => n.id);
    expect(agentIds).toContain("batman");
    expect(agentIds).not.toContain("echo");
  });
});
