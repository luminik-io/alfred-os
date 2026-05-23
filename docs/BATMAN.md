# Batman: the multi-repo coordinator

Batman is Alfred's plan-approve-execute-report agent for features that
span more than one repository. It reads a single parent issue, drafts a
bundle plan, posts that plan to Slack for an explicit operator approval,
files scoped child issues across the named repos, and posts a follow-up
report naming the children that landed.

Single-repo work is NOT Batman territory. For a feature that fits in one
repo, the right shape is Drake (planner) filing the issue and Lucius
(feature dev) implementing it. Use Batman only when the change spans
multiple repos or modules and a coordination layer earns its keep.

## When to reach for Batman

- Building a `billing-v2` launch across backend, frontend, and mobile.
- Splitting a `payments-v3` rollout across two backend repos and a
  shared client SDK.
- Coordinating a `data-schema-v2` migration across a producer service,
  a consumer service, and a data pipeline.

The common shape: one operator-authored issue, multiple downstream
repos, child scopes that can be worked in parallel once approved.

## When NOT to use Batman

- Single-repo features ("add a settings dropdown"). That is Drake then
  Lucius.
- Cross-cutting refactors where the children would all be in the same
  repo. That is also Drake then Lucius (or just Lucius if Drake already
  filed it).
- Anything where a human plans to write the scoping doc themselves. The
  plan-approve cycle exists to capture intent into machine-actionable
  child issues; if you are skipping that, you do not need Batman.

## Lifecycle

```
+---------+    +------------------+    +--------+    +---------+
| plan    | -> | request_approval | -> | execute| -> | report  |
+---------+    +------------------+    +--------+    +---------+
     ^                  ^                  ^              ^
     |                  |                  |              |
parent issue       Slack reaction      gh issue       Slack thread
body parsed        from operator       create per     reply naming
into BundlePlan    (white_check_mark)  child repo     filed children
```

Every step lives on a dataclass so the same code is exercised in tests
with in-memory fakes. Read `lib/batman.py` and
`tests/test_batman_execute.py` together to follow the wiring.

### 1. plan(issue) -> BundlePlan

Batman parses the parent-issue body, derives a bundle slug from the
title (`billing-v2` for "Bundle: billing-v2 rollout"), and builds a
`BundlePlan` carrying:

- `bundle_slug`: short id used as the `agent:bundle:<slug>` label on
  every child.
- `affected_repos`: declaration-ordered list of `owner/repo` slugs.
- `children`: per-repo `ChildIssue` records ready to file.
- `done_when`: free-text criteria the operator wrote.
- `plan_markdown`: the human-facing post Batman sends to Slack.

### 2. request_approval(plan) -> ApprovalEnvelope

Batman posts `plan.plan_markdown` to the configured Slack channel via
`slack_format.firing_thread_root`. The returned `message_ts` is the
anchor the approval gate polls. If Slack is unreachable (no bot token,
channel unset, transport down), the envelope is `None` and Batman halts
after the plan no matter what `BATMAN_AUTO_EXECUTE` says, never silently
executes without a captured approval message.

### 3. await_approval(envelope) -> ApprovalResult

Polls `reactions.get` on the plan message every 30s by default until
either the operator reacts (`white_check_mark` -> approve, `x` ->
reject) or the wall-clock timeout expires. Only the configured operator
user id can approve, a teammate accidentally clicking the green check
does nothing.

Verdicts map onto `ExecuteResult.reason`:

| Slack verdict          | Batman reason          |
|------------------------|------------------------|
| `approved`             | `ok`                   |
| `rejected`             | `rejected_by_operator` |
| `timeout`              | `approval_timeout`     |
| `transport-unavailable`| `approval_transport_down` |

### 4. execute(plan) -> ExecuteResult

Files one `agent:implement` child issue per `ChildIssue` in the target
repo. Each child carries both `agent:implement` and `agent:bundle:<slug>`
so Lucius will claim it and the bundle remains trackable.

Partial failures are tolerated: every target is attempted, the outcome
is recorded per-repo, and the result names which children landed and
which failed (`reason="partial"`). The operator can then re-file just
the failures.

### 5. report(plan, result)

Posts a follow-up Slack message naming every child URL that landed and
every repo that failed. Same channel as the plan; carries the bundle
slug so a thread search picks both messages up together.

## Worked example

Parent issue (filed by the operator in `your-org/your-product`):

```md
Title: Bundle: billing-v2 rollout
Labels: agent:large-feature

Bundle: billing-v2 rollout

Repos:
- your-org/your-backend
- your-org/your-frontend
- your-org/your-mobile

Children:
- backend: introduce BillingV2Service
- backend: migrate /api/v1/invoices
- frontend: pricing page rewrite
- mobile: settings screen v2

Done when:
- All children merged to main
- Tests green across all repos
```

Batman fires (cron, or manually with `alfred run batman`), reads the
issue, derives `billing-v2` as the bundle slug, and posts to
`#your-fleet-channel`:

