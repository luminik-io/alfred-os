# Batman: the architect agent

Batman is Alfred's OSS architect agent for features that span more than one
repository. It reads a single parent issue, drafts the rollout, posts the plan
to Slack or the local client for operator approval, files scoped child issues
across the named repos, and posts a follow-up report naming the child issues it
filed.

Use Batman when the change spans multiple repos or packages and needs one
accountable agent above the repo-local work. For a feature that fits in one
repo, the right shape is Drake scoping the issue and Lucius implementing it.

## When to reach for Batman

- Building a `billing-v2` launch across backend, frontend, and mobile.
- Splitting a `payments-v3` rollout across two backend repos and a
  shared client SDK.
- Coordinating a `data-schema-v2` migration across a producer service,
  a consumer service, and a data pipeline.

The common shape: one parent issue, multiple downstream
repos, child scopes that can be worked in parallel once approved. Batman owns
that plan and fan-out boundary; Lucius, Bane, Nightwing, reviewers, and the
merge gate own the resulting PRs.

## When to use Lucius or Drake instead

- Single-repo features ("add a settings dropdown"). That is Drake then
  Lucius.
- Cross-cutting refactors where the children would all be in the same
  repo. That is also Drake then Lucius (or just Lucius if Drake already
  filed it).
- Anything where a human already wrote the scoping doc and only needs one repo
  changed. Lucius can take that directly.

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

Operators should treat that Slack thread as the working room for the plan:
ask Batman to tighten scope, add acceptance criteria, remove a repo, or
split the bundle before approving. Teammates can use structured commands or
plain English: "make the mobile part read-only", "do not touch billing yet",
"add empty states", or "turn this into two smaller issues" is enough signal.
Trusted feedback users can amend the plan without approval authority. Alfred
replies with the execution scope if approved now. When the configured operator
approves, Alfred reads the thread and appends those replies to every child
issue as explicit amendments. Repo add/remove replies also amend Batman's
execution scope before child issues are filed. Explicit `question:` replies
keep execution paused until the plan is resolved.

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

Trusted replies on report or PR threads are captured during the configured
report-feedback window and saved under `$ALFRED_HOME/state/followups/` as
follow-up context. `change:`, `fix:`, `test:`, and plain-language notes become
action items for the next pass. `question:`, `hold:`, `blocker:`, and scope
changes require a decision before more work starts. These replies never approve,
merge, or change code by themselves.

## Parent issue body template

Batman accepts two body shapes. Pick whichever feels natural; the
parser tries the canonical shape first and falls back to the loose
shape automatically (a warning lands in `/tmp/alfred.batman.stderr`
when the fallback fires, so you know to tighten up next time). If the
loose shape is present but Alfred would still have to guess a default
rollout, the plan is marked with a blocking readiness finding instead
of synthesizing backend/frontend/mobile work.

**Canonical shape** (matches the worked example below; explicit
`Repos:` and `Children:` blocks):

```md
Title: Bundle: billing-v2 rollout
Labels: agent:large-feature

Bundle: billing-v2 rollout

Repos:
- your-org/your-backend
- your-org/your-frontend
- your-org/your-mobile

Children:
- your-backend: introduce BillingV2Service
- your-backend: migrate /api/v1/invoices
- your-frontend: pricing page rewrite
- your-mobile: settings screen v2

Done when:
- All children merged to main
- Tests green across all repos
```

**Loose shape** (markdown sections, what an operator or AI assistant
naturally types from a prose feature description):

```md
## Affected Repos
- your-backend
- your-frontend
- your-mobile

## Rollout order
- your-backend
- your-frontend
- your-mobile

## Acceptance Criteria

### your-backend
- New `/billing/...` endpoints behind the `billing-v2` feature flag.

### your-frontend
- Billing settings page wired to the v2 endpoints.

### your-mobile
- Subscription paywall reads from the v2 schema.
```

When the loose shape fires, Batman synthesizes one child issue per
affected repo with the title `<repo>: implement <slug>` and the
per-repo acceptance-criteria block as the seed body. The plan post
to Slack uses the same approval contract. Authoring the canonical
shape gives you finer control over child titles, but the loose
shape is enough to get a working fan-out.

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
- your-backend: introduce BillingV2Service
- your-backend: migrate /api/v1/invoices
- your-frontend: pricing page rewrite
- your-mobile: settings screen v2

Done when:
- All children merged to main
- Tests green across all repos
```

Batman fires (cron, or manually with `alfred run batman`), reads the
issue, derives `billing-v2` as the bundle slug, and posts to
`#your-fleet-channel`:

```
batman, plan drafted for billing-v2 (4 child issue(s), 3 repo(s))

*Alfred plan ready* · `billing-v2`
*Parent:* <https://github.com/your-org/your-product/issues/42|your-org/your-product#42>
*Work:* Bundle: billing-v2 rollout
*Readiness:* ready for approval
*Next step:* reply in this thread to steer the plan, or approve only if it is right.
*Replies Alfred understands:* `change:`, `acceptance:`, `test:`, `add repo:`, `remove repo:`, `question:`, `open questions: none`
*Approval gate:* :white_check_mark: starts this exact scope; :x: stops it.

*Scope if approved now:* 3 repos, 4 child issues
  - `your-org/your-backend`: introduce BillingV2Service
  - `your-org/your-backend`: migrate /api/v1/invoices
  - `your-org/your-frontend`: pricing page rewrite
  - `your-org/your-mobile`: settings screen v2

*Done when:*
- All children merged to main
- Tests green across all repos

No child issues are filed until this plan is approved.
```

