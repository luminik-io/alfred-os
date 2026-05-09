# Architecture

This document explains why alfred-os has the shape it has. Read [`README.md`](README.md) for the elevator pitch and [`BOOTSTRAP.md`](BOOTSTRAP.md) for the setup. This is the design rationale.

## Per-firing flow

```mermaid
sequenceDiagram
    participant launchd
    participant runner as bin/&lt;codename&gt;.py
    participant lib as lib/agent_runner.py
    participant claude as claude -p
    participant gh as gh CLI
    participant slack as Slack webhook

    launchd->>runner: fire (every N min)
    runner->>lib: with_lock(AGENT)
    runner->>lib: preflight(spec)
    runner->>lib: SpendState / is_globally_blocked
    runner->>gh: pick_issue(): find oldest agent:implement
    runner->>lib: claim_issue(repo, num, codename, firing_id)
    lib->>gh: add agent:in-flight label
    lib->>gh: post claim comment
    runner->>lib: make_worktree(repo, agent, issue)
    runner->>claude: claude -p '&lt;prompt&gt;' --max-turns N
    claude-->>runner: ClaudeResult (turns, cost, session_id, result_text)
    runner->>gh: gh pr create
    runner->>lib: release_issue(transition_to=agent:pr-open, pr_url=...)
    runner->>slack: slack_post('✅ shipped', severity=info)
    runner->>lib: remove_worktree
```

Every box outside the host is reached by stdlib subprocess + HTTP. No persistent connection. State on disk under `${HERMES_HOME}/state/`.

## Why this shape

alfred-os is built for one operator. One Mac Mini in a closet, one Anthropic Claude Pro / Max subscription, one founder merging the PRs. Every design decision falls out of those three constraints.

- **No GitHub Actions for the agent loop.** Earlier versions ran each agent as a workflow file (`agent-feature.yml`, `agent-tests.yml`, etc.) that called `anthropic-ai/claude-code-action`. That setup needed a paid Anthropic API key, doubled the spend, and made the Mac's existing Pro subscription dead weight. It was retired on 2026-04-24.
- **No cloud queue, no shared service.** The fleet writes to plain JSON files in `~/.hermes/state/`. There is no Redis, no SQS, no Postgres. State that lives outside the operator's filesystem becomes state the operator has to operate.
- **No multi-tenancy.** Hardcoding "your account, your repos, your channel" is fine when the user is the maintainer.

## The codename pattern

Codenames are Batman side-characters: Lucius for feature dev, Bane for tests, Robin for triage, Nightwing for review-fix, Huntress for E2E smoke, Oracle for monitoring, Ra's al Ghul for code review, Planner for issue creation. The naming is deliberate: codenames appear in PR titles, Slack messages, and commit authorship, so they must be memorable and internally consistent.

Same codename across repos means "same role applied to that repo's code," not "one agent spans repos." Lucius in `<your-backend-repo>` and Lucius in `<your-frontend-repo>` are two separate processes running the same prompt against different codebases. They never share state.

This is the opposite of the CrewAI / AutoGen design, where a generalist agent decomposes tasks across roles at runtime. alfred-os wires the roles at deploy time. The decomposition is the cron schedule. The negotiation channel is the consumer's Slack channel.

Why narrow specialists rather than one general agent: each role gets a different turn budget, a different IAM scope, a different tool list, a different escalation rule, a different failure-mode taxonomy. Lucius is allowed `Read,Edit,Write,Bash,Grep` and 80 turns; Robin gets `Read,Bash` and 30 turns. Generalist prompts that try to cover every case end up with the worst spend profile of all the cases combined.

## Cron-driven, not chat-driven

What this gets you:

- **24/7 unattended operation.** Lucius fires every 20 minutes whether or not the operator is at the keyboard. Bane fires nightly. Oracle posts a morning brief at 06:00.
- **Idempotent firings.** Every firing reads its inputs from scratch (open issues, PR list, file system). If a firing crashes, the next one starts clean. There is no resume protocol to debug.
- **No babysitter process.** No long-lived agent that has to survive a Mac sleep cycle. `launchd` re-fires whenever the schedule says so.
- **Spend predictability.** Each firing has a hard turn cap and a hard timeout. The fleet's worst-case daily cost is bounded by the schedule, not by what the operator is asking for.

What it costs you:

- **No real-time interactivity.** The operator does not chat with Lucius. The operator labels a GitHub issue `agent:implement` and waits up to 20 minutes for Lucius to pick it up. The Slack `#your-fleet-channel` channel is one-way: the agents post, the operator reads.
- **No exploratory work.** The agents do not investigate vague hunches. They do work that fits in a `claude -p --max-turns N` budget against a structured input (an issue, a PR, a file diff).

## Plan-review gate

The single biggest quality lever in the system. The full canonical description lives in `~/.hermes/overnight/manifest.md`; the short version:

