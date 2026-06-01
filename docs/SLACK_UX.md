# Slack-native planning UX

Status: design contract for Alfred messages and plan collaboration.

Slack is Alfred's primary product surface. The local UI should make Alfred
easier to operate, but the collaboration loop belongs in Slack threads because
that is where teammates already discuss scope, links, approvals, and follow-up.

## Message Jobs

Every Alfred Slack message should do one job.

| Job | When | Required outcome |
|---|---|---|
| Plan drafted | Batman has a plan and is waiting | The reader knows what will happen if they approve. |
| Feedback captured | Someone replied with plan changes | The reader knows the change was heard and implementation is still paused. |
| Plan revised | Alfred applied feedback | The reader sees the new scope and knows whether approval is possible. |
| Implementation started | Approval landed | The reader sees which repos are being worked in and where to follow progress. |
| PR opened | A worker shipped work | The reader can open the PR, issue, and run context. |
| Follow-up captured | Someone replied after a report or PR link | The reader knows the feedback is context for the next pass, not merge approval. |
| Needs input | Alfred cannot safely continue | The reader sees the exact question or smallest next action. |
| Failed | A run broke | The reader sees whether it is environment, code, quota, or missing context. |

## Message Shape

Use this order for approval and action messages:

1. Short title with state: `Alfred plan ready · billing-v2`.
2. Primary links: parent issue, PR, or Slack thread.
3. Next step: approve, reject, answer a question, or edit scope.
4. Execution scope: repos, child issues, rollout order.
5. Readiness: ready, needs scope, blocked by questions, or failed.
6. Next step: one concrete action.
7. Evidence or details: compact and optional.

Raw logs, long stack traces, local paths, and transcripts belong behind links or
in the local UI. They should not lead a Slack message.

## Planning Replies

Plan threads accept plain language, plus structured commands when the user wants
precision:

```text
acceptance: the Slack plan thread shows clear next steps before approval
test: add a regression test for unresolved questions
add repo: luminik-io/alfred-os-site
remove repo: mobile
question: should this include the docs site?
open questions: none
```

Rules:

- Questions block execution until answered or explicitly cleared with
  `open questions: none` / `open questions: accepted as risk`.
- Scope changes are echoed back before approval.
- Approved amendments are copied into child issues.
- Alfred should never file child issues from a vague parent plan just because a
  reaction appeared.
- A rejection ends the firing cleanly and keeps the thread as the audit trail.

## Post-report And PR Replies

After Alfred posts child issues, PR links, or a report, trusted thread replies
can be captured as follow-up context:

```text
change: tighten the empty state copy
fix: the docs link points to the old page
test: add coverage for the approval thread
question: should this also touch mobile?
```

Rules:

- Captured replies do not approve, merge, or change code by themselves.
- `question:`, `hold:`, `blocker:`, and scope-changing replies require a
  decision before more work starts.
- The acknowledgement should link the relevant issue or PR, summarize the next
  action items, and state the safety boundary clearly.
- Follow-up context belongs in the next plan, child issue, or PR pass so Slack
  remains the audit trail.
- Batman waits `BATMAN_REPORT_FEEDBACK_TIMEOUT_S` seconds after a report post
  before collecting trusted replies. Captured context is also written to
  `$ALFRED_HOME/state/followups/` and appears in the local Plans inbox as a
  `needs follow-up` item.
- From the local Plans detail page, an operator can convert a captured
  follow-up into a scoped planning draft for the next pass, or mark it handled.
  Both actions archive the original follow-up and remain local-only.

## Slack Planning Inbox Commands

Trusted users can inspect the same local planning queue from Slack:

