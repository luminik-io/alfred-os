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

## Local UI boundary

The native client and `alfred serve` should make the fleet easier to trust:
health, plans, runs, memory review, setup checks, and safe repairs. They should
not become a second chat app. Every plan should link back to the Slack thread
where collaboration happened.
