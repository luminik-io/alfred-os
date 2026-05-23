# alfred-claude-proxy

A small localhost daemon that brokers `claude -p` invocations on behalf of
launchd-spawned agent processes. It solves the macOS Keychain ACL issue
documented in [`MACOS_KEYCHAIN.md`](MACOS_KEYCHAIN.md): when `claude` is
invoked from a process spawned by launchd, the per-application ACL on the
OAuth credential blocks the read, so every invocation returns a 401 even
though `claude` works fine in your interactive shell.

The proxy fixes this by running as a long-lived Aqua-session agent. Agent
processes that need to call `claude` connect to it over a unix domain
socket; the proxy spawns `claude` itself, and that child inherits the
proxy's Aqua-session Keychain access.

## When to use it

You want the proxy if any of these are true:

- You schedule agents via launchd on macOS.
- `claude -p` works in your shell but fails with auth errors when invoked
  from a launchd-managed plist.
- You currently work around the issue with the "Allow all applications to
  access this item" Keychain setting and want to tighten that back up.

The proxy makes no difference on Linux (no Keychain), and it makes no
difference for invocations from your interactive terminal. It is purely
an opt-in transport for the launchd-on-macOS path.

## What it does NOT change

- **Billing.** The proxy spawns `claude`, which authenticates with your
  existing OAuth token, against your existing account. No new account
  type, no new pricing, no new rate limits.
- **Token rotation.** `claude login`, token refresh, and revocation work
  exactly as before. The proxy never touches the credential.
- **Agent prompts or behavior.** The proxy is a transport layer; it pumps
  the same bytes `claude --output-format stream-json` would have written
  to stdout. Agents don't know whether they were proxied or not.

## Install

1. Confirm Python 3.11+ is available. The proxy is stdlib-only.

2. Copy the example launchd unit and edit its paths:

   ```sh
   cp examples/launchd/luminik.claude-proxy.plist.example \
      ~/Library/LaunchAgents/luminik.claude-proxy.plist
   $EDITOR ~/Library/LaunchAgents/luminik.claude-proxy.plist
   ```

   Replace every `__PLACEHOLDER__` with an absolute path. In particular,
   set `__CLAUDE_BIN__` to the absolute path of your `claude` binary
   (e.g. `/opt/homebrew/bin/claude` on Apple Silicon). Relying on `PATH`
   inside launchd is unreliable.

3. Bootstrap it:

   ```sh
   launchctl bootstrap gui/$(id -u) \
      ~/Library/LaunchAgents/luminik.claude-proxy.plist
   ```

4. The first time `claude` is spawned, macOS may prompt to grant the
   binary access to the Keychain credential. Click "Always Allow". This
   is the standard one-time ACL grant; the proxy preserves the binary
   path across restarts, so the prompt does not recur.

## Verify

Confirm the socket is alive and the proxy reports the expected binary:

```sh
echo '{"type":"health"}' | nc -U $ALFRED_HOME/run/claude-proxy.sock
# -> {"claude_bin":"/opt/homebrew/bin/claude","uptime_seconds":42,"pid":1234,"type":"health.ok"}
```

End-to-end check (this actually invokes `claude -p "say ok"`):

```sh
echo '{"type":"probe"}' | nc -U $ALFRED_HOME/run/claude-proxy.sock
# -> {"duration_ms":2400,"type":"probe.ok"}
```

If the probe returns `{"type":"probe.fail", ...}`, read
[`MACOS_KEYCHAIN.md`](MACOS_KEYCHAIN.md) for diagnosis.

## Enable in alfred-os

Add this to `~/.alfred/.env` (or wherever your agent shells source their
environment from):

```sh
export ALFRED_CLAUDE_PROXY_SOCKET=$ALFRED_HOME/run/claude-proxy.sock
```

`agent_runner.claude_invoke_streaming` checks that env var on every call.
When it is set and the socket is reachable, the invocation routes through
the proxy. When it is unset (or the socket is missing, or the daemon is
restarting) the call falls back to a direct subprocess. Agents do not
need to be modified; the opt-in is host-level.

## Protocol

Newline-delimited JSON over the unix socket. One request per connection,
followed by a stream of response events.

### Invoke

Client sends:

```json
{
  "type": "invoke",
  "prompt": "Summarize the last commit",
  "workdir": "$ALFRED_HOME/wt/some-agent",
  "allowed_tools": "Read,Edit,Bash",
  "session_id": "20260523-163233-7913",
  "claude_args": ["--permission-mode", "bypassPermissions"],
  "timeout_seconds": 2400
}
```

Server replies (one event per line):

- `{"type":"proxy.accepted","claude_bin":"...","pid":12345}` first.
- Every line `claude --output-format stream-json` would have emitted,
  unmodified.
- `{"type":"proxy.terminal","exit_code":0,"duration_ms":142000}` last.

If the proxy itself fails (bad workdir, claude binary missing, timeout)
the stream ends with `{"type":"proxy.error","reason":"...","detail":"..."}`
in place of `proxy.terminal`.

### Health

```json
{"type":"health"}
```

Server replies once with `{"type":"health.ok","claude_bin":"...","uptime_seconds":N,"pid":P}`
and closes. Always fast; never queues behind in-flight invokes.

### Probe

```json
{"type":"probe"}
```

Server spawns a tiny `claude -p "say ok"` invocation and reports either
`probe.ok` (with `duration_ms`) or `probe.fail` (with `reason` and a
`stderr_tail` field for diagnosis).

## Trade-offs

### vs the "Allow all applications" Keychain ACL setting

You can solve the same problem by opening Keychain Access, selecting the
`claude` credential, and ticking "Allow all applications to access this
item". That works and requires no daemon, but it loosens the ACL for
**every** binary on the system, not just `claude`. Any process running
as your user can then read the OAuth token. The proxy keeps the ACL
narrow (one binary, one path) at the cost of running one extra process.

### vs a stable symlink + targeted ACL grant

The cleanest manual fix is documented in
[`MACOS_KEYCHAIN.md`](MACOS_KEYCHAIN.md): symlink `claude` somewhere
stable, grant the ACL to the symlink target once, and you never need a
proxy. Pick that if you run a single host and want zero moving parts.
Pick the proxy if you manage a fleet (so the ACL grant doesn't have to
be repeated per machine) or if you want every claude invocation to flow
through a single audit log.

## Limitations

- macOS only. There is no Linux Keychain to bypass; the proxy is a no-op
  there, and `ALFRED_CLAUDE_PROXY_SOCKET` should remain unset.
- Solves the Keychain problem and nothing else. It does not implement
  caching, multiplexing, rate-limiting, or token rotation.
- The daemon binds a per-user unix socket with mode 0600 and rejects
  connections from other uids. Adequate isolation for single-operator
  hosts. Multi-tenant hosts should run one daemon per user.
- If `claude` is upgraded mid-invocation (e.g. `brew upgrade node`), the
  proxy continues using the binary path it resolved at startup. Restart
  the daemon (`launchctl kickstart -k gui/$(id -u)/org.alfred-os.claude-proxy`)
  after upgrades.

## Audit log

When enabled (default), the daemon appends one JSON line per invocation
to `$ALFRED_HOME/state/claude-proxy/log.jsonl`:

```json
{"ts":1716480000,"session_id":"...","workdir":"...","allowed_tools":"...","exit_code":0,"duration_ms":142000,"claude_bin":"/opt/homebrew/bin/claude"}
```

Disable with `--audit-log ''` if you do not want the log.
