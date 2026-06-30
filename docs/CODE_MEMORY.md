# Code memory (code-structure layer)

Alfred's memory has three layers, each answering a different question:

| Layer | Question it answers | Backend |
|---|---|---|
| Semantic lessons | "What did a past firing learn about this repo?" | Redis Agent Memory (vectors) |
| Operational graph | "What relations has the fleet recorded?" | FleetBrain / AGE graph |
| **Code structure** | "Where is this symbol, who calls it, what breaks if I change it, who owns it?" | **codebase-memory-mcp** |

This doc covers the third layer. The first two are in
[MEMORY_PROVIDERS.md](MEMORY_PROVIDERS.md) and [FLEET_BRAIN.md](FLEET_BRAIN.md).

## What it is

[codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)
(DeusData, MIT) is a standalone binary that indexes your in-scope repositories
into a code graph and answers read-only structure queries over MCP. Alfred
attaches it to every firing as an MCP server, so the fleet agents get
code-structure tools the model can call on demand:

- **search** the code graph for symbols, definitions, and references
- **call graph** for a function (callers and callees)
- **impact / blast radius** for a proposed change
- **who-owns** a file or symbol

The binary is **never vendored** into this repository. Alfred invokes it as an
external process, so the alfred-os tree stays clean and passes `scrub-check`.
The launcher fetches a pinned release on first use (opt-out), or you can point
it at a binary you installed yourself.

## How it is wired

- **MCP attachment.** `lib/agent_runner/process.py` attaches the
  `code_memory` server to each `claude` firing in the same `--mcp-config` as
  the read-only memory server, and adds its tools to the agent allowlist. It is
  a capability, on by default, and degrades to a clean no-op when the binary is
  not installed.
- **Launcher.** `bin/code-memory-mcp` resolves the binary, fetches the pinned
  release if needed, and runs the stdio MCP server (`serve`) or rebuilds the
  index (`index` / `refresh`). Run `bin/code-memory-mcp doctor` to see what is
  resolved.
- **Indexing.** The launcher indexes the repos in your scope list into
  `$ALFRED_HOME/state/code-memory`. If no scope list is configured, Alfred
  auto-discovers git repos under `WORKSPACE_ROOT/product` by default, skipping
  archive, worktree, build, and dependency directories. The installed
  `code-map-refresh` agent keeps Alfred's lightweight local JSON code map
  current. The `code-memory-mcp` launcher refreshes the MCP graph separately so
  search, call-graph, impact, and who-owns queries track git changes without a
  full rebuild.

## Install and index

```sh
# Resolve + fetch the pinned binary, then build the initial index.
bin/code-memory-mcp doctor      # shows resolved binary, version pin, index dir
bin/code-memory-mcp index       # full build for the in-scope repos
bin/code-memory-mcp refresh     # incremental rebuild of the MCP graph

# The full fleet also installs code-map-refresh for the local JSON code map.
alfred agents                   # confirm code-map-refresh appears
```

If the binary cannot be resolved (no network, autofetch disabled, unsupported
platform), the MCP server is a no-op for that firing and the rest of memory is
unaffected. Nothing fails closed.

## Configuration

All knobs are environment variables; set them in `$ALFRED_HOME/.env` or
`$ALFRED_HOME/.env`. Defaults work out of the box.

| Variable | Default | What it does |
|---|---|---|
| `ALFRED_CODE_MEMORY_MCP` | `1` (on) | Attach the code-memory MCP to firings. Set `0` to disable. |
| `ALFRED_CODE_MEMORY_REPOS` | (falls back to `ALFRED_CODE_MAP_REPOS`, then auto-discovery) | Comma-separated repo dir names under your workspace to index. |
| `ALFRED_CODE_MEMORY_DISCOVERY_LIMIT` | `25` | Max git repos auto-discovered when no explicit code-memory/code-map scope is configured. |
| `ALFRED_WORKSPACE_SUBDIR` | (falls back to `WORKSPACE_SUBDIR`, then `product`) | Optional subdirectory under `WORKSPACE_ROOT` to scan for code-memory repos. Set it to an empty value to scan `WORKSPACE_ROOT` directly. |
| `ALFRED_CODE_MEMORY_BIN` | (unset) | Explicit path to the `codebase-memory-mcp` binary. Skips PATH + autofetch. |
| `ALFRED_CODE_MEMORY_VERSION` | pinned (`v0.8.1`) | Upstream release tag to fetch. |
| `ALFRED_CODE_MEMORY_REPO` | `DeusData/codebase-memory-mcp` | Upstream GitHub repo for release assets. |
| `ALFRED_CODE_MEMORY_AUTOFETCH` | `1` (on) | Fetch the pinned binary on first use. Set `0` for a strict no-network install. |
| `ALFRED_CODE_MEMORY_CONNECT_TIMEOUT_S` | `10` | Connect timeout for first-use release downloads. |
| `ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S` | `120` | Overall timeout for first-use release downloads. |
| `ALFRED_CODE_MEMORY_INDEX_DIR` | `$ALFRED_HOME/state/code-memory` | Default storage root for code-memory state when `ALFRED_CODE_MEMORY_HOME` is unset. |
| `ALFRED_CODE_MEMORY_HOME` | `ALFRED_CODE_MEMORY_INDEX_DIR` | HOME used for the upstream binary, which stores graph DBs under `.cache/codebase-memory-mcp`. |

Binary resolution order (first hit wins):

1. `ALFRED_CODE_MEMORY_BIN` if it points at an executable
2. `codebase-memory-mcp` on `PATH` (system or package install)
3. `$ALFRED_HOME/bin/codebase-memory-mcp` (the pinned cache, auto-fetched here)

## Scope

The code-memory layer is **read-only** structure intelligence. It never edits
repositories, never writes lessons, and never replaces the semantic-lesson or
operational-graph layers. It complements them: lessons say what Alfred learned,
the graph says what the fleet recorded, and code memory says how the code is
actually shaped right now.

## Privacy

The binary runs locally and indexes only the repos you list. No code, symbols,
or graph data leave the host. Fetching the binary contacts GitHub releases
only; disable that with `ALFRED_CODE_MEMORY_AUTOFETCH=0` and install the binary
yourself.
