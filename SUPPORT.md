# Support

## Where to ask

| You want | Channel |
|---|---|
| **Help getting started** | Read [`INSTALL.md`](INSTALL.md) → [`docs/TUTORIAL.md`](docs/TUTORIAL.md) → [`BOOTSTRAP.md`](BOOTSTRAP.md). Most setup questions are answered there. |
| **Bug report** | [Open an issue](https://github.com/luminik-io/pennyworth/issues/new?template=bug.yml) using the bug template. Include: pennyworth version, macOS version, the exact command, full output. |
| **Feature request** | [Open an issue](https://github.com/luminik-io/pennyworth/issues/new?template=feature.yml) using the feature template. Be specific about the use case before the proposed solution. |
| **Question / discussion** | [Open an issue](https://github.com/luminik-io/pennyworth/issues/new?template=question.yml) with the `question` label, OR use [GitHub Discussions](https://github.com/luminik-io/pennyworth/discussions) when enabled. |
| **Security vulnerability** | Do **not** open a public issue. See [`SECURITY.md`](SECURITY.md) for the private-disclosure process. |
| **Code contribution** | Read [`CONTRIBUTING.md`](CONTRIBUTING.md), open a draft PR. |

## Response time

This is a weekend-maintained project. Realistic expectations:

- **Critical security issues**: acknowledged within 72 hours.
- **Bugs with reproducer**: triaged within a week.
- **Feature requests**: read but rarely actioned unless aligned with the roadmap (see [`ROADMAP.md`](ROADMAP.md) and the design constraints in [`CONTRIBUTING.md`](CONTRIBUTING.md)).
- **Questions**: best-effort; the docs are maintained as the canonical answer source.

## What's out of scope for support

- **Multi-tenant deployments.** Pennyworth is single-operator by design. We don't support shared-fleet topologies.
- **Hosted SaaS.** This is a framework, not a service. We won't run agents for you.
- **Linux fleets.** First-class Linux support is on the roadmap but not shipped. See [`docs/LINUX.md`](docs/LINUX.md) for interim cron / systemd patterns.
- **Custom Claude Code installs.** If `npm install -g @anthropic-ai/claude-code` doesn't work for you, talk to Anthropic; the CLI is theirs.
- **Per-fleet skill sets.** [`docs/SKILLS.md`](docs/SKILLS.md) recommends a starter set; what your fleet actually needs is yours to decide.
- **AWS account setup, IAM policy authoring beyond the templates we ship.** Templates in [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md) cover the common patterns; deeper AWS work is consultancy territory, not framework support.

## Getting unstuck quickly

The fastest path when something doesn't work:

1. **`bash bin/doctor.sh`** — preflight every agent. Most config issues surface here.
2. **`tail -f /tmp/<your-fleet>.<agent>.std{out,err}`** — per-agent logs from launchd.
3. **`cat $HERMES_HOME/state/<agent>/spend-$(date +%Y-%m-%d).json`** — current-day spend + last error subtype.
4. **`gh issue view <N> -R <repo> --json comments`** — check claim/release comment trail (often reveals "why didn't my agent pick this up").
5. **Re-read [`INSTALL.md`](INSTALL.md) "Troubleshooting"** — covers the top 6 install/auth gotchas.

If those don't resolve it, file an issue with the output of all five.

## Maintainer time

Pennyworth is maintained by a solo founder on weekends. PR review is best-effort, may take 1-3 weeks for non-urgent changes. If you need faster turnaround for a serious bug, label the issue `severity:p0` and explain the impact — those get prioritised.
