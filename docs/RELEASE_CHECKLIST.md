# Release Checklist

Use this before tagging a public Alfred release.

## Preflight

- Confirm `VERSION` has the intended version without a leading `v`.
- Confirm `CHANGELOG.md` has a section for that version and the `Next` section only contains future work.
- Confirm GitHub Pages is set to workflow publishing, not branch/root publishing:

  ```sh
  gh api repos/luminik-io/alfred-os/pages --jq '.build_type'
  # expected: workflow
  ```

- Run the local gates:

  ```sh
  uv run --with pytest pytest tests/
  uv run --with 'ruff>=0.6' ruff check .
  uv run --with 'ruff>=0.6' ruff format --check .
  uv run --with 'mypy>=1.10' mypy lib/
  bash bin/scrub-check.sh
  bash bin/doctor.sh
  ```

- If shell scripts changed, run `shellcheck` on the changed files.
- If docs site content changed, run `npm --prefix site run build`.

## Scrub Gate

`bash bin/scrub-check.sh` must pass before tagging. It scans tracked and untracked worktree files, excluding generated dependency trees and lockfiles, for:

- Host-private paths or identifiers from local development machines or earlier private systems.
- Real-looking Slack webhook URLs, Slack bot or app tokens, and AWS access key IDs.

Keep example secrets obviously fake, for example `xoxb-...` or `https://hooks.slack.com/services/T.../B.../...`.

## Tag And Release

1. Commit the version, changelog, and docs updates.
2. Tag from the release commit:

   ```sh
   git tag -s "v$(cat VERSION)" -m "v$(cat VERSION)"
   git push origin main --tags
   ```

3. Watch the `Release` workflow. It verifies `VERSION`, extracts notes from `CHANGELOG.md`, creates the GitHub Release as a **draft**, and prints the source tarball sha256 for Homebrew. The draft is not public yet, by design.
4. Run the signed desktop release workflow against the tag so the signed `.dmg` / `.AppImage` / `.deb` assets attach to the draft release. The release body claims a signed download, so the assets must be attached before anyone can read that claim.
5. Open the draft release, confirm the body and the attached assets, then press Publish. Publishing marks it as the latest release.
6. Update `Formula/alfred-os.rb` with the printed sha256 before publishing the tap update.
7. Re-run the `Site` workflow and verify the live docs page:

   ```sh
   gh workflow run site.yml --repo luminik-io/alfred-os --ref main
   curl -fsSL https://alfred.luminik.io/ | grep -E 'Alfred|Starlight'
   ```

8. Flip the site download links to the published release assets.
9. Smoke-test the published install path from a fresh directory.

The full tag-to-publish flow, and why the draft gate keeps the download claim
honest, is in [`RELEASING.md`](RELEASING.md).
