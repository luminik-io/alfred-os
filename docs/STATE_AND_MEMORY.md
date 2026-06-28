# State and memory

What Alfred remembers between firings, what it forgets, and where every byte of that memory lives on disk.

Alfred is built on the premise that the host filesystem is a fine operational
state store for a single-operator fleet. Every firing reads its inputs from
scratch, writes operational state to plain JSON or JSONL files under
`$ALFRED_HOME/state/`, and records local review and reliability state in
`$ALFRED_HOME/fleet-brain.db`. Recalled semantic lessons live in the local
Redis Agent Memory Server by default. If you delete the state tree and
FleetBrain database, the next firing rebuilds whatever it still can from GitHub
and local config.

This page maps the local state files, the fleet-brain database, and the contract each part carries.

## The state tree

```
$ALFRED_HOME/state/
├── global-blocked-until.json     fleet-wide rate-limit block
├── paused-repos.json             repos the operator manually paused
├── code-map.json                 cross-repo files, symbols, imports, routes
├── slack-webhook.cache           30-day cache of the resolved webhook URL
├── _paused/                      per-agent pause markers
├── fleet/
│   └── enabled.txt               runner-gate list of enabled codenames
├── engines/
│   └── <codename>                claude | codex | hybrid override
├── <codename>/
│   ├── spend-YYYY-MM-DD.json     per-agent per-day spend ledger
│   ├── spend-dryrun-YYYY-MM-DD.json
│   └── events/                   per-firing event log
├── claims/                       agent-claim audit trail (when present)
├── transcripts/<codename>/<YYYY-MM>/<firing-id>.jsonl
├── codex/<codename>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}
├── worktrees/                    throwaway git worktrees per firing
└── memory-outbox/<codename>.jsonl  reflect / firing_log / note_repo records
```

`$ALFRED_HOME` defaults to `~/.alfred`. Forks can override it. Worktrees technically sit under `$ALFRED_HOME/worktrees/` (a sibling of `state/`), but every other artifact is a child of `state/`.

### What each file carries

**`global-blocked-until.json`** is the fleet-wide rate-limit block. When a Claude-backed agent returns `error_rate_limit` or `error_budget`, the runner calls `set_global_block(hours=1, reason=...)` which writes this file. Every other agent's `is_globally_blocked()` check at the top of `main()` reads it, prints `[<AGENT>-GLOBAL-BLOCKED]`, and exits 0 until the timestamp passes. See [Architecture](./../ARCHITECTURE.md) for the rationale.

**`paused-repos.json`** is the operator's manual repo pause list. `alfred status` reads it, `set_repo_paused()` writes it. Every consumer's `pick_*` helper skips paused repos. Missing or malformed file is treated as "no repos paused" (fail-open).

**`code-map.json`** is the cross-repo map that `code-map-refresh` writes every six hours. Drake, Batman, and code-map-aware review prompts read it for repo HEAD SHAs, source files, public-ish symbols, imports, API calls, server routes, and contract drift. It is advisory and local. Safe to delete; the next `code-map-refresh` firing rebuilds it.

**`slack-webhook.cache`** stores the resolved Slack incoming-webhook URL with a 30-day TTL. Lets agents avoid an AWS Secrets Manager round-trip every firing.

**`_paused/<codename>`** is a marker file the `alfred pause <codename>` command writes. The pause survives a `deploy.sh` re-render, and `alfred resume <codename>` removes it.

**`fleet/enabled.txt`** is the runner-level fleet gate. Listed codenames are enabled; missing codenames fall back to each runner's default. `alfred enable`/`alfred disable` mutates this file.

**`engines/<codename>`** is the persisted engine override (`claude`, `codex`, or `hybrid`). `alfred engine set` writes it. See [Engine routing](./../docs/ENGINE_ROUTING.md).

