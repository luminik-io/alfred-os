# Releasing Alfred

How a tagged Alfred release goes out, end to end. This is the process runbook.
For the pre-tag gate list (tests, scrub, docs build) see
[`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md). For how the desktop installer is
built and what artifacts it produces, see
[`DESKTOP_CLIENT.md`](DESKTOP_CLIENT.md).

## Why the release starts as a draft

The release body is the `Highlights` block from `CHANGELOG.md`. From v0.5.0 on,
that block tells the reader the native desktop app is signed and can be
downloaded. The signed `.dmg` (macOS) and `.AppImage` / `.deb` (Linux) assets
are produced and attached by a separate signing workflow that runs against the
tag, not by `release.yml`. `release.yml` only creates the release and prints the
source tarball checksum for the Homebrew formula.

So the release is created as a **draft**. A draft is not public and is not the
latest release. That keeps the download claim honest: nobody can read
"download the signed app" on a published release page until the signed assets
are actually attached. A human attaches the assets and presses Publish.

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
   - creates (or updates) the GitHub release as a **draft**,
   - prints the source tarball `sha256` for the Homebrew formula.

   At this point the release exists but is not public and has no signed assets.

4. **Run the signed desktop release workflow against the tag.** This is the
   separate signing pipeline. It builds the signed macOS `.dmg` and the Linux
   `.AppImage` / `.deb` from the tagged source and uploads them to the draft
   release created in step 3. The desktop bundle version is already aligned to
   the release in the prep step (`clients/desktop/package.json` and
   `src-tauri/Cargo.toml` are set to the release number, and `tauri.conf.json`
   reads the version from `package.json`), so the signed installers carry the
   release version with no separate manual bump here. Confirm every expected
   asset is attached before moving on.

5. **Publish the release.** Once the signed assets are attached, a human opens
   the draft release, checks the body and the asset list, and presses Publish.
   Publishing marks it as the latest release. Now the download claim in the
   `Highlights` is backed by real, signed, attached assets.

6. **Update the Homebrew formula.** Put the `sha256` from step 3 into
   `Formula/alfred-os.rb` and push it to the tap.

7. **Flip the download page links.** Point the site download links at the
   published release assets, then re-run the `Site` workflow and verify the live
   page.

## Order that keeps the claim honest

The single rule: the release stays a draft until the signed assets are attached.
Steps 3 and 4 produce the release and the assets; step 5 is the human gate that
makes the download claim public only after both are done. Do not add `--latest`
to the `release.yml` create or edit step, because `--latest` publishes the
release and removes the draft gate.
