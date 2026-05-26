# fleet-brain: Alfred's memory layer

Alfred's brain is the per-host store of what the fleet has learned. Engine-aware firings that know their target repo can read from it (recall) and write to it (reflect). The next firing starts with the lessons relevant to its codename and repo prepended to the prompt.

The brain is a single SQLite file in your `$ALFRED_HOME`. It never leaves your machine. The only outbound surface is the prompt context Alfred prepends to a firing, which goes to Claude Code or Codex on your existing CLI auth. No telemetry, no phone-home, no cloud sync.

## Why it exists

Most agent fleets are amnesiac: every firing starts from zero, re-discovers the same repo conventions, and re-makes the same mistakes. The brain closes that loop. After a firing learns that `your-org/api` keeps GraphQL schemas in `src/schema.graphql`, the next firing knows it without re-reading the repo.

The practical point: memory is local, boring, and inspectable. It should help
the fleet stop rediscovering repo conventions without adding a hosted service
or another account to manage.

## Quick start

```python
from fleet_brain import FleetBrain

brain = FleetBrain()  # opens $ALFRED_HOME/fleet-brain.db, runs migrations

brain.reflect(
    codename="lucius",
    repo="your-org/api",
    body="GraphQL schema lives in src/schema.graphql; tests live next to it.",
    tags=["graphql", "layout"],
)

# Next firing prepends these to the system prompt.
lessons = brain.recall(codename="lucius", repo="your-org/api")
for L in lessons:
    print(L.body)
```

## Entity model

| Entity      | What it is                                      | Stored in           |
|-------------|-------------------------------------------------|---------------------|
| `Lesson`    | One recall-able fact a firing learned           | `lessons`           |
| tags        | Many-to-many taxonomy buckets on a lesson       | `lesson_tags`       |
| `RepoNote`  | Free-text running summary for one repository    | `repo_notes`        |
| `FiringLog` | One firing's audit row (status, summary, cost)  | `firing_logs`       |
| `FileTouch` | One repo file an agent touched during a firing  | `file_touches`      |
| `MemoryCandidate` | Proposed lesson awaiting review before recall | `memory_candidates` |
| `FailureEvent` | Normalized non-success outcome for diagnosis | `failure_events` |

`severity` on a lesson follows the fleet's Slack severity routing:

- `info`: recall-only context.
- `warning`: worth bubbling into a future prompt.
- `blocker`: the next firing must read this before doing anything.

`status` on a firing log is one of `ok`, `blocked`, `partial`, `silent`.

## CLI

The operator surface is `alfred brain ...`, a passthrough to the standalone
`bin/alfred-brain.py` script.

```
alfred brain status
alfred brain lessons <codename> <repo>
alfred brain lessons - your-org/api          # widen codename
alfred brain reflect <codename> <repo> <body> [--tag T --severity warning]
alfred brain reflect <codename> <repo> <body> --candidate
alfred brain propose <codename> <repo> <body> [--tag T --confidence 0.8]
alfred brain candidates [--status candidate|validated|rejected|retired|all]
alfred brain promote <candidate-id>
alfred brain reject <candidate-id> --note "too vague"
alfred brain firings [--codename C] [--status S]
alfred brain files <repo> [--codename C] [--path P]
alfred brain failures [--codename C] [--repo R] [--subtype S]
alfred brain doctor [--json]
alfred brain forget <id>
alfred brain forget --before 30d
alfred brain export [--out PATH]
```

Sample session:

```
$ alfred brain reflect lucius your-org/api \
    "GraphQL schema lives in src/schema.graphql" \
    --tag graphql --tag layout
alfred-brain: reflected lesson 01HZAQ...

$ alfred brain lessons lucius your-org/api
01HZAQ...  2026-05-23 12:00  lucius/your-org/api
  [graphql,layout] GraphQL schema lives in src/schema.graphql

$ alfred brain status
alfred-brain: db = ~/.alfred/fleet-brain.db
  lessons     1
  firings     0
  file_touches 0
  candidates  0 (0 open)
  failures    0
  repo_notes  0
  tags        2
  codenames   1
  repos       1
```

## Configuration

| Env var                    | Default                              |
|----------------------------|--------------------------------------|
| `ALFRED_FLEET_BRAIN_DB`    | `$ALFRED_HOME/fleet-brain.db`        |
| `ALFRED_HOME`              | `~/.alfred`                          |
| `ALFRED_BRAIN_LOG_LEVEL`   | `WARNING` (CLI), `INFO` (ingest)     |
| `ALFRED_MEMORY_REFLECTION_MODE` | `direct` (`candidate` or `off` are also accepted) |

Setting `ALFRED_FLEET_BRAIN_DB` explicitly is the cleanest way to keep the brain on a separate disk, encrypt it, or pin it to a portable drive.

## Runtime recall + reflection

When an engine-aware runner knows the repository it is working on, it asks the
configured memory provider for up to three lessons before invoking the engine.
Those lessons are prepended as hints. The prompt also includes an optional
machine-readable reflection block; if the engine returns durable lessons,
Alfred strips that block from the user-facing result and writes the lessons to
the fleet-brain.

Memory is on by default through the in-tree `fleet` provider. To disable it:

```sh
export ALFRED_MEMORY_PROVIDERS=null
```

To chain a read-only personal knowledge base behind the fleet-brain:

```sh
export ALFRED_MEMORY_PROVIDERS=fleet,gbrain
export ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain
```

The fleet-brain remains the first writable provider, so reflection never writes
to the optional fallback.

By default, engine-returned reflection blocks are trusted and written as
lessons. If you want a review queue first, set:

```sh
export ALFRED_MEMORY_REFLECTION_MODE=candidate
```

