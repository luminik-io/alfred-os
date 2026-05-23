# macOS Keychain + launchd: the `claude` 401 problem

If you run `claude` interactively, everything works. If you launch the
same `claude` invocation from a launchd-managed agent, every call returns
a 401 even though the operator and the token are unchanged. This page
explains why and walks through the three fixes, ranked.

## Symptoms

- `claude -p "say ok"` succeeds in your interactive terminal.
- The same binary, invoked from a `.plist` you bootstrapped with
  `launchctl bootstrap gui/$(id -u) ...`, returns:

  ```
  Error: 401 Unauthorized
  ```

- Re-running `claude login` in your terminal works for one shell, then
  the launchd-spawned invocations fail again as soon as the token has
  to be re-read from the Keychain.

- The Keychain credential exists and is unlocked; running `security
  find-internet-password` for it from your shell succeeds.

## Why

macOS Keychain ACLs are bound to **the requesting process's binary
path**, not its uid. When you ran `claude login` from your terminal,
macOS stored the OAuth token with an ACL that lists exactly your
`claude` binary as allowed to read it.

When launchd spawns a process at a session type other than `Aqua` (most
agent plists default to `Background` or `System`), the resulting process
runs in a different **security context**. Even though the uid matches,
the kernel's keychain trust evaluator sees a request from "binary X
running in non-Aqua session" and denies the read, because the ACL was
granted to "binary X running in the Aqua session that authenticated the
operator".

You can confirm this with one diagnostic:

```sh
launchctl bsexec gui/$(id -u) /opt/homebrew/bin/claude -p "say ok"
```

If `bsexec`'ing into your Aqua session makes the call succeed while the
exact same command from a non-Aqua plist fails, the diagnosis is the
Keychain ACL.

## Fix 1: stable symlink + targeted ACL (cleanest)

If your `claude` binary path moves around (Homebrew upgrades, version
managers like `n` or `mise`), every move forces a new ACL grant. Pin a
stable symlink first:

```sh
mkdir -p ~/.local/bin
ln -sfn "$(command -v claude)" ~/.local/bin/claude
# Re-run `claude login` once so the ACL binds to the symlink target.
~/.local/bin/claude login
```

Then point your plists at `~/.local/bin/claude`. The ACL grant survives
PATH changes because the target of the symlink does not change.

This is the cleanest fix for a single-machine operator and requires no
daemon. The proxy below is the right pick if you manage a fleet.

## Fix 2: `alfred-claude-proxy` (recommended for fleets)

Run a long-lived proxy in your Aqua session that spawns `claude` on
behalf of agent processes. The child claude inherits the proxy's
session security context, so the ACL evaluator sees a request from the
same Aqua session that originally granted the ACL.

See [`CLAUDE_PROXY.md`](CLAUDE_PROXY.md) for install and verification.

You still grant the ACL once (to the `claude` binary the proxy spawns),
but every agent on the host benefits.

## Fix 3: "Allow all applications" Keychain ACL (last resort)

In Keychain Access:

1. Find the `claude` OAuth credential (search for the issuer URL).
2. Right-click -> Get Info -> Access Control.
3. Select "Allow all applications to access this item".

This works, but it broadens the ACL from "one named binary" to "every
process running as your user". On a single-developer laptop the trade-
off is small; on a shared host or a machine running untrusted code, the
trade-off is large. Use this only when 1 and 2 are not options.

## Decision tree

```
Are you on macOS?
  No  -> none of this applies; ignore.
  Yes -> Do you only run claude from your interactive terminal?
           Yes -> nothing to fix.
           No  -> Do you manage more than one host?
                    No  -> use Fix 1 (symlink + targeted ACL).
                    Yes -> use Fix 2 (alfred-claude-proxy).
```

## Why we wrote a proxy instead of "just" fixing the ACL

The ACL fix is per-host and per-binary. A fleet of N machines means N
ACL grants, and any `brew upgrade` that changes the resolved path means
a new round. The proxy turns that operator cost into a one-time
launchd-unit install plus one ACL grant per host (for the proxy's
spawned claude). Onboarding a new agent or a new role is then a config
change, not a host visit.
