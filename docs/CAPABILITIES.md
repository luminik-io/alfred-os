# Capability Doctor

`alfred capabilities` is a read-only inventory of the local features that make
the fleet useful beyond a bare scheduler. It does not install packages or make
network calls. The native onboarding flow uses the same payload on the Tools
step, so a user can see whether code graph memory, context compression, and
engineering skill packs are ready before they let the fleet run real work.

```sh
alfred capabilities
alfred capabilities --json
```

## Current Capabilities

| Capability | Why it matters | Source |
| --- | --- | --- |
| Code graph memory | Gives agents structural code search, call paths, impact checks, and route ownership through the optional code-memory MCP layer. | [`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp), MIT |
| Context compression | Gives Alfred a place to integrate local token compression and retrieval for long tool outputs, logs, and memory context. | [`headroomlabs-ai/headroom`](https://github.com/headroomlabs-ai/headroom), Apache-2.0 |
| Engineering skill packs | Gives local agent hosts repeatable review, QA, security, frontend, docs, and shipping workflows. | [`garrytan/gstack`](https://github.com/garrytan/gstack), `vercel-labs/agent-skills`, `addyosmani/agent-skills` |

The JSON shape is versioned:

```json
{
  "version": 1,
  "summary": {"ready": 1, "actionable": 2, "disabled": 0, "total": 3},
  "capabilities": []
}
```

Each row has a stable `key`, `state`, `detail`, `detected` object, and
`install_hint`. A future repair action can install or configure a missing row,
but the doctor itself stays read-only. The desktop intentionally displays the
hint and source attribution rather than hiding a missing capability behind a
generic setup warning.
