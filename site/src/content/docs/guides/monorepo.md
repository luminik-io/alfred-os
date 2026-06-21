---
title: Alfred on a monorepo
description: How Alfred runs against a pnpm, Turborepo, or Cargo workspace, per-package scoping, per-package tests, and worktree cost.
---

Yes, Alfred runs against a monorepo. Most of the runtime is repo-agnostic: the
scheduler fires an agent, the runner picks a labelled issue out of GitHub, a
worktree is created, the engine writes code, a PR is opened. What changes in a
monorepo is the unit of scope inside that worktree, the test command Bane
runs, and the size of the worktree itself. The next sections cover those three
points concretely.

This page mirrors [`docs/MONOREPO.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/MONOREPO.md).

## Three monorepo shapes Alfred has been tried against

### Nx + pnpm workspaces

```text
my-monorepo/
  package.json
  pnpm-workspace.yaml
  nx.json
  apps/
    web/
    admin/
  packages/
    ui/
    sdk/
    utils/
  tools/
  docs/
```

`pnpm-workspace.yaml` declares `apps/*` and `packages/*`. Alfred treats the
whole thing as one GitHub repo. In `~/.alfredrc`:

```sh
ALFRED_LUCIUS_REPOS=my-monorepo
ALFRED_DRAKE_REPOS=my-monorepo
ALFRED_RASALGHUL_REPOS=my-monorepo
```

Or via the installer:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/my-monorepo \
  --slack-webhook skip
```

Drake files one `agent:implement` issue per logical change, not per package. The
issue body names which package the work belongs in (see the worked example
below).

### Turborepo + npm workspaces

```text
my-turbo/
  package.json
  turbo.json
  apps/
    next-site/
    expo-app/
  packages/
    config-eslint/
    config-tsconfig/
    ui/
```

Same Alfred shape: one repo slug, one set of labels, agents scoped by
`agent:implement` issue body. The only real difference from the Nx case is the
test command Bane uses (covered below).

### Cargo workspaces (Rust)

```text
my-rust/
  Cargo.toml
  Cargo.lock
  crates/
    core/
    cli/
    server/
    proto/
  xtask/
  tests/
```

`Cargo.toml` declares `members = ["crates/*", "xtask"]`. Alfred still sees one
repo. The pre-push hook in `~/.alfredrc.d/lucius.yaml` should use crate-aware
commands:

```toml
[pre_push]
my-rust = "cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace"
```

For tighter feedback loops on a touched crate, see "Per-package tests" below.

## Per-package scoping

The unit Lucius claims is always a GitHub issue. The unit Lucius writes
against is whatever paths the issue body names. Drake's job in a monorepo is
to write `agent:implement` issues that name a specific package and the
specific files inside it.

Example issue body Drake should write:

```md
Title: Add `intent` prop to `<Button>` in packages/ui

Repo: my-monorepo
Package: packages/ui
Files in scope:
  - packages/ui/src/Button.tsx
  - packages/ui/src/Button.stories.tsx
  - packages/ui/src/Button.test.tsx

## Goal
Add an `intent` prop to `<Button>` with values `primary | secondary |
danger`. Existing call sites default to `primary` (no behavior change).

## Acceptance criteria
- [ ] `Button` accepts `intent?: "primary" | "secondary" | "danger"` and
      renders the matching token class.
- [ ] Existing snapshot in `packages/ui/src/Button.test.tsx` still passes.
- [ ] One new test case covers `intent="danger"` rendering.
- [ ] No edits outside `packages/ui/`.

## Out of scope
- Adopting the new prop in `apps/web` or `apps/admin`.
- Token additions to `packages/tokens`.
```

The "Out of scope" line is what keeps Lucius from drifting into the rest of
the tree. If Drake writes a vague body, Lucius will spread edits across
packages and Ra's al Ghul will reject the PR.

If you want to enforce this at the prompt level, add to
`~/.alfred/prompts/lucius.md`:

```md
When working in a monorepo, do not edit files outside the package(s) named in
the issue body unless the acceptance criteria explicitly require it. If you
believe a cross-package edit is needed, stop and print
`[BLOCKED] cross-package edit required: <reason>`.
```

## Per-package tests

Pre-push commands are configured per repo in `~/.alfredrc.d/<codename>.yaml`.
For a monorepo, you usually want the pre-push to be the workspace-wide test
command (it's the safest default for an agent that does not know which
package it touched). Bane and Lucius read the same config.

Workspace-wide default in `~/.alfredrc.d/lucius.yaml`:

```toml
[pre_push]
my-monorepo = "pnpm -r lint && pnpm -r typecheck && pnpm -r test"
my-turbo = "pnpm turbo run lint typecheck test"
my-rust = "cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace"
```

If your monorepo is large enough that running the workspace-wide test on
every PR is slow, narrow the pre-push by package. The simplest way is a small
shell script the agent calls instead of a one-liner:

```sh
# ~/.alfred/hooks/lucius-pre-push.sh
#!/usr/bin/env bash
set -euo pipefail
changed=$(git diff --name-only origin/main...HEAD | awk -F/ '{print $1"/"$2}' | sort -u)
echo "Changed packages: $changed"
for pkg in $changed; do
  case "$pkg" in
    packages/*|apps/*) pnpm -F "./$pkg" test ;;
  esac
done
```

Then in the YAML:

```toml
[pre_push]
my-monorepo = "bash ~/.alfred/hooks/lucius-pre-push.sh"
```

For Bane (test-coverage agent), the same pattern applies. Bane writes only
test files, so the pre-push it cares about is the package's test command,
not the whole workspace.

## Worktrees

Every Lucius firing creates a fresh git worktree of the full monorepo, not a
sparse checkout of one package. The directory is
`$ALFRED_HOME/worktrees/eng-lucius-<repo>-<issue>-<ts>/`, branched from a fresh
`origin/main`.

Why the whole repo and not a sparse checkout:

- The engine sometimes needs to grep across packages to understand a type or
  a shared util before editing the package the issue names.
- Some refactors land in one package but require updating a generated file
  somewhere else (a workspace lockfile, a typegen output, an `index.ts`
  re-export at the workspace root).
- Sparse checkouts add a class of "file missing" errors that look like real
  bugs to the engine and waste turns chasing them.

The cost is wall-clock per firing. In our setup, a 4 GB monorepo with around
80k tracked files takes roughly 8 seconds to `git worktree add` on an M2 Mac
with an SSD; a 1 GB repo takes about 2 seconds. We have not benchmarked
beyond 5 GB. If your monorepo is much larger, see the "When NOT to use a
monorepo" section.

Lucius cleans the worktree in `remove_worktree(...)` at the end of every
firing path. `agent-cleanup` sweeps any orphan worktrees older than four
hours nightly, in case a firing crashed before cleanup.

## Code-map awareness

`code-map-refresh` scans every repo named in `ALFRED_CODE_MAP_REPOS` and
writes `$ALFRED_HOME/state/code-map.json`. The map records source files,
public-ish symbols, imports, API calls, server routes, and contract drift.
It is a local planning aid for Drake, Batman, and code review prompts. It is
not a compiler and it does not replace reading the diff.

The current implementation walks the tree; it does not split a monorepo into
per-package sub-maps. That means Drake and code-map-aware review prompts see
one big flat map for the whole monorepo rather than a `packages/ui`-scoped
view.

In practice this is fine for the planner: Drake reads the map plus the
acceptance criteria and writes an issue. It is less useful for a strict
"only look at this package" review prompt. If you want package-scoped
review, scope it in the prompt instead (for example, "Limit your review to
files under `packages/ui/`") and rely on the PR diff to enforce it.

A per-package code map is a reasonable feature request; today it is not
shipped.

## When NOT to use a monorepo with Alfred

Honest tradeoffs, written by someone who has run Alfred against both shapes:

- **Very large monorepos (>5 GB or >200k tracked files).** Worktree creation
  starts to dominate firing wall-clock, and `agent-cleanup` has to delete
  big directories on every sweep. At that scale, splitting hot apps into
  their own repos (and keeping shared packages in one "platform" repo) gives
  faster iteration. We have not measured Alfred against a Google-sized
  monorepo and would not claim it works there.
- **Per-package CI that depends on path filters.** If your CI only runs the
  ui test job when `packages/ui/**` changes, Bane and Lucius will still
  trigger the full workspace pipeline because they push from the workspace
  root. The fix is either to make the pre-push command match the CI's path
  filter, or to accept slower PR feedback.
- **Per-package access control.** Alfred's IAM-per-agent model assumes one
  GitHub repo grants one set of permissions. If you need different agents to
  see different parts of the tree (for example, `lucius` for OSS packages
  and a separate `lucius-internal` for proprietary ones), a monorepo
  flattens that. Two repos with two agent identities is simpler.

If none of those bite, a monorepo is a fine fit. The first solo-builder
workspace we ran Alfred against was a pnpm + Turborepo monorepo, and the
default flow held up.

## See also

- [Workspace patterns](/getting-started/workspace-patterns/): one-repo,
  multi-repo, and specs-led layouts.
- [Specs-driven development](/guides/specs-driven-development/): the kind of
  scoped issue Drake should file for monorepo work.
- [Worked example: Batman across three repos](/guides/multi-repo-worked-example/):
  the cross-repo counterpart to this guide.
