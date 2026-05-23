---
title: "Worked example: Batman across three repos"
description: One feature shipped across backend, frontend, and mobile using the default Alfred fleet plus Batman, from operator issue to bundle rollup.
---

This walkthrough shows one feature shipped across three repos using the
default Alfred fleet plus Batman. The example is "add an organisation slug
to every account-scoped URL" because it is the smallest feature I have shipped
that genuinely required coordinated edits to backend, frontend, and mobile.

This page mirrors [`docs/MULTI_REPO_WORKED_EXAMPLE.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/MULTI_REPO_WORKED_EXAMPLE.md).

The repos referenced below are real names from the workspace this example
is drawn from:

- `luminik-io/luminik-specs` (specs repo, planning context)
- `luminik-io/luminik-backend` (Kotlin API)
- `luminik-io/luminik-frontend` (React web app)
- `luminik-io/luminik-mobile` (React Native app)

Fleet configuration for this example:

```sh
ALFRED_DRAKE_REPOS=luminik-backend,luminik-frontend,luminik-mobile
ALFRED_LUCIUS_REPOS=luminik-backend,luminik-frontend,luminik-mobile
ALFRED_RASALGHUL_REPOS=luminik-backend,luminik-frontend,luminik-mobile
BATMAN_SCAN_REPOS=luminik-backend,luminik-frontend,luminik-mobile
```

## Step 1: operator files one `agent:large-feature` issue

The operator opens the issue in the repo that owns the first decision. For a
schema-shaped feature like this, that is the backend. The issue carries two
labels: `agent:large-feature` and `agent:bundle:add-org-slug`.

```md
Title: Add `org_slug` to account-scoped URLs

Labels: agent:large-feature, agent:bundle:add-org-slug

## What

Every account-scoped URL today is keyed by numeric `account_id`. We want a
URL-safe `org_slug` (lowercase, hyphen-separated, unique per account) in
addition to the numeric id, so:

  - GET /api/v1/orgs/acme/projects works alongside
    GET /api/v1/accounts/4711/projects
  - https://app.example.com/acme/projects works alongside
    https://app.example.com/accounts/4711/projects
  - The mobile deep link luminik://acme/projects resolves to the same place.

Numeric routes keep working. The slug is the new preferred form for new
links shared by the product.

## Acceptance criteria

- [ ] Backend: `Account` has a unique non-null `slug` column with a
      lower-snake-case constraint; a new slug-resolver endpoint exists;
      existing id-keyed routes still work.
- [ ] Frontend: any new internal link uses the slug; account switcher writes
      the slug into the URL; loading by slug works on cold load.
- [ ] Mobile: deep links match `/<slug>/...` and `/accounts/<id>/...`.
- [ ] One end-to-end test that creates an account, reads it by slug, and
      reads it by id.

## Repos this touches

  - luminik-backend (first; schema + endpoint)
  - luminik-frontend (after backend ships staging)
  - luminik-mobile (after backend ships staging)

## Out of scope

- Custom domains per org.
- Renaming the `account_id` foreign key column.
- SEO-rewriting public marketing URLs.

## Rollback

Revert the migration and the slug-resolver endpoint; frontend and mobile
fall back to id-keyed routes automatically.

## Human approval checklist

- [ ] Slug uniqueness collision plan reviewed
- [ ] Migration tested against a staging copy of prod
- [ ] Marketing aware before launch
```

## Step 2: Batman picks the issue up

Batman fires once per hour. On the next firing it scans `BATMAN_SCAN_REPOS`,
finds the `agent:large-feature` issue, resolves the bundle from the
`agent:bundle:add-org-slug` label (one issue at this point, the trigger
itself), and posts a plan summary.

```
[15:04:11] batman  preflight ok                        ● green
[15:04:12] batman  scanning 3 repos for agent:large-feature
[15:04:14] batman  found 1 issue, 1 bundle: add-org-slug
[15:04:14] batman  plan drafted                        ● green
[15:04:15] batman  posted plan to #alfred-fleet
```

The post Batman emits in Slack (rendered shape):

```
batman · plan drafted

Issue:        luminik-backend#247: Add `org_slug` to account-scoped URLs
Bundle:       add-org-slug
Affected:     luminik-backend, luminik-frontend, luminik-mobile
Rollout:      luminik-backend → luminik-frontend → luminik-mobile
Engine:       hybrid

This is plan-only. To proceed, the operator labels each child issue
`agent:implement`.
```

OSS Batman stops here. It does not file the child issues automatically.
That stop is intentional: the operator approves the plan and decides which
repos get autonomous execution.

## Step 3: operator (or a custom Batman extension) files the child issues

In the public package, the operator files the three child issues. Each
inherits `agent:bundle:add-org-slug` so the bundle stays trackable, and
each is labelled `agent:implement` so Lucius will claim it.

If you want this automated, the hook point is to layer a private extension
on top of Batman that calls `gh issue create` after the plan is approved.
The OSS package keeps that step explicit.

### Child issue 1 (backend)

```md
Repo: luminik-io/luminik-backend
Title: Add `org_slug` column and resolver endpoint for accounts

Labels: agent:implement, agent:bundle:add-org-slug

## Goal
Introduce a unique `slug` column on the `accounts` table and a
slug-resolver endpoint. Numeric routes continue to work.

## Files in scope
- src/main/resources/db/migration/V20260601__add_account_slug.sql
- src/main/kotlin/io/luminik/account/AccountController.kt
- src/main/kotlin/io/luminik/account/AccountService.kt
- src/test/kotlin/io/luminik/account/AccountControllerTest.kt

## Acceptance criteria
- [ ] Migration adds `slug VARCHAR(64) NOT NULL UNIQUE` with a lower-snake-case
      CHECK constraint, backfilled from existing account names with a
      deterministic slugifier.
- [ ] `GET /api/v1/orgs/{slug}` returns the same payload as
      `GET /api/v1/accounts/{id}`.
- [ ] Existing id-keyed routes return identical responses.
- [ ] Two new tests: one for slug resolution, one for collision handling.

## Out of scope
- Frontend or mobile changes.
- Custom domains.
```

### Child issue 2 (frontend)

```md
Repo: luminik-io/luminik-frontend
Title: Use `org_slug` in account-scoped URLs

Labels: agent:implement, agent:bundle:add-org-slug

## Goal
Switch internal account-scoped link generation to slug form. Keep id-keyed
URLs working for back-compat.

## Files in scope
- src/lib/routes.ts
- src/features/account-switcher/AccountSwitcher.tsx
- src/features/account-switcher/AccountSwitcher.test.tsx
- src/pages/[slug]/projects.tsx

## Acceptance criteria
- [ ] `accountUrl(account)` returns `/<slug>/...` when slug is present,
      `/accounts/<id>/...` otherwise.
- [ ] AccountSwitcher writes the slug into the URL on switch.
- [ ] Cold load on `/<slug>/projects` resolves and renders projects.
- [ ] No edits to backend response shapes.

## Out of scope
- Marketing pages and SEO routes.
- Mobile deep linking.

## Depends on
luminik-backend bundle:add-org-slug merged to main and deployed to staging.
```

### Child issue 3 (mobile)

```md
Repo: luminik-io/luminik-mobile
Title: Accept `<slug>` in deep links

Labels: agent:implement, agent:bundle:add-org-slug

## Goal
Mobile deep-link handler must accept `luminik://<slug>/...` in addition to
`luminik://accounts/<id>/...`.

## Files in scope
- src/navigation/linking.ts
- src/navigation/linking.test.ts

## Acceptance criteria
- [ ] `luminik://acme/projects` resolves to the Projects screen for the
      `acme` account.
- [ ] `luminik://accounts/4711/projects` continues to resolve.
- [ ] One test per deep-link form.

## Out of scope
- Push notification payloads (separate bundle).

## Depends on
luminik-backend bundle:add-org-slug merged to main and deployed to staging.
```

## Step 4: Lucius picks up the backend issue first

Lucius fires every 20 minutes. The backend issue has no `depends-on` blocker,
so it is eligible on the first firing after labelling.

```
[15:24:11] lucius  preflight ok                        ● green
[15:24:12] lucius  pick_issue: oldest agent:implement
[15:24:13] lucius  claimed luminik-backend#251         ● green
[15:24:14] lucius  worktree opened
                   ~/.alfred/worktrees/eng-lucius-luminik-backend-251-20260601-152414/
[15:24:15] lucius  branch: agent/lucius/251-add-org-slug-column-and-resolver
[15:24:16] lucius  invoking hybrid engine, max_turns=140
[15:27:42] lucius  engine returned success, 38 turns, $0.41
[15:27:43] lucius  pre-push: ./gradlew check          (running…)
[15:30:12] lucius  pre-push ok
[15:30:14] lucius  pushed branch
[15:30:16] lucius  gh pr create
[15:30:17] lucius  PR opened: luminik-backend#412     ● green
[15:30:17] lucius  [OK] commit 7c4a1f2
[15:30:18] lucius  release_issue → agent:pr-open
[15:30:19] lucius  Slack-post info
```

The Slack post Lucius emits:

```
lucius · PR opened · green

Issue:    luminik-backend#251
PR:       luminik-backend#412
Branch:   agent/lucius/251-add-org-slug-column-and-resolver
Engine:   hybrid (claude)
Turns:    38
Cost:     $0.41
Pre-push: ./gradlew check (ok, 2m 28s)
```

## Step 5: Ra's al Ghul reviews

Ra's al Ghul fires every 30 minutes. It picks the fresh `agent:authored`
PR.

```
[15:48:11] rasalghul  reviewing luminik-backend#412
[15:51:33] rasalghul  posted review comment, 2 nits, 0 P0/P1
```

The review comment (rendered shape):

```
rasalghul · review

Correctness: ok (migration is idempotent, resolver handles missing slug)
Security:    ok (no input echo, no SQL string concat)
Performance: ok (slug column gets unique index from the UNIQUE constraint)
Maintainability: 2 nits (P2)

Nits:
1. AccountController.kt line 88: extract the slug regex constant; it's used
   twice in this file.
2. V20260601 migration: the CHECK constraint message could name the column
   for easier debugging.

Ship-ready: yes
```

## Step 6: Nightwing applies the nits

Nightwing fires every 45 minutes and only lands P0/P1 reviewer comments.
P2 nits are out of scope by default. For this example, assume the operator
also asked Nightwing to address P2 nits on this PR by labelling it
`nightwing:p2`. On the next firing:

```
[16:33:11] nightwing  picking review threads on luminik-backend#412
[16:33:14] nightwing  2 unresolved threads (P2 by label override)
[16:33:15] nightwing  worktree opened
[16:35:42] nightwing  engine returned success, 7 turns, $0.09
[16:35:43] nightwing  pushed fix commit 9a2cdde
[16:35:44] nightwing  resolved 2 threads on luminik-backend#412
```

## Step 7: Bane adds tests on the side

Bane fires every 4 hours and writes only test files. It looks at the
recently-changed files in `luminik-backend` and notices `AccountService.kt`
is now the lowest-coverage actively-changed file.

```
[18:04:11] bane   lowest-coverage actively-changed file
                  luminik-backend/src/.../AccountService.kt (62%)
[18:04:12] bane   worktree opened
[18:07:38] bane   engine returned success, 22 turns, $0.18
[18:07:39] bane   PR opened: luminik-backend#414      ● green
                  agent:authored, tests-only
```

Bane's PR is a separate `agent:authored` PR; it does not push to Lucius's
branch. The squash-merge utility (`automerge`) treats it on its own merits.

## Step 8: backend merges, the bundle progresses

After Ra's al Ghul says "Ship-ready: yes" and CI is green for 30 minutes,
`automerge` squash-merges `luminik-backend#412`. The issue transitions to
`agent:done`.

A separate deploy step (Alfred does not own this) rolls staging. Once
backend is live on staging, the operator unblocks the frontend and mobile
child issues by removing their `agent:blocked` label or otherwise marking
them eligible.

In parallel on the next Lucius firings, the frontend and mobile issues get
claimed and worked. They run on different worktrees, in different repos,
and never collide.

```
[19:04:11] lucius  claimed luminik-frontend#188        ● green
[19:04:14] lucius  worktree opened
                   ~/.alfred/worktrees/eng-lucius-luminik-frontend-188-20260601-190414/
...
[19:24:11] lucius  claimed luminik-mobile#92           ● green
[19:24:14] lucius  worktree opened
                   ~/.alfred/worktrees/eng-lucius-luminik-mobile-92-20260601-192414/
```

Each gets its own review pass, its own Nightwing fixes if needed, its own
automerge. The labels on the original `agent:large-feature` issue are
unchanged; the bundle is tracked by the `agent:bundle:add-org-slug` label
that every child carries.

## Step 9: final bundle rollup

When the last child PR in the bundle merges, the operator (or a custom
extension to Batman) posts a closing rollup. The OSS package does not
auto-close the parent `agent:large-feature` issue; the operator does it.

Example closing rollup post:

```
batman · bundle shipped · add-org-slug

Parent:   luminik-backend#247
Children:
  - luminik-backend#251 → PR #412 (merged 15:50 → 21:30)
  - luminik-frontend#188 → PR #207 (merged 19:48 → 20:30)
  - luminik-mobile#92  → PR #61  (merged 19:55 → 20:30)

Bane added 2 test PRs along the way (#414 backend, #208 frontend).
Total cost: $1.84 across 7 firings.
Total wall-clock: 6h 26m.
```

## What this example demonstrates

- The operator files one `agent:large-feature` issue, not three.
- Batman posts a plan, not an execution.
- The child `agent:implement` issues are filed by the operator (or by a
  private Batman extension) and each lives in the repo that owns the
  change. This answers the most common "but who files the per-repo issues"
  question: a human does in the OSS path; an automation can in a custom one.
- Lucius, Ra's al Ghul, Nightwing, and Bane act on whatever is in their
  inbox without knowing they are part of a bundle. The bundle label is for
  tracking, not coordination. They never call each other; they only see
  GitHub.
- The worktree per firing means three Lucius firings can run in three
  different repos at the same time without interfering.

## See also

- [Workspace patterns](/getting-started/workspace-patterns/): one-repo,
  multi-repo, and specs-led layouts.
- [Specs-driven development](/guides/specs-driven-development/): writing the
  parent `agent:large-feature` issue.
- [Output samples](/reference/output-samples/): every output shape that
  appears in the trace above, in one place.