The configured approver reacts with `:white_check_mark:`. Batman files four child
issues, each labelled `agent:implement` plus `agent:bundle:billing-v2`,
each linked back to `your-org/your-product#42`. Lucius picks them up
across the three repos on its next firings; no further Batman action is
needed until the configured approver wants to start a new bundle.

If the configured approver reacts with `:x:` instead, or if the approval timeout
expires with no reaction, Batman halts and posts a report explaining
which path was taken. No child issues are filed.

## Configuration

All configuration is via environment variables (12-factor).

For a guided first setup, run:

```sh
python3 bin/alfred-batman-setup.py
```

The wizard checks or writes the Claude OAuth token, Slack bot token,
operator Slack member id, approval channel, parent repo, picker, and
approval timeout in one idempotent block in `~/.alfredrc`. It finishes
with `bin/doctor.sh --lifecycle` unless `--skip-doctor` is passed.

The same flow is also available through the Alfred CLI after deploy:

```sh
alfred batman setup
alfred batman setup --mode approval-gate --approval-mode file
alfred setup-batman --check-only
```

If `BATMAN_PARENT_REPO` is a specs or planning repo outside the repos passed to
`alfred-init.py --repos`, bootstrap Alfred's labels there before filing parent
issues:

```sh
alfred labels bootstrap my-org/specs
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `BATMAN_AUTO_EXECUTE` | `0` | Controls the gate. Values: `0` (halt after plan, the safe default), `approval-gate` (require approval), `1` (execute without a gate). |
| `BATMAN_PARENT_REPO` | (unset) | `owner/repo` Batman reads parent issues from. When unset, Batman exits cleanly without querying GitHub. |
| `BATMAN_PICKER` | `oldest` | `oldest` (FIFO by `createdAt`) or `newest`. |
| `BATMAN_BUNDLE_SLUG_PREFIX` | empty | Optional prefix prepended to the derived slug. Useful when several teams share a Slack channel and want their bundles distinguishable. |
| `BATMAN_APPROVAL_TIMEOUT_S` | `86400` | Wall-clock seconds the gate will wait for a reaction. |
| `BATMAN_APPROVAL_MODE` | `slack-or-file` | Approval surface when `BATMAN_AUTO_EXECUTE=approval-gate`. Values: `slack-or-file` (Slack reactions plus Alfred client approve/decline), `slack` (Slack only), `file` (Alfred client/file marker only). |
| `BATMAN_REPORT_FEEDBACK_TIMEOUT_S` | `60` | Seconds Batman waits after posting a report so trusted Slack replies can be captured as follow-up context. Set `0` to skip the wait. |
| `BATMAN_SLACK_CHANNEL` | empty | Channel to post the plan and report to. When empty, falls back to the framework's default channel (`slack_format._home_channel`). |

The Slack approval gate also reads these (from `slack_approval`):

| Variable | Purpose |
|----------|---------|
| `ALFRED_OPERATOR_SLACK_USER_ID` | Required when `BATMAN_AUTO_EXECUTE=approval-gate`. Slack user id whose reactions count. |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write`, `reactions:read`, and `channels:history` or `groups:history` when thread feedback should be captured. |

### `BATMAN_AUTO_EXECUTE` matrix

| Value | Plan posted? | Approval polled? | Children filed? |
|-------|--------------|------------------|-----------------|
| `0` (default) | yes | no | no, halt after plan |
| `approval-gate` | yes | yes, according to `BATMAN_APPROVAL_MODE` | only after approval |
| `1` | yes | no | yes, immediately |

Fresh installs default to `0`, so Batman drafts the rollout and stops after the
plan. Switch to `approval-gate` when you are ready for Batman to wait on the
configured approval surface and then file child issues. `1` files immediately
and should be reserved for trusted parent issues.

## Safety story

- Batman's multi-repo fan-out is public OSS code, not an internal-only path.
  Batman owns the architect lane: it turns an approved `agent:large-feature`
  parent into scoped child `agent:implement` issues across repos. Lucius, Bane,
  Nightwing, reviewers, and your merge gate then carry those child issues to
  PRs in isolated worktrees.
- Approval is a hard gate when `BATMAN_AUTO_EXECUTE=approval-gate`: if
  Slack mode cannot capture a `message_ts`, Batman halts rather than
  executing without a captured approval anchor. In `file` mode, Batman
  waits for the Alfred client marker instead.
- Only the configured operator's reaction counts. A teammate reacting
  with `:white_check_mark:` is ignored.
- Partial-execute failures do not crash. Every target is attempted,
  every outcome is recorded, the report names what landed and what
  failed.
- Poorly scoped parent issues should halt at the plan stage. If Batman
  cannot parse repos and children, the plan shows no children and execute
  returns `no_children` rather than letting Lucius build from vague prose.
  Add `Repos:`, `Children:`, and `Done when:` before approving.
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
