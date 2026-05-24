# Claude Code

Alfred is the scheduler and guardrail layer; Claude Code is the default engine. This doc covers installation, Pro-vs-Max sizing, authentication, the multi-account swap pattern (`alfred claude`), and the optional Codex path.

Default billing posture: Alfred uses the local CLI account you have already authenticated. It does not need Anthropic or OpenAI API keys for the normal Claude Code / Codex CLI flow.

## What "Claude Code" is

The official Anthropic CLI for Claude. Two surfaces:

- `claude`: interactive REPL. Used once during install for auth.
- `claude -p '<prompt>'`: non-interactive single-prompt invocation. **What every agent uses.** Returns a structured JSON result the framework parses (turns, cost, success/failure subtype, session id for resume).

Alfred doesn't talk to the Anthropic API directly. It shells out to `claude`. The CLI handles model selection, billing, rate-limit signalling, retries, MCP plumbing. Re-implementing any of that is out of scope.

Keep `ANTHROPIC_API_KEY` unset for subscription-backed Claude Code runs. Claude Code gives environment-variable API keys priority over Pro/Max subscription auth, which can move a firing onto API billing.

## Claude accounts vs engine routing

There are two separate switches:

- **Claude account routing**: `alfred claude primary|secondary|swap` chooses which local Claude Code auth directory future scheduled firings use. This is account/quota routing for Claude only.
- **Agent engine routing**: `alfred engine set <codename> <claude|codex|hybrid>` chooses whether a codename runs through Claude Code, Codex, or Claude-first hybrid fallback.

Engine modes:

| Mode | Behavior |
|---|---|
| `claude` | Use Claude Code only. |
| `codex` | Use Codex only. |
| `hybrid` | Use Claude Code first. Fall back to Codex when Claude returns `error_budget`, `error_rate_limit`, or `error_authentication`. |

```sh
alfred engine status
alfred engine status lucius
alfred engine set rasalghul codex
alfred engine set lucius hybrid
alfred codex status
alfred codex probe
alfred auth status
```

Resolution order for one codename:

1. `ALFRED_<CODENAME>_ENGINE`
2. Optional legacy env var for migrated fleets
3. Fleet-wide `ALFRED_ENGINE`, useful for testing
4. `$ALFRED_HOME/state/engines/<codename>`
5. Optional legacy state file
6. The codename's default, usually `hybrid`

## Optional Codex routing

If the `codex` CLI is installed and authenticated, agents can route work through `codex_invoke()` directly or through the per-agent engine router above. Authenticate Codex with your ChatGPT account if you want ChatGPT-plan usage rather than a manual API-key flow. OpenAI controls Codex plan availability, credits, and workspace settings, so use `alfred codex status/probe` plus the official Codex help page as the source of truth for a given account. The default Alfred posture is review-safe. Write-capable agents must deliberately opt into writable worktrees and, when needed for autonomous `gh` or keychain access, the Codex bypass flag:

- Default sandbox: `read-only`
- Default approval policy: `never`
- Artifacts: `$ALFRED_HOME/state/codex/<agent>/<YYYY-MM>/<firing-id>.{last.md,stdout.txt,stderr.txt}`
- Unsupported Claude-only controls (`allowed_tools`, `max_turns`, `resume_session`) are rejected up front.
- Builder agents that use Codex should run in a disposable worktree and pass
  `bypass_approvals_and_sandbox=True` only when they need autonomous write or
  GitHub CLI access.

Environment overrides:

```sh
CODEX_BIN=$HOME/.local/bin/codex
CODEX_MODEL=<your-model-id>
CODEX_SANDBOX=read-only
CODEX_APPROVAL_POLICY=never
```

`deploy.sh` links an interactive-shell `codex` binary into `~/.local/bin/codex` when one exists. Rendered scheduler units include `~/.local/bin` in PATH, so Codex can stay optional without pinning app-bundle paths into agent config.

Use Codex alongside Claude when you want an independent reviewer or when Claude quota is scarce. Keep feature-writing agents on Claude or `hybrid` until you have deliberately designed the Codex write path, commit verification, and PR creation boundary for that codename.

References:

