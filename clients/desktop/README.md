# Alfred Desktop

Native Mac/Linux control center for a local Alfred install.

Slack remains Alfred's collaboration surface. This app is for local trust and
repair: what needs attention, which plans are waiting, which runs failed, which
memory candidates need review, and which local commands are safe to run next.

The app is the friendly path into Alfred. The CLI remains fully supported for
automation, debugging, and users who prefer a terminal.

The Setup tab keeps the same dual path: it shows the install/repair command,
can start the local runtime for you, runs common Alfred checks in-app, and
still exposes copyable CLI commands as a fallback.

## Run locally

The desktop app can start the local API from the Setup tab. If you prefer to
run the runtime yourself, start it first:

```sh
alfred serve --no-browser
```

If port 7000 is already taken:

```sh
alfred serve --port 7010 --no-browser
```

Then run the desktop shell:

```sh
cd clients/desktop
npm install
npm run tauri dev
```

## Checks

```sh
npm run typecheck
npm run build
source "$HOME/.cargo/env"
cargo fmt --manifest-path src-tauri/Cargo.toml --check
npm run tauri -- build --no-bundle --ci
```

`--no-bundle` proves the native binary builds without requiring code signing,
DMG packaging, or Linux package artifacts.

## Security boundary

The frontend does not call arbitrary URLs. The Tauri command only allows
read-only Alfred JSON API paths on `http://localhost`, `http://127.0.0.1`, or
`http://[::1]`. Links to Slack, GitHub, and `alfred serve` open outside the app
through Tauri's opener plugin.

State-changing controls use a narrow native allowlist. The app can start the
local runtime, run fleet/auth/agent checks, run safe agent dry-runs, run memory
health checks, check Redis memory, and call local follow-up planning endpoints.
It does not expose arbitrary shell execution. Broader pause, resume,
lock-clearing, and memory-promotion actions should keep the same contract:
explicit preview, affected path, result, and rollback hint.
