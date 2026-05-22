---
title: Workspace patterns
description: Choose one repo, multi-repo, or specs-led setup before agents start firing.
---

Alfred can run against one repo, a multi-repo product workspace, or a
specs-led workspace. The important part is making the repo scope explicit.

This page mirrors the full GitHub guide:
[`docs/WORKSPACE_PATTERNS.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/WORKSPACE_PATTERNS.md).

## Default Layout

```text
~/code/
  alfred-os/
  product/
    api/
    web/
    mobile/
    specs/
```

Set `WORKSPACE_ROOT=~/code`. Repo-operating agents look under
`$WORKSPACE_ROOT/product/<repo>`.

## One Repo

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/my-app \
  --slack-webhook skip
```

Use this for one app, one library, or one Mac/iOS app repo.

## Multi-Repo

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

The starter fleet receives the selected repo list. Drake can plan across the
selected repos, Lucius can pick labelled issues in any selected repo, and Ra's al Ghul
can review PRs across the same repo set.

## Specs-Led

Clone the specs repo for context, but keep the first write loop focused on code
repos:

```text
~/code/product/
  api/
  web/
  mobile/
  specs/
```

Use `--repos my-org/api,my-org/web,my-org/mobile`, then edit
`~/.alfred/prompts/drake.md`, `~/.alfred/prompts/lucius.md`, and
`~/.alfred/prompts/rasalghul.md` to reference the specs checkout.

Specs-driven means agents read the specs repo as planning context while
code-writing agents stay scoped to code repos. Include `specs` in `--repos`
only when you want Alfred to create labels there or pick issues and PRs from it.

## Batman

Batman is included and supports multi-repo planning. The public version is
plan-only:

- scans `BATMAN_SCAN_REPOS`
- picks open `agent:large-feature` issues
- groups siblings with `agent:bundle:<slug>`
- posts a rollout plan
- stops before automatic cross-repo PR execution

Enable it with:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents drake,lucius,rasalghul,agent-cleanup,batman \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```
