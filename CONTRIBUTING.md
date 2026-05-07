# Contributing

Alfred-OS is the framework behind one operator's production fleet. PRs welcome. The maintainer's bar: "does this make alfred-os a better framework for narrow-specialist cron-driven Claude Code agents". Not "does this support every adjacent use case". Changes that add configurability at the cost of more moving parts get declined.

## Proposing a new codename agent

File an issue first. The issue answers:

1. **Role**: one paragraph. What does this agent do that none of the existing ones do? "Like Lucius but for X" is fine if X is genuinely separate.
2. **Schedule**: how often does it fire? Why that cadence and not 2x or 0.5x?
3. **Trigger**: does it scan for something (a label, a file, an open PR) or run unconditionally?
4. **AWS scope**: if it touches AWS, what IAM actions does it need? Spell out the inline policy for its dedicated `<codename>-cron` user.
5. **Failure mode**: what does the agent do if `claude -p` returns `error_max_turns`? `error_rate_limit`? An empty result? Failure handling is half the work.
6. **Spend cap**: proposed `turns_today` ceiling and `consecutive_failures` ceiling.

The maintainer responds "go ahead" or "not now, here's why." Don't write the prompt before the issue is accepted. Refining the prompt is most of the work.

## Changing a prompt

Prompts in `agents/<dept>/prompts/*.md` are the canonical source. The live runtime inlines them via `hermes cron edit`, so editing the file in this repo is half the change. The other half:

```sh
hermes cron edit <cron-id> --prompt "$(cat agents/engineering/prompts/lucius-feature-dev.md)"
```

To test a prompt change before letting it run on a real cron:

1. Pause the cron: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/my.fleet.<agent>.plist`
2. Edit the prompt file.
3. Re-sync the cron via `hermes cron edit`.
4. Fire the agent by hand: `launchctl kickstart -k gui/$(id -u)/my.fleet.<agent>` (after re-loading the plist with `launchctl bootstrap`).
5. Inspect the debug dump in `/tmp/<agent>-debug-<ts>/` (Lucius writes `prompt.txt` and `result.json` for every firing).
6. Resume the cron when satisfied.

Voice rules are locked. No em-dashes. No "leverage", "unlock", "seamless", "transform", "comprehensive", "robust", "streamline". No fabricated numbers; cite the file or codepath the number came from. The existing CLAUDE.md files are the voice reference. Match them.

## Commit messages

Conventional Commits, lowercase after the type:

```
feat(lucius): cap issue body at 8000 chars before invoking claude
fix(huntress): swap stale playwright selector for data-testid
docs(architecture): clarify plan-review gate
chore: bump deploy.sh to honor WORKSPACE_ROOT
```

Body explains why, not what. The diff already shows what.

## Testing changes to the runtime library

`infra/agents/lib/agent_runner.py` is shared by every agent. Test changes by:

1. Editing the lib in this repo.
2. Running `bash infra/agents/deploy.sh` (it's idempotent).
3. Firing the smallest agent that exercises the changed code path. Bat-Signal is good for `slack_post` changes; Bane is good for spend-state changes; Lucius for `make_worktree` changes.

No formal test suite. The runtime is short enough to read end-to-end. Production firings are the integration test.

## OSS-readiness pass

Open thread. State of the cleanup is tracked. If you spot something that needs fixing (a hardcoded host-specific path, a doc that points at a private URL, a stale TODO), open a small PR rather than asking. Drive-by fixes are the easiest contribution to land.

Before opening a release PR, run `bash bin/scrub-check.sh` locally and follow [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md). The scrub check is also wired into CI.
