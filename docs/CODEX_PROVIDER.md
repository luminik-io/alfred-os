# Codex Provider

Codex is an optional Alfred engine adapter. Alfred does not require OpenAI API
keys for the normal local CLI path. It shells out to the authenticated `codex`
CLI, the same way Claude-backed agents shell out to `claude`.

Use this doc when you want Codex as a primary engine for a codename, as a
Claude-first fallback, or as an independent reviewer.

## Quick Checks

```sh
codex --version
alfred codex status
alfred codex probe
alfred auth status
```

`alfred codex probe` runs one tiny non-interactive `codex exec` request in a
read-only sandbox and writes artifacts to `/tmp/alfred-codex-probe-*`.

## Engine Modes

```sh
alfred engine status
alfred engine status rasalghul
alfred engine set rasalghul codex
alfred engine set lucius hybrid
alfred engine set lucius claude
```

| Mode | Behavior |
|---|---|
| `claude` | Claude Code only. |
| `codex` | Codex only. |
| `hybrid` | Claude Code first. Fall back to Codex on Claude `error_budget`, `error_rate_limit`, or `error_authentication`. |

Resolution order:

1. `ALFRED_<CODENAME>_ENGINE`
2. Optional legacy env var for migrated fleets
3. `ALFRED_ENGINE`
4. `$ALFRED_HOME/state/engines/<codename>`
5. Optional legacy state file
6. Codename default

## Runtime Contract

`lib/agent_runner.py` owns the adapter.

- Binary: `CODEX_BIN` or `codex` on PATH.
- Model: `CODEX_MODEL`, or Codex CLI default.
- Default sandbox: `read-only`.
- Default approval policy: `never`.
- Artifacts: `$ALFRED_HOME/state/codex/<agent>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}`.
- Unsupported Claude-only controls (`allowed_tools`, `max_turns`, `resume_session`) are rejected up front.

Write-capable agents must opt in deliberately:

- Run in a disposable git worktree.
- Pass a writable sandbox only for that codename.
- Use `bypass_approvals_and_sandbox=True` only when autonomous write or GitHub
  CLI access really needs it.
- Verify git state and PR creation after the Codex call, just as Claude-backed
  agents do.

Ra's al Ghul style review agents should stay read-only. Lucius/Bane/Drake style
builder agents can use Codex, but their runner should own the write boundary,
commit check, PR creation, and cleanup.

## Billing Posture

Alfred's default posture is local CLI auth, not direct API-key billing.

- For Claude Code, keep `ANTHROPIC_API_KEY` unset when you want Pro/Max
  subscription-backed usage.
- For Codex, sign in through the Codex CLI with your ChatGPT account when you
  want ChatGPT-plan usage.
- If `OPENAI_API_KEY` is set, make sure that is intentional for your Codex
  workflow.

References:

- Anthropic: [Use Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
- Anthropic: [Manage API key environment variables in Claude Code](https://support.claude.com/en/articles/12304248-managing-api-key-environment-variables-in-claude-code)
- OpenAI: [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-codex-in-chatgpt-faq)

## Troubleshooting

**`codex: command not found`.** Install/sign in to Codex CLI, rerun
`bash deploy.sh`, or set `CODEX_BIN=<absolute-path>` in `~/.alfredrc`.

**Hybrid never falls back.** Hybrid fallback is intentionally narrow. It only
falls back from Claude to Codex for auth, budget, and rate-limit subtypes. A
normal Claude tool error should be fixed in the runner or prompt, not hidden by
switching engines.

**Codex cannot read GitHub auth or credentials stored in the host credential store.** Keep
review-only agents in read-only mode. For builder agents, run in a disposable
worktree and opt into the bypass flag only at that codename's call site.

**Unexpected billing path.** Run `alfred auth status`, check provider env vars,
and confirm the active account in each CLI.
