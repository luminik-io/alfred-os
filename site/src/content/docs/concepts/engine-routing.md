---
title: Engine routing
description: How Alfred decides whether each codename runs through Claude Code, Codex, or a Claude-first hybrid with Codex fallback.
---

Alfred is the scheduler and guardrail layer. The actual LLM work is done by the engine: a local CLI you have already authenticated. The framework owns the per-codename decision of which engine that is. Default posture is local subscription auth; Alfred does not need Anthropic or OpenAI API keys for the normal Claude Code or Codex CLI flow.

This page covers the three modes, the precedence chain, the fallback behavior, the default routing matrix for the shipped fleet, and where the multi-engine roadmap is going. Full doc at [`docs/ENGINE_ROUTING.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/ENGINE_ROUTING.md).

## Three modes

| Mode | Behavior |
|---|---|
| `claude` | Use Claude Code only. No fallback. |
| `codex` | Use Codex only. No fallback. |
| `hybrid` | Use Claude Code first. Retry transient failures on the same engine, and fall back to Codex only when Claude ran but produced no useful result. Default for most codenames. |

`hybrid` is the default for builder agents because it gives them a second shot when Claude ran but produced no usable result, without hiding quota, auth, or transport faults behind another provider. Reviewer agents that are happy with either engine often run pure `codex` so they preserve Claude quota for builders.

## Per-agent overrides

The framework reads the engine for each firing from a precedence chain. The first source that returns a normalized mode wins.

1. `ALFRED_<CODENAME>_ENGINE` (e.g. `ALFRED_LUCIUS_ENGINE=claude`, `ALFRED_RASALGHUL_ENGINE=codex`).
2. `ALFRED_ENGINE` for fleet-wide testing (useful in `alfred-dry-run`).
3. `$ALFRED_HOME/state/engines/<codename>`, written by `alfred engine set`.
4. The codename's compiled-in default, usually `hybrid`.

Alfred CLI:

```sh
alfred engine status                 # one line per codename, resolved mode
alfred engine status lucius          # one codename, plus where the value came from
alfred engine set lucius hybrid      # persist to $ALFRED_HOME/state/engines/lucius
alfred engine set rasalghul codex
alfred codex status                  # check the Codex CLI is reachable
alfred codex probe                   # run one tiny non-interactive request
alfred auth status                   # auth-surface check across both engines
```

Set the env-var form in `~/.alfredrc` when you want the override to follow your shell. Set the state-file form when you want the override to follow the host scheduler (it survives a `deploy.sh` re-render).

## Hybrid fallback behavior

Hybrid mode tries Claude first. Every invocation outcome is classified before
Alfred decides what to do next:

- **TRANSIENT** (`error_rate_limit`, `error_overloaded`, `error_timeout`,
  `error_api`, connection resets, context overflow): retry the same engine with
  exponential backoff and jitter.
- **FATAL** (`error_authentication`, `error_budget`, 401/403/422): surface the
  failure honestly and do not burn the fallback.
- **CAPABILITY** (`error_max_turns`, parse failure, loop detection, or another
  no-useful-result failure): fall back to Codex because a different engine may
  handle the task better.

The fallback only fires on a capability gap. It does not hide auth, quota, or
transport faults behind a different provider.

When a Claude-backed firing returns `error_rate_limit` or `error_budget`, the runner also calls `set_global_block(hours=1, reason=...)`. That writes `$ALFRED_HOME/state/global-blocked-until.json`, which every other Claude-backed firing reads at the top of `main()`. They print `[<AGENT>-GLOBAL-BLOCKED]` and exit 0 for the next hour. The block stops the stampede; without it, the whole fleet would spend the hour firing into the same rate-limit wall.

All shipped agents check the global block before dispatch today, regardless of engine mode. The block is a fleet-wide pause, not a Claude-only router bypass.

## Default routing matrix

The shipped fleet has the following defaults. Override per codename when your account economics or quality posture call for it.

| Codename | Default mode | Why |
|---|---|---|
| **batman** | `hybrid` | Architect for cross-repo execution. Long-context planning prefers Claude; Codex fallback gives the architect lane a second model when Claude produced no useful plan. |
| **lucius** | `hybrid` | Builder. Wants Claude for first-class code generation, with Codex available only for capability gaps. |
| **drake** | `claude` | Planner. Cross-repo grep plus issue-filing benefits from Claude's longer effective context and tool integration. |
| **bane** | `hybrid` | Test-coverage builder. Same posture as Lucius; tests are valuable enough to fall back rather than skip. |
| **rasalghul** | `codex` | Reviewer. An independent reviewer on a different model surfaces blind spots the builder model shares. Also preserves Claude quota for builders. |
| **nightwing** | `hybrid` | Review-fix builder. Needs Claude for the same reasons as Lucius. |
| **robin** | `hybrid` | Bug triage. Light-touch; either engine works. |
| **huntress** | `claude` | Post-deploy smoke. Lower volume; Claude is fine. |
| **gordon** | `claude` | Deploy-health. Read-only; quiet on healthy days. |
| **automerge** | n/a | No engine call. |
| **agent-cleanup** | n/a | No engine call. |

These are starting points, not laws. If you have a Claude Max plan and abundant quota, push more codenames to pure `claude`. If you have OpenAI credits to burn and want a second opinion on every PR, push more reviewers to pure `codex`. The override surface is per-codename for exactly this reason.

## Subscription economics

Alfred's default posture is to use the local CLI subscription auth you have already paid for. It does not need API keys for normal operation.

- Claude Code with a Pro or Max plan: keep `ANTHROPIC_API_KEY` unset. Claude Code gives env-var API keys priority over subscription auth, which silently moves a firing onto API billing.
- Codex with a ChatGPT plan: sign in through the Codex CLI with your ChatGPT account. Keep `OPENAI_API_KEY` unset unless you intentionally want API-key billing.
- AWS: only used when an agent needs Secrets Manager, and only with per-agent IAM (see [AWS setup](/guides/aws/)).

The shipped fleet is designed to run on subscriptions you already have. No double billing. If you want to add API-key fallback for redundancy, set the env vars deliberately and document what you did in `~/.alfredrc`.

## Multi-engine roadmap

The current engine surface is two: Claude Code and Codex. The runtime contract is engine-agnostic. `AgentResult` carries `success`, `subtype`, `num_turns`, `cost_usd`, `session_id`, and `result_text` regardless of which engine produced it. Adding a third engine means writing a new `<engine>_invoke()` that returns the same shape.

On the roadmap:

- **Gemini CLI**: when Google ships a stable non-interactive `gemini -p` equivalent with a structured result. Useful as a third independent reviewer or as a hedge against Anthropic and OpenAI both being down at once.
- **Ollama and other local engines**: for teams that want every firing on-host with no provider call at all. Trade-off is model quality; reasonable for utility roles.
- **Anthropic native agents**: when the upstream Agent Teams or Memory Tool primitives stabilize, Alfred will lean on them rather than re-implementing them.

Each new engine needs three things to land: a CLI binary on PATH, a deterministic non-interactive prompt mode that returns structured results, and classifier coverage so retry, breaker, and fallback policy treat failures honestly.

## See also

- [Architecture](/concepts/architecture/): why the engine is a fresh subprocess per firing.
- [How it works](/concepts/how-it-works/): the firing trace including the engine call.
- [Claude Code and Codex](/guides/claude-code/): install, auth, Pro vs Max sizing, account swap.
- [State and memory](/concepts/state-and-memory/): the `engines/<codename>` state file.
- [Install](/getting-started/install/): first-run install flow.
