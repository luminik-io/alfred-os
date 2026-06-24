# Install tiers

Alfred installs in three tiers. Only the first is required. The other two are optional surfaces that talk to the core over inspectable seams.

| Tier | What it is | Required? | Needs a desktop? |
|---|---|---|---|
| `core` | Fleet, Alfred CLI, host scheduler, `alfred serve` JSON API | Yes | No (headless, Linux-friendly) |
| `client` | Alfred Desktop (`clients/desktop`) | No | Yes |
| `slack` | Planning listener + issue bridge | No | No |

For the architecture behind these tiers, see [`ARCHITECTURE.md`](ARCHITECTURE.md). For the from-zero walkthrough of `core`, see [`../INSTALL.md`](../INSTALL.md). This page is the tier map; it does not replace the install walkthrough.

## `core`: the standalone base

The core install is the whole product for most setups. It is the fleet (`lib/agent_runner/` plus the `bin/*.py` runners), the Alfred CLI (`bin/alfred`), the host scheduler (launchd on macOS, `systemd --user` on Linux), and `alfred serve`.

Core is fully standalone. The CLI and fleet do not need Alfred Desktop, a browser, or Slack to function. A headless Debian or Ubuntu box can run the entire fleet from cron-style timers with nothing on screen. See [`LINUX.md`](LINUX.md) for the `systemd --user` path.

Install it the same way as the main walkthrough:

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL
gh auth login
claude
./bin/alfred-init.py
```

`install.sh` puts `git`, `gh`, `jq`, the AWS CLI, Python, Node, and `uv` in place, installs the Claude Code CLI, and creates `$ALFRED_HOME` (default `~/.alfred`) and `$WORKSPACE_ROOT` (default `~/code`). It does not enable any agents or touch the scheduler; `alfred-init.py` and `deploy.sh` do that. See [`../INSTALL.md`](../INSTALL.md) for what each step does and how to recover when one fails.

### The `serve` extra

`alfred serve` is the localhost JSON API over `$ALFRED_HOME/state`. It binds to `127.0.0.1` by default and is the API Alfred Desktop reads through. Its Python dependencies are an optional extra so a pure-fleet install stays small:

```sh
pip install 'alfred-os[serve]'   # FastAPI + uvicorn + jinja2
alfred serve --no-browser        # listens on http://127.0.0.1:7010
```

If port 7010 is taken, use another localhost port and point Alfred Desktop
at it from Setup. Binding to `0.0.0.0` is allowed but discouraged: the dashboard
exposes paths and event payloads that may carry repo URLs or other operator
context. The fleet runs fine without `serve` ever starting; it is only needed
when you want the dashboard or Alfred Desktop.

## `client`: the desktop app

Alfred Desktop is an optional Tauri app under `clients/desktop`. It is a thin local control surface and installer, not a second Alfred runtime. Slack remains the collaboration surface; the client is for local trust and repair: what needs attention now, which plans are waiting, why a run failed, which memory candidates are ready, and which safe action repairs the fleet.

It talks to core only over the `alfred serve` JSON seam, restricted to `http://localhost`, `http://127.0.0.1`, or `http://[::1]` and a fixed set of Alfred JSON paths plus a narrow native command allowlist. It opens no public port, runs no relay, and keeps `$ALFRED_HOME` as the single source of truth. You can run Alfred entirely without it.

There are two ways to install it. Pick the signed release for a normal setup; build from source only when you are working on the client itself.

Signed release (recommended):

```sh
brew install --cask alfred-os               # macOS 11+, signed and notarized
# or download Alfred.dmg / Alfred.AppImage / Alfred.deb from
# https://alfred.luminik.io/download/
```

The cask installs alongside the `alfred-os` CLI formula, so `brew install alfred-os && brew install --cask alfred-os` gives you both halves. On first launch with no runtime running, the app opens into the guided setup wizard, which can start `alfred serve` for you, so a fresh user never lands on a dead end.

Build from source (client development):

```sh
alfred serve --port 7010 --no-browser       # or let Setup start it for you
cd clients/desktop
npm install
npm run tauri dev
```

Inbox opens to the decision queue plus a capacity rail for Claude and Codex subscription headroom (read locally, no billing API; backed by the live `GET /api/usage` endpoint), and Agents defaults to a cinematic roster with a list toggle. Setup can start the local runtime, run `alfred status --json`, run auth checks, list agents, run the memory doctor, and dry-run an agent through a narrow allowlist. Agents controls handle pause, resume, and run-once actions through the same native boundary. See [`DESKTOP_CLIENT.md`](DESKTOP_CLIENT.md) for the full client design and the API contract, and [`SERVE.md`](SERVE.md) for the `alfred serve` endpoints.

## `slack`: the planning surface

The Slack tier is the planning listener plus the issue bridge. It turns Slack into an intake and refinement surface without making chat an approval mechanism for code.

- The **listener** (`lib/slack_listener.py`) runs in Socket Mode. The configured approver and trusted users can DM or mention Alfred; the listener refines the request into a saved local draft, scores readiness, and asks for missing scope. It never files issues, opens PRs, or runs code.
- The **bridge** (`lib/slack_issue_bridge.py`) is off by default. When the configured approver explicitly approves a draft, and the bridge is enabled with a repo allowlist, it files one labeled GitHub issue. From there the fleet claims it through every existing gate. The bridge runs no code.

The base install already includes `slack-sdk` and `boto3` (promoted out of optional extras in v0.4.0), so the only thing the Slack tier needs beyond `core` is configuration:

```sh
# Minimal: incoming webhook for fleet posts (one-way: agents post, you read)
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...' >> ~/.alfredrc

# Optional: the planning listener (Socket Mode, bot token + app token)
echo 'SLACK_BOT_TOKEN=xoxb-...' >> ~/.alfredrc
echo 'SLACK_APP_TOKEN=xapp-...' >> ~/.alfredrc
echo 'ALFRED_OPERATOR_SLACK_USER_ID=U0123' >> ~/.alfredrc
echo 'ALFRED_TRUSTED_SLACK_USER_IDS=U0123,U0456' >> ~/.alfredrc
./bin/alfred-slack-listener.py

# Optional: arm the issue bridge (default OFF). Both are required to enable.
echo 'ALFRED_BRIDGE_ENABLED=1' >> ~/.alfredrc
echo 'ALFRED_BRIDGE_REPOS=acme-org/api,acme-org/web' >> ~/.alfredrc
```

Leave `ALFRED_BRIDGE_ENABLED` unset to keep approvals as refine-only no-ops. For the message contract, the reaction approval gate, and the listener boundary, see [`SLACK_UX.md`](SLACK_UX.md), [`SLACK_APPROVAL.md`](SLACK_APPROVAL.md), and the webhook walkthrough in the Slack setup guide.
Trusted users can create and refine planning drafts. Only `ALFRED_OPERATOR_SLACK_USER_ID` can approve Slack-origin filing into GitHub.

## Picking your tiers

- **Headless Linux fleet, no UI:** `core` only. Run the CLI and scheduler; skip `serve`, the client, and Slack, or wire just an incoming webhook for one-way posts.
- **Mac user who wants Alfred Desktop:** `core` + `client`. Install the `serve` extra, run `alfred serve`, and drive the fleet from the desktop app.
- **Team that plans in Slack:** `core` + `slack`. Run the listener, keep the bridge off until you trust the flow, then arm it with an allowlist.
- **Everything:** all three. The client and Slack surfaces both sit on top of the same `core` and never bypass its gates.