**`<codename>/spend-YYYY-MM-DD.json`** is the per-agent per-day ledger. Tracks `turns_today`, `cost_usd`, `consecutive_failures`, success counts. Auto-resets at midnight via the filename. The agent's runner enforces its ceiling and self-pauses if exceeded. Dry-run firings write to a separate `spend-dryrun-YYYY-MM-DD.json` sibling so they never poison real spend.

**`<codename>/events/`** is a per-firing event log. Each firing writes a structured trail of what it tried, what it claimed, what the engine returned, and how it exited. Used by `alfred status` and the recap agents.

**`claims/<repo>-<issue>.json`** is the local mirror of an in-flight claim when the runner needs to reconcile against the GitHub-side audit comments. The source of truth for the [state machine](./STATE_MACHINE.md) is the structured HTML comment on the issue itself; this file is the local cache.

**`transcripts/<codename>/<YYYY-MM>/<firing-id>.jsonl`** is the planned home for full Claude transcripts. The convention is written and the path helpers exist, but the current runner does not write transcripts by default. Codex transcripts under `codex/` are written today.

**`codex/<codename>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}`** is the per-firing Codex artifact bundle. `last.md` is the final message, `stdout.txt` and `stderr.txt` are the captured streams. See [Codex provider](./CODEX_PROVIDER.md).

**`worktrees/eng-<codename>-<repo>-<issue>-<ts>/`** is a throwaway git worktree, created by `make_worktree` and removed at the end of the firing. Surviving worktrees are pruned at the start of the next firing or by `agent-cleanup`.

**`memory-outbox/<codename>.jsonl`** is the append-only outbox the fleet-brain ingest drainer reads. The built-in engine-aware runners now write directly to the configured memory provider, but the outbox remains available for downstream fleets, imports, and tools that want an asynchronous drain path.

## What Alfred remembers vs forgets between firings

| Kind | Survives a firing | Survives a reboot | Survives `deploy.sh` |
|---|---|---|---|
| Per-day spend ledger | yes | yes | yes |
| Global rate-limit block | yes (until expiry) | yes | yes |
| Repo pause list | yes | yes | yes |
| Per-codename pause marker | yes | yes | yes |
| Engine override | yes | yes | yes |
| `code-map.json` | yes (until next refresh) | yes | yes |
| Transcripts and Codex artifacts | yes | yes | yes |
| In-flight worktree | no (removed on exit) | n/a | n/a |
| Process state, in-memory caches | no | no | n/a |
| Engine session id from `claude -p` | written to the result; not resumed | n/a | n/a |
| Semantic lessons in Redis Agent Memory | yes | yes | yes |
| FleetBrain review and reliability ledger | yes | yes | yes |

The contract is intentionally narrow: operational state is JSON or JSONL on
disk, promoted lessons live in Redis Agent Memory, and review/reliability rows
live in FleetBrain. Anything else is reconstructed from GitHub, the repo
checkout, or the operator's `~/.alfredrc`.

## The fleet brain

