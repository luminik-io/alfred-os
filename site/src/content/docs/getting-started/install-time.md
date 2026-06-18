---
title: How long install takes
description: Honest read on Alfred install duration for existing dev setups and fresh machines, plus the one-time gates to check before starting.
---

Honest read on install duration. The "30 minutes" number on the README assumes
a lot of prerequisites are already met. On a fresh machine, the real figure is
one to two hours.

This page mirrors [`docs/INSTALL_TIME.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/INSTALL_TIME.md).

## If you already have the prerequisites

If all of the following are already true on the machine you are installing on,
expect 30 minutes from `git clone` to a green `doctor.sh`:

- macOS with Homebrew installed and on PATH.
- `gh auth login` already done; `gh auth status` reports a token with `repo`
  scope.
- Claude Code first-run completed: `claude` runs, you have signed in, and
  your subscription is active.
- A GitHub org or personal account you can create issues, labels, and PRs in.
- One repo cloned locally under your intended `WORKSPACE_ROOT/product/`.
- Python 3.11+ available (the runtime uses `tomllib`).

In that case the actual work is small: clone the repo, run
`./bin/alfred-init.py --non-interactive --agents starter --repos my-org/my-app
--slack-webhook skip`, run `bash deploy.sh`, run `bash bin/doctor.sh`.

## If you are setting up a fresh machine

Add 30 to 90 minutes if any of these are not done:

- **Paid Claude Code subscription not set up.** Going through the subscription
  flow plus the first `claude` sign-in is 15 to 30 minutes on a good day,
  longer if billing is being added to an org account.
- **GitHub org permissions not in place.** Creating an org or getting added
  to an existing one with `repo` scope (and verifying you can create issues,
  labels, and PRs there) typically runs 10 to 20 minutes.
- **Slack workspace admin not handled.** If you want Slack reports, either an
  incoming webhook URL or a bot token must be created. Webhook is 5 minutes;
  bot token (with chat:write and the channel invite) is 15 to 30 minutes if
  you need admin approval.
- **Codex (optional engine).** If you want the hybrid engine fallback,
  installing and signing into Codex adds 10 to 15 minutes.

A realistic fresh-machine total is 60 to 120 minutes.

## One-time gates before you start

These are the things you cannot do from inside the install script, so check
them first:

- [ ] You have a paid Claude Code subscription (or a Bedrock setup) and
      `claude` runs locally.
- [ ] You have a GitHub account or org where you can create issues, labels,
      and PRs in the repos you intend to operate against.
- [ ] You can run `gh auth login` and end up with a token that has `repo`
      scope.
- [ ] You have a Slack workspace where you can either add an incoming webhook
      or create a bot user. Skip this and pass `--slack-webhook skip` to the
      installer; Slack can be added later.
- [ ] You have one or more repos cloned locally that you want Alfred to
      operate against. Alfred does not clone them for you.

If any of those is "no", expect to spend more time on that than on Alfred
itself.

## What happens after install

Once `doctor.sh` is green, the host scheduler starts the firing cadence
immediately. The first Drake firing will happen within the configured
interval (default every 2 hours). If you want a faster first signal, file one
`agent:implement` issue manually and wait 20 minutes for Lucius's next firing,
or run `alfred run lucius --force` to trigger a firing on demand.

## See also

- [Install](/getting-started/install/): the actual install steps.
- [AI-assisted install](/getting-started/ai-assisted-install/): copy-paste
  prompt for Claude Code or Codex to do the install for you.
- [Dry-run mode](/getting-started/dry-run/): watch a full firing lifecycle
  with no LLM call before turning the fleet on for real.
