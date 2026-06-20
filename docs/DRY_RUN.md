# Dry-run mode

Dry-run is a diagnostic path. By default it prints a safe simulation for any
configured codename, without touching the scheduler, GitHub, Slack, AWS,
Playwright, an LLM, or a real worktree. Runners that declare native dry-run
support can also execute their lifecycle hooks with outside-world calls
stubbed.

A developer with **nothing configured**, meaning no `gh` auth, no AWS, no Slack,
and no Claude, can run a dry-run and see the sequence Alfred would follow. The
output is a narrated, step-numbered trace, legible enough to record with
asciinema.

## How it differs from doctor mode

`ALFRED_DOCTOR=1` (see [`bin/doctor.sh`](../bin/doctor.sh)) short-circuits a
runner to a **preflight-only** check: it verifies host configuration and exits
before the lifecycle starts.

Dry-run is the opposite: it shows the firing path and, for native dry-run
runners, executes that path with outside calls stubbed. Use doctor mode to
answer "is this host configured correctly?"; use dry-run to answer "what does a
firing do, step by step?".

## Activating it

Two equivalent switches:

- The `ALFRED_DRY_RUN` environment variable, set to any truthy value
  (`1`, `true`, `yes`, `on`):

  ```sh
  ALFRED_DRY_RUN=1 python3 examples/bin/echo_summarise.py
  ```

- The `--dry-run` CLI flag, accepted by the example runners and `bin/lucius.py`:

  ```sh
  python3 examples/bin/echo_summarise.py --dry-run
  ```

A runner that sees `--dry-run` calls `agent_runner.set_dry_run()`, which writes
`ALFRED_DRY_RUN=1` back into the process environment so every downstream code
path, and any subprocess-spawned child, agrees on the mode.

From a fresh checkout (no deploy needed), put `lib/` on `PYTHONPATH`:

```sh
PYTHONPATH=lib python3 examples/bin/echo_summarise.py --dry-run
```

After install, the Alfred CLI resolves any codename without touching the host
scheduler:

```sh
alfred dry-run lucius
alfred dry-run drake
alfred dry-run all
alfred dry-run lucius --native
```

By default, Alfred resolves the label, script, schedule, and role, then prints
the firing boundaries it would cross without invoking GitHub, Slack, AWS,
Playwright, an LLM, or a real worktree. Pass `--native` when you want a runner
that declares native dry-run support to execute under `ALFRED_DRY_RUN=1`.
Codenames that do not yet declare native dry-run support still get the
simulation.

## What is stubbed vs real

Everything inside Alfred runs for real: the lock, preflight narration, event
log, prompt construction, and the runner's own result-branching logic. Calls to
the outside world stay stubbed.

Every side-effecting boundary is stubbed behind a single `is_dry_run()` helper,
checked at exactly these boundaries in [`lib/agent_runner/`](../lib/agent_runner/__init__.py):

| Boundary | Real behaviour | Dry-run behaviour |
|---|---|---|
| `claude_invoke`, `codex_invoke`, `invoke_agent_engine` | Shell out to the `claude` / `codex` CLI | Return a clearly-marked synthetic `ClaudeResult` (`success`, `cost_usd=0.0`, `result_text` labelled `[dry-run] synthetic ...`). No LLM is ever invoked. |
| `SpendState.increment` / `.set` / `.save` | Write the per-day `spend-<date>.json` ledger | Write a separate `spend-dryrun-<date>.json` ledger instead. The real counters are never touched, so a dry-run can't trip a daily cap. |
| `set_global_block` | Write the fleet-wide Claude-provider-limit block | Log the block it would set and return the `until` string; the file is never written, so real scheduled agents are not blocked. |
| `slack_post` | POST to the Slack webhook | Log `[dry-run] ... would post to Slack (severity=...): <message>` to stdout and return `True`. |
| `claim_issue`, `release_issue`, `force_release_stale_claim`, `gh_issue_edit`, `gh_issue_comment`, `gh_pr_comment`, `gh_pr_create`, `ensure_labels` | Run `gh` to mutate GitHub | Log the `gh` call that would run and return success (a fake PR URL for `gh_pr_create`). No `gh` subprocess is spawned. |
| `make_worktree`, `make_worktree_from_branch`, `remove_worktree` | `git worktree add` against the operator's checkout; the runner may `git push` | Create a self-contained throwaway git repo in a temp dir (with an `origin/main` ref and one commit ahead on the firing branch) so a runner that inspects the worktree sees a coherent state. Nothing is fetched from or pushed to a real remote. The temp dir is removed at the end. |

