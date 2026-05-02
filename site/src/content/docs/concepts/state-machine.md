---
title: Issue claim state machine
description: How pennyworth prevents two actors (any agent, any operator) from doing the same work twice on the same GitHub issue.
---

The full doc lives at [`docs/STATE_MACHINE.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/STATE_MACHINE.md). This page is the executive summary.

## The problem

Two actors can race on the same issue:

- Two agent firings (rare — `with_lock` serializes per codename, but cross-codename collisions exist).
- One agent + the operator pushing a manual branch.
- Two operators if you ever expand beyond solo (out of scope today, but the design accommodates it).

Without a coordination primitive, you get **duplicate work**. The reference fleet hit this on day one of full autonomy: Lucius shipped a quick PR for issue #303 at 09:22; the operator opened a careful PR for the same issue at 11:30, neither aware of the other.

## The mechanism

State carried entirely on **GitHub labels + structured HTML comments**. No shared database, no shared filesystem, no Slack lock. GitHub is the synchronisation point.

### Lifecycle labels (mutually exclusive)

| Label | Meaning | Set by |
|---|---|---|
| `agent:implement` | Eligible for autonomous pickup | Drake (or human) |
| `agent:in-flight` | An agent is actively working it | `claim_issue()` |
| `agent:pr-open` | A PR exists for this issue | `release_issue(transition_to=...)` |
| `agent:done` | Closed and shipped | external (PR merge handler) |

### Sticky modifiers (orthogonal)

| Label | Meaning |
|---|---|
| `do-not-pickup` | Operator override — agents skip this issue |
| `needs:human-scope` | Issue is too vague; not eligible for autonomous pickup |

### Claim comments

Posted alongside every label change so the audit trail survives manual label edits:

```
<!-- agent-claim:codename=lucius firing_id=20260501-194217-643a ts=2026-05-01T19:42:33Z -->
<!-- agent-release:codename=lucius firing_id=20260501-194217-643a outcome=success pr=https://github.com/foo/bar/pull/42 ts=2026-05-01T20:08:11Z -->
```

`find_stale_claims()` reads these to decide who currently holds an in-flight claim and how old that claim is, without depending on label-event timestamps.

## Race resolution

`claim_issue()`:

1. Reads current label set; refuses if any blocker label is present.
2. Atomically adds `agent:in-flight` + posts the claim comment.
3. Re-reads recent comments to detect any unreleased earlier claim.
4. If an earlier claimant exists (by `createdAt` timestamp), the loser:
   - Removes its own `agent:in-flight` label
   - Restores `agent:implement`
   - Posts a release comment with `outcome=race-yielded-to=<earlier_codename>:<earlier_firing_id>`
5. The earlier claimant keeps the issue uncontested.

The loser exits the firing without burning a Claude turn on duplicate work. The race window collapses from ~20 minutes (between agent pick + PR open) to the sub-second gap between read-labels and add-label.

## Stale-claim sweep

A runner crashing between `claim_issue` and `release_issue` would normally leave an issue blocked indefinitely. `find_stale_claims()` reads claim comments and surfaces any in-flight claim with no matching release after `max_age_hours` (default 4). `force_release_stale_claim()` then transitions the issue back to `agent:implement` so the queue picks it up again.

Wire it into your fleet's daily cleanup runner. The shipped CLI exposes this as `pennyworth-label-state sweep-claims [--max-age-hours N] [--dry-run]`.

## Operator overrides

Two ways to take an issue manually without racing an agent:

```sh
# Mark a single issue do-not-pickup
pennyworth-label-state claim <repo>#<N>
# ... do your work ...
pennyworth-label-state release <repo>#<N>
```

```sh
# Take a whole repo offline from the fleet
pennyworth-label-state repo pause <repo>
# ... refactor in peace ...
pennyworth-label-state repo resume <repo>
```

The pre-push git hook ([`examples/git-hooks/pre-push`](https://github.com/luminik-io/pennyworth/blob/main/examples/git-hooks/pre-push)) enforces this symmetrically: if you push a branch whose commits reference `Closes #N` and that issue is currently in-flight or has a PR open, the push is refused.

Override per-push: `git push --no-verify`.
Override globally: `LABEL_STATE_SKIP_DEDUP_CHECK=1` in your shell rc.

## API reference

```python
# State transitions
claim_issue(repo, num, *, codename, firing_id) -> bool
release_issue(repo, num, *, codename, firing_id,
              outcome="success", transition_to=None, pr_url=None) -> bool

# Inspection
issue_dedup_check(repo, num) -> dict
find_stale_claims(repo, *, max_age_hours=4) -> list[dict]

# Recovery
force_release_stale_claim(repo, num, *, sweep_id) -> bool

# Operator overrides
is_repo_paused(repo) -> bool
list_paused_repos() -> list[str]
set_repo_paused(repo, paused) -> list[str]

# Constants
LIFECYCLE_LABELS: list[tuple[str, str, str]]
CLAIM_COMMENT_PREFIX: str
RELEASE_COMMENT_PREFIX: str
PAUSED_REPOS_FILE: Path
```

See [agent_runner API reference](/pennyworth/reference/agent-runner/) for the full module surface.
