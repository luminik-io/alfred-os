---
title: Security
description: Reporting vulnerabilities, scope, hardening recommendations.
---

Full policy at [`SECURITY.md`](https://github.com/luminik-io/alfred-os/blob/main/SECURITY.md). The shape:

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Preferred: open a [private security advisory](https://github.com/luminik-io/alfred-os/security/advisories/new).

Acknowledgement target: 72 hours. Patch target for critical / high severity: 14 days.

## Scope

In scope:

- `lib/agent_runner.py`
- `bin/`
- `examples/bin/label_state.py`
- `examples/git-hooks/pre-push`
- `install.sh`
- The Astro Starlight site at `site/`

Out of scope:

- The Anthropic Claude Code CLI (report to Anthropic).
- Third-party skills (gstack, CodeRabbit, etc.). Report upstream.
- Consumer fleet code that imports `agent_runner`. Consumer's responsibility.
- Operator misconfigurations (leaked AWS keys, public Slack webhooks). Hardening documented; can't be enforced.

## Critical classes

- Remote code execution from a Slack message body, gh API response, or any external data the runner reads.
- Privilege escalation that lets a per-agent IAM identity act outside its declared policy.
- Secret leakage paths.
- Bypass of the `do-not-pickup` operator override.
- Race conditions in `claim_issue` that allow duplicate claims.

## Hardening recommendations

For consumer fleets:

1. **Per-agent IAM**, never operator SSO. See [AWS setup](/guides/aws/).
2. **Secrets via AWS Secrets Manager**, not env files committed to home.
3. **Pre-push hook installed** in every operator-touched repo.
4. **Read every skill before installing.** Skills run with `claude`'s permissions.
5. **Webhook URLs treated as secrets.** Rotate on suspected exposure.
6. **Bot tokens (`xoxb-`) and app tokens (`xapp-1-`) treated as secrets.** Same.
7. **Audit `agent:authored` PRs before merge.** Auto-merge of unaudited code is out of scope.
