# Operating the fleet

Week one is install and verify. Week two is operating the thing. This is what week two looks like.

By the time you reach this page you have a deployed Alfred, an `alfred status` that comes back green, and at least one codename that has shipped a PR. The questions that remain are operational: how often should I look at Slack, which CLI command tells me what, what do the sentinels mean, and what do I do when the fleet goes quiet?

This page is the runbook.

## Daily Slack rhythm

If you wired up the Slack webhook, three posts on the configured channel are your operating loop.

| Post | When | What it carries |
|---|---|---|
| `fleet-recap (morning)` | 07:30 daily | Yesterday's PRs shipped, in-flight work, doctor status, anything red. |
| `shipped-summary-daily` | end of day | Today's merged PRs, issues closed, LOC, model and config changes. |
| `fleet-recap (evening)` | 22:00 daily | Per-agent spend, firing count, success rate, anything that paused itself. |

Plus the per-firing posts: shipped, blocked, partial. Quiet days are the goal. A morning post that says "yesterday: 6 PRs shipped, 0 blockers" is the steady state.

When you see a `[BLOCKED]` or a `pause` event in Slack, that is the only signal that needs same-day attention. Everything else can wait until you have time.

## CLI recipes

The `alfred` CLI is your day-to-day verb set. Pure stdlib, no daemon. Common recipes:

```sh
alfred status               # local fleet health, locks, pauses, approval waits
alfred agents               # configured agents, schedule, enable state, host-unit status
alfred shipped              # merged PRs, issues, LOC, and config changes
alfred shipped --period weekly
alfred engine status        # one line per codename, resolved Claude/Codex mode
alfred auth status          # Claude + Codex auth surface check
alfred codex probe          # one tiny Codex request end-to-end
alfred claude status        # show which Claude account scheduled firings use
alfred enabled-agents       # print the runner-gate list
alfred pause <codename>     # stop scheduled firings for one agent
alfred pause all            # stop the entire fleet
alfred resume <codename>    # reverse a pause
alfred run <codename>       # kick one firing now (one-shot)
alfred run <codename> --force  # ignore the pause marker for this kick
```

The two recipes you will use most:

- **"Did anything ship today?"** `alfred shipped` and (if you want more detail) `alfred shipped --period weekly`.
- **"Why is Lucius quiet?"** `alfred status` first. If it shows Lucius as paused, `alfred resume lucius`. If it shows a lock, `alfred agents` tells you whether the unit is loaded. If both look fine, the agent had no `agent:implement` issue to claim and exited silently.

## Logs

Three places to look.

**Per-agent host-scheduler logs.** Every scheduled firing's stdout and stderr land at `/tmp/my.fleet.<codename>.stdout` and `/tmp/my.fleet.<codename>.stderr`. macOS clears `/tmp` on reboot; copy anything you need to keep. To watch a firing live:

```sh
tail -f /tmp/my.fleet.lucius.stdout /tmp/my.fleet.lucius.stderr
```

**Per-firing transcripts.** Under `$ALFRED_HOME/state/transcripts/<codename>/<YYYY-MM>/<firing-id>.jsonl` when transcripts are enabled, and `$ALFRED_HOME/state/codex/<codename>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}` for Codex firings. The Codex artifacts are the most useful when a `[BLOCKED]` post does not explain itself; `last.md` is the engine's own final message.

**Per-firing event log.** `$ALFRED_HOME/state/<codename>/events/` carries a structured trail of what each firing tried. `alfred status` summarizes this; for raw inspection, `cat` the JSONL.

See [State and memory](./STATE_AND_MEMORY.md) for the full directory tree.

## Reading the sentinels

Every firing prints exactly one sentinel string on its way out. The scheduler logs them; Slack posts the user-facing ones. Knowing them by sight is how you read the fleet at a glance.

| Sentinel | Meaning | What to do |
|---|---|---|
| `[OK] commit <sha>` | The firing committed and opened a PR. | Nothing. The PR is in the queue. |
| `[ALREADY-IMPLEMENTED]` | Work was already in the codebase. Issue closed. | Nothing. Often a sign Drake filed something Lucius had already solved. |
| `[PARTIAL]` | Hit `error_max_turns` mid-work. Worktree left for the next firing. | Nothing. The next firing retries. If you see two in a row on the same issue, the issue may be too large; consider splitting it. |
| `[BLOCKED]` | Engine could not resolve an error. Slack posts the reason at `warn`. | Read the Slack post. Common causes: missing dep, failing test the agent could not fix, repo convention the agent does not know. |
| `[<AGENT>-LOCKED]` | A previous firing of this codename is still running. | Nothing, unless you see it for hours; then run `alfred clear-lock <codename> --check`. Cleanup preserves dirty or ahead worktrees and creates local `recovery/*` refs when commits need a handle. |
| `[<AGENT>-PREFLIGHT-FAILED]` | A required CLI is missing, `gh` auth expired, or a watched repo is gone. | Run `alfred doctor`. It names the missing piece. |
| `[<AGENT>-DOCTOR-OK]` | `ALFRED_DOCTOR=1` was set; the firing verified preflight and exited. | Nothing. This is how `alfred doctor` validates each agent. |
| `[<AGENT>-GLOBAL-BLOCKED]` | A Claude provider limit is in effect; this firing exited silently. | Nothing for ~1 hour; the block clears automatically. Hybrid agents are unaffected and keep going via Codex. |
| `[<AGENT>-DEDUP-SKIP]` | Another firing already claimed this issue, or the repo is paused. | Nothing. Cooperative behavior. |
| `[<AGENT>-NO-COMMIT]` | The engine reported success but no commit landed. | Inspect the salvage draft PR (if one was opened) or check the firing log to learn why. |
| `[SILENT]` | No matching issue. | Nothing. The non-event is the signal. |

