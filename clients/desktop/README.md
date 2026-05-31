# Alfred Desktop

Native Mac/Linux control center for a local Alfred install.

Slack remains Alfred's collaboration surface. This app is for local trust and
repair: what needs attention, which plans are waiting, which runs failed, which
memory candidates need review, and which local actions are safe to run next.

The app is the friendly path into Alfred. The CLI remains fully supported for
automation, debugging, and users who prefer a terminal.

## Where this fits (layered install)

Alfred installs in layers, and this client is the optional top one:

1. **`alfred` CLI + runtime** — the base. Schedules the fleet, runs agents, and
   owns all state under `$ALFRED_HOME` (`~/.alfred` by default). Required.
2. **`alfred serve`** — a local read-only HTTP API + web dashboard over that
   state, on `http://127.0.0.1:7000`. The desktop client talks to it.
3. **Alfred Desktop (this app)** — the optional native control plane: a menu-bar
   tray, an at-a-glance fleet view, a Compose tab for authoring plans, and a
   narrow set of safe local actions. It does not run agents itself; it reads
   `alfred serve` and shells a small allowlist of `alfred` CLI verbs.

You can run the fleet headless with just the CLI. The desktop app is a
convenience surface on top, not a dependency.

The Compose tab is the in-app spec/plan authoring surface: describe the work in
plain language, and Alfred's planning assistant scores how ready it is to run,
surfaces the clarifying questions that are still open, and saves a draft to the
Plans inbox. Each submission refines the same draft.

The Setup tab keeps the same dual path: it can start the local runtime, run
common Alfred checks in-app, and keep the underlying CLI commands visible as
advanced detail for users who want to inspect the runtime.

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
npm test
npm run build
source "$HOME/.cargo/env"
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo test --manifest-path src-tauri/Cargo.toml
npm run tauri -- build --no-bundle --ci
```

`--no-bundle` proves the native binary builds without requiring code signing,
DMG packaging, or Linux package artifacts.

## Build installers locally

To produce the full installable artifacts (not just the dev binary):

```sh
cd clients/desktop
npm ci
npm run tauri -- build
```

The bundle targets are configured in `src-tauri/tauri.conf.json`. The app
version is read from `package.json`, so bump it there to change the release
version. Outputs land under `src-tauri/target/release/bundle/`:

| Platform | Artifacts | Path |
| --- | --- | --- |
| macOS | `.dmg`, `.app` | `bundle/dmg/`, `bundle/macos/` |
| Linux | `.AppImage`, `.deb` | `bundle/appimage/`, `bundle/deb/` |

You can only build a platform's installers on that platform: build the macOS
artifacts on macOS and the Linux artifacts on Linux. The
[release workflow](#releases) does both on the matching CI runners.

Linux builds need the WebKitGTK system libraries
(`libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`,
`patchelf`, plus `build-essential` and `file`). The release workflow installs
them; install them yourself for a local Linux build.

## Unsigned app: first-launch note

The current artifacts are **unsigned**. macOS Gatekeeper blocks unsigned apps on
a normal double-click. On first launch:

- **macOS:** right-click (or Control-click) the app and choose **Open**, then
  confirm in the dialog. After the first approved launch it opens normally. If
  the `.dmg`/`.app` was downloaded (and so carries the quarantine flag), you can
  also clear it with `xattr -dr com.apple.quarantine "/Applications/Alfred.app"`.
- **Linux:** the `.AppImage` needs the executable bit
  (`chmod +x Alfred_*.AppImage`); the `.deb` installs normally with
  `sudo dpkg -i alfred-desktop_*.deb` (or `sudo apt install ./alfred-desktop_*.deb`).

Code signing and notarization (a smoother, no-right-click first launch on macOS)
require an Apple Developer ID and are a planned follow-up. See
[Releases](#releases).

## Releases

Releases are cut by the `desktop-release` GitHub Actions workflow
(`.github/workflows/desktop-release.yml`).

- **Tag a release:** push a tag matching `desktop-v*`, e.g.
  `git tag desktop-v0.1.0 && git push origin desktop-v0.1.0`. The workflow builds
  on macOS and Linux runners and attaches the `.dmg`, zipped `.app`, `.AppImage`,
  and `.deb` to a **draft** GitHub Release for review before it is published.
- **Dry run:** trigger the workflow manually (`workflow_dispatch`) to build the
  artifacts and upload them to the workflow run without publishing a release.

Keep `package.json`'s `version` in step with the tag (the tag does not set the
in-app version; `package.json` does).

### Deferred: macOS signing + notarization

The macOS artifacts ship unsigned for now, which is why first launch needs the
right-click -> Open step above. Adding an Apple Developer ID (signing identity +
notarization credentials) to the release workflow is a follow-up that will let
the app launch without that step. Linux artifacts are unaffected.

## Security boundary

The frontend does not call arbitrary URLs. The Tauri command only allows an
allowlisted set of Alfred JSON API paths on `http://localhost`,
`http://127.0.0.1`, or `http://[::1]`: read-only GET paths plus a narrow set of
POST endpoints (follow-up planning actions and the Compose draft endpoint
`POST /api/plans/draft`). Links to Slack, GitHub, and `alfred serve` open
outside the app through Tauri's opener plugin.

State-changing controls use a narrow native allowlist. The app can start the
local runtime, run fleet/auth/agent checks, run safe agent dry-runs, run memory
health checks, check Redis memory, author planning drafts from the Compose tab,
and call local follow-up planning endpoints. It does not expose arbitrary shell
execution. Broader pause, resume,
lock-clearing, and memory-promotion actions should keep the same contract:
explicit preview, affected path, result, and rollback hint.
