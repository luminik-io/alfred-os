# Support

## Where to ask

| You want | Channel |
|---|---|
| **Help getting started** | Read [`INSTALL.md`](INSTALL.md) → [`docs/TUTORIAL.md`](docs/TUTORIAL.md) → [`BOOTSTRAP.md`](BOOTSTRAP.md). Most setup questions are answered there. |
| **Bug report** | [Open an issue](https://github.com/luminik-io/alfred-os/issues/new?template=bug.yml) using the bug template. Include: Alfred version, macOS version, the exact command, full output. |
| **Feature request** | [Open an issue](https://github.com/luminik-io/alfred-os/issues/new?template=feature.yml) using the feature template. Be specific about the use case before the proposed solution. |
| **Question / discussion** | [Open an issue](https://github.com/luminik-io/alfred-os/issues/new?template=question.yml) with the `question` label, OR use [GitHub Discussions](https://github.com/luminik-io/alfred-os/discussions) when enabled. |
| **Security vulnerability** | Do **not** open a public issue. See [`SECURITY.md`](SECURITY.md) for the private-disclosure process. |
| **Code contribution** | Read [`CONTRIBUTING.md`](CONTRIBUTING.md), open a draft PR. |

## How issues get handled

The fastest issues to resolve include an exact command, full output, host OS,
Alfred version or commit SHA, and whether the failure happens in dry-run mode.

Priority order:

- **Security issues**: use the private disclosure path in [`SECURITY.md`](SECURITY.md).
- **Install and setup regressions**: include `alfred doctor` output.
- **Runtime bugs with a reproducer**: include the affected agent, labels, repo, and log excerpt.
- **Feature requests**: tie the request to the design boundaries in [`ROADMAP.md`](ROADMAP.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **Questions**: start with the docs and open a focused issue when the docs leave a real gap.

## Out of scope for support

- **Multi-tenant deployments.** Alfred is single-operator by design.
- **Hosted SaaS.** Framework, not a service. We won't run agents for you.
- **Custom Claude Code installs.** If `npm install -g @anthropic-ai/claude-code` doesn't work for you, talk to Anthropic. The CLI is theirs.
- **Per-fleet skill sets.** [`docs/SKILLS.md`](docs/SKILLS.md) documents the current skill boundary. What your fleet actually needs is yours to decide.
- **AWS account setup, IAM policy authoring beyond the templates we ship.** Templates in [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md) cover the common patterns. Deeper AWS work is consultancy territory.

## Getting unstuck

Fastest path:

1. **`alfred doctor`**: preflight every agent. Most config issues surface here.
2. **`tail -f /tmp/<your-fleet>.<agent>.std{out,err}`**: per-agent logs from launchd.
3. **`cat $ALFRED_HOME/state/<agent>/spend-$(date +%Y-%m-%d).json`**: current-day spend + last error subtype.
4. **`gh issue view <N> -R <repo> --json comments`**: claim/release comment trail. Often reveals "why didn't my agent pick this up".
5. **Re-read [`INSTALL.md`](INSTALL.md) "Troubleshooting"**: top install/auth gotchas.

If none of that resolves it, file an issue with the output of all five.

## Maintainer time

This is an open-source project with an opinionated scope. Small, reproducible
bug fixes and docs improvements are the easiest contributions to review. Scope
expansions should start as a discussion so the design stays coherent.
