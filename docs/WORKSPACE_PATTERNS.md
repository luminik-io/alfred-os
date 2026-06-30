# Workspace Patterns

Alfred can run against one repo, a multi-repo product workspace, or a
specs-led workspace. The important part is to make the shape explicit before
agents start firing.

For the full specs workflow, read
[`SPECS_DRIVEN_DEVELOPMENT.md`](SPECS_DRIVEN_DEVELOPMENT.md).

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

With that layout, set `WORKSPACE_ROOT=~/code` in `$ALFRED_HOME/.env`. Alfred's
repo-operating agents will look for local repos at
`$WORKSPACE_ROOT/product/<repo>`.

## If your repos already live somewhere else

Alfred resolves `$WORKSPACE_ROOT/<subdir>/<repo>` where `<subdir>` defaults
to `product`. Override the subdir with `WORKSPACE_SUBDIR` in `$ALFRED_HOME/.env`
when your existing layout doesn't match the default, and skip the symlink
shim entirely:

```sh
# Your repos live at ~/repos/{api,web}/ (drop the product/ segment):
WORKSPACE_ROOT=~/repos
WORKSPACE_SUBDIR=""

# Your repos live at ~/code/src/{api,web}/ (rename the segment):
WORKSPACE_ROOT=~/code
WORKSPACE_SUBDIR=src

# Your repos live at "~/work area/<repo>/" (drop the segment):
WORKSPACE_ROOT="~/work area"
WORKSPACE_SUBDIR=""
```

For per-repo overrides (one repo somewhere unusual while the rest follow
the default), use `GH_REPO_TO_LOCAL` in a fleet overlay; see
`docs/CONNECTORS.md` for the overlay pattern.

## One Repo

Use this when you have one app, one library, or one Mac/iOS app repo:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos my-org/my-app \
  --slack-webhook skip
```

This creates labels on `my-org/my-app`, writes `ALFRED_LUCIUS_REPOS=my-app`
and similar variables into `$ALFRED_HOME/.env`, deploys the full fleet, and runs
doctor.

## Multi-Repo Product Workspace

Use this when one product spans backend, frontend, mobile, packages, or infra:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

The full fleet receives the same selected repo list. That is the right default
for a small solo-builder workspace because Drake and Batman can plan across all
repos, Lucius and Nightwing can pick labelled implementation work from any
selected repo, Bane can run QA, and Ra's al Ghul can review PRs across the same
selected repos.

If you need different repo lists per agent, run the interactive wizard or edit
the generated variables in `$ALFRED_HOME/.env`:

```sh
ALFRED_DRAKE_REPOS=api,web,mobile
ALFRED_LUCIUS_REPOS=api,web
ALFRED_RASALGHUL_REPOS=api,web,mobile
```

Then redeploy:

```sh
bash deploy.sh
./bin/alfred doctor
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
  --agents all \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

Then edit `~/.alfred/prompts/drake.md` to explain where specs live and which
spec documents should be treated as source of intent. Drake should copy the
relevant spec links and acceptance criteria into the GitHub issue body. Lucius
receives spec context through that issue body when it claims the work.

For review agents, keep spec references in the PR description or review prompt
so the reviewer can compare implementation against the intended contract.

Only include the specs repo in `--repos` if you want Alfred to create labels
there and allow repo-operating agents to pick issues or PRs from it.

## Batman For Multi-Repo Planning

Batman is included in the full fleet and acts as the architect agent for cross-repo
work. `BATMAN_PARENT_REPO` parent issues run the plan, approval, child-issue
filing, and report loop.

Batman owns the feature shape above the repo-local work. It plans the rollout
and files scoped child issues when the gate allows it so Lucius and the rest of
the fleet get clear implementation work.

The full-fleet setup configures it from the start:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

For parent-plan execution, set the runner gate explicitly and redeploy:

```sh
alfred enable batman
# Add or verify BATMAN_PARENT_REPO=my-org/specs in $ALFRED_HOME/.env.
# Set BATMAN_AUTO_EXECUTE=approval-gate when you want approved child filing.
alfred labels bootstrap my-org/specs
bash deploy.sh
./bin/alfred doctor
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
  you want scheduled agents to touch.
