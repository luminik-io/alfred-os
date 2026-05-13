# Claude Code

Alfred runs most agents as a `claude -p` subprocess. The framework is the harness, Claude Code is the default brain. This doc covers installation, Pro-vs-Max sizing, authentication, the multi-account swap pattern (`alfred claude`), and the optional Codex path.

## What "Claude Code" is

The official Anthropic CLI for Claude. Two surfaces:

- `claude`: interactive REPL. Used once during install for auth.
- `claude -p '<prompt>'`: non-interactive single-prompt invocation. **What every agent uses.** Returns a structured JSON result the framework parses (turns, cost, success/failure subtype, session id for resume).

Alfred doesn't talk to the Anthropic API directly. It shells out to `claude`. The CLI handles model selection, billing, rate-limit signalling, retries, MCP plumbing. Re-implementing any of that is out of scope.

## Optional Codex routing

If the `codex` CLI is installed and authenticated, agents can route a task through `route_llm("codex", ...)` or call `codex_invoke()` directly. The default posture is review-safe. Write-capable agents must deliberately opt into writable worktrees and, when needed for autonomous `gh` or keychain access, the Codex bypass flag:

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

`deploy.sh` links an interactive-shell `codex` binary into `~/.local/bin/codex` when one exists. Rendered launchd plists include `~/.local/bin` in PATH, so Codex can stay optional without pinning app-bundle paths into agent config.

Use Codex alongside Claude when you want an independent reviewer or when Claude quota is scarce. Keep feature-writing agents on Claude until you have deliberately designed the Codex write path, commit verification, and PR creation boundary for that codename.

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

## Pro vs Max sizing

Claude Code is metered against your **subscription quota**, not API tokens. Two tiers:

| Tier | Approx weekly turns | Use case |
|---|---|---|
| Pro ($20/mo) | ~1500 | One operator, occasional agent runs, manual code work in parallel |
| Max ($100/mo or $200/mo) | ~5000-10000+ | Continuous fleet, 6+ codename agents on 20-min cadences |

A "turn" is roughly one model response. A typical Lucius firing on a small backend issue burns 30-80 turns. A multi-file refactor can hit 150+. Empirically, Lucius alone running every 20 minutes against an active issue queue averages 2000-3500 turns/day. Add Bane (test coverage), Drake (planner), Ra's (review), Nightwing (review-fix), and you exceed Pro quota in a day.

Recommendation: start on Pro to validate the framework, upgrade to Max once you've got more than 2 codenames firing daily. The `alfred claude` swap pattern below also lets you split spend across two accounts.

When the subscription cap trips mid-firing, the framework treats it as a fleet-wide event. `set_global_block(hours=1, reason="...")` poisons the run-permission file at `$ALFRED_HOME/state/global-blocked-until.json`. Every other agent's first preflight check sees the block and exits silently. After an hour, the block expires and the fleet resumes.

## The `alfred claude` swap pattern

Two Anthropic accounts? `alfred claude` points the launchd-spawned `claude` at either one without re-authenticating each time.

The mechanism: launchd-spawned agents honor `CLAUDE_CONFIG_DIR`.
`alfred claude` flips the launchd global env var between the primary
`~/.claude/` directory and `~/.claude-secondary/`. Primary is explicit so
older `~/.claude.json` files cannot accidentally win Claude Code's default
profile lookup.

```sh
alfred claude status      # which account is active right now
alfred claude primary     # set CLAUDE_CONFIG_DIR=~/.claude
alfred claude secondary   # set CLAUDE_CONFIG_DIR=~/.claude-secondary
alfred claude swap        # toggle
alfred claude probe       # run a tiny real auth check
```

Typical usage: run on `primary` until it hits the weekly cap (Slack alert from `set_global_block`), `alfred claude swap`, fleet resumes on `secondary`'s quota.

To populate the secondary config, log in once with `CLAUDE_CONFIG_DIR` pointed
at the secondary directory:

```sh
mkdir -p ~/.claude-secondary
CLAUDE_CONFIG_DIR=$HOME/.claude-secondary claude
alfred claude secondary
```

## CLAUDE_BIN env var

If `claude` isn't on the PATH that launchd inherits (common when `npm` install puts it under `~/.local/share/fnm/aliases/.../bin`), set the absolute path in `~/.alfredrc`. Prefer a stable symlink such as `$HOME/.local/bin/claude` over an fnm-managed path:

```sh
CLAUDE_BIN=$HOME/.local/bin/claude
```

`deploy.sh` links the interactive-shell `claude` binary into `~/.local/bin/claude` when one exists, mirroring the Codex setup, so launchd-rendered plists pick it up via `~/.local/bin` on PATH. The framework's `claude_invoke()` uses `CLAUDE_BIN` if set, otherwise `claude` from PATH. Get the right path:

```sh
which claude
# → /Users/you/.local/bin/claude (if symlinked) or .../fnm/aliases/.../bin/claude
```

## Cost vs token-API mental model

A Max-subscription fleet shipping 10-20 PRs a day costs $100/mo flat. Same as if you only used Claude Code interactively for 1 hour a day.

The subscription model does not pass through token costs. The fleet is bounded by the weekly turn quota, not USD-per-token. Per-day spend caps in `SpendState` are safety rails against runaway loops (e.g. a prompt accidentally entering a 500-turn while-loop), not bill-tracking. There is no incremental bill.

If you need token-level billing (Bedrock, direct Anthropic API), override `CLAUDE_BIN` to a wrapper script that translates `claude -p` invocation into your API of choice. Out of scope for the default install.

## Skills

Claude Code supports installable skills (small markdown + script bundles that extend the model's tool surface). The fleet uses several: code review, gstack, security checks. See [`docs/SKILLS.md`](SKILLS.md) for the recommended set and install commands.

## Troubleshooting

**`claude: command not found` from a launchd-spawned agent.**
The plist's PATH doesn't include the npm global bin. Set `CLAUDE_BIN` in `~/.alfredrc` (sourced by launchd via the agent's environment), or symlink `claude` to `/usr/local/bin/`.

**`codex: command not found` from a launchd-spawned agent.**
Run `deploy.sh` again after installing Codex, or set `CODEX_BIN=<absolute-path>` in `~/.alfredrc`. Prefer a stable symlink such as `$HOME/.local/bin/codex` over an app-bundle path.

**`error_rate_limit` immediately on every firing.**
You've blown the weekly cap. `cat $ALFRED_HOME/state/global-blocked-until.json` shows when it expires. Either wait, swap to a second account via `alfred claude swap`, or upgrade to Max.

**`error_max_turns` on every firing of one agent.**
That agent's max-turns budget is too tight for the work. Either widen the budget in the stable role runner (look for `max_turns=` in files such as `bin/lucius.py`), or scope-cap the issues that agent picks up.

**`session_id` resume doesn't work.**
The framework writes `last_session_id_per_target` into the agent's spend file (`$ALFRED_HOME/state/<agent>/spend-YYYY-MM-DD.json`). If this is empty, resume isn't being attempted. Check the agent's prompt; it should pass `--resume <session_id>` when re-firing on the same issue after a max-turns event.

**Different model than expected.**
`claude -p` uses whatever model your account defaults to. To pin a model per agent, pass `--model claude-opus-4-7` (or your target) in the agent's `claude_invoke_streaming()` call. Don't pin it framework-wide. Different agents have different cost/quality tradeoffs.
