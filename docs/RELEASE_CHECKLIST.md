# Release Checklist

Use this before tagging a public alfred-os release.

## Preflight

- Confirm `VERSION` has the intended version without a leading `v`.
- Confirm `CHANGELOG.md` has a section for that version and the `[Unreleased]` section only contains future work.
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

- Host-private paths or identifiers from the extracted reference fleet.
- Real-looking Slack webhook URLs, Slack bot or app tokens, and AWS access key IDs.

Keep example secrets obviously fake, for example `xoxb-...` or `https://hooks.slack.com/services/T.../B.../...`.

## Tag And Release

1. Commit the version, changelog, and docs updates.
2. Tag from the release commit:

   ```sh
   git tag -s "v$(cat VERSION)" -m "v$(cat VERSION)"
   git push origin main --tags
   ```

3. Watch the `Release` workflow. It verifies `VERSION`, extracts notes from `CHANGELOG.md`, creates the GitHub Release, and prints the source tarball sha256 for Homebrew.
4. Update `Formula/alfred-os.rb` with the printed sha256 before publishing the tap update.
5. Smoke-test the published install path from a fresh directory.
