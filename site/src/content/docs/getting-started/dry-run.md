---
title: Dry-run mode
description: Watch a full agent firing lifecycle with no LLM call, no spend, and no side effects.
---

Dry-run is a low-commitment "watch it work" path. It runs the **whole** agent firing lifecycle — preflight, lock, pick, claim, worktree, prompt build, engine invoke, result branching, PR-create / release, Slack report — but stubs every side-effecting boundary so the run costs nothing.

A developer with **nothing configured** — no `gh` auth, no AWS, no Slack, no Claude — can run a dry-run firing and watch the sequence end to end, exiting 0. The output is a narrated, step-numbered trace.

Condensed companion to [`docs/DRY_RUN.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/DRY_RUN.md).

## How it differs from doctor mode

`ALFRED_DOCTOR=1` short-circuits a runner to a **preflight-only** check: it verifies host configuration and exits before the lifecycle starts.

Dry-run is the opposite: it runs the **entire** lifecycle and instead stubs the calls that would touch the outside world. Use doctor mode to answer "is this host configured correctly?"; use dry-run to answer "what does a firing actually do, step by step?".

## Try it in 2 minutes

From a fresh checkout — no [install](/getting-started/install/) needed — put `lib/` on `PYTHONPATH`:

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
PYTHONPATH=lib python3 examples/bin/echo_summarise.py --dry-run
```

You get a step-numbered trace of the full lifecycle and an exit code of 0. The same works for `examples/bin/hello.py` (the minimal agent) and `bin/lucius.py` (the feature-dev agent).

## Activating it

Two equivalent switches:

- The `ALFRED_DRY_RUN` environment variable, set to any truthy value (`1`, `true`, `yes`, `on`).
- The `--dry-run` CLI flag, accepted by the example runners and `bin/lucius.py`.

A runner that sees `--dry-run` calls `agent_runner.set_dry_run()`, which writes `ALFRED_DRY_RUN=1` back into the process environment so every downstream seam — and any subprocess-spawned child — agrees on the mode.

## What is stubbed vs real

Everything that does **not** touch the outside world runs for real: the lock, preflight (its result is narrated but a config gap no longer aborts the firing), the event log, prompt construction, and the runner's own result-branching logic.

Every side-effecting boundary is stubbed behind a single `is_dry_run()` helper in `lib/agent_runner.py`:

| Boundary | Dry-run behaviour |
|---|---|
| `claude_invoke`, `codex_invoke`, `invoke_agent_engine` | Return a clearly-marked synthetic result (`cost_usd=0.0`, `result_text` labelled `[dry-run] synthetic ...`). No LLM is ever invoked. |
| `SpendState` | Write a separate `spend-dryrun-<date>.json` ledger. The real per-day counters are never touched, so a dry-run can't trip a daily cap. |
| `set_global_block` | Log the block it would set; the fleet-wide poison pill file is never written. |
| `slack_post` | Log the line it would post (severity included) and return success. The webhook is never hit. |
| `claim_issue`, `release_issue`, `gh_pr_create`, `gh_issue_edit`, and the other `gh` helpers | Log the `gh` call that would run and return success. No `gh` subprocess is spawned. |
| `make_worktree` / `remove_worktree` | Create a self-contained throwaway git repo in a temp dir — coherent enough for a runner to inspect — then remove it. Nothing is fetched from or pushed to a real remote. |

With nothing configured, the runners also substitute clearly-labelled fake data: a synthetic issue from `pick_issue`, a `dry-run-org/<repo>` placeholder when `GH_ORG` is unset, and a `dry-run-repo` slug when the repo env vars are missing. Preflight still runs and still reports what is missing — in dry-run the runner narrates the gap and continues instead of exiting.

See [`docs/DRY_RUN.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/DRY_RUN.md) for the full seam table and for how to add dry-run support to your own runner.