- Anthropic: [Use Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
- Anthropic: [Manage API key environment variables in Claude Code](https://support.claude.com/en/articles/12304248-managing-api-key-environment-variables-in-claude-code)
- OpenAI: [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-codex-in-chatgpt-faq)

## Install

```sh
npm install -g @anthropic-ai/claude-code
```

`install.sh` does this for you. To confirm:

```sh
claude --version
```

For non-npm install paths (Linux package, devcontainer), see https://docs.claude.com/en/docs/claude-code.

## Authenticate

Run `claude` once interactively:

```sh
claude
```

Opens a browser tab against your Anthropic account. Approve, return to terminal, hit any key. The auth blob is stored at `~/.claude/`. Subsequent `claude -p '...'` calls use the cached auth. No re-login until the token expires (rare).

From a fresh terminal, verify:

```sh
echo "say hi" | claude -p
```

Should print a one-line response and exit 0.

### Authenticating scheduled (launchd / systemd) firings

The interactive auth above stores the OAuth token in your platform's credential store: macOS Keychain on Darwin, libsecret on Linux. That works from your shell because the shell session can read those stores. **It does not work from launchd or `systemd --user`-spawned agent processes** — those run in a different security context and cannot read the same credential, so every `claude -p` call returns 401 even though the same token is on disk.

The supported fix is a long-lived OAuth token that `claude` reads from an env var, bypassing the credential store entirely. Run once interactively:

```sh
claude setup-token
```

Approve in the browser, copy the printed token. Add it to `~/.alfredrc` (which `agent-launch` sources on every firing):

```sh
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

Tighten permissions so other accounts on the host cannot read it:

```sh
chmod 600 ~/.alfredrc
```

The token is valid for 1 year, ties directly to your subscription (no extra cost, no API-key billing), and is what `claude` reads first when both an env var and a Keychain entry exist. Rotate by re-running `claude setup-token` and overwriting the line. Revoke via your [Anthropic account settings](https://console.anthropic.com/settings/keys) if the file is ever exposed. The same env var works on Linux for the same reason — host credential stores and user-service contexts often disagree, the env var sidesteps both.

If you prefer not to use the env var (for example, your organisation forbids long-lived subscription tokens), you can leave `claude` reading the credential store and accept that scheduled firings will not authenticate.

## Pro vs Max sizing

Claude Code can run against your **subscription usage** rather than direct API token billing when you log in with a Pro or Max account and avoid API-key env vars. Usage is shared with other Claude surfaces and reset behavior is controlled by Anthropic, so treat the table as sizing guidance, not a billing guarantee:

| Tier | Use case |
|---|---|
| Pro | One operator, occasional agent runs, manual code work in parallel |
| Max 5x / 20x | Continuous fleet, multiple codename agents on frequent cadences |

A "turn" is roughly one model response. A typical Lucius firing on a small backend issue burns 30-80 turns. A multi-file refactor can hit 150+. Empirically, Lucius alone running every 20 minutes against an active issue queue averages 2000-3500 turns/day. Add Bane (test coverage), Drake (planner), Ra's (review), Nightwing (review-fix), and you exceed Pro quota in a day.

Recommendation: start on Pro to validate the framework, upgrade to Max once you've got more than 2 codenames firing daily. The `alfred claude` swap pattern below also lets you separate work across two accounts when you operate that way.

When the provider usage cap trips mid-firing, the framework treats it as a fleet-wide event. `set_global_block(hours=1, reason="...")` poisons the run-permission file at `$ALFRED_HOME/state/global-blocked-until.json`. Every other agent's first preflight check sees the block and exits silently. After an hour, the block expires and the fleet resumes.

## The `alfred claude` swap pattern

Two Anthropic accounts? `alfred claude` points the host-scheduled `claude` at either one without re-authenticating each time.

The mechanism: scheduled agents honor `CLAUDE_CONFIG_DIR`. On macOS,
`alfred claude` writes the launchd global env var. On Linux, it writes the
`systemd --user` manager environment with `systemctl --user set-environment`.
Primary is explicit so older `~/.claude.json` files cannot accidentally win
Claude Code's default profile lookup.

```sh
alfred claude status      # which account is active right now
alfred claude primary     # set CLAUDE_CONFIG_DIR=~/.claude
alfred claude secondary   # set CLAUDE_CONFIG_DIR=~/.claude-secondary
alfred claude swap        # toggle
alfred claude probe       # run a tiny real auth check
```

Typical usage: run on `primary` until it hits a usage cap or auth issue (Slack alert from `set_global_block`), `alfred claude swap`, fleet resumes on `secondary`'s quota.

On Linux, already-running services keep their current environment. Restart the
affected timer or service after switching:

```sh
alfred claude secondary
systemctl --user restart my.fleet.lucius.timer
```

To populate the secondary config, log in once with `CLAUDE_CONFIG_DIR` pointed
at the secondary directory:

```sh
mkdir -p ~/.claude-secondary
CLAUDE_CONFIG_DIR=$HOME/.claude-secondary claude
alfred claude secondary
```

## CLAUDE_BIN env var

If `claude` isn't on the PATH that the host scheduler inherits (common when `npm` install puts it under `~/.local/share/fnm/aliases/.../bin`), set the absolute path in `~/.alfredrc`. Prefer a stable symlink such as `$HOME/.local/bin/claude` over an fnm-managed path:

```sh
CLAUDE_BIN=$HOME/.local/bin/claude
```

`deploy.sh` links the interactive-shell `claude` binary into `~/.local/bin/claude` when one exists, mirroring the Codex setup, so rendered scheduler units pick it up via `~/.local/bin` on PATH. The framework's `claude_invoke()` uses `CLAUDE_BIN` if set, otherwise `claude` from PATH. Get the right path:

```sh
which claude
# → /Users/you/.local/bin/claude (if symlinked) or .../fnm/aliases/.../bin/claude
```

## Cost vs token-API mental model

Under the default subscription-backed path, Alfred does not add token-metered charges by itself. It consumes the same Claude Code usage pool your terminal sessions consume.

Two caveats matter:

1. If `ANTHROPIC_API_KEY` is present, Claude Code can use API billing instead of subscription auth.
2. Anthropic usage credits can let paid-plan users continue after included usage limits at standard API pricing if they choose to enable and use that path.

Per-day spend caps in `SpendState` are safety rails against runaway loops (e.g. a prompt accidentally entering a 500-turn while-loop), not authoritative provider billing records.

If you need token-level billing (Bedrock, direct Anthropic API), override `CLAUDE_BIN` to a wrapper script that translates `claude -p` invocation into your API of choice. Out of scope for the default install.

## Skills

Claude Code supports installable skills (small markdown + script bundles that extend the model's tool surface). The fleet uses several: code review, gstack, security checks. See [`docs/SKILLS.md`](SKILLS.md) for the recommended set and install commands.

## Troubleshooting

**`claude: command not found` from a scheduled agent.**
The scheduler unit's PATH doesn't include the npm global bin. Set `CLAUDE_BIN` in `~/.alfredrc`, or symlink `claude` to `/usr/local/bin/` or `~/.local/bin`.

**`codex: command not found` from a scheduled agent.**
Run `deploy.sh` again after installing Codex, or set `CODEX_BIN=<absolute-path>` in `~/.alfredrc`. Prefer a stable symlink such as `$HOME/.local/bin/codex` over an app-bundle path.

**`error_rate_limit` immediately on every firing.**
You've hit a provider usage limit. `cat $ALFRED_HOME/state/global-blocked-until.json` shows Alfred's local cool-down. Either wait for the provider reset, swap to a second account via `alfred claude swap`, upgrade, or intentionally use provider-approved usage credits if that is your billing choice.

**Unexpected API charges.**
Check whether `ANTHROPIC_API_KEY` is set in your shell, scheduler environment, or `~/.alfredrc`. For subscription-backed runs, unset it and re-run `claude /status` interactively to confirm the active account. If Codex is unexpectedly using API billing, check whether `OPENAI_API_KEY` is set or whether Codex was logged in through an API-key flow instead of ChatGPT-plan auth.

**`error_max_turns` on every firing of one agent.**
That agent's max-turns budget is too tight for the work. Either widen the budget in the stable role runner (look for `max_turns=` in files such as `bin/lucius.py`), or scope-cap the issues that agent picks up.

**`session_id` resume doesn't work.**
The framework writes `last_session_id_per_target` into the agent's spend file (`$ALFRED_HOME/state/<agent>/spend-YYYY-MM-DD.json`). If this is empty, resume isn't being attempted. Check the agent's prompt; it should pass `--resume <session_id>` when re-firing on the same issue after a max-turns event.

**Different model than expected.**
`claude -p` uses whatever model your account defaults to. To pin a model per agent, pass `--model claude-opus-4-7` (or your target) in the agent's `claude_invoke_streaming()` call. Don't pin it framework-wide. Different agents have different cost/quality tradeoffs.
