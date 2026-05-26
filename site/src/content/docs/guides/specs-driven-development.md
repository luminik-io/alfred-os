---
title: Specs-driven development
description: Use specs as durable context for Drake, Batman, Lucius, reviewers, and tests.
---

Alfred does not require a specific spec framework. It needs a durable source of
intent that an agent can read before it acts: a GitHub issue, a spec file, a
roadmap, an `AGENTS.md` or `CLAUDE.md` file, or a dedicated specs repo.

Specs-driven Alfred means the important context lives outside the chat
transcript. Agents read the spec, turn it into scoped GitHub work, execute in
clean worktrees, and return PRs, reviews, tests, and Slack reports.

Full GitHub guide: [`docs/SPECS_DRIVEN_DEVELOPMENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SPECS_DRIVEN_DEVELOPMENT.md).

## Why Specs Matter

One-off prompts are good for small tasks. Recurring engineering work needs
stable inputs:

- what user or system behavior should change
- which repo owns the first implementation
- which repos may need follow-up work
- how a reviewer can verify the result
- what is out of scope for the current PR

Without those inputs, autonomous agents tend to either guess or stop. Alfred
uses specs to reduce both failure modes.

## Minimal Spec Shape

You can scaffold this shape with:

```sh
alfred spec new "Feature name" --repo my-org/api --out docs/specs/feature.md
alfred spec lint docs/specs/feature.md
```

```md
# Feature: <name>

Status: draft | approved | shipped
Owner: <human owner>
Repos: api, web, mobile

## Goal

What user or system behavior changes?

## Current Behavior

What does the product do today?

## Target Behavior

What should be true after this ships?

## Acceptance Criteria

- [ ] A reviewer can verify this with <command, endpoint, screen, or file>.
- [ ] Tests cover <specific behavior>.
- [ ] The PR does not change <explicit out-of-scope area>.

## Rollout

1. <repo A first because...>
2. <repo B after...>

## Out Of Scope

- <what the agent must not include>

## Rollback

How to revert or disable the change if it breaks.
```

## How Alfred Uses Specs

1. **Drake reads specs and roadmap context.** It files scoped
   `agent:implement` issues only when the acceptance criteria are concrete and
   testable.
2. **Batman plans multi-repo work.** A labelled `agent:large-feature` issue,
   optionally grouped with `agent:bundle:<slug>`, becomes a rollout plan across
   the configured repos. Public Batman stops at the plan.
3. **Lucius implements one repo at a time.** It claims a single
   `agent:implement` issue, opens an isolated worktree, invokes Claude Code or
   Codex, pushes a branch, and opens a PR.
4. **Ras al Ghul, Bane, and Nightwing close the review path.** Review, tests,
   and P0/P1 comment fixes happen as separate bounded jobs.
5. **Slack and shipped summaries show the outcome.** The operator sees what was
   planned, claimed, opened, merged, or blocked.

## Workspace Patterns

For one repo, keep specs inside that repo:

```text
my-app/
  AGENTS.md
  docs/specs/
  src/
```

For multi-repo products, keep the specs checkout beside the code repos:

```text
~/code/product/
  api/
  web/
  mobile/
  specs/
```

Use code repos in the first write path:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos my-org/api,my-org/web,my-org/mobile \
  --slack-webhook skip
```

Then edit the seeded prompts under `~/.alfred/prompts/` to mention the specs
checkout. Do not include the specs repo in `--repos` unless you want Alfred to
create labels there and pick issues or PRs from it.

## Large Features

For a cross-repo feature, create a GitHub issue in the repo that owns the first
decision and label it:

```text
agent:large-feature
agent:bundle:<short-slug>   # optional when several issues belong together
```

Batman scans `BATMAN_SCAN_REPOS`, drafts the rollout plan, posts it to Slack or
local logs, and stops. That stop is intentional in the public package. The
operator decides when to split the plan into implementation issues.

## Good Agent-Readable Specs

- Use stable file names and headings. Agents can link to `SPECS/012-auth.md`
  more reliably than "the auth doc."
- Name exact endpoints, tables, screens, commands, and test files when they are
  known.
- Put "Out of scope" in every spec. It prevents scope creep inside a worktree.
- Split cross-repo work into repo-sized implementation issues after the plan.
- Keep specs current, but trust code when they disagree. File a docs or specs
  issue when drift is discovered.
- Put repo-specific guidance in `AGENTS.md` or `CLAUDE.md` so the coding engine
  receives local rules every time.

## External References

These are useful references, not Alfred dependencies:

- [GitHub Spec Kit](https://github.com/github/spec-kit): a structured
  spec-to-plan-to-tasks workflow for agentic development.
- [AGENTS.md](https://agents.md/) and the
  [Codex AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md):
  agent-readable repo instructions.
- [Claude Code best practices](https://code.claude.com/docs/en/best-practices):
  explore, plan, implement, and verify with persistent project guidance.
- [Kiro specs](https://kiro.dev/docs/specs/): requirements, design, and tasks
  as persistent development artifacts.

Alfred can consume outputs from any of these styles. The only hard requirement
is that the work item given to an autonomous agent has clear scope, a repo
boundary, and testable acceptance criteria.
