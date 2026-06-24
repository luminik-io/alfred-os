# macOS permissions

Alfred is a tool that touches your repos, your git history, and optionally
Slack, so it is fair to ask exactly what it can reach on your Mac. This page
lists every macOS prompt you might see, why it appears, and what Alfred does
**not** ask for.

The short version: Alfred runs as you, on your machine, against the local CLIs
you already authenticated. It does not request screen recording, accessibility
control, your contacts, your location, your microphone, or your camera. If you
see a prompt for any of those, something other than Alfred is asking.

## The CLI and the fleet

The core runtime is plain Python plus `git`, `gh`, and your coding CLIs
(`claude`, optional `codex`). It runs in your shell and under your host
scheduler (`launchd` on macOS). It needs nothing beyond ordinary file and
network access that any command-line tool already has, so macOS does not show a
permission dialog for it.

What it reaches, and why:

- **Your repos and worktrees** under `ALFRED_HOME` (`~/.alfred` by default) and
  the checkouts you pointed it at. This is ordinary file access in your home
  directory. Alfred only touches repos you added to `~/.alfredrc`.
- **Your local CLI auth.** Alfred shells out to `claude` and optional `codex`
  using the auth those tools already stored. It never reads your provider
  password and never asks for an API key.
- **Outbound network** to the model provider you chose (Anthropic for Claude
  Code, OpenAI for Codex), to GitHub through `gh`, and to your Slack webhook if
  you configured one. There are no other destinations. See
  [Privacy](../README.md#privacy-what-alfred-touches-and-what-it-does-not).

## Alfred Desktop

Alfred Desktop is the optional native app. It is signed and notarized, so
Gatekeeper lets it open without the "unidentified developer" block. It is a thin
local dashboard over `alfred serve`; it does not run agents by itself.

Prompts you may see, and why:

- **"Alfred Desktop was downloaded from the internet. Are you sure you want to
  open it?"** Standard Gatekeeper first-launch check for any downloaded app.
  Because the app is notarized, you click Open once and macOS remembers it. If
  you installed through `brew install --cask alfred-os`, Homebrew clears the
  quarantine flag and you may not see this at all.
- **Outgoing network connection (if you run a firewall like Little Snitch).**
  The app talks to `127.0.0.1` (loopback) to reach the local `alfred serve` API.
  Its content security policy only allows `self` and the local IPC bridge, so
  the window itself makes no third-party calls. The CLI it drives is what reaches
  GitHub, your model provider, and Slack, exactly as described above.
- **Notifications (only if a build enables them).** Used to tell you a plan needs
  approval or a run finished. Decline it and the app still works; you just rely
  on Slack and the in-app feed instead.

## What Alfred never requests

Alfred does not ask for, and does not need, any of the following macOS
permissions. This list is deliberate. If a prompt for one of these appears
while installing or running Alfred, stop and check what is actually asking.

- **Screen Recording.** Alfred never captures your screen.
- **Accessibility / control of your computer.** Alfred does not drive other apps
  or read other apps' windows.
- **Input Monitoring.** Alfred does not log keystrokes.
- **Camera or Microphone.** Alfred has no audio or video features.
- **Contacts, Calendars, Reminders, Photos.** Alfred has no reason to read your
  personal data and does not.
- **Location.** Alfred does not use location.
- **Full Disk Access.** Alfred works inside your home directory and the repos you
  added. It does not request the system-wide Full Disk Access entitlement.

## Removing access

- Uninstall the desktop app: `brew uninstall --cask alfred-os`, or drag
  Alfred.app to the Trash.
- Stop the runtime: `alfred pause`, then remove the scheduler entries your
  install created under `launchd/`.
- Revoke a model provider or GitHub by logging out of that CLI
  (`gh auth logout`, or the provider CLI's own logout). Alfred holds no
  credentials of its own to revoke.

See also the [threat model](THREAT_MODEL.md) for how Alfred contains what an
agent run can do once it is running.
