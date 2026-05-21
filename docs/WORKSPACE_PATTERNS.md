# Workspace Patterns

Alfred can run against one repo, a multi-repo product workspace, or a
specs-led workspace. The important part is to make the shape explicit before
agents start firing.

## Default Layout

By default:

- Alfred source lives wherever you cloned `alfred-os`.
- Runtime state lives in `$ALFRED_HOME`, usually `~/.alfred`.
- Product checkouts live under `$WORKSPACE_ROOT/product/<repo>`.
- `$WORKSPACE_ROOT` defaults to `~/code`.

Recommended first layout:

```text
~/code/
  alfred-os/
  product/
    my-app/
```

Multi-repo layout:

```text
~/code/
  alfred-os/
  product/
    api/
    web/
    mobile/
    specs/
```

With that layout, set `WORKSPACE_ROOT=~/code` in `~/.alfredrc`. Alfred's
repo-operating agents will look for local repos at
`$WORKSPACE_ROOT/product/<repo>`.

## One Repo

Use this when you have one app, one library, or one Mac/iOS app repo:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/my-app \
  --slack-webhook skip
```

This creates labels on `my-org/my-app`, writes `ALFRED_LUCIUS_REPOS=my-app`
and similar variables into `~/.alfredrc`, deploys the starter fleet, and runs
doctor.

## Multi-Repo Product Workspace

Use this when one product spans backend, frontend, mobile, packages, or infra:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

The starter fleet receives the same selected repo list. That is the right
default for a small solo-builder workspace because Drake can plan across all
repos, Lucius can pick labelled implementation work from any selected repo, and
Ra's al Ghul can review PRs across the same surface.

If you need different repo lists per agent, run the interactive wizard or edit
the generated variables in `~/.alfredrc`:

```sh
ALFRED_DRAKE_REPOS=api,web,mobile
ALFRED_LUCIUS_REPOS=api,web
ALFRED_RASALGHUL_REPOS=api,web,mobile
```

Then redeploy:

```sh
bash deploy.sh
bash bin/doctor.sh
```

## Specs-Led Workspace

A specs repo is valuable context, but it is not always a repo you want a
write-capable coding agent to edit.

Recommended shape:

```text
~/code/product/
  api/
  web/
  mobile/
  specs/
```

First setup:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

Then edit `~/.alfred/prompts/drake.md`, `~/.alfred/prompts/lucius.md`, and
`~/.alfred/prompts/rasalghul.md` to explain where specs live and which spec
documents should be treated as source of intent.

Only include the specs repo in `--repos` if you want Alfred to create labels
there and allow repo-operating agents to pick issues or PRs from it.

## Batman For Multi-Repo Planning

Batman is included in Alfred and is the multi-repo coordinator. In the OSS
release it is plan-only:

- scans `BATMAN_SCAN_REPOS`
- looks for `agent:large-feature`
- groups issues with `agent:bundle:<slug>`
- drafts a rollout plan and posts it to Slack/local logs
- stops before automatic cross-repo execution

Enable it when your workspace has cross-repo feature work:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents drake,lucius,rasalghul,agent-cleanup,batman \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

Or enable it after an existing starter setup:

```sh
alfred enable batman
# Add or verify BATMAN_SCAN_REPOS=api,web,mobile in ~/.alfredrc.
bash deploy.sh
bash bin/doctor.sh
```

Private fleets can add site-specific approval and execution layers on top of
this planning primitive. The public Alfred package keeps the execution chain
explicit so operators can decide how much autonomy they want.

## Common Gotchas

- `WORKSPACE_ROOT` is not the repo folder. It is the parent that contains the
  `product/` directory.
- A repo name in `ALFRED_LUCIUS_REPOS=api` means local checkout
  `$WORKSPACE_ROOT/product/api`.
- `--repos` can be bare names (`api,web`) or full slugs (`my-org/api,my-org/web`).
- `GH_ORG` must match the owner of the selected repos.
- Do not point agents at every checkout on your machine. Start with the repos
  you actually want scheduled agents to touch.
