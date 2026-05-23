# macOS Keychain + launchd: the `claude` 401 problem

If you run `claude` interactively, every call works. If you launch the
same `claude` invocation from a launchd-managed agent, every call returns
`Not logged in` (or HTTP 401), even though the operator and the token are
unchanged. This page explains why and walks through the fix.

## Symptoms

- `claude -p "say ok"` succeeds in your interactive terminal.
- The same binary, invoked from a `.plist` you bootstrapped with
  `launchctl bootstrap gui/$(id -u) ...`, returns `Not logged in` (or
  HTTP 401 from API calls).
- Re-running `claude login` in your terminal works for that shell, then
  the launchd-spawned invocations fail again as soon as the token has
  to be re-read from the Keychain.
- The Keychain entry exists and the Keychain is unlocked. Running
  `security find-generic-password -l "Claude Code-credentials" -w`
  from your interactive shell prints the OAuth token JSON.
- The same `security` command via
  `launchctl bsexec gui/$(id -u) security find-generic-password ...`
  fails with `SecKeychainSearchCreateFromAttributes`.

The last two bullets are the diagnostic: the credential is present and
readable from interactive context, and unreadable from a launchd-spawned
process tree even when bsexec'd into the user's gui domain.

## Why

The OAuth token lives in a Keychain entry whose **Access Control list**
names the applications allowed to read it. Each entry on the trusted-
applications list is identified by a **code-signing requirement** (a
hash plus signature predicate). When a process requests the token, the
kernel computes the calling process's code-signing identity and checks
it against every entry on the trusted-applications list. A match means
silent access. No match means either a confirmation prompt (in a GUI
session) or denial (in a launchd / non-interactive session, where there
is no UI to prompt to).

When you first ran `claude login` interactively, the system added a
trusted-applications entry whose code requirement matches `claude.exe`
as it was invoked from your shell's process tree. A launchd-spawned
`claude.exe` is the **same binary** at the **same path**, but its
calling-process identity is computed from the new process tree's
signing context. That identity does not match the stored requirement,
so the kernel denies the read.

This is the most common point of confusion: it looks like a session-type
problem because launchd sessions are non-interactive, but the gating
mechanism is the trusted-applications ACL, not the session type. The
fix is to add `claude.exe` itself to the trusted-applications list, so
the kernel grants access regardless of who spawns it.

## The fix: targeted ACL grant for `claude.exe`

The right ACL change adds **one named binary** (`claude.exe`) to the
"Always allow access by these applications" list. Other applications
on your machine remain gated by the existing "Confirm before allowing
access" prompt. The security posture is unchanged for every binary
except claude.

### Option A: `bin/alfred-grant-keychain.sh` (recommended)

The repo ships a helper that detects the Claude credential entries,
resolves your `claude.exe` path, and either walks you through the
Keychain Access GUI flow or applies the change via the `security` CLI:

```sh
# Run it in advisory mode first (no changes made):
bash bin/alfred-grant-keychain.sh

# Apply via the CLI path (prompts once for your login keychain password):
bash bin/alfred-grant-keychain.sh --apply
```

The script:

1. Resolves the real `claude.exe` (following symlinks through fnm /
   nvm / asdf / volta session shims). Override via `CLAUDE_BIN` env.
2. Enumerates Keychain entries that match `Claude Code-credentials*`
   plus `Claude Safe Storage`.
3. Reports the calling binary's code-signing identity and the
   partition the script will add (`teamid:<ANTHROPIC_TEAM>` if signed,
   `unsigned:` if not).
4. With `--apply`, calls
   `security set-generic-password-partition-list` for each entry so
   the partition list includes `claude.exe` alongside the existing
   `apple:` / `apple-tool:` defaults.

Read the script before running it. It only modifies Keychain partition
lists; it does not touch any other Keychain item, file, or process.

### Option B: Keychain Access GUI (no script)

If you prefer to do this by hand:

1. Open **Keychain Access** (Cmd-Space, type "Keychain Access").
2. In the search box, type `Claude Code-credentials`.
3. Double-click the entry, switch to the **Access Control** tab.
4. Leave the radio on **"Confirm before allowing access"**. Do **not**
   switch to "Allow all applications".
5. Under "Always allow access by these applications", click **`+`**.
6. Press Cmd-Shift-G, paste the full path to your `claude.exe`
   (run `bin/alfred-grant-keychain.sh` first to print this path), and
   select the file.
7. Click **Save Changes**. Enter your Mac login password when prompted.

Repeat for any related entries you find (`Claude Code-credentials-
<suffix>`, `Claude Safe Storage`). The `<suffix>` variants are hashed
account identifiers; if you have multiple Claude accounts on this Mac,
each lives in its own entry.

## What about "Allow all applications"?

Keychain Access offers a radio button that grants every process running
as your user silent access to the entry. This is **not** the recommended
path. The credential is an OAuth refresh token with `subscriptionType:
max` scope plus your full Claude Code session permissions; any process
on the machine (including unprivileged or untrusted ones) can read it
with that radio set. The targeted ACL grant above gives `claude.exe` the
same silent access without widening the blast radius. We mention "Allow
all" only because operators have asked us to be explicit about why it is
not the answer.

## Why we still ship `alfred-claude-proxy`

After the ACL is fixed, why not let every agent shell out to `claude`
directly?

- **Connection reuse**: the proxy keeps the Claude API connection warm
  across firings. A direct subprocess pays the TLS handshake on every
  call.
- **Centralised logging and rate limit**: the proxy is the single
  observation point for engine cost and error rates.
- **Future credential transport**: when Anthropic ships a CLI option
  to read credentials from a file or env, the proxy is the right place
  to wire that in without touching every agent.

The proxy is the long-running architecture; the ACL grant is the
one-time door-opening. You need both for autonomous unattended firings,
and the helper script makes the ACL step a single command.

## Confirming it worked

After the ACL grant:

```sh
# 1. The proxy is loaded and the socket is up:
launchctl print "gui/$(id -u)/org.alfred-os.claude-proxy" | head

# 2. End-to-end probe (proxy spawns claude -p "say ok"):
echo '{"type":"probe"}' | nc -U "$ALFRED_HOME/run/claude-proxy.sock"
```

Expected: `{"type":"probe.ok",...}`.

If you still see `claude-exit-1` from the probe, run
`bash bin/alfred-grant-keychain.sh` again to confirm the partition list
was updated, then re-bootstrap the proxy:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/org.alfred-os.claude-proxy.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/org.alfred-os.claude-proxy.plist
```

## Why this is the way it is

The Keychain trust evaluator is doing exactly the right thing from a
security standpoint: a credential created in one context should not
silently flow to a different context just because the same uid is
involved. The cost is the one-time targeted ACL grant. Compared to
storing the OAuth token in a flat file with `chmod 600`, the Keychain
approach is the safer default; the trade-off is the operator visibility
into the trusted-applications list. The helper script and this doc are
the operator visibility.
