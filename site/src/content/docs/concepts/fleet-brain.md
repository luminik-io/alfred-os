---
title: FleetBrain
description: "Alfred's local operational ledger: reviewable memory candidates, failure history, GitHub cache, file touches, and read-only MCP access."
---

FleetBrain is Alfred's per-host operational ledger. It is not a cloud service
and it is not a hidden agent loop. It is a local SQLite file under
`$ALFRED_HOME/fleet-brain.db`, read and written by short-lived firings.

Redis Agent Memory is the default recalled-lesson store. FleetBrain keeps the
review queue, firing history, failure patterns, GitHub cache, file touches, and
evidence that makes those lessons trustworthy.

Full source doc: [`docs/FLEET_BRAIN.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/FLEET_BRAIN.md).

## What it stores

| Entity | Purpose |
|---|---|
| `Lesson` | A recallable fact about a codename, repo, convention, or recurring failure. |
| `MemoryCandidate` | A proposed lesson awaiting review. |
| `FailureEvent` | A normalized non-success outcome, useful for spotting repeated setup or runtime failures. |
| `GitHubItem` | Cached issue/PR state pulled through the local GitHub CLI. |
| `BundleItem` | Membership in an `agent:bundle:<slug>` rollout. |
| `WorkerHeartbeat` | Last-seen liveness row for stale-worker detection. |
| `FiringLog` | A compact audit row for one firing. |
| `FileTouch` | A repo-relative file path touched by an agent, optionally tied to a firing or PR. |
| `RepoNote` | Free-text notes about one repository. |

The important distinction: state files under `$ALFRED_HOME/state/` tell Alfred
what is paused, claimed, blocked, or recently run. The fleet brain stores
review queues and history that should influence future firings.

## Operator commands

```sh
alfred brain status
alfred brain lessons lucius your-org/api
alfred brain reflect lucius your-org/api "Run npm test before opening frontend PRs" --tag tests
alfred brain propose lucius your-org/api "Use request fixtures for API tests" --tag tests
alfred brain candidates
alfred brain promote <candidate-id>
alfred brain reject <candidate-id> --note "too vague"
alfred brain failures --codename huntress
alfred brain files your-org/api
alfred github-poll --repo your-org/api
alfred brain github --state open
alfred brain bundles billing
alfred brain workers --stale
alfred brain promotions
alfred brain failure-patterns --codename huntress
alfred brain governor
alfred brain doctor
alfred mcp serve
```

Runtime memory is on by default through `redis,fleet`: Redis handles recalled
lessons, and FleetBrain keeps the local review and reliability ledger. Turn
runtime recall off:

```sh
export ALFRED_MEMORY_PROVIDERS=null
```

Engine-written reflections are reviewable by default. Keep the default for
most fleets:

```sh
export ALFRED_MEMORY_REFLECTION_MODE=candidate
```

Then use `alfred brain candidates` to promote useful lessons and reject noisy
ones. Set `ALFRED_MEMORY_REFLECTION_MODE=direct` only for trusted local
runs where direct lesson writes are intentional.

Check the local memory server:

```sh
alfred brain redis-status
alfred brain redis-sync --dry-run
alfred brain redis-sync --codename lucius --repo your-org/api
```

Only reviewed local lessons are synced. Raw transcripts, event logs, and
pending memory candidates stay local.

The GitHub poller keeps issue, PR, and bundle state in the same local memory
store. The doctor command uses that cache to report poll freshness, open bundle
shape, stale worker heartbeats, repeated failures, and high-confidence memory
candidates that are ready for review. The governor command turns those signals
into a read-only action list for you and the local dashboard.
`alfred brain harvest` previews candidate lessons from repeated failure
patterns; `alfred brain harvest --apply` queues those lessons for review.

The Planning tab also uses the brain. When promoted lessons exist for the repos
in a draft, Alfred recalls a small prompt-safe set as advisory planning memory.
Saved specs include those hints under "Planning Memory" and queue a reviewable
candidate so useful spec-to-issue lessons can be promoted explicitly. Current
code and the current issue still win over memory.

Slack follows the same memory boundary. A trusted follow-up reply is captured
as local planning context first. When someone runs `draft <id>` in Slack,
Alfred converts that follow-up into a local planning draft, recalls reviewed
planning memory, reruns readiness, and only then queues any new memory candidate.
Raw chat is never promoted as long-term truth by itself.

Slack-created planning drafts use the same automatic review queue. If the draft
is already implementation-ready, Alfred proposes a `slack-planning` candidate
with the local draft path and thread evidence. The candidate is visible in
`memory`, but it is not recallable until you promote it. Disable this
with `ALFRED_SLACK_MEMORY_CANDIDATES=0`.

Slack can drive the same review loop:

```text
memory
memories
remember your-org/api: Use request fixtures for API tests.
memory remember your-org/api: Keep candidate review explicit.
memory promote <candidate-id>
memory reject <candidate-id> too vague
memory harvest
memory harvest now
memory redis
memory sync
```

`remember ...` and `memory remember ...` stage candidates only. Promotion and
rejection stay explicit. Alfred Desktop uses the same local candidate
queue through `alfred serve`, so Slack, CLI, and client review the same rows.
`memory harvest` previews repeated-failure lessons and `memory harvest now`
queues them as candidates. Check Redis Agent Memory Server with `memory redis`,
preview reviewed-lesson sync with `memory sync`, and write carried-forward
reviewed lessons with `memory sync now`.

For unattended fleets, schedule `memory-harvest.py` from launchd or systemd. It
queues the same reviewable repeated-failure candidates and nudges Slack only
when there is something to review. It does not promote lessons or sync Redis.

## MCP access

`alfred mcp serve` exposes a small read-only JSON-RPC stdio surface for local
MCP clients. It returns allowlisted summaries only: no raw prompts, transcripts,
stdout, stderr, webhook URLs, or result blobs.

Available tools include:

- `alfred_brain_status`
- `alfred_memory_recall`
- `alfred_memory_candidates`
- `alfred_recent_file_touches`
- `alfred_failure_patterns`
- `alfred_memory_doctor`

## Privacy model

The brain never leaves your host unless you export or back it up yourself. The
only prompt-boundary effect is recall: Alfred prepends selected lessons to the
next engine invocation. Treat the brain like shell history: useful, local, and
owned by the local host.

## Next memory work

The next useful layer is reliability, not more mystery:

- Add approved follow-up execution for governor findings, such as filing a
  setup issue or pausing a single codename.
- Connect saved planning candidates to Batman and Drake so the brain can answer
  which spec generated which issues and PRs.
- Add semantic recall for lessons, plans, and failure summaries.
- Add lightweight memory-quality checks before candidate promotion.

See also:

- [State and memory](/concepts/state-and-memory/)
- [Alfred CLI](/reference/cli/)
- [`docs/MEMORY_PROVIDERS.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/MEMORY_PROVIDERS.md)
