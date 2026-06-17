# Alfred Desktop

Native Mac/Linux control center for a local Alfred install.

Slack remains Alfred's collaboration surface. This app is for local trust and
repair: what needs attention, which plans are waiting, which runs failed, which
memory candidates need review, which Slack collaborators are trusted, and which
local actions are safe to run next.

The app is the friendly path into Alfred. The CLI remains fully supported for
automation, debugging, and users who prefer a terminal.

## Where this fits (layered install)

Alfred installs in layers, and this client is the optional top one:

1. **`alfred` CLI + runtime**: the base. Schedules the fleet, runs agents, and
   owns all state under `$ALFRED_HOME` (`~/.alfred` by default). Required.
2. **`alfred serve`**: a local HTTP API + web dashboard over that
   state. The desktop client prefers `http://127.0.0.1:7010` and falls
   back to `http://127.0.0.1:7000`.
3. **Alfred Desktop (this app)**: the optional native control plane: a menu-bar
   tray, five primary destinations (Review, Board, Compose, Fleet, Set up), a
   command palette, and a narrow set of safe local actions. It does not run
   agents itself; it reads `alfred serve` and shells a small allowlist of
   `alfred` CLI verbs.

You can run the fleet headless with just the CLI. The desktop app is a
convenience surface on top, not a dependency.

The app navigation is five primary destinations, each a full page with its own
in-page tabs where it needs depth, never a long scroll and never a slide-over
drawer. The design north-star is in [`CLIENT_REDESIGN.md`](CLIENT_REDESIGN.md).

**Review** is the home / heartbeat: a pinned cost / health strip over three
in-page lanes. **Needs you** is decisions and failures waiting on the operator,
**Activity** is what is running and scheduled, and **Shipped** is merged work
(defaulting to the last 24h, with a 24h / 7d / 14d filter). Shipped is backed by
`GET /api/shipped` and renders the same readable cards as the Slack board; a card
opens a request lifecycle thread (Intake -> Plan -> Queued -> Building ->
Shipped). The cards deep-link to GitHub for the actual code review; the app never
embeds a diff or merge UI.

**Board** is the first-class Kanban (Queued / In progress / Shipped) with
per-card Queue / Hold / Done actions and a "queue an issue" composer (Done closes
the issue via GitHub's native closed state). It shares the board state with
Review's Shipped lane and the Slack board.

**Compose** is plain-language request intake: describe the work in plain words,
and Alfred's planning assistant scores how ready it is to run, surfaces the
clarifying questions still open, and saves a draft to the planning inbox. Each
submission refines the same draft. The plain-mode spec coach is the default when
the runtime starts with `ALFRED_INTAKE_PROFILE=plain`.

**Fleet** is the operator-depth page, organized as in-page tabs (this replaces
the old slide-over Operator drawer). **Agents** carries pause, resume, run-once,
and dry-run controls per codename. **Logs** has an **Activity** feed plus a
**Latest run** view that shows one agent's most recent captured run, refreshed on
the dashboard poll rather than streamed byte-by-byte. **Lessons** shows
reviewable memory candidates with promote / reject and failure-pattern harvest.
**Plans** is the plan and issue detail inspector.

**Set up** is the client-owned, onboarding-first surface and the repair path: it
detects installed engine CLIs, connects GitHub and picks repos, starts the local
runtime, runs common Alfred checks in-app, adds or removes local trusted Slack
collaborators, and keeps the underlying CLI commands visible as advanced detail.

A command palette (Cmd+K) navigates anywhere, and a dark/light "Wayne
Enterprises" theme toggle lives in the top bar.

## Run locally

The desktop app can start the local API from the Setup gear. If you prefer to
run the runtime yourself, start it first:

```sh
alfred serve --no-browser
```

The desktop app's Start runtime action uses port 7010 because macOS can reserve
7000 for Control Center. A manually started `alfred serve --no-browser` on 7000
still works; the app probes it as a fallback.

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

## First launch and install

Release artifacts use stable public asset names so the download page can point
at the latest release without a site change for every version:

- `Alfred.dmg`
- `Alfred.app.zip`
- `Alfred.AppImage`
- `Alfred.deb`

On first launch:

- **macOS:** the release `.dmg` and `.app.zip` are Developer ID signed,
  notarized, and stapled. Open the DMG, drag Alfred to Applications, and launch
  it normally. If you build an unsigned app locally, right-click (or
  Control-click) the app and choose **Open** once.
- **Linux:** the `.AppImage` needs the executable bit
  (`chmod +x Alfred.AppImage`); the `.deb` installs normally with
  `sudo dpkg -i Alfred.deb` (or `sudo apt install ./Alfred.deb`).

## Releases

Releases start in the public `Release` workflow
(`.github/workflows/release.yml`) and finish after the signed desktop assets are
attached to that draft release.

- **Tag a release:** push a tag matching `v*.*.*`, e.g.
  `git tag v0.5.0 && git push origin v0.5.0`. The public workflow creates or
  updates a **draft** GitHub Release and prints the source tarball checksum for
  the Homebrew formula.
- **Attach desktop assets:** run the signed desktop release pipeline for the
  same tag and confirm `Alfred.dmg`, `Alfred.app.zip`, `Alfred.AppImage`, and
  `Alfred.deb` are present on the draft before publishing it.
- **Dry run:** trigger the public release workflow manually
  (`workflow_dispatch`) to update release notes or recompute the source tarball
  checksum without publishing a release.

Keep `package.json`'s `version` in step with the tag (the tag does not set the
in-app version; `package.json` does).

## Security boundary

The frontend does not call arbitrary URLs. The Tauri command only allows an
allowlisted set of Alfred JSON API paths on `http://localhost`,
`http://127.0.0.1`, or `http://[::1]`: read-only GET paths (including
`GET /api/shipped` for the Kanban board) plus a narrow set of POST endpoints
(follow-up planning actions, the Compose draft endpoint `POST /api/plans/draft`,
the Kanban queue control `POST /api/queue`, and local Slack trusted-user
updates). `POST /api/queue` mutates fleet/repo state, so it requires the
operator's per-launch token via the `X-Alfred-Token` header, not just a
same-origin request. Local plan and firing detail stays in native inspector
panes; explicit Slack and GitHub links open outside the app through Tauri's
opener plugin.

State-changing controls use a narrow native allowlist. The app can start the
local runtime, run fleet/auth/agent checks, pause, resume, run once, run safe
agent dry-runs, run memory health checks, check Redis memory, author planning
drafts from the Compose tab, call local follow-up planning endpoints, and update
the local Slack trust file. It does not expose arbitrary shell execution.
Broader lock-clearing and memory-promotion actions should keep the same
contract: explicit preview, affected path, result, and rollback hint.