| Command | What it does |
|---|---|
| `plans` | Shows the newest saved plans, Slack drafts, and captured follow-ups. |
| `plan <id>` | Shows source, status, parent link, repos, readiness, preview, and next actions. |
| `draft <id>` | Converts a captured follow-up into a local planning draft with memory recall and readiness checks. |
| `handled <id>` | Operator-only. Archives a captured follow-up without creating a draft. |
| `memory` / `memories` | Shows pending memory candidates and suggested promotions. |
| `remember [repo:] <lesson>` / `memory remember ...` | Queues a reviewable memory candidate from Slack. |
| `memory promote <id>` | Operator-only. Promotes a candidate into future recall. |
| `memory reject <id>` | Operator-only. Rejects a noisy candidate. |
| `memory harvest` | Previews repeated-failure lessons from the reliability governor. |
| `memory harvest now` | Operator-only. Queues harvested lessons as reviewable candidates. |
| `memory redis` | Checks the optional Redis Agent Memory Server bridge. |
| `memory sync` | Previews reviewed-lesson sync to Redis AMS. |
| `memory sync now` | Operator-only. Writes reviewed lessons to Redis AMS. |

These commands do not start work, approve execution, file GitHub issues, or
merge PRs. They are the Slack-native bridge between "someone replied with useful
context" and "Alfred has a scoped draft for the next pass." `remember ...` and
`memory remember ...` stage candidates only; they never become prompt context
until the operator runs `memory promote <id>`. Scheduled `memory-harvest.py`
runs follow the same rule: they only stage repeated-failure candidates for
review.

When a Slack-created draft is already scoped enough to be implementation-ready,
Alfred also queues a reviewable `slack-planning` memory candidate automatically.
That candidate records the local draft id and evidence, but it is still only a
candidate. It does not enter recall until the operator explicitly promotes it.
Set `ALFRED_SLACK_MEMORY_CANDIDATES=0` to disable this automatic queueing.

## DM And App Mention Intake

When the Slack planning listener is running, trusted users can DM Alfred or
mention it in a channel with rough work. Alfred saves a local planning draft,
runs readiness checks, replies with the missing scope questions, and registers
the thread for future refinement.

Useful fields:

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

Rules:

- Only configured operator/trusted users can create drafts or amend threads.
- The operator can add or remove local Slack collaborators with `trust <@user>`
  and `untrust <@user>`. Trusted collaborators can steer plans and create
  drafts; execution approval still belongs to the operator.
- If trusted users are not configured, Alfred ignores every event.
- Intake creates local draft JSON under `$ALFRED_HOME/state/planning-drafts/`.
- Replies in the intake thread revise the same saved draft, regenerate the issue
  body and spec body, rerun readiness checks, and append a revision entry.
- `open questions: none` clears previously captured questions after the thread
  has resolved them.
- Planning memory may appear as advisory hints when memory is enabled, but the
  current Slack thread and readiness findings still win.
- Chat intake never files issues, opens PRs, merges, or approves execution.
- A draft can graduate into a GitHub issue only through an explicit operator
  action outside the listener.

## Tone

Alfred should sound like a calm engineering lead:

- specific title, no generic "update" posts
- product language before implementation detail
- one decision per message
- links named by purpose, not pasted as raw URLs
- no duplicate top-level posts for the same firing
- no role-specific assumptions about who is reading
- emojis can help scanning, but text must carry the meaning

## Good Plan Post

```text
*Alfred plan ready* · `billing-v2`
*Parent:* <https://github.com/example/api/issues/42|example/api#42>
*Work:* Add invoice payment retries
*Readiness:* ready for approval
*Next step:* reply in this thread to steer the plan, or approve only if it is right.
*Replies Alfred understands:* `change:`, `acceptance:`, `test:`, `add repo:`, `remove repo:`, `question:`, `open questions: none`
*Approval gate:* :white_check_mark: starts this exact scope; :x: stops it.

*Scope if approved now:* 2 repos, 2 child issues
  - `example/api`: backend retries and idempotency
  - `example/web`: retry state in invoice UI

*Done when:*
- failed payments retry without duplicate charges
- invoice UI shows retrying, failed, and recovered states
- unit and integration tests cover the retry path

*After approval:* Alfred files child issues, keeps this thread as the plan log, and links PRs back here.
```

## Anti-patterns

- A top-level "plan drafted" post followed by a second top-level summary for the
  same firing.
- "Everything is ready" when there are open questions.
- Raw JSON, raw timestamps, or local filesystem paths as the main content.
- A long plan excerpt without the decision, scope, and next action above it.
- A PR notification without issue and thread links.
- Audience-specific wording when the feature is for every operator.