> Quality ceiling is set by the plan, not the executor. Therefore for every coding task: draft a short plan (problem statement, chosen approach, key interfaces, risks, test strategy), save it under `~/.hermes/overnight/plans/T#N-plan.md`, dispatch that plan to a separate Claude Code session running in **review-only mode**, apply the feedback, then execute.

The review session runs against a stricter critique prompt: "Critique this plan. Identify missing cases, type issues, architectural smells, simpler alternatives. Pay close attention to data types, uniqueness constraints, and error handling." The reviewer never touches code. The executor never sees the original draft, only the post-review version.

The same shape applies to a quality-review gate after implementation: dispatch the implemented files to a review-only Claude Code session, apply feedback, fix issues. Only after both gates pass does the work land as a commit.

This is alfred-os's main answer to the question "what stops a single autonomous agent from confidently shipping bad code." The reviewer is a separate session with no investment in the original plan, run on the same model. The cost is one extra `claude -p` call per task. The catch is that you have to believe the reviewer is uncorrelated with the executor - same model, same prompt template would defeat the gate. Different mode (read-only, critique-focused) is enough in practice.

## Worktree isolation per firing

Concurrent firings must never clobber each other or the operator's main checkout. The runtime puts every firing in its own throwaway git worktree:

```py
# infra/agents/lib/agent_runner.py
WORKTREE_ROOT = HERMES / "worktrees"
# wt = ~/.hermes/worktrees/<agent>-<repo>-<issue>-<ts>/
```

In `infra/agents/bin/lucius.py`:

```py
wt, branch = make_worktree(local, AGENT, str(issue_num))
# ...
result = claude_invoke(prompt, workdir=wt, ...)
# ...
remove_worktree(local, wt)
```

Three Lucius firings against three different issues create three worktrees and three branches. Each firing's `claude -p` runs with its `cwd` pinned to the worktree. None can accidentally `git push` to another firing's branch, none can edit a file the operator is actively editing in the canonical checkout.

The worktree is removed at the end of the firing, success or failure. If a firing crashes mid-run, the next one's `make_worktree` call also runs `git worktree prune` first to clean up orphans.

## Spend tracking and the global block

Every agent maintains a per-day spend file:

```
~/.hermes/state/<agent>/spend-YYYY-MM-DD.json
```

Tracked fields, per `agent_runner.py`:

```py
firings_today, turns_today, cost_usd_today,
successes_today, failures_today, consecutive_failures,
blocked_until, last_session_id_per_target
```

Each agent has its own caps. From `infra/agents/bin/lucius.py`:

```py
if spend.state["turns_today"] >= 5000:
    msg = f"[LUCIUS-DAILY-CAP] turns_today={...} >= 5000."
    slack_post(msg + " Auto-pausing lucius.")
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
    return 0
if spend.state["consecutive_failures"] >= 8:
    msg = f"[LUCIUS-FAIL-STREAK] {...} consecutive failures."
    slack_post(msg)
    return 0
```

Beyond per-agent caps, the fleet shares a global block. When any agent's `claude -p` returns `error_rate_limit` or `error_budget` (the Anthropic subscription hit its weekly cap), it writes:

```py
# infra/agents/lib/agent_runner.py
GLOBAL_BLOCKED_FILE = STATE_ROOT / "global-blocked-until.json"

def set_global_block(hours: int, reason: str) -> str:
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)) ...
    GLOBAL_BLOCKED_FILE.write_text(json.dumps({"until": until, "reason": reason}))
```

Every other agent's first action in `main()` is:

```py
blocked = is_globally_blocked()
if blocked:
    print(f'[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.')
    return 0
```

Without this, the entire fleet would spend the next hour firing into the rate-limit wall and burning more turns just to learn the wall is still there. With it, the wall hit by Lucius at 22:46 silences Bane's nightly run, Oracle's 06:00 brief, and Huntress's next smoke until the block expires.

The pre-flight `claude -p "reply OK"` canary that earlier versions used was removed: it was tripping false-positives on its 60-second timeout and killing real firings. The real `claude -p` call below it already surfaces `error_rate_limit` cleanly.

## Slack `#your-fleet-channel` as the human surface

Every meaningful event posts to `#your-fleet-channel`. Successes, failures, rate limits, salvaged WIP PRs, "no work to do" silences. The Slack channel doubles as the human surface and the audit log: one place to scroll back through and see what the fleet did overnight.

The webhook is fetched from AWS Secrets Manager and cached at `~/.hermes/state/slack-webhook.cache` with a 7-day TTL. Cache lives outside `/tmp` so it survives reboots, and the long TTL avoids depending on a healthy AWS SSO session for routine Slack posts. From `agent_runner.py`:

```py
SLACK_WEBHOOK_CACHE = STATE_ROOT / "slack-webhook.cache"
SLACK_WEBHOOK_CACHE_TTL = 7 * 24 * 3600
```