Anything ending in `-BLOCKED`, `-FAILED`, or `-NO-COMMIT` needs your attention. Everything else is the fleet doing its job.

## When to run `alfred run <codename> --force`

`alfred run <codename>` kicks a one-shot firing now. Use it when:

- You filed an `agent:implement` issue and want to see it picked up before the next 20-minute cycle.
- You changed a prompt template and want to verify the change end-to-end.
- You resumed an agent after a pause and want to confirm it is alive.

`--force` overrides the pause marker. Use it when:

- You deliberately want one firing despite the pause (debugging, validating a fix).
- The pause marker was set automatically by a self-pause event and you want to retry once before resuming the schedule.

Do **not** use `--force`:

- During a global rate-limit block. The block is the fleet's way of waiting out a wall. Forcing more firings burns turns to re-confirm the wall.
- When `alfred status` shows the agent is already locked. Two firings of the same codename at once is exactly what `with_lock` exists to prevent.
- When the underlying issue is "the agent ran out of spend for the day". Wait for midnight, or raise the cap deliberately.

## "Fleet went quiet" troubleshooting

If you expected activity and Slack is silent, walk this list in order. Each step is cheap and most failures are caught in the first two.

### 1. Claude auth expired

```sh
alfred auth status
```

If Claude reports unauthenticated, run `claude` interactively once. The auth blob at `~/.claude/` expires rarely but does expire. The hybrid agents will have been failing over to Codex; pure-Claude agents will have been silent. See [Engine routing](./ENGINE_ROUTING.md).

### 2. Global block engaged

```sh
cat $ALFRED_HOME/state/global-blocked-until.json
```

If present and the `until` is in the future, a Claude-backed firing tripped a provider limit. Pure-Claude agents are silent until expiry. There is nothing to do except wait, or set `hybrid` on the codenames you want to keep working.

### 3. All repos paused

```sh
cat $ALFRED_HOME/state/paused-repos.json
alfred status
```

If every repo is paused, every consumer's `pick_*` helper returns nothing and every firing exits as `[SILENT]`. You may have run `label-state repo pause <repo>` and forgotten to resume.

### 4. Agent self-paused

```sh
ls $ALFRED_HOME/state/_paused/
```

A self-pause writes a marker file here. Common causes: spend cap exceeded, eight consecutive failures, an explicit Slack-posted reason. `alfred status` summarizes this. `alfred resume <codename>` reverses it.

### 5. Schedule conflict or wrong unit

```sh
alfred agents
launchctl list | grep my.fleet      # macOS
systemctl --user list-timers        # Linux
```

If the unit is not loaded, the host scheduler will never fire it. Common after a `deploy.sh` that did not complete cleanly. Re-run `bash deploy.sh && alfred doctor`.

### 6. launchd plist load failure (macOS)

```sh
tail -n 200 /tmp/my.fleet.<codename>.stderr
```

A plist that fails to load on `launchctl bootstrap` writes the reason to the unit's stderr. Most common cause: a typo in `agents.conf` after a manual edit, or a `PATH` entry that no longer exists. Re-run `bash launchd/render.sh && bash deploy.sh`.

### 7. Desktop notifications from headless firings

There are two distinct notification sources, and only one is code-controllable:

- **Claude Code notifications** (the assistant pinging you when a session needs attention or finishes). The fleet suppresses these by default: every headless `claude -p` invocation is launched with `--settings '{"agentPushNotifEnabled":false,"preferredNotifChannel":"none"}'`. That flag *adds* a settings source on top of the config-dir settings: it does not touch auth (credentials come from the config dir, not `settings.json`), so suppression never logs an agent out. To re-enable notifications (e.g. while debugging a single firing interactively), set `ALFRED_AGENT_NOTIFICATIONS=1` in the agent's environment.
- **macOS "Background Items Added" / login-item banners** for the launchd jobs themselves. These come from macOS, not from Alfred, and there is no code path that can silence them. Turn them off in **System Settings → Notifications → Background Items Added** (and review **General → Login Items & Extensions**). This is a per-host System Settings toggle, not something `deploy.sh` or any agent can change.

## Weekly hygiene

Block thirty minutes once a week. The fleet does most of its own housekeeping via `agent-cleanup` (daily 03:00), but a human pass catches drift the daily cleanup will not.

- `alfred shipped --period weekly`: read the digest. Anything missing from what you expected? Anything that landed and you did not notice?
- `alfred doctor`: confirm preflight still passes for every configured agent.
- `bash bin/scrub-check.sh`: if you contribute to Alfred itself, run this before pushing.
- `ls $ALFRED_HOME/state/claims/` if it exists: stale claims should be empty after the daily sweep, but inspect anything older than 24h.
- Rotate `/tmp/my.fleet.*` logs if `/tmp` is filling up. macOS clears them on reboot, but a host that stays up for weeks accumulates a lot.
- Prune `$ALFRED_HOME/state/transcripts/` and `$ALFRED_HOME/state/codex/` older than a month if you do not need them for forensics. `agent-cleanup` already handles spend files and worktrees.

## See also

- [Architecture](./../ARCHITECTURE.md): the design that makes this runbook short.
- [Agent lifecycle](./ARCHITECTURE.md#agent-lifecycle): one firing traced end to end.
- [State and memory](./STATE_AND_MEMORY.md): every state file this page reads or writes.
- [Engine routing](./ENGINE_ROUTING.md): per-codename Claude / Codex / hybrid.
- [Issue claim state machine](./STATE_MACHINE.md): cooperative coordination via GitHub labels and comments.
- [Alfred CLI](../bin/alfred): every `alfred` subcommand with one-line help.
