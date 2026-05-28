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

1. Short title with state: `Batman plan ready · billing-v2`.
2. Primary links: parent issue, PR, or Slack thread.
3. Decision needed: approve, reject, answer a question, or edit scope.
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
```

Rules:

- Questions block execution until answered.
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
*Batman plan ready* · `billing-v2`
*Parent:* <https://github.com/example/api/issues/42|example/api#42>
*Title:* Add invoice payment retries
*Decision:* react :white_check_mark: to approve, :x: to reject, or reply with changes before approving.
*Reply commands:* `acceptance:`, `test:`, `add repo:`, `remove repo:`, `question:`, or plain language.
*Readiness:* ready for approval

*Execution scope if approved now:* 2 repos, 2 child issues
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