```
batman, plan drafted for billing-v2 (4 child issue(s), 3 repo(s))

*Batman plan: `billing-v2`*
*Parent:* <https://github.com/your-org/your-product/issues/42|your-org/your-product#42> -- Bundle: billing-v2 rollout
*Affected repos:* your-org/your-backend, your-org/your-frontend, your-org/your-mobile

*Children to file:*
  - `your-org/your-backend` -- introduce BillingV2Service
  - `your-org/your-backend` -- migrate /api/v1/invoices
  - `your-org/your-frontend` -- pricing page rewrite
  - `your-org/your-mobile` -- settings screen v2

*Done when:*
- All children merged to main
- Tests green across all repos

React with :white_check_mark: to approve, :x: to reject.
```

Operator reacts with `:white_check_mark:`. Batman files four child
issues, each labelled `agent:implement` plus `agent:bundle:billing-v2`,
each linked back to `your-org/your-product#42`. Lucius picks them up
across the three repos on its next firings; no further Batman action is
needed until the operator wants to start a new bundle.

If the operator reacts with `:x:` instead, or if the 15-minute timeout
expires with no reaction, Batman halts and posts a report explaining
which path was taken. No child issues are filed.

## Configuration

All configuration is via environment variables (12-factor).

| Variable | Default | Purpose |
|----------|---------|---------|
| `BATMAN_AUTO_EXECUTE` | `0` | Controls the gate. Values: `0` (halt after plan, the safe default), `approval-gate` (require Slack approval), `1` (execute without a gate). |
| `BATMAN_PARENT_REPO` | (unset) | `owner/repo` Batman reads parent issues from. When unset, the legacy cross-repo bundle scan path runs instead. |
| `BATMAN_PICKER` | `oldest` | `oldest` (FIFO by `createdAt`) or `newest`. |
| `BATMAN_BUNDLE_SLUG_PREFIX` | empty | Optional prefix prepended to the derived slug. Useful when several teams share a Slack channel and want their bundles distinguishable. |
| `BATMAN_APPROVAL_TIMEOUT_S` | `900` | Wall-clock seconds the gate will wait for a reaction. |
| `BATMAN_SLACK_CHANNEL` | empty | Channel to post the plan and report to. When empty, falls back to the framework's default channel (`slack_format._home_channel`). |

The Slack approval gate also reads these (from `slack_approval`):

| Variable | Purpose |
|----------|---------|
| `ALFRED_OPERATOR_SLACK_USER_ID` | Required when `BATMAN_AUTO_EXECUTE=approval-gate`. Slack user id whose reactions count. |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write`, `reactions:read`. |

### `BATMAN_AUTO_EXECUTE` matrix

| Value | Plan posted? | Approval polled? | Children filed? |
|-------|--------------|------------------|-----------------|
| `0` (default) | yes | no | no, halt after plan |
| `approval-gate` | yes | yes, Slack reactions | only on `:white_check_mark:` |
| `1` | yes | no | yes, immediately |

The default (`0`) preserves the historical alfred-os behaviour: Batman
drafts a plan and stops. Operators who want autonomy opt into
`approval-gate` (recommended) or `1` (only when you trust the parent
issues already).

## Safety story

- Batman never edits code. The only writes are GitHub issue creates and
  Slack posts.
- Approval is a hard gate when `BATMAN_AUTO_EXECUTE=approval-gate`: if
  the Slack post cannot capture a `message_ts`, Batman halts rather
  than executing without a captured approval anchor.
- Only the configured operator's reaction counts. A teammate reacting
  with `:white_check_mark:` is ignored.
- Partial-execute failures do not crash. Every target is attempted,
  every outcome is recorded, the report names what landed and what
  failed.
- To abort mid-execute: kill the runner process. The next firing
  re-parses the parent issue from scratch; children that already landed
  are not re-filed (gh `issue create` is idempotent on title within a
  repo only by convention, not by the API, so verify the parent before
  re-running).

## Multi-product framing

The line between Batman and Lucius is the line between a coordinated
multi-repo feature and a single-repo unit of work:

- **Batman**: "Build billing-v2 across `your-backend`, `your-frontend`,
  and `your-mobile`." Multiple repos, multiple modules, multiple PRs,
  one shared bundle.
- **Drake -> Lucius**: "Add a Stripe-customer cache to `your-backend`."
  One repo, one PR, one issue.

If you find yourself writing a Batman parent issue with only one entry
under `Repos:`, the right shape is a Drake-filed `agent:implement`
issue, not a Batman bundle.

## Deferred / out of scope (for now)

- Multi-repo CI progress polling: Batman files children, but does not
  track their PRs. A future iteration could subscribe to
  `agent:bundle:<slug>` labels and surface a rollup.
- Post-bundle reflection: closing the parent issue automatically when
  every child is `agent:done` is not implemented; the operator does
  this.
- Cross-repo dependency ordering: every child is filed in declaration
  order, with no per-repo "wait until backend ships" gate. If you need
  that, file the dependent children manually after the upstream ones
  merge.

## See also

- `lib/batman.py`, the lifecycle implementation.
- `tests/test_batman_execute.py`, the canonical reference for what
  every reason code means.
- `docs/STATE_MACHINE.md` for the label transitions Batman participates
  in.
- `docs/MULTI_REPO_WORKED_EXAMPLE.md` for the end-to-end story from
  parent issue to merged children.
