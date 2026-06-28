# How long install takes

Honest read on install duration. The 30-minute fast path assumes a lot of
preconditions are already met. On a fresh machine, the real figure is one to
two hours.

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
`./bin/alfred-init.py --non-interactive --agents all --repos my-org/my-app
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

## If you also want the desktop app

The native client (`client` tier) is a separate, much shorter step on top of
the CLI core. It does not run agents on its own; it drives the same core over
`alfred serve`.

- **Signed install: 2 to 5 minutes.** `brew install --cask alfred-os` (or
  downloading `Alfred.dmg` from the [download page](https://alfred.luminik.io/download/))
  is a normal app install. On first launch with no runtime running, the app
  opens straight into the guided setup wizard, which can start `alfred serve`
  for you, so there is no separate "connect" step to figure out.
- **Build from source: 10 to 20 minutes.** Only needed for client
  development: `cd clients/desktop && npm install && npm run tauri dev` pulls
  the Node toolchain and the Tauri Rust dependencies the first time.

The desktop app needs the CLI core installed and at least one repo configured
to show anything useful, so its timing sits on top of the core figures above,
not instead of them.

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

- [`../INSTALL.md`](../INSTALL.md): the actual install steps.
- [`AI_ASSISTED_INSTALL.md`](AI_ASSISTED_INSTALL.md): copy-paste prompt for
  Claude Code or Codex to do the install for you.
- [`DRY_RUN.md`](DRY_RUN.md): watch a full firing lifecycle with no LLM call
  before turning the fleet on for real.