The state files above are operational memory. They tell Alfred what is blocked, what is paused, what spend is left, and which worktree to clean up. They are not where the fleet remembers *lessons* (repo conventions, recurring bugs, the operator's preferred PR style).

Promoted lesson recall belongs to Redis Agent Memory Server. Engine-aware
runners that know their target repo recall up to three lessons before invoking
the engine. If the engine returns a machine-readable memory reflection block,
Alfred strips it from the user-facing result and queues those entries as
FleetBrain candidates by default. The LLM judge saves safe candidates
autonomously rather than waiting on a human queue (see
[Fleet brain](./FLEET_BRAIN.md)). Set `ALFRED_AUTO_PROMOTE=0` to opt out, or
`ALFRED_MEMORY_REFLECTION_MODE=direct` only when direct lesson writes are
intentional.

FleetBrain stores recent file touches when an agent or outbox import knows which
repo-relative paths changed. Use `alfred brain files <repo>` to inspect that
local history. It also stores reviewable memory candidates and normalized failure
events so repeated runtime problems are searchable instead of living only in
Slack.

Repeated failures can be grouped with `alfred brain failure-patterns` and
summarized with `alfred brain governor`. The governor classifies local setup
problems, provider limits, auth failures, timeouts, and agent-quality loops,
then returns a read-only action list for you and the dashboard.

The default provider chain is `redis,fleet`. Set
`ALFRED_MEMORY_PROVIDERS=null` to disable runtime recall and reflection, or
`ALFRED_MEMORY_PROVIDERS=redis,fleet,gbrain` to add a read-only personal
knowledge base behind the default stack. Keep `fleet` in the chain if you want
reviewable memory candidates, firing logs, GitHub cache, worker heartbeats, and
telemetry inputs. See [Fleet brain](./FLEET_BRAIN.md) for the local ledger and
CLI, and [Memory providers](./MEMORY_PROVIDERS.md) for the provider chain.

## Context governor

Before a firing reaches Claude or Codex, Alfred runs a local context governor
over the final prompt after memory recall has been attached. The governor is
default-on and no-ops for ordinary prompt sizes. When a prompt exceeds the
configured character or UTF-8 byte budget, Alfred keeps the beginning and tail,
replaces the middle with an explicit `ALFRED_CONTEXT_GOVERNOR` marker, and
records exact before/after character and byte counts in the engine result
metadata. It never summarizes or invents facts. The agent must recover any
omitted detail from the repository, tests, memory, or code-memory MCP tools.

Configuration lives in `~/.alfredrc` or `$ALFRED_HOME/.env`:

| Variable | Default | What it does |
|---|---:|---|
| `ALFRED_CONTEXT_GOVERNOR` | `1` | Set `0` to disable prompt compaction. An empty value keeps the default enabled. |
| `ALFRED_CONTEXT_MAX_CHARS` | `96000` | Maximum final prompt characters before the governor trims the middle. |
| `ALFRED_CONTEXT_MAX_BYTES` | `96000` | Maximum final UTF-8 bytes, capped at the argv-safe default for Claude `-p`. |
| `ALFRED_CONTEXT_HEAD_CHARS` | `58000` | Preferred number of leading characters to preserve. |
| `ALFRED_CONTEXT_TAIL_CHARS` | `30000` | Preferred number of trailing characters to preserve. |

The `memory-harvest.py` scheduled wrapper runs the same safe loop as
`memory harvest now`: repeated failure patterns become reviewable candidates,
not trusted lessons. Slack remains the review surface for `memory` and the
approve/reject actions.

Use `alfred brain doctor` for a read-only health check, `alfred brain governor`
for the current action queue, and `alfred mcp serve` when a local MCP client
needs read-only memory access.

FleetBrain is dependency-inverted on a `Store` Protocol, so operational storage
can change without touching agent runners. Runtime lesson recall goes through
the memory provider chain, with Redis Agent Memory first by default.

## Privacy model

Everything in this tree is local to your machine. Nothing in Alfred transmits state files, transcripts, lessons, or spend ledgers off-host. The only outbound channels are:

- The configured engine (`claude -p` or `codex exec`), which sends the prompt you compose to Anthropic or OpenAI on your existing CLI auth.
- The GitHub CLI (`gh`), which talks to GitHub on your existing `gh auth login` token.
- The Slack incoming webhook, when configured.

If you delete `$ALFRED_HOME/`, you delete every byte Alfred remembers about your fleet. Treat the directory the way you treat your shell history: it is operator data, not fleet data, and never leaves the host unless you put it somewhere yourself.

## See also

- [Architecture](./../ARCHITECTURE.md): the runtime boundary and the rationale for host-filesystem state.
- [Issue claim state machine](./STATE_MACHINE.md): the coordination primitive backing `claims/` and the GitHub-side audit comments.
- [Fleet brain](./FLEET_BRAIN.md): the local operational ledger and review queue.
- [Engine routing](./ENGINE_ROUTING.md): how `engines/<codename>` is resolved at firing time.
- [Operating the fleet](./OPERATING_THE_FLEET.md): the day-to-day commands that read and write this tree.
