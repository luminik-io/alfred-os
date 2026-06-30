# Threat model

Alfred runs coding agents against your repositories without you watching every
step. That is the whole point, and it is also the thing that has to be
contained. This page states plainly what each agent run can and cannot do, and
the boundaries that keep an autonomous run from becoming a problem.

For the private vulnerability-reporting process, see [`SECURITY.md`](../SECURITY.md).
For what leaves your machine, see
[Privacy](../README.md#privacy-what-alfred-touches-and-what-it-does-not).

## The trust model in one line

Alfred runs as you, on your machine, with the access you already have. It does
not add privilege. It adds **containment**: each run is isolated, bounded, and
ends at a gate you control.

## What a single run can do

One firing is one short-lived process working one task:

- It works in an **isolated git worktree**, a separate checkout of the repo.
  Other runs, and your own working copy, are untouched. A bad run cannot corrupt
  a sibling run or your main branch.
- It can read and write files **inside that worktree**, run the project's tools,
  and open a branch and a pull request through `gh`.
- It can post a summary to your Slack webhook if you configured one.
- It runs under a **hard spend cap** per agent per day. When a Claude-backed
  agent hits a provider limit, every other agent skips for an hour rather than
  hammering the limit.

## What a single run cannot do

- It **cannot merge its own work.** Alfred opens PRs and reviews them; a human
  merges. The `agent:in-flight` -> `agent:pr-open` -> `agent:done` lifecycle is
  explicit, and human merge is by design. Optional automerge only ever lands
  small PRs that match a policy you wrote, and you can leave it off.
- It **cannot touch a repo you did not add.** Alfred only operates on the repos
  listed in `$ALFRED_HOME/.env`. It does not discover or clone other repos.
- It **cannot escape the approval gate.** A locally drafted single-repo plan
  carries `agent:plan-pending-approval` and is held from autonomous pickup until
  you approve it and the label clears. A `do-not-pickup` override holds an issue
  no matter what.
- It **cannot quietly exceed its budget.** Spend is tracked and capped; an
  exhausted run stops rather than continuing.
- It **does not act outside its declared IAM scope.** When you follow the
  per-agent IAM guidance in [`AWS_SETUP.md`](AWS_SETUP.md), each agent has its own
  least-privilege identity, not your admin SSO.

## Boundaries that contain a run

| Boundary | What it stops |
|---|---|
| Isolated worktree per firing | One run corrupting another, or your main checkout |
| Single short-lived process per firing | A runaway loop persisting; the OS scheduler owns cadence |
| Hard spend cap per agent per day | Unbounded cost from a stuck run |
| Provider-limit backoff (one agent trips, all pause an hour) | Lockstep retries deepening a rate limit |
| Approval gate (`agent:plan-pending-approval`) | Autonomous pickup of un-approved single-repo plans |
| `do-not-pickup` operator override | Any agent claiming an issue you parked |
| Never auto-merge by default | Unaudited code reaching your main branch |
| Per-agent IAM (recommended) | An agent acting with operator-level cloud privilege |

## Inputs Alfred treats as untrusted

Alfred reads data from outside the machine, and that data is treated as
untrusted input, never as instructions to run blindly:

- **Slack message bodies.** Trusted control commands are codename-, plan-id-, and
  memory-id-validated, run no shell, and only steer local state. A follow-up
  reply after a PR link is captured as context, not as a merge approval.
- **GitHub API responses** (issue and PR bodies, labels, CI status).
- **Tool and command output** the run reads back.

Remote code execution from any of these sources is treated as **critical**. See
[`SECURITY.md`](../SECURITY.md) for the full critical/standard classification and
the private disclosure path.

## What is explicitly out of scope

- **The local CLIs themselves** (`@anthropic-ai/claude-code`, Codex). Report
  issues to their vendors.
- **Third-party skills** you choose to install. Skills are markdown plus scripts
  and run with the same permissions as `claude`. Read every skill before
  installing it; see [`SKILLS.md`](SKILLS.md).
- **Operator misconfiguration** (a leaked Slack webhook, a public AWS key). The
  framework documents the hardening but cannot enforce your secrets hygiene.
- **Reading your own files in your home directory.** The framework runs as you;
  this is by design, not a vulnerability.

## If you want to verify the privacy claim yourself

The privacy posture is meant to be inspectable, not taken on faith. Run a
network monitor (Little Snitch, `lsof -i`, a proxy) during a firing and confirm
the only outbound destinations are the model provider you chose, GitHub, your
Slack webhook, and the anonymous usage beacon at
`alfred-proof-telemetry.luminik.workers.dev/ingest`. That beacon is on by
default and sends aggregate counts only; turn it off with `alfred telemetry off`
if you do not want it. If you find a call we did not document, that is exactly
the kind of finding the [audit issue](../README.md#open-audit-issue) exists to
collect. One undocumented call is a bug, and we want to hear about it.
