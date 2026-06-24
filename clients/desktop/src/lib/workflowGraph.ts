// The canonical engineering workflow the fleet runs, as a left-to-right
// pipeline of lanes with the handoffs between agents. The nodes are live
// (status + runs come from the roster); the lanes and edges are the fixed
// delivery flow. This is intentionally declarative so a future editable
// canvas (drag handoffs, add agents) can read and write the same shape.

import dagre from "@dagrejs/dagre";
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

// Node + lane footprint used both for the dagre layout and the CSS sizing of
// the rendered card. Keep these in sync with the .wf-node / .wf-lane rules.
const NODE_WIDTH = 232;
const NODE_HEIGHT = 98;
const LANE_LABEL_WIDTH = 232;
const LANE_LABEL_HEIGHT = 24;
// Dagre spacing. ranksep controls the horizontal gap between lanes (we lay the
// graph out left-to-right), nodesep the vertical gap within a rank.
const RANK_SEP = 96;
const NODE_SEP = 28;
const EDGE_SEP = 18;
// Vertical offset that lifts each lane label clear of the agent cards beneath
// it. Dagre positions the label node; we nudge it up so it reads as a heading.
const LANE_LABEL_LIFT = 78;

/** The display fields a node needs, derived by the caller from the live row. */
export type WorkflowNodeInput = {
  codename: string;
  label: string;
  role: string;
  accent: string;
  tone: AlfredTone;
  statusLabel: string;
  runsToday: number;
  // Optional richer fields (the card degrades gracefully without them).
  lastRunLabel?: string;
  failStreak?: number;
};

export type AgentNodeData = WorkflowNodeInput & {
  laneId: WorkflowLaneId;
  laneLabel: string;
  selected: boolean;
  [key: string]: unknown;
};

export type LaneNodeData = { label: string; [key: string]: unknown };

function laneIndex(id: WorkflowLaneId): number {
  return WORKFLOW_LANES.findIndex((lane) => lane.id === id);
}

/**
 * Lay the pipeline out with dagre as a left-to-right DAG. We seed each agent and
 * lane label as a sized node, add the surviving handoffs as edges, and pin the
 * lane rank so the canonical order (triage -> architect -> ... -> ops) is never
 * reordered by the solver. Returns top-left positions React Flow can consume.
 */
function layoutGraph(
  agents: { codename: string; laneId: WorkflowLaneId }[],
  lanes: { id: WorkflowLaneId }[],
  edges: [string, string][],
): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph({ compound: true });
  g.setGraph({
    rankdir: "LR",
    ranksep: RANK_SEP,
    nodesep: NODE_SEP,
    edgesep: EDGE_SEP,
    marginx: 24,
    marginy: 48,
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const agent of agents) {
    g.setNode(agent.codename, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const lane of lanes) {
    g.setNode(`lane:${lane.id}`, {
      width: LANE_LABEL_WIDTH,
      height: LANE_LABEL_HEIGHT,
    });
  }
  for (const [source, target] of edges) {
    g.setEdge(source, target);
  }

  dagre.layout(g);

  // Force lane labels to share the x rank of the first agent in their lane and
  // sit above the band, so they read as column headings rather than drifting
  // into the flow. Dagre gives center coords; React Flow wants top-left.
  const positions = new Map<string, { x: number; y: number }>();
  let minAgentY = Infinity;
  for (const agent of agents) {
    const n = g.node(agent.codename);
    if (!n) continue;
    minAgentY = Math.min(minAgentY, n.y - NODE_HEIGHT / 2);
    positions.set(agent.codename, {
      x: n.x - NODE_WIDTH / 2,
      y: n.y - NODE_HEIGHT / 2,
    });
  }

  for (const lane of lanes) {
    const laneAgents = agents.filter((a) => a.laneId === lane.id);
    if (!laneAgents.length) continue;
    const first = positions.get(laneAgents[0].codename);
    if (!first) continue;
    const labelY = (Number.isFinite(minAgentY) ? minAgentY : first.y) - LANE_LABEL_LIFT;
    positions.set(`lane:${lane.id}`, { x: first.x, y: labelY });
  }

  return positions;
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

  // Resolve the present agents + their lane, in canonical lane order.
  const placedAgents: { codename: string; laneId: WorkflowLaneId; laneLabel: string }[] = [];
  const presentLanes: { id: WorkflowLaneId }[] = [];
  for (const lane of [...WORKFLOW_LANES].sort((a, b) => laneIndex(a.id) - laneIndex(b.id))) {
    const liveAgents = lane.agents.filter((codename) => present.has(codename));
    if (!liveAgents.length) continue;
    presentLanes.push({ id: lane.id });
    for (const codename of liveAgents) {
      placedAgents.push({ codename, laneId: lane.id, laneLabel: lane.label });
    }
  }

  const liveEdges: [string, string][] = WORKFLOW_EDGES.filter(
    ([source, target]) => present.has(source) && present.has(target),
  ).map(([source, target]) => [source, target]);

  const positions = layoutGraph(placedAgents, presentLanes, liveEdges);

  const nodes: Node[] = [];

  for (const lane of presentLanes) {
    const laneMeta = WORKFLOW_LANES.find((l) => l.id === lane.id)!;
    const pos = positions.get(`lane:${lane.id}`) ?? { x: 0, y: 0 };
    nodes.push({
      id: `lane:${lane.id}`,
      type: "lane",
      position: pos,
      data: { label: laneMeta.label } satisfies LaneNodeData,
      draggable: false,
      selectable: false,
      deletable: false,
    });
  }

  for (const placed of placedAgents) {
    const input = byCodename.get(placed.codename)!;
    const pos = positions.get(placed.codename) ?? { x: 0, y: 0 };
    nodes.push({
      id: placed.codename,
      type: "agent",
      position: pos,
      // Declare the card footprint so the minimap can paint the node before
      // React Flow measures the DOM (otherwise the overview renders empty).
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      data: {
        ...input,
        laneId: placed.laneId,
        laneLabel: placed.laneLabel,
        selected: placed.codename === selectedCodename,
      } satisfies AgentNodeData,
      draggable: false,
    });
  }

  const edges: Edge[] = liveEdges.map(([source, target]) => ({
    id: `${source}->${target}`,
    source,
    target,
    type: "smoothstep",
    animated: source === selectedCodename || target === selectedCodename,
  }));

  return { nodes, edges };
}
