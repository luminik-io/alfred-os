# Releasing Alfred

How a tagged Alfred release goes out, end to end. This is the process runbook.
For the pre-tag gate list (tests, scrub, docs build) see
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). For how the desktop installer is
built and what artifacts it produces, see
[`DESKTOP_CLIENT.md`](DESKTOP_CLIENT.md).

## Why the release starts as a draft

The release body is the `Highlights` block from `CHANGELOG.md`. From v0.5.0 on,
that block tells the reader Alfred Desktop can be downloaded. The signed and
notarized macOS `.dmg` is produced and attached by a separate desktop workflow
(`.github/workflows/desktop-release.yml`) that runs against the tag, not by
`release.yml`. `release.yml` only creates the release and the changelog body.

So the release is created as a **draft**. A draft is not public and is not the
latest release. That keeps the download claim honest: nobody can read
"download the desktop app" on a published release page until the signed assets
are attached. The desktop workflow attaches them automatically; a human then
checks the asset list and presses Publish.

## One-time secret setup (do this once)

The desktop release workflow signs and notarizes entirely from repository
**secrets**, so the operator never builds or signs locally again. Add these once
under **Settings -> Secrets and variables -> Actions -> New repository secret**.
The workflow reads them by exact name; nothing here touches the values in this
repo.

**Code signing (always required):**

| Secret name | What it is / how to generate it |
| --- | --- |
| `APPLE_CERTIFICATE_P12_BASE64` | Your **Developer ID Application** certificate exported as a base64 `.p12`. In Keychain Access, find the "Developer ID Application: ... (TEAMID)" cert, right-click -> Export -> `.p12`, set an export password. Then `base64 -i cert.p12 \| pbcopy` and paste. |
| `APPLE_CERTIFICATE_PASSWORD` | The export password you set on the `.p12` above. |
| `APPLE_SIGNING_IDENTITY` | The exact identity string, e.g. `Developer ID Application: Your Name (TEAMID)`. Find it with `security find-identity -v -p codesigning`. |
| `APPLE_KEYCHAIN_PASSWORD` | Any throwaway password. It locks the temporary keychain the workflow creates and then deletes; it is not a real account password. Generate one, e.g. `openssl rand -base64 24`. |

**Notarization (pick ONE of the two options):**

Option A, App Store Connect API key (preferred, no Apple-ID password):

| Secret name | What it is / how to generate it |
| --- | --- |
| `APPLE_API_KEY_ID` | The Key ID from App Store Connect -> Users and Access -> Integrations -> App Store Connect API. Create a key with the "Developer" role. |
| `APPLE_API_ISSUER_ID` | The Issuer ID shown on that same API keys page. |
| `APPLE_API_KEY_P8` | The full contents of the downloaded `AuthKey_XXXX.p8` file. Paste the whole file, header and footer lines included. You can only download it once. |

Option B, Apple ID + app-specific password (fallback):

| Secret name | What it is / how to generate it |
| --- | --- |
| `APPLE_ID` | The Apple ID email of an account on the team. |
| `APPLE_TEAM_ID` | Your 10-character Team ID (Apple Developer -> Membership). |
| `APPLE_APP_SPECIFIC_PASSWORD` | An app-specific password generated at appleid.apple.com -> Sign-In and Security -> App-Specific Passwords. Not your normal Apple ID password. |

If both option A and option B secrets are present, the workflow uses option A.
If neither is present, the notarize step fails with a clear message.

## Flow for a version (vX.Y.Z)

1. **Bump `VERSION` and `CHANGELOG.md`** in a prep PR (this is what the v0.5.0
   prep PR does). `VERSION` holds the number with no leading `v`. The changelog
   has a dated section for that version, and the `Next` section holds only
   future work. Land the prep PR on `main`.

2. **Tag from the release commit.** From `main` at the merged prep commit:

   ```sh
   git tag -s "v$(cat VERSION)" -m "v$(cat VERSION)"
   git push origin main --tags
   ```

3. **`release.yml` creates a DRAFT release.** On the pushed tag the `Release`
   workflow:
   - verifies the tag matches the `VERSION` file and fails if they differ,
   - extracts the matching `CHANGELOG.md` section into the release body,
   - creates (or updates) the GitHub release as a **draft**.

   At this point the release exists but is not public and has no desktop assets.

4. **`desktop-release.yml` builds, signs, notarizes, and attaches the macOS
   `.dmg` automatically.** This workflow triggers on the same `vX.Y.Z` tag push
   (it runs alongside `release.yml`) and again on `release: published`. On a
   `macos-latest` runner it:
   - imports the Developer ID Application cert from the secrets above into a
     temporary keychain,
   - builds the universal macOS app with Tauri, codesigning the `.app` with that
     identity,
   - notarizes the `.dmg` with `xcrun notarytool submit --wait` (App Store
     Connect API key, or Apple ID fallback),
   - staples the ticket with `xcrun stapler staple`,
   - uploads the stapled `Alfred.dmg` and a `checksums.txt` to the draft release
     for this tag (idempotent, `--clobber`), and
   - always deletes the temporary keychain in an `always()` cleanup step.

   The desktop bundle version is already aligned to the release in the prep step
   (`clients/desktop/package.json` and `src-tauri/Cargo.toml` are set to the
   release number, and `tauri.conf.json` reads the version from
   `package.json`), so the installer carries the release version with no
   separate manual bump. The download page uses
   `/releases/latest/download/Alfred.dmg`, which the workflow attaches under that
   stable name. If a build fails (for example a notarization rejection), fix it
   and re-run the workflow against the tag with **Run workflow** (the `tag`
   input).

   Linux `.AppImage` / `.deb` packages are not produced by this signed workflow.
   If you need them for a release, build and attach them separately; the macOS
   signed path is what the Homebrew cask and the download page depend on.

5. **Publish the release.** Once `Alfred.dmg` and `checksums.txt` are attached, a
   human opens the draft release, checks the body and the asset list, and
   presses Publish. Publishing marks it as the latest release. Now the download
   claim in the `Highlights` is backed by a real, signed, notarized asset.

6. **Homebrew cask: nothing to do.** `Casks/alfred-os.rb` points at
   `/releases/latest/download/Alfred.dmg` with `sha256 :no_check`, so it tracks
   each new signed build with no per-release sha bump. The `.dmg` is
   Developer-ID signed and notarized (Gatekeeper verifies it), and each release
   carries a `checksums.txt` for manual verification. See the cask header for
   the per-release-pinning alternative if you ever want it.

7. **Verify the download page.** Re-run the `Site` workflow and verify the live
   page. The page points at the latest release's stable asset name, so no site
   code change is needed.

## Order that keeps the claim honest

The single rule: the release stays a draft until the signed desktop assets are
attached. Step 3 creates the draft, step 4 attaches the signed `.dmg`
automatically, and step 5 is the human gate that makes the download claim public
only after both are done. Do not add `--latest` to the `release.yml` create or
edit step, because `--latest` publishes the release and removes the draft gate.
