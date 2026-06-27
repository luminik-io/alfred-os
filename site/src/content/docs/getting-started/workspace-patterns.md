---
title: Workspace patterns
description: Choose one repo, multi-repo, or specs-led setup before agents start firing.
---

Alfred can run against one repo, a multi-repo product workspace, or a
specs-led workspace. The important part is making the repo scope explicit.

This page mirrors the full GitHub guide:
[`docs/WORKSPACE_PATTERNS.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/WORKSPACE_PATTERNS.md).

For a deeper spec workflow, read [Specs-driven development](/guides/specs-driven-development/).

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
  --agents all \
  --repos my-org/my-app \
  --slack-webhook skip
```

Use this for one app, one library, or one Mac/iOS app repo.

## Multi-Repo

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

The full fleet receives the selected repo list. Drake and Batman can plan
across the selected repos, Lucius and Nightwing can pick labelled issues in any
selected repo, Bane can run QA, and Ra's al Ghul can review PRs across the same
repo set.

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
`~/.alfred/prompts/drake.md` to reference the specs checkout. Drake should copy
the relevant spec links and acceptance criteria into the GitHub issue body.
Lucius receives spec context through that issue body when it claims the work.

Specs-driven means agents read the specs repo as planning context while
code-writing agents stay scoped to code repos. Include `specs` in `--repos`
only when you want Alfred to create labels there or pick issues and PRs from it.

## Batman

Batman is included in the full fleet as the architect agent for cross-repo work. It has two
public paths:

- `BATMAN_PARENT_REPO` parent issues can go through plan, approval, child-issue
  filing, and status reporting.
- `BATMAN_SCAN_REPOS` legacy scans pick open `agent:large-feature` issues,
  group siblings with `agent:bundle:<slug>`, post a rollout plan, and stop
  before child issue filing.

Batman owns the feature shape above the repo-local work. It plans the rollout
and files scoped child issues for the normal fleet queue when the gate allows
it.

Configure it with the rest of the fleet:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

Then arm the runner gate with `alfred enable batman` when parent-plan work is
ready.
