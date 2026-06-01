# Alfred goals

Alfred should treat substantial work as a durable goal, not as a loose prompt.
The goal is the operator-owned contract that says what must become true, how
Alfred should prove it, which boundaries stay intact, and when Alfred must stop
for human input.

## Product shape

Slack remains the primary interface. A person should be able to start or refine
a goal in natural language inside a thread:

- "Make onboarding work end to end for my repo."
- "Do not implement yet. Ask me questions until the spec is clear."
- "Keep going until the tests pass, but do not touch billing."
- "Pause this goal."

The native client is the local control surface for the same goal state. It should
show active goals, blocked goals, evidence, plans, runs, memory used, and safe
local actions without pushing the operator into a browser for local Alfred state.
The CLI remains the portable substrate:

```sh
alfred goal create
alfred goal status
alfred goal pause
alfred goal resume
alfred goal clear
```

Engine-specific goal modes are useful execution hints, but Alfred should not make
them the source of truth. The Alfred goal ledger needs to be shared by Slack, the
native client, the CLI, the planner, the evaluator, and memory. When an engine
supports a native goal contract, Alfred can pass a tightened version into that
engine; Alfred still owns the Slack thread, operator gates, evidence ledger, and
blocked/completed lifecycle.

## Goal contract

A goal should contain:

- **Outcome:** what must be true when Alfred is done.
- **Verification:** tests, screenshots, check output, files, PR state, or other
  evidence that proves completion.
- **Constraints:** repos, files, tools, budgets, safety limits, and non-goals.
- **Iteration policy:** how Alfred chooses the next step after each failed check.
- **Human gates:** when Alfred must ask before implementing, merging, spending,
  deleting, or widening scope.
- **Blocked condition:** what evidence proves Alfred cannot continue responsibly.

## Lifecycle

```mermaid
stateDiagram-v2
  [*] --> Draft
  Draft --> Clarifying: readiness gate fails
  Clarifying --> Draft: operator answers
  Draft --> Active: approved or safe to start
  Active --> Evaluating: turn finishes
  Evaluating --> Active: evidence says not done
  Evaluating --> Achieved: evidence proves done
  Evaluating --> Blocked: stop condition met
  Active --> Paused: operator pauses
  Paused --> Active: operator resumes
  Draft --> Cleared
  Active --> Cleared
  Blocked --> Active: operator unblocks
```

## Runtime responsibilities

- **Slack listener:** maps thread replies, mentions, and reactions into goal
  events. It should preserve natural conversation, not force command syntax.
- **Planner:** turns vague work into a spec, asks blocking questions, and refuses
  implementation while readiness is low.
- **Executor:** runs the chosen engine and records attempts, evidence, and state.
- **Evaluator:** checks the goal contract against surfaced evidence after each
  attempt. The worker should not be the only judge of completion.
- **Memory layer:** recalls relevant lessons at goal start and proposes new
  memories when a goal exposes repeatable lessons.
- **Client and CLI:** expose the same state and safe actions. The client can be
  absent; Slack plus CLI must still work.

## Design implications

- The native client should favor in-app inspectors, queues, and action panes over
  links to local `alfred serve` pages.
- The Home view should summarize decisions, not list every raw event.
- Plans, runs, and memory candidates should be inspectable in-place.
- External links should be explicit: GitHub, Slack, docs, or browser-only
  resources.
- A future Goals tab should be a goal inbox and evidence inspector, not another
  dashboard dump.