"No config at all" cases the runners stub with clearly-labelled fake data:

- **`pick_issue`**: there is no `gh` auth and no real repo, so each runner
  returns a single synthetic issue (number `0`, title and body prefixed
  `[dry-run]`). The rest of the firing then exercises real code paths against
  the stubbed boundaries above.
- **Missing `GH_ORG`**: `_full_repo` falls back to a `dry-run-org/<repo>`
  placeholder instead of raising, so a missing org can't crash the narrated
  lifecycle.
- **Missing repo env vars** (`ECHO_REPO_SLUG`, `ALFRED_LUCIUS_REPOS`): the
  runners fall back to a `dry-run-repo` / `dry-run-org/dry-run-repo` slug.
- **Preflight gaps**: `preflight()` still runs and reports what is missing. In
  dry-run, the runner narrates the gap and continues. A real firing still exits
  clean on a config gap.

## Example output

```
$ PYTHONPATH=lib python3 examples/bin/echo_summarise.py --dry-run
[dry-run]  1. (start) echo dry-run firing, no LLM, no spend, no gh/slack/git side effects
[ECHO-PREFLIGHT-FAILED] 2 issue(s):
  - env var `ECHO_REPO_SLUG` is unset
  - env var `GH_ORG` is unset
[dry-run]  2. (preflight) preflight reported config gaps, continuing (dry-run)
[dry-run]  3. (pick) would `gh issue list --label agent:summarise`; using a synthetic issue instead
[dry-run]  4. (gh) would claim dry-run-org/dry-run-repo#0 for echo (...): add agent:in-flight, post claim comment
[dry-run]  5. (llm) would invoke claude with prompt of 463 chars, model=(cli-default), max_turns=5
[dry-run]  6. (spend) would increment real ledger (firings_today+=1, turns_today+=3, cost_usd_today+=0.0); dry-run ledger only
[dry-run]  7. (gh) would `gh issue comment #0` on dry-run-org/dry-run-repo: **Echo (auto-summary):** [dry-run] synthetic claude result ...
[dry-run]  8. (gh) would release dry-run-org/dry-run-repo#0 for echo (...): outcome=success, remove agent:in-flight, add agent:done
[dry-run]  9. (spend) would set real ledger (consecutive_failures=0); dry-run ledger only
[dry-run] 10. (spend) would increment real ledger (successes_today+=1); dry-run ledger only
[dry-run] 11. (slack) would post to Slack (severity=info): Echo summarised dry-run-org/dry-run-repo#0: ...
```

The same works for [`examples/bin/hello.py`](../examples/bin/hello.py) (the
minimal agent) and [`bin/lucius.py`](../bin/lucius.py) (the feature-dev agent,
which additionally narrates the worktree and push steps).

## Adding dry-run support to your own runner

The seams above already cover any runner built on `agent_runner`. To make a
runner fully demoable with zero config, do three things in your `bin/*.py`:

1. Accept the flag near the top of the file, before the lifecycle starts:

   ```python
   from agent_runner import is_dry_run, set_dry_run, dry_run_log
   if "--dry-run" in sys.argv:
       set_dry_run(True)
   ```

2. In dry-run, narrate the preflight gap and continue instead of returning:

   ```python
   try:
       preflight(PREFLIGHT)
   except PreflightFailed:
       if is_dry_run():
           dry_run_log("preflight", "preflight reported config gaps, continuing (dry-run)")
       else:
           return 0
   ```

3. In your `pick_*` helper, return a clearly-labelled synthetic work item when
   `is_dry_run()` is true, so the rest of the firing has something to act on.

Use `dry_run_log(step, message)` for any extra narration; `step` is a short
lifecycle tag (`pick`, `git`, `gh`, ...) and the output is auto-numbered.
