---
title: Slack-native planning
description: "How Alfred uses Slack threads for plan review, scope changes, approvals, and follow-up."
---

Slack is Alfred's primary collaboration surface. The local UI shows health,
runs, plans, and memory, but plan discussion belongs in the Slack thread where
the team can see scope, ask questions, approve, reject, and inspect follow-up
PRs.

Full design contract: [`docs/SLACK_UX.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_UX.md).

## The message contract

Every message has one job:

- plan drafted
- feedback captured
- plan revised
- implementation started
- PR opened
- follow-up captured
- needs input
- failed

Approval messages should lead with the decision, the parent issue, the affected
repos, the readiness state, and the next step. Logs and transcripts belong in
linked evidence, not at the top of the Slack message.

## Reply to change the plan

People can reply naturally, or use structured commands:

```text
acceptance: the Slack plan thread shows clear next steps before approval
test: add a regression test for unresolved questions
add repo: luminik-io/alfred-os-site
remove repo: mobile
question: should this include the docs site?
open questions: none
```

Questions block execution until they are answered or explicitly cleared with
`open questions: none` / `open questions: accepted as risk`. Scope changes are
echoed back before approval. Once approved, Alfred copies the accepted
amendments into child issues so the PR work does not drift from the discussion.

## DM or mention Alfred to shape work

With the optional Slack planning listener running, trusted users can DM Alfred
or mention it in a channel with rough work. Alfred saves a local draft, scores
readiness, asks concrete missing-scope questions, and registers the thread so
follow-up replies stay attached to the draft.

```text
title: improve billing retry copy
problem: customers cannot tell whether retry is automatic
desired: invoice page explains retry status and next step
repo: example/web
acceptance: retrying, failed, recovered states are visible
test: component coverage for all three states
question: should this touch emails too?
open questions: none
```

Only configured operator/trusted users can create drafts. If none are
configured, the listener ignores every event. The listener writes local draft
JSON and feedback files; it never files issues, opens PRs, merges, or approves
execution.

## Reply after a PR or report

Threads keep working after implementation starts. Trusted users can reply with:

```text
change: tighten the empty state copy
fix: the docs link points to the old page
test: add coverage for the approval thread
question: should this also touch mobile?
```

Alfred treats those replies as follow-up context for the next plan, issue, or
PR pass. They do not approve, merge, or change code by themselves.

## Control the fleet from chat

A trusted user can also drive the fleet by **leading a message with a known
verb**. These are control and query commands, handled separately from planning
intake:

| Command | What it does |
|---|---|
| `status` | Fleet health: loaded agents, pauses, locks. |
| `runs` | Recent firings per agent. |
| `plans` | Local planning inbox: saved plans, Slack drafts, and captured follow-ups. |
| `plan <id>` | Inspect one local plan or follow-up. |
| `draft <id>` | Convert a captured follow-up into a local planning draft. |
| `handled <id>` | Operator-only: archive a captured follow-up without drafting. |
| `memory` / `memories` | Show pending memory candidates and promotion suggestions. |
| `remember [repo:] <lesson>` | Queue a reviewable memory candidate from Slack. |
| `memory promote <id>` | Operator-only: promote a candidate into future recall. |
| `memory reject <id>` | Operator-only: reject a noisy candidate. |
| `memory redis` | Check the optional Redis Agent Memory Server bridge. |
| `pause <codename>` | Stop scheduled firings for one agent (or `all`). |
| `resume <codename>` | Reverse a pause. |
| `help` | List these commands. |

Only a message whose first token is a known verb triggers an action. Free-form
prose like "can you pause everything later?" never controls the fleet; it falls
through to planning intake. `pause`/`resume` run the `alfred` CLI through an
explicit argv with no shell, and the codename is charset-validated before it
reaches the command, so chat can never inject a flag. `status` and `runs` are
read-only. `draft <id>` and `handled <id>` only touch local follow-up files and
planning drafts; they never approve execution, file GitHub issues, start agents,
or merge PRs. `remember ...` only stages a memory candidate; it does not enter
future prompt context until the operator promotes it.

## Watch progress in the thread

When the issue bridge files an issue from an approved draft, the thread does not
go quiet. A read-only sweep posts the fleet's progress back into the same thread
as it happens: issue claimed, PR opened, CI green or failing, merged. Each state
posts at most once, so a sweep with no change posts nothing.

The sweep runs inside the listener's idle loop on a cadence set by
`ALFRED_SLACK_THREAD_SYNC_INTERVAL_S` (default 5 minutes), or on your own
schedule with `alfred slack-thread-sync`. It only reads the issue and its linked
PR; it never edits labels, claims issues, comments on GitHub, or runs code. Full
setup is in [`docs/SLACK_SETUP.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_SETUP.md).

## Plain mode

For a non-technical front door, set `ALFRED_INTAKE_PROFILE=plain`. The same
structured draft and every downstream gate are unchanged; only the conversation
changes. Instead of readiness scores and repo names, the person sees plain
questions and a short "Here's what I'll do... OK to go ahead?" plan, and approves
an outcome rather than code. See [plain mode](/concepts/plain-mode/).

## Local UI boundary

The native client and `alfred serve` should make the fleet easier to trust:
health, plans, runs, memory review, setup checks, and safe repairs. They should
not become a second chat app. Every plan should link back to the Slack thread
where collaboration happened.

The listener and bridge are the optional `slack` tier of the [layered install](/concepts/layered-install/). The core fleet runs without them; a fresh install keeps the issue bridge off until the operator arms it.
