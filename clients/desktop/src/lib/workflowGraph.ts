// The canonical engineering workflow the fleet runs, as a left-to-right
// pipeline of lanes with the handoffs between agents. The nodes are live
// (status + runs come from the roster); the lanes and edges are the fixed
// delivery flow. This is intentionally declarative so a future editable
// canvas (drag handoffs, add agents) can read and write the same shape.

import type { Edge, Node } from "@xyflow/react";

import type { AlfredTone } from "../components/ui/alfred";

export type WorkflowLaneId =
  | "intake"
  | "architect"
  | "implement"
  | "review"
  | "ship"
  | "ops";

export type WorkflowLane = {
  id: WorkflowLaneId;
  label: string;
  agents: string[];
};

// Ordered lanes (left to right). Each agent sits in exactly one lane.
export const WORKFLOW_LANES: readonly WorkflowLane[] = [
  { id: "intake", label: "Triage & plan", agents: ["robin", "drake", "damian"] },
  { id: "architect", label: "Architect", agents: ["batman"] },
  { id: "implement", label: "Implement", agents: ["lucius", "bane", "nightwing"] },
  { id: "review", label: "Review", agents: ["rasalghul"] },
  { id: "ship", label: "Ship", agents: ["automerge"] },
  { id: "ops", label: "Ops & health", agents: ["gordon", "fleet-doctor", "huntress"] },
];

// Canonical handoffs (source codename -> target codename). A real run does not
// always traverse every edge, but this is the path work can take through the
// fleet.
export const WORKFLOW_EDGES: readonly [string, string][] = [
  ["robin", "drake"],
  ["drake", "batman"],
  ["damian", "batman"],
  ["drake", "lucius"],
  ["batman", "lucius"],
  ["lucius", "rasalghul"],
  ["bane", "rasalghul"],
  ["nightwing", "rasalghul"],
  ["rasalghul", "automerge"],
  ["automerge", "gordon"],
];

// Every codename that appears in the delivery graph. Agents outside this set
// (overnight orchestrator, brand/content monitors) are not part of the
// engineering pipeline and stay in the List view.
export const WORKFLOW_AGENTS: readonly string[] = WORKFLOW_LANES.flatMap(
  (lane) => lane.agents,
);

// Layout constants. Lanes step right; agents stack and center within a lane.
const LANE_GAP_X = 250;
const NODE_GAP_Y = 128;
const LANE_LABEL_Y = -150;

/** The display fields a node needs, derived by the caller from the live row. */
export type WorkflowNodeInput = {
  codename: string;
  label: string;
  role: string;
  accent: string;
  tone: AlfredTone;
  statusLabel: string;
  runsToday: number;
};

export type AgentNodeData = WorkflowNodeInput & {
  laneId: WorkflowLaneId;
  selected: boolean;
  [key: string]: unknown;
};

export type LaneNodeData = { label: string; [key: string]: unknown };

function laneIndex(id: WorkflowLaneId): number {
  return WORKFLOW_LANES.findIndex((lane) => lane.id === id);
}

function laneOf(codename: string): WorkflowLane | undefined {
  return WORKFLOW_LANES.find((lane) => lane.agents.includes(codename));
}

// Center a lane's agents vertically around y=0 so the pipeline reads as a band.
function yWithinLane(indexInLane: number, laneSize: number): number {
  return (indexInLane - (laneSize - 1) / 2) * NODE_GAP_Y;
}

/**
 * Build React Flow nodes + edges for the workflow graph from live node inputs.
 * Missing agents (in a lane but not in the live roster) are skipped; inputs not
 * in any lane are ignored. Pure and deterministic for testing.
 */
export function buildWorkflowGraph(
  inputs: WorkflowNodeInput[],
  selectedCodename: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const byCodename = new Map(inputs.map((input) => [input.codename, input]));
  const present = new Set(byCodename.keys());
  const nodes: Node[] = [];

  for (const lane of WORKFLOW_LANES) {
    const liveAgents = lane.agents.filter((codename) => present.has(codename));
    const x = laneIndex(lane.id) * LANE_GAP_X;

    // One non-interactive label per lane that actually has agents.
    if (liveAgents.length) {
      nodes.push({
        id: `lane:${lane.id}`,
        type: "lane",
        position: { x, y: LANE_LABEL_Y },
        data: { label: lane.label } satisfies LaneNodeData,
        draggable: false,
        selectable: false,
        deletable: false,
      });
    }

    liveAgents.forEach((codename, indexInLane) => {
      const input = byCodename.get(codename)!;
      nodes.push({
        id: codename,
        type: "agent",
        position: { x, y: yWithinLane(indexInLane, liveAgents.length) },
        data: {
          ...input,
          laneId: lane.id,
          selected: codename === selectedCodename,
        } satisfies AgentNodeData,
        draggable: false,
      });
    });
  }

  const edges: Edge[] = WORKFLOW_EDGES.filter(
    ([source, target]) => present.has(source) && present.has(target),
  ).map(([source, target]) => ({
    id: `${source}->${target}`,
    source,
    target,
    type: "smoothstep",
    animated:
      source === selectedCodename || target === selectedCodename,
  }));

  return { nodes, edges };
}

export const __test = { laneOf, laneIndex, yWithinLane };
