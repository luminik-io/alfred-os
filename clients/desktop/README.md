# Alfred Desktop

Native Mac/Linux control center for a local Alfred install.

Slack remains Alfred's collaboration surface. This app is for local trust and
repair: what needs attention, which plans are waiting, which runs failed, which
memory candidates need review, and which local commands are safe to run next.

## Run locally

Start the local API first:

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

State-changing controls are intentionally command previews or external links in
this first client. Future pause, resume, doctor, and memory-promotion actions
should return an explicit command preview and affected path before they mutate
local state.
