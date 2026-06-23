import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  type NodeProps,
  Position,
  ReactFlow,
  type ReactFlowProps,
  useReactFlow,
  useStore,
} from "@xyflow/react";
import { useEffect, useMemo, useRef } from "react";

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

const FIT_OPTIONS = { padding: 0.18, minZoom: 0.45, maxZoom: 1.25 } as const;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Keep the whole pipeline in frame as the canvas resizes. React Flow tracks the
 * container size but does not re-fit on its own, so the graph would clip when
 * the window resizes, the sidebar toggles, or the layout stacks the inspector
 * below on narrow screens. We watch the store's width/height and re-fit
 * (debounced) so the view stays correct at every breakpoint and zoom level.
 */
function FitToContainer({ signature }: { signature: string }) {
  const { fitView } = useReactFlow();
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    if (!width || !height) {
      return;
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      void fitView({ ...FIT_OPTIONS, duration: prefersReducedMotion() ? 0 : 220 });
    }, 80);
    return () => clearTimeout(timer.current);
  }, [width, height, signature, fitView]);

  return null;
}

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

  // Re-fit whenever the set of agents changes (not on mere selection), so a
  // roster that loads or changes size still frames cleanly.
  const signature = useMemo(
    () => agents.map((agent) => agent.codename).join(","),
    [agents],
  );

  return (
    <div className="workflow-graph" aria-label="Agent workflow graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={FIT_OPTIONS}
        minZoom={0.35}
        maxZoom={1.75}
        // Trackpad/touch-first canvas controls (Figma/n8n style): two-finger
        // scroll pans, pinch (or ctrl/cmd + scroll) zooms, drag pans. The +/-
        // controls and fit button cover mouse-only users.
        panOnScroll
        zoomOnScroll={false}
        zoomOnPinch
        panOnDrag
        zoomOnDoubleClick={false}
        nodesConnectable={false}
        edgesFocusable={false}
        nodesDraggable={false}
        // We render our own selected state (data-selected) from the inspector,
        // so disable React Flow's native selection to avoid a second indicator.
        // onNodeClick still fires.
        elementsSelectable={false}
        proOptions={{ hideAttribution: false }}
        onNodeClick={(_event, node) => {
          if (node.type === "agent") {
            onSelect(node.id);
          }
        }}
      >
        <FitToContainer signature={signature} />
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