Then review with `alfred brain candidates`, promote durable lessons, and reject
anything speculative. This is useful when you are first turning memory on for a
large fleet and want to see what agents try to remember.

## The outbox + ingest loop

The outbox drainer is still available for downstream fleets or offline import
jobs. Each codename can append one JSON record per line to
`$ALFRED_HOME/state/memory-outbox/<codename>.jsonl`. The `bin/fleet-ingest.py`
drainer reads those files, dispatches each record into the brain, and tracks a
per-file watermark so re-running is idempotent.

The drainer is opt-in. To enable it, add this line to `launchd/agents.conf`:

```
my.fleet.fleet-ingest    fleet-ingest.py    interval:900    no    my.fleet.fleet-ingest    Memory outbox drainer
```

Outbox record shapes:

```json
{"event": "reflect", "codename": "lucius", "repo": "your-org/api",
 "body": "...", "tags": ["graphql"], "firing_id": "01HZ...",
 "severity": "info", "ts": "2026-05-23T12:00:00Z"}

{"event": "firing_log", "firing_id": "01HZ...", "codename": "lucius",
 "repo": "your-org/api", "status": "ok", "summary": "...",
 "started_at": "...", "finished_at": "...", "cost_cents": 12,
 "pr_url": "...", "sentinel": null,
 "files_touched": [{"path": "src/api.py", "change_type": "modified"}]}

{"event": "note_repo", "repo": "your-org/api", "body": "..."}

{"event": "file_touch", "repo": "your-org/api", "path": "src/api.py",
 "codename": "lucius", "firing_id": "01HZ...", "pr_url": "...",
 "change_type": "modified", "ts": "2026-05-23T12:00:00Z"}

{"event": "memory_candidate", "codename": "lucius", "repo": "your-org/api",
 "body": "...", "tags": ["tests"], "source": "import", "confidence": 0.8}

{"event": "failure_event", "codename": "huntress", "repo": "your-org/web",
 "firing_id": "01HZ...", "subtype": "error_timeout", "summary": "...",
 "engine": "claude", "severity": "warning"}
```

Unknown event values are logged and skipped. The cursor still advances so one malformed line never wedges the drain.

`file_touch` records give operators a small local blast-radius index: which
agent touched which repo-relative path, when, and optionally under which firing
or PR. Query it with `alfred brain files your-org/api` or add `--path` when a
file starts failing and you want the recent agent history for it.

Failure events give the same kind of local trail for runner problems. Query
them with `alfred brain failures`, then run `alfred brain doctor` when you want
a quick health summary without opening SQLite manually.

## Read-only MCP bridge

`alfred mcp serve` exposes a small JSON-RPC stdio surface for local MCP clients
that want to read Alfred memory without mutating it. The exposed tools are:

- `alfred_brain_status`
- `alfred_memory_recall`
- `alfred_memory_candidates`
- `alfred_recent_file_touches`
- `alfred_failure_patterns`
- `alfred_memory_doctor`

The bridge returns allowlisted summaries only: no raw prompts, transcripts,
stdout, stderr, tokens, webhook URLs, or result blobs.

## Privacy + GC

The brain is local-only. Treat it as you would treat your shell history: it is the operator's data, not the fleet's data, and nothing in the OSS surface ever transmits it.

GC controls:

- `alfred brain forget <id>`: delete one lesson.
- `alfred brain forget --before 30d`: delete every lesson older than 30 days.
- Delete the SQLite file to start over. The next `FleetBrain()` call recreates the schema.

## Architecture notes

- `lib/fleet_brain/schema.py`: `CREATE TABLE IF NOT EXISTS` statements. Idempotent on every connection.
- `lib/fleet_brain/store.py`: `Store` Protocol plus the `SQLiteStore` implementation. Connections are short-lived (per call); the `:memory:` path caches a single handle for test ergonomics.
- `lib/fleet_brain/__init__.py`: the public `FleetBrain` class. Dependency-inverted on `Store` so a future PGLite/AGE-backed implementation drops in.
- `bin/alfred-brain.py`: operator CLI.
- `bin/alfred-mcp.py`: read-only JSON-RPC stdio bridge.
- `bin/fleet-ingest.py`: outbox drainer.

## v2 roadmap: PGLite + Apache AGE

The internal Alfred fleet runs a richer brain: PGLite (Postgres in WASM) with the Apache AGE graph extension and pgvector for embeddings, fronted by a localhost HTTP bridge. That stack supports:

- Cypher graph traversal (cross-firing blast-radius, cross-repo dependency walks).
- Bi-temporal queries (`valid_from`, `valid_to`, `recorded_from`, `recorded_to` on every vertex and edge).
- Semantic recall via 1024-d vector embeddings.
- Richer MCP tools for graph and semantic recall.

It also drags in a Node.js process tree, an AGE migration story, and a bridge daemon. The v1 SQLite brain is a deliberate scope cut: same entity model, same public API, no graph or vector layer. The `Store` Protocol means swapping in a PGLite-backed implementation later does not touch `FleetBrain` or the runners.

What is deferred to v2:

- AGE graph queries (`MATCH (a:Agent)-[:FIRED_AS]->(f:Firing)` and friends).
- Vector embeddings for semantic recall (`brain.recall(query="graphql auth")`).
- Bi-temporal columns on every entity.
- HTTP bridge so non-Python tooling can read the brain.

See `ROADMAP.md` and the site roadmap for current status.

## See also

- [`docs/STATE_MACHINE.md`](STATE_MACHINE.md): issue claim state, which `FiringLog.status` mirrors.
- [`docs/AGENTS.md`](AGENTS.md): the fleet's codename roster.
- `lib/fleet_brain/__init__.py`: the public API reference docstrings.
