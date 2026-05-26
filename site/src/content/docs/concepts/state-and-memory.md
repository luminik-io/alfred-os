---
title: State and memory
description: What Alfred remembers between firings, what it forgets, and where every byte of that memory lives on disk.
---

Alfred is built on the premise that the host filesystem is a fine state store for a single-operator fleet. Every firing reads its inputs from scratch, writes its outputs to plain JSON or JSONL files under `$ALFRED_HOME/state/`, and exits. There is no daemon holding state in RAM, no Redis, no Postgres, no shared cluster. If you delete `$ALFRED_HOME/state/`, the next firing rebuilds whatever it still needs.

This page is the map of that directory and the contract each file carries. Full doc at [`docs/STATE_AND_MEMORY.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/STATE_AND_MEMORY.md).

## The state tree

```
$ALFRED_HOME/state/
├── global-blocked-until.json     fleet-wide rate-limit block
├── paused-repos.json             repos the operator manually paused
├── code-map.json                 cross-repo HEAD SHAs and file index
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

**`global-blocked-until.json`** is the fleet-wide rate-limit block. When a Claude-backed agent returns `error_rate_limit` or `error_budget`, the runner calls `set_global_block(hours=1, reason=...)` which writes this file. Every other agent's `is_globally_blocked()` check at the top of `main()` reads it, prints `[<AGENT>-GLOBAL-BLOCKED]`, and exits 0 until the timestamp passes. See [Architecture](/concepts/architecture/) for the rationale.

**`paused-repos.json`** is the operator's manual repo pause list. `alfred status` reads it, `set_repo_paused()` writes it. Every consumer's `pick_*` helper skips paused repos. Missing or malformed file is treated as "no repos paused" (fail-open).

**`code-map.json`** is the cross-repo index that `code-map-refresh` writes every six hours. Drake and code-map-aware review prompts read it for repo HEAD SHAs and a light file inventory. Safe to delete; the next `code-map-refresh` firing rebuilds it.

**`slack-webhook.cache`** stores the resolved Slack incoming-webhook URL with a 30-day TTL. Lets agents avoid an AWS Secrets Manager round-trip every firing.

**`_paused/<codename>`** is a marker file the `alfred pause <codename>` command writes. The pause survives a `deploy.sh` re-render, and `alfred resume <codename>` removes it.

**`fleet/enabled.txt`** is the runner-level fleet gate. Listed codenames are enabled; missing codenames fall back to each runner's default. `alfred enable`/`alfred disable` mutates this file.

**`engines/<codename>`** is the persisted engine override (`claude`, `codex`, or `hybrid`). `alfred engine set` writes it. See [Engine routing](/concepts/engine-routing/).

**`<codename>/spend-YYYY-MM-DD.json`** is the per-agent per-day ledger. Tracks `turns_today`, `cost_usd`, `consecutive_failures`, success counts. Auto-resets at midnight via the filename. The agent's runner enforces its ceiling and self-pauses if exceeded. Dry-run firings write to a separate `spend-dryrun-YYYY-MM-DD.json` sibling so they never poison real spend.

**`<codename>/events/`** is a per-firing event log. Each firing writes a structured trail of what it tried, what it claimed, what the engine returned, and how it exited. Used by `alfred status` and the recap agents.

**`claims/<repo>-<issue>.json`** is the local mirror of an in-flight claim when the runner needs to reconcile against the GitHub-side audit comments. The source of truth for the [state machine](/concepts/state-machine/) is the structured HTML comment on the issue itself; this file is the local cache.

**`transcripts/<codename>/<YYYY-MM>/<firing-id>.jsonl`** is the planned home for full Claude transcripts. The convention is written and the path helpers exist, but the current runner does not write transcripts by default. Codex transcripts under `codex/` are written today.

**`codex/<codename>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}`** is the per-firing Codex artifact bundle. `last.md` is the final message, `stdout.txt` and `stderr.txt` are the captured streams. See [Codex provider](https://github.com/luminik-io/alfred-os/blob/main/docs/CODEX_PROVIDER.md).

**`worktrees/eng-<codename>-<repo>-<issue>-<ts>/`** is a throwaway git worktree, created by `make_worktree` and removed at the end of the firing. Surviving worktrees are pruned at the start of the next firing or by `agent-cleanup`.

**`memory-outbox/<codename>.jsonl`** is the append-only outbox the fleet-brain ingest drainer reads. Built-in engine-aware runners now write directly to the configured memory provider, but the outbox remains available for downstream fleets, imports, and tools that want an asynchronous drain path.

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
| Lessons in the fleet brain | yes | yes | yes |

The contract is intentionally narrow: anything an agent must remember between firings is a JSON or JSONL file on disk. Anything else is reconstructed from GitHub, the repo checkout, or the operator's `~/.alfredrc`.

## The fleet brain

The state files above are operational memory. They tell Alfred what is blocked, what is paused, what spend is left, and which worktree to clean up. They are not where the fleet remembers *lessons* (repo conventions, recurring bugs, the operator's preferred PR style).

That role belongs to the fleet brain: a single SQLite file under `$ALFRED_HOME/fleet-brain.db` with a `reflect` / `recall` API. Engine-aware runners that know their target repo recall up to three lessons before invoking the engine, then record any durable lessons the engine returns in a machine-readable reflection block.

The same brain stores recent file touches when an agent or outbox import knows
which repo-relative paths changed. Use `alfred brain files <repo>` to inspect
that local history. It also stores reviewable memory candidates and normalized
failure events so repeated runtime problems are searchable instead of living
only in Slack.

The brain ships on by default through the local `fleet` provider. Set `ALFRED_MEMORY_PROVIDERS=null` to disable it, or `ALFRED_MEMORY_PROVIDERS=fleet,gbrain` to add a read-only fallback provider. See [`docs/FLEET_BRAIN.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/FLEET_BRAIN.md) for the full design, schema, and CLI.

Use `alfred brain doctor` for a read-only health check, and `alfred mcp serve`
when a local MCP client needs read-only memory access.

The brain v1 store is dependency-inverted on a `Store` Protocol, so a future PGLite or graph-backed implementation drops in without touching agent runners.

## Privacy model

Everything in this tree is local to the operator's machine. Nothing in the Alfred OSS surface transmits state files, transcripts, lessons, or spend ledgers off-host. The only outbound channels are:

- The configured engine (`claude -p` or `codex exec`), which sends the prompt you compose to Anthropic or OpenAI on your existing CLI auth.
- The GitHub CLI (`gh`), which talks to GitHub on your existing `gh auth login` token.
- The Slack incoming webhook, when configured.

If you delete `$ALFRED_HOME/`, you delete every byte Alfred remembers about your fleet. Treat the directory the way you treat your shell history: it is operator data, not fleet data, and never leaves the host unless you put it somewhere yourself.

## See also

- [Architecture](/concepts/architecture/): the runtime boundary and the rationale for host-filesystem state.
- [How it works](/concepts/how-it-works/): the gates that read this tree on every firing.
- [Issue claim state machine](/concepts/state-machine/): the coordination primitive backing `claims/` and the GitHub-side audit comments.
- [Engine routing](/concepts/engine-routing/): how `engines/<codename>` is resolved at firing time.
- [Operating the fleet](/getting-started/operating-the-fleet/): the day-to-day commands that read and write this tree.
