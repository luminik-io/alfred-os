# FleetBrain: Alfred's local operational ledger

FleetBrain is Alfred's per-host operational ledger. Redis Agent Memory is the
default recalled-lesson store; FleetBrain keeps the review queue, firing
history, failure patterns, GitHub cache, file touches, and evidence that makes
those lessons trustworthy.

FleetBrain is a single SQLite file in your `$ALFRED_HOME`. Raw prompts, paths,
candidate text, and firing history stay on your machine. The normal outbound
surface is the prompt context Alfred prepends to a firing, which goes to Claude
Code or Codex on your existing CLI auth. Anonymous aggregate usage counts are on
by default; opt out with `alfred telemetry off`.

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
| `GitHubItem` | Cached issue/PR state from `fleet-github-poll.py` | `github_items` |
| `BundleItem` | Membership in an `agent:bundle:<slug>` rollout | `bundle_items` |
| `WorkerHeartbeat` | Last-seen liveness row for stale-worker checks | `worker_heartbeats` |

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
alfred brain github [--repo R] [--kind issue|pr] [--bundle slug]
alfred brain bundles [bundle-slug]
alfred brain workers [--stale]
alfred brain promotions
alfred brain failure-patterns [--codename C] [--repo R]
alfred brain governor [--json]
alfred brain doctor [--json]
alfred brain redis-status [--json]
alfred brain redis-sync [--codename C] [--repo R] [--dry-run]
alfred github-poll --repo your-org/api --repo your-org/web
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
  github      0
  bundles     0
  workers     0 (0 running)
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
| `ALFRED_MEMORY_REFLECTION_MODE` | `candidate` (`direct` or `off` are also accepted) |

Setting `ALFRED_FLEET_BRAIN_DB` explicitly is the cleanest way to keep the brain on a separate disk, encrypt it, or pin it to a portable drive.

## Runtime recall + reflection

When an engine-aware runner knows the repository it is working on, it asks the
configured memory provider for up to three lessons before invoking the engine.
Those lessons are prepended as hints. The prompt also includes an optional
machine-readable reflection block; if the engine returns durable lessons,
Alfred strips that block from the user-facing result and queues reviewable
memory candidates in the fleet-brain.

Runtime lesson recall is on by default through Redis Agent Memory Server, with
FleetBrain behind it as the local review queue and operational ledger. To
disable runtime recall and reflection:

```sh
export ALFRED_MEMORY_PROVIDERS=null
```

To keep the default Redis plus FleetBrain stack and add a read-only personal
knowledge base behind it:

```sh
export ALFRED_MEMORY_PROVIDERS=redis,fleet,gbrain
export ALFRED_GBRAIN_BIN=/usr/local/bin/gbrain
```

To point Alfred at a different Redis Agent Memory Server:

```sh
export ALFRED_MEMORY_PROVIDERS=redis,fleet
export ALFRED_REDIS_MEMORY_URL=http://127.0.0.1:9090
export ALFRED_REDIS_MEMORY_NAMESPACE=alfred
```

Keep `fleet` in the chain unless you are deliberately running without local
candidate review, firing logs, GitHub cache, worker heartbeats, and telemetry
inputs.

Use `alfred brain ams-status` or `alfred brain redis-status` to check the local
Agent Memory Server. Use `alfred brain redis-sync --dry-run` before
`alfred brain redis-sync` to carry older reviewed local lessons into Redis.
Unreviewed memory candidates and raw event logs stay local.

By default, engine-returned reflection blocks go to the review queue. If you
want trusted local runs to write lessons directly, set:

```sh
export ALFRED_MEMORY_REFLECTION_MODE=direct
```

Most installs should keep the default. Review with `alfred brain candidates`,
approve durable lessons, and reject anything speculative.

## The outbox + ingest loop

The outbox drainer is still available for downstream fleets or offline import
jobs. Each codename can append one JSON record per line to
`$ALFRED_HOME/state/memory-outbox/<codename>.jsonl`. The `bin/fleet-ingest.py`
drainer reads those files, dispatches each record into the brain, and tracks a
per-file watermark so re-running is idempotent.

The drainer is opt-in. To enable it, add this line to `launchd/agents.conf`:

