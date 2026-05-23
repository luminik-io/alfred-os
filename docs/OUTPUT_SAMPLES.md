# Output samples

This page is a reference for every shape of output Alfred produces. If you are
deciding whether to install, you can mentally simulate the operator experience
by reading this end to end.

All samples are representative, not auto-generated. The exact wording in your
fleet will differ; the shape will not.

## Slack: Lucius firing report (success)

When it appears: Lucius claimed an `agent:implement` issue, the engine wrote a
commit, the pre-push command passed, and the PR was opened.

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

## Slack: Lucius firing report (blocked)

When it appears: Lucius claimed an issue but the engine could not finish.
Either Claude printed `[BLOCKED]` itself, or the runner detected a missing
artifact. The issue is released back to `agent:implement`.

```
lucius · BLOCKED · warn

Issue:    luminik-frontend#188
Branch:   agent/lucius/188-use-org-slug-in-account-urls
Engine:   hybrid (claude)
Turns:    22
Cost:     $0.27
Reason:   [BLOCKED] cannot resolve type error in src/lib/routes.ts:42.
          accountUrl(account) is called with `Account | undefined`, but
          types/account.ts only exports Account. Need either a narrowing or
          a contract update; out of scope per acceptance criteria.

Worktree retained for inspection:
  ~/.alfred/worktrees/eng-lucius-luminik-frontend-188-20260601-194414/
```

## Slack: shipped summary (daily, post-merge)

When it appears: `bin/shipped-summary-daily.sh` runs (default 18:00 local).
It collects every merged `agent:authored` PR from the day and posts one
roll-up.

```
shipped · yesterday · 3 PRs merged

luminik-backend
  #412 Add org_slug column and resolver endpoint
       Lucius · 38 turns · $0.41 · merged 21:30 by automerge
  #414 Tests for AccountService.slug paths
       Bane · 22 turns · $0.18 · merged 22:14 by automerge

luminik-frontend
  #207 Use org_slug in account-scoped URLs
       Lucius · 27 turns · $0.31 · merged 20:30 by automerge

Total: $0.90 / 87 turns / 3 PRs
```

## Slack: Batman bundle plan post

When it appears: Batman scanned `BATMAN_SCAN_REPOS`, found an
`agent:large-feature` issue, resolved its bundle, and posted the plan.
Public Batman stops at the plan.

```
batman · plan drafted

Issue:        luminik-backend#247: Add org_slug to account-scoped URLs
Bundle:       add-org-slug
Affected:     luminik-backend, luminik-frontend, luminik-mobile
Rollout:      luminik-backend → luminik-frontend → luminik-mobile
Engine:       hybrid

This is plan-only. To proceed, the operator labels each child issue
agent:implement.
```

## doctor.sh: clean run

When it appears: `bash bin/doctor.sh`. The script triggers a doctor-mode
firing of every enabled agent (no LLM spend) and reports preflight status.

```
$ bash bin/doctor.sh
[doctor] alfred-os doctor starting
[doctor] ALFRED_HOME=/Users/prasad/.alfred
[doctor] WORKSPACE_ROOT=/Users/prasad/code
[doctor] GH_ORG=luminik-io

[doctor] preflight ............................. ok
[doctor]   gh ............................ ok (gh version 2.45.0)
[doctor]   git ........................... ok (git 2.43.0)
[doctor]   claude ........................ ok (Claude Code 1.2.4)
[doctor]   codex ......................... ok (codex 0.3.1)
[doctor]   gh auth ....................... ok (prasad@luminik.io)

[doctor] workspace repos ......................... ok
[doctor]   ~/code/luminik-backend ........ ok (origin: luminik-io/luminik-backend)
[doctor]   ~/code/luminik-frontend ....... ok (origin: luminik-io/luminik-frontend)
[doctor]   ~/code/luminik-mobile ......... ok (origin: luminik-io/luminik-mobile)

[doctor] agents (doctor-mode firings) ............ ok
[doctor]   drake     [DRAKE-DOCTOR-OK]
[doctor]   lucius    [LUCIUS-DOCTOR-OK]
[doctor]   rasalghul [RASALGHUL-DOCTOR-OK]
[doctor]   batman    [BATMAN-DOCTOR-OK]
[doctor]   agent-cleanup [AGENT-CLEANUP-DOCTOR-OK]

[doctor] all green; fleet ready to run.
```

## doctor.sh: one failure

When it appears: same script, but one preflight step fails. Doctor exits
non-zero. The fleet keeps running, but the failing agent will exit early on
every firing until the cause is fixed.

