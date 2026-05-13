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

## Response time

Weekend-maintained project. Realistic expectations:

- **Critical security issues**: acknowledged within 72 hours.
- **Bugs with reproducer**: triaged within a week.
- **Feature requests**: read but rarely actioned unless aligned with the roadmap (see [`ROADMAP.md`](ROADMAP.md) and the design constraints in [`CONTRIBUTING.md`](CONTRIBUTING.md)).
- **Questions**: best-effort. The docs are the canonical answer source.

## Out of scope for support

- **Multi-tenant deployments.** Alfred is single-operator by design.
- **Hosted SaaS.** Framework, not a service. We won't run agents for you.
- **Linux fleets.** First-class Linux support is on the roadmap but not shipped. See [`docs/LINUX.md`](docs/LINUX.md) for interim cron / systemd patterns.
- **Custom Claude Code installs.** If `npm install -g @anthropic-ai/claude-code` doesn't work for you, talk to Anthropic. The CLI is theirs.
- **Per-fleet skill sets.** [`docs/SKILLS.md`](docs/SKILLS.md) recommends a starter set. What your fleet actually needs is yours to decide.
- **AWS account setup, IAM policy authoring beyond the templates we ship.** Templates in [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md) cover the common patterns. Deeper AWS work is consultancy territory.

## Getting unstuck

Fastest path:

1. **`bash bin/doctor.sh`**: preflight every agent. Most config issues surface here.
2. **`tail -f /tmp/<your-fleet>.<agent>.std{out,err}`**: per-agent logs from launchd.
3. **`cat $ALFRED_HOME/state/<agent>/spend-$(date +%Y-%m-%d).json`**: current-day spend + last error subtype.
4. **`gh issue view <N> -R <repo> --json comments`**: claim/release comment trail. Often reveals "why didn't my agent pick this up".
5. **Re-read [`INSTALL.md`](INSTALL.md) "Troubleshooting"**: top install/auth gotchas.

If none of that resolves it, file an issue with the output of all five.

## Maintainer time

Solo founder on weekends. PR review is best-effort, 1-3 weeks for non-urgent changes. If you need faster turnaround for a serious bug, label the issue `severity:p0` and explain the impact.
