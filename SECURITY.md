# Security

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.** Report privately so we can patch before disclosure.

Preferred channel: open a [private security advisory](https://github.com/luminik-io/alfred-os/security/advisories/new) on this repository. Backup channel: email the maintainer listed in `pyproject.toml` `[project] authors` field.

We aim to acknowledge within 72 hours and ship a patch (or document the trade-off) within 14 days for critical / high severity issues.

## Scope

In scope:

- `lib/agent_runner.py` — the framework primitives every consumer agent imports.
- `bin/` — the operator-facing helpers.
- `examples/bin/label_state.py` — operator CLI for the state machine.
- `examples/git-hooks/pre-push` — pre-push hook installed in consumer repos.
- `install.sh` — fresh-machine bootstrap.
- The Astro Starlight site at `site/` — content + build config.

Out of scope:

- The Anthropic Claude Code CLI itself (`@anthropic-ai/claude-code`) — report to Anthropic.
- Third-party skills (`gstack`, CodeRabbit, etc.) — report upstream.
- Consumer fleet code that imports `agent_runner` — that's the consumer's responsibility.
- Operator misconfigurations (leaked AWS keys, public Slack webhooks, etc.). We document hardening in `docs/AWS_SETUP.md` and `docs/SLACK_SETUP.md` but cannot enforce.

## What we treat as critical

- Remote code execution from a Slack message body, gh API response, or any data the agent runner reads from an external source.
- Privilege escalation that lets a per-agent IAM identity act outside its declared policy.
- Secret leakage paths (e.g. a code path that posts an AWS Secrets value to Slack, even on error).
- Bypass of the `do-not-pickup` operator override.
- Race conditions in `claim_issue` that allow two agents to claim the same issue simultaneously without one losing.

## What we treat as standard

- Local file disclosure within the operator's home directory (the framework runs as the operator; reading their files is by design).
- Denial of service via legitimate use (rate-limit hit, max-turns exhausted) — those are framework features, not bugs.
- Issues in third-party skills the operator chose to install.

## Hardening recommendations

For consumer fleets running alfred-os in production:

1. **Per-agent IAM, never operator SSO.** See `docs/AWS_SETUP.md`. Operator's SSO has admin; cron-spawned agents must not.
2. **Secrets via AWS Secrets Manager**, not env files committed to the operator's home. The framework's resolve-then-cache pattern (`slack_post`) is the model.
3. **Pre-push hook installed** in every repo the operator pushes to. `examples/git-hooks/pre-push` blocks accidental races against in-flight agents.
4. **Read every skill before installing.** Skills are markdown + scripts; they run with the same permissions as `claude`. See `docs/SKILLS.md`.
5. **Webhook URLs treated as secrets.** Anyone with the webhook URL can post to your channel as the bot. Rotate immediately on suspected exposure.
6. **Bot tokens (`xoxb-…`) and app tokens (`xapp-1-…`) treated as secrets.** Never put them in commits, screenshots, chat. Rotate via Slack admin → Apps → reinstall.
7. **Audit `agent:authored` PRs before merge.** Alfred-OS provides the `agent:in-flight` → `agent:pr-open` → `agent:done` lifecycle, but human merge is by design — automated merge of unaudited code is out-of-scope for the framework.

## Disclosure history

No vulnerabilities have been disclosed yet (project is at v0.1.0). Previous disclosures will be listed here with links to the GitHub Security Advisory.