```
$ bash bin/doctor.sh
[doctor] alfred-os doctor starting
[doctor] ALFRED_HOME=/Users/prasad/.alfred
[doctor] WORKSPACE_ROOT=/Users/prasad/code
[doctor] GH_ORG=luminik-io

[doctor] preflight ............................. ok
[doctor]   gh ............................ ok
[doctor]   git ........................... ok
[doctor]   claude ........................ ok
[doctor]   codex ......................... ok
[doctor]   gh auth ....................... ok

[doctor] workspace repos ......................... FAIL
[doctor]   ~/code/luminik-backend ........ ok
[doctor]   ~/code/luminik-frontend ....... ok
[doctor]   ~/code/luminik-mobile ......... MISSING
[doctor]     expected: /Users/prasad/code/luminik-mobile
[doctor]     hint: git clone https://github.com/luminik-io/luminik-mobile

[doctor] agents (doctor-mode firings) ............ partial
[doctor]   drake     [DRAKE-DOCTOR-OK]
[doctor]   lucius    [LUCIUS-PREFLIGHT-FAILED] missing checkout: luminik-mobile
[doctor]   rasalghul [RASALGHUL-DOCTOR-OK]
[doctor]   batman    [BATMAN-DOCTOR-OK]

[doctor] 1 failure. Fix the workspace, re-run doctor, then deploy.
exit 1
```

## A well-written `agent:implement` issue (Drake output)

When it appears: Drake fired against a specs/roadmap source and filed one
new issue. Drake also adds the body to the issue on GitHub.

```md
Title: Add `intent` prop to `<Button>` in packages/ui

Labels: agent:implement

## Goal
Add an `intent` prop to `<Button>` with values `primary | secondary |
danger`. Existing call sites default to `primary` (no behavior change).

## Files in scope
- packages/ui/src/Button.tsx
- packages/ui/src/Button.stories.tsx
- packages/ui/src/Button.test.tsx

## Acceptance criteria
- [ ] `Button` accepts `intent?: "primary" | "secondary" | "danger"` and
      renders the matching token class.
- [ ] Existing snapshot in `packages/ui/src/Button.test.tsx` still passes.
- [ ] One new test case covers `intent="danger"` rendering.
- [ ] No edits outside `packages/ui/`.

## Out of scope
- Adopting the new prop in `apps/web` or `apps/admin`.
- Token additions to `packages/tokens`.

## Source
specs/030-button-intent.md (commit 9c4a1b2)
```

## A Lucius PR (title, first commit message, body)

When it appears: Lucius ran the engine successfully, the pre-push passed,
and `gh pr create` returned a PR URL. The PR is labelled `agent:authored`.

PR title:

```
Add intent prop to <Button> (packages/ui)
```

First commit message:

```
feat(ui): add `intent` prop to Button (primary | secondary | danger)

The new `intent` prop is optional and defaults to `primary` so every
existing call site keeps its current behavior. The matching token class is
applied via the existing className pipeline; no new tokens are introduced
in this PR.

Tests added:
- packages/ui/src/Button.test.tsx: renders `intent="danger"` with the
  expected token class.

Closes #312
```

PR body:

```md
Implements luminik-frontend#312.

## What
Adds an optional `intent` prop to `<Button>`. Default is `primary`. No
existing call site changes.

## Where
- packages/ui/src/Button.tsx
- packages/ui/src/Button.test.tsx
- packages/ui/src/Button.stories.tsx

## Verification
- `pnpm -F ui test` passes locally (worktree pre-push).
- Existing snapshot for `<Button>` unchanged.
- New test covers `intent="danger"`.

## Out of scope
- Adopting the prop in `apps/web` or `apps/admin`.
- Token additions to `packages/tokens`.

---
Authored by Lucius (Alfred). Engine: hybrid (claude). Turns: 17. Cost: $0.21.
```

## Terminal: `alfred run echo --force`

When it appears: the operator runs a single firing of the Echo example agent
manually, bypassing the schedule. `--force` overrides the paused-agent check.

```
$ alfred run echo --force
[alfred] firing echo (override: --force)
[alfred] env: ALFRED_HOME=/Users/prasad/.alfred AGENT_CODENAME=echo
[echo] preflight ok
[echo] hello from echo at 2026-06-01T15:04:11Z
[echo] firing complete, exit 0
[alfred] done in 0.12s
```

## State JSON: shape under `$ALFRED_HOME/state/`

When it appears: every firing reads and writes per-agent state under
`$ALFRED_HOME/state/`. One example is the spend ledger at
`$ALFRED_HOME/state/spend/<codename>.json`:

```json
{
  "codename": "lucius",
  "date": "2026-06-01",
  "firings_today": 14,
  "successes_today": 9,
  "failures_today": 2,
  "noops_today": 3,
  "turns_today": 348,
  "cost_usd_today": 4.27,
  "consecutive_failures": 0,
  "last_firing_id": "20260601-191211-3f",
  "last_firing_outcome": "ok",
  "last_firing_pr": "luminik-backend#412",
  "blocked_until": null
}
```

Other state files in the same directory follow the same shape principles
(JSON object, one file per concern, no nested state machines): `code-map.json`
for the indexer output, `global-blocked-until.json` for the fleet-wide rate
limit signal, and `claims/<repo>-<issue>.json` for issue claim records.

## See also

- [`MULTI_REPO_WORKED_EXAMPLE.md`](MULTI_REPO_WORKED_EXAMPLE.md): every sample
  above traced into a single end-to-end story.
- [`STATE_MACHINE.md`](STATE_MACHINE.md): the issue-claim shape behind
  Lucius's `release_issue` log lines.
- [`GLOSSARY.md`](GLOSSARY.md): one-line definitions for every codename, label,
  and sentinel that appears here.
