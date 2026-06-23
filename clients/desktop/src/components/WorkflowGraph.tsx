import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  type NodeProps,
  Position,
  ReactFlow,
  type ReactFlowProps,
} from "@xyflow/react";
import { useMemo } from "react";

import {
  type AgentNodeData,
  buildWorkflowGraph,
  type LaneNodeData,
  type WorkflowNodeInput,
} from "../lib/workflowGraph";
import { AlfredStatusDot } from "./ui/alfred";

import "@xyflow/react/dist/style.css";

/** A lane heading sitting above its column of agents. */
function LaneNode({ data }: NodeProps) {
  const { label } = data as LaneNodeData;
  return <div className="wf-lane">{label}</div>;
}

/** One agent in the pipeline: monogram, name, role, live status, runs today. */
function AgentNode({ data }: NodeProps) {
  const node = data as AgentNodeData;
  const monogram = (node.label || node.codename).trim().charAt(0).toUpperCase();
  return (
    <div
      className="wf-node"
      data-tone={node.tone}
      data-selected={node.selected ? "true" : "false"}
      style={{ "--agent-accent": node.accent } as React.CSSProperties}
    >
      <Handle type="target" position={Position.Left} className="wf-node__handle" />
      <span className="wf-node__mark" aria-hidden="true">
        {monogram}
      </span>
      <span className="wf-node__body">
        <span className="wf-node__name">{node.label || node.codename}</span>
        <span className="wf-node__role">{node.role || node.codename}</span>
      </span>
      <span className="wf-node__meta">
        <span className="wf-node__status">
          <AlfredStatusDot tone={node.tone} aria-hidden="true" />
          {node.statusLabel}
        </span>
        <span className="wf-node__runs">{node.runsToday} today</span>
      </span>
      <Handle type="source" position={Position.Right} className="wf-node__handle" />
    </div>
  );
}

const NODE_TYPES: ReactFlowProps["nodeTypes"] = { agent: AgentNode, lane: LaneNode };

export function WorkflowGraph({
  agents,
  selectedCodename,
  onSelect,
}: {
  agents: WorkflowNodeInput[];
  selectedCodename: string | null;
  onSelect: (codename: string) => void;
}) {
  const { nodes, edges } = useMemo(
    () => buildWorkflowGraph(agents, selectedCodename),
    [agents, selectedCodename],
  );

  return (
    <div className="workflow-graph" aria-label="Agent workflow graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.4}
        maxZoom={1.6}
        nodesConnectable={false}
        edgesFocusable={false}
        proOptions={{ hideAttribution: false }}
        onNodeClick={(_event, node) => {
          if (node.type === "agent") {
            onSelect(node.id);
          }
        }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