`slack_post()` returns a boolean: `True` on confirmed POST, `False` on any failure. Most callers fire-and-forget. A few (the brand-mention scanner) only mark a record as "seen" after a confirmed post.

## AWS IAM-per-agent

Every AWS-touching cron has its own IAM user with a least-privilege inline policy. Huntress reads two specific Secrets Manager entries (E2E test creds + the Slack webhook). Oracle has read-only ECS / ALB / CloudWatch and explicitly no `secretsmanager:*`.

The operator's SSO chain is never used by cron. Two reasons:

1. **SSO sessions expire.** A 12-hour SSO token elapsing at 22:00 takes down everything that depends on it. Scoped IAM access keys do not expire.
2. **Blast radius.** If a Lucius prompt-injection were to coax an `aws s3 rm --recursive` out of `claude -p`, the resulting access scope is whatever was authenticated. The operator's SSO chain has full admin. A scoped `<your-codename>-cron` IAM user has read on a handful of secrets.

Each agent's prompt invokes `aws` with explicit env-stripping:

```sh
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_SECURITY_TOKEN AWS_PROFILE=<your-codename>-cron aws ...
```

Env vars beat profiles in the AWS credential chain, so any leaked `AWS_*` from the operator's shell would override the profile silently. The `env -u` strips them.

## Claude account fallback

When the primary Claude subscription hits its weekly cap, the cron can fall back to a second Anthropic account by pointing `CLAUDE_CONFIG_DIR` at a separate config dir (e.g. `~/.claude-secondary/`). The launchd plists honor `EnvironmentVariables`, so flipping the routing for cron-spawned agents is a matter of `launchctl setenv CLAUDE_CONFIG_DIR ~/.claude-secondary` and re-loading. The operator's interactive Claude Code sessions keep using the primary config because they don't read that env var.

[NEEDS-OPERATOR-INPUT] The fallback config dir is referenced in the live setup but isn't wired into the `infra/agents/` plists in this repo. Document the actual switching mechanism (env var, plist patch, hermes flag?) once decided.

## Failure modes and recovery

`infra/agents/bin/lucius.py` is the canonical example. The exit codes are not real exit codes - they are sentinel strings printed to stdout for the launchd log and `#your-fleet-channel`.

| Sentinel | When | What the system does |
|---|---|---|
| `[OK] commit <sha> | files=N | <summary>` | `claude -p` returned success and committed. | Push, open PR, label `agent:authored`, post success to Slack. |
| `[ALREADY-IMPLEMENTED] file:line` | The work is already in the codebase. | Comment on the issue, label `done-already`, close the issue. No PR. |
| `[PARTIAL] <progress>` | Hit `error_max_turns`. | Comment on the issue, leave the worktree, retry next firing. Not counted as a failure (resume is the plan). |
| `[BLOCKED] <reason>` | Claude could not resolve an error. | Slack-post with the reason. Counted as a failure. |
| `[SILENT]` | No work matched the agent's filter (e.g. no `agent:implement` issues). | Exit 0, no Slack post. The non-event is the signal. |
| `[LUCIUS-NO-COMMIT]` | `claude -p` returned success but no commit landed. | Look for unstaged changes; if any, salvage as a `do-not-review` draft PR. Otherwise count as failure. |
| `[LUCIUS-DAILY-CAP]` | Per-agent turn cap exceeded. | Auto-pause the launchd job via `launchctl bootout`. |
| `[LUCIUS-FAIL-STREAK]` | 8 consecutive failures with 0 successes. | Slack-post; agent stays on schedule, but the streak is now visible for the operator to investigate. |
| `[<AGENT>-GLOBAL-BLOCKED]` | Another agent already tripped the global block. | Exit silently. |

The salvage path for `[LUCIUS-NO-COMMIT]` is interesting: when `claude -p` returns success but `git rev-list origin/main..HEAD` shows zero commits, the runner inspects `git status --porcelain`. If there are unstaged changes, it auto-commits them with a `WIP:` prefix, pushes, opens a draft PR with the `do-not-review` label, and marks the firing as a failure for spend purposes. The operator gets a draft to inspect rather than losing the work to the worktree-cleanup step. From `lucius.py`:

```py
if commit_count == 0:
    status = run(["git", "status", "--porcelain"], cwd=str(wt)).stdout.strip()
    if status:
        run(["git", "add", "-A"], cwd=str(wt))
        run(["git", "-c", "user.email=<codename>@example.com", "-c", "user.name=<Codename>",
             "commit", "-m", f"WIP: partial implementation of #{issue_num}\n\n..."])
        run(["git", "push", "-u", "origin", branch])
        pr_url = gh_pr_create(repo, title=f"DRAFT: WIP partial implementation of #{issue_num}",
                              body_file=body_file, head=branch,
                              labels=["agent:authored", "do-not-review"])
```

The pattern across agents: never silently lose work, always surface a signal in `#your-fleet-channel`, count failures conservatively, recover on the next firing.