```
alfred.fleet-ingest    fleet-ingest.py    interval:900    no    alfred.fleet-ingest    Memory outbox drainer
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

## GitHub poller, bundles, and stale workers

`bin/fleet-github-poll.py` pulls issue and PR state through `gh` and stores it
locally in the brain. It is intentionally pull-based: no webhook endpoint, no
GitHub App, no long-running service.

```sh
alfred github-poll --repo your-org/api --repo your-org/web
alfred brain github --state open
alfred brain bundles billing
```

Any label named `agent:bundle:<slug>` or `bundle:<slug>` is mirrored into
`bundle_items`, so Batman-style work can be inspected without re-querying
GitHub every time. Outbox imports can also write `github_item`, `bundle_item`,
and `worker_heartbeat` records directly.

Workers can publish liveness through `alfred brain heartbeat`. `alfred brain
workers --stale` reports running firings whose last heartbeat is older than
the threshold, and `alfred brain doctor` includes that signal alongside memory
candidate backlog, failure history, GitHub poll freshness, and bundle counts.

`alfred brain promotions` lists high-confidence candidates with evidence, tags,
or warning/blocker severity. You can promote and reject these by hand, but the
autonomous path below is the intended way candidates become lessons; this view
is for inspecting the queue and handling anything the judge held.

`alfred brain failure-patterns` groups repeated non-success outcomes by
codename, repo, subtype, and engine, then classifies the likely cause. The
local setup classifier catches errors such as missing Playwright browser
binaries and turns them into a concrete setup action instead of treating them
as product regressions.

`alfred brain governor` combines repeated failure patterns, stale workers, and
memory-promotion suggestions into one review action list. It is still
read-only: it does not pause a codename, create an issue, or mutate memory. It
gives the configured approver and the local dashboard a single place to see what needs
attention next.

`alfred brain harvest` is the write-side companion for repeated failures. It
previews candidate lessons by default and only writes when run with `--apply`.
The generated rows stay in the memory-candidate queue until the configured
approver approves or rejects them.

`bin/memory-harvest.py` is the scheduler wrapper for that same loop. It runs
`alfred brain harvest --apply --json`, posts to Slack only when new candidates
are queued or the run fails, and leaves promotion to the autonomous path or the
operator. Add it to `launchd/agents.conf` or `systemd` when you want Alfred to
build the candidate queue automatically without expanding prompt recall silently.

## Autonomous capture and save

The intent is that memory captures AND saves itself through the LLMs, rather
than waiting on a human review queue. `alfred brain auto-promote`
(`FleetBrain.auto_promote_candidates`) makes the LLM the primary save decision.
It is enabled by default when `ALFRED_AUTO_PROMOTE` is unset, blank, or a
recognized truthy value (`1`, `true`, `yes`, `on`, `enabled`). Set
`ALFRED_AUTO_PROMOTE=0` for a normal opt-out, or `ALFRED_AUTO_PROMOTE_KILL=1`
to halt it immediately. Any other nonblank `ALFRED_AUTO_PROMOTE` value fails
closed. A disabled run reads and writes nothing.

Each pending candidate must still carry evidence and not conflict with another
unreviewed version of the same lesson. The structural confidence
bar (default 0.5, env `ALFRED_AUTO_PROMOTE_THRESHOLD`) is only a light
pre-filter so any evidenced candidate reaches the real decision-maker: an LLM
safety judge (`lib/memory_judge.py`, default ON, gated by
`ALFRED_AUTO_PROMOTE_LLM_JUDGE`). The judge reads the candidate's
topic/body/evidence and:

- saves both safe and behavior-changing lessons. Behavior-changing lessons are
  the most actionable kind, so they are auto-saved too (recorded with a distinct
  note and counted under `auto_saved_behavior_change`) rather than held for a
  human. Every auto-save is reversible with `alfred brain forget`, which is the
  safety net.
- holds a candidate it calls a duplicate, so dedup owns merging without the
  lesson re-entering the harvest loop.
- only ever lowers the score: the run takes `min(structural, judge)` confidence,
  so a high judge score can never rescue a below-bar candidate, and a candidate
  the judge drops under the bar is held.

The judge fails closed: any LLM error, timeout, or unparseable verdict leaves
the candidate pending and is counted under `judge_errors`, never auto-saved on a
failed or empty judgment. When the judge is explicitly disabled, the low default
bar is raised to a conservative no-judge floor
(`ALFRED_AUTO_PROMOTE_NO_JUDGE_THRESHOLD`, default 0.9) so default-confidence
candidates are not promoted with no model or human review. Each run is bounded
by a per-run promotion cap (`ALFRED_AUTO_PROMOTE_MAX_PER_RUN`, default 5), a
judge-call budget (`ALFRED_AUTO_PROMOTE_MAX_JUDGE_CALLS`, default 25, floored by
the cap), and the candidate-side conflict check.

A saved candidate is promoted through the same `promote_memory_candidate` path
as a manual promotion, recorded with `reviewer="auto"` so the batch stays
auditable. Auto-promoted lessons are written to Redis Agent Memory Server under
a deterministic candidate-derived memory id, so the write is idempotent and can
be reverted with the auto-promotion rollback lever.

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
- `lib/fleet_brain/__init__.py`: the public `FleetBrain` class. Dependency-inverted on `Store` so operational storage can change without touching runners.
- `bin/alfred-brain.py`: Alfred CLI.
- `bin/alfred-mcp.py`: read-only JSON-RPC stdio bridge.
- `bin/fleet-ingest.py`: outbox drainer.

## Planning memory

`alfred serve` uses promoted lessons while shaping new work. The Planning tab
recalls a small prompt-safe set of repo lessons, shows them beside the draft,
and embeds them in saved specs under "Planning Memory." Those hints are
advisory: current code, current issues, and readiness checks still win.

Saving a spec queues a reviewable memory candidate, so useful spec-to-issue
lessons can be promoted explicitly instead of silently becoming future prompt
context.

Slack uses the same rule. A trusted Slack follow-up is first captured as local
planning context, not as memory. When someone runs `draft <id>`, Alfred turns
that follow-up into a local planning draft, recalls reviewed planning memory,
runs readiness checks, and only then queues any new memory candidate. This keeps
Slack convenient without letting raw chat become long-term truth.

Slack can also drive the review loop directly:

```text
memory
memories
remember luminik-io/alfred-os: Slack memory candidates must stay reviewable.
memory remember luminik-io/alfred-os: keep the namespace discoverable.
memory promote <candidate-id>
memory reject <candidate-id> too vague for future recall
memory harvest
memory harvest now
memory redis
memory sync
```

`remember ...` and `memory remember ...` stage candidates; they do not become
prompt context. Approval and rejection stay configured-approver only. Alfred Desktop
uses the same local candidate queue through `alfred serve`, so Slack, CLI, and
client review the same rows. `memory harvest` previews repeated-failure lessons
from the reliability governor; `memory harvest now` queues those lessons as
reviewable candidates. `memory redis` checks the Redis Agent Memory Server.
Promoting a candidate already routes the lesson toward Redis, so `memory sync`
is a back-fill for older lessons: it previews a one-way copy of already-promoted
local lessons into Redis, and `memory sync now` runs it.

If `memory-harvest.py` is scheduled, it simply performs the `memory harvest now`
write step for repeated failures. It still only creates candidates, so the Slack
review loop is unchanged.

## What to build next

FleetBrain is intentionally small: file touches, failure events, reviewable
memory candidates, repeated-failure classification, GitHub cache, worker
heartbeats, and read-only MCP access. The useful next work is not "more
storage"; it is closing feedback loops:

1. **Evidence-linked lesson promotion.** Every promoted lesson should point to
   the firing, PR, issue, file touch, or operator note that made it trustworthy.
   That keeps memory from becoming folklore.
2. **Action execution for governor findings.** The governor now proposes
   actions; a future pass should let the operator approve safe follow-ups such
   as filing a setup issue or pausing one codename.
3. **Spec and bundle evidence.** Planning drafts now queue candidates; next,
   Batman and specs-driven workflows should remember which specs generated
   which issues, which PRs landed, and which acceptance criteria needed
   follow-up.
4. **Memory quality gates.** Candidate promotion should run lightweight checks:
   no secrets, source attached, confidence present, not contradicted by a newer
   lesson, and scoped to a codename/repo when possible.

## FleetBrain direction

FleetBrain remains the local operational ledger. Keep it boring unless the
operational evidence outgrows a single-host SQLite store. If that day comes,
the `Store` boundary is the place to change storage without touching agent
runners.

Do not add a graph database or vector layer to FleetBrain for recalled lessons.
That job belongs to Redis Agent Memory.

See `ROADMAP.md` and the site roadmap for current status.

## See also

- [`docs/STATE_MACHINE.md`](STATE_MACHINE.md): issue claim state, which `FiringLog.status` mirrors.
- [`docs/AGENTS.md`](AGENTS.md): the fleet's codename roster.
- `lib/fleet_brain/__init__.py`: the public API reference docstrings.
