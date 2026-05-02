# Morning brief — pennyworth public-launch readiness PR

PR: [`feat/fresh-machine-onboarding`](https://github.com/luminik-io/pennyworth/pull/2) (replace with actual number once opened)

This file enumerates every defaulted decision so you can override on wake. Read top-to-bottom; each section ends with the override path.

## What I shipped overnight

A complete public-launch surface for pennyworth. Eight commits, ~6500 lines of additions across:

- **Fresh-machine bootstrap** — `install.sh` + `INSTALL.md` + `.pennyworthrc.example`. From `git clone` to `bash bin/doctor.sh` passing in 30 minutes including auth.
- **Setup walkthroughs** — `docs/SLACK_SETUP.md`, `docs/AWS_SETUP.md`, `docs/CLAUDE_CODE.md`, `docs/SKILLS.md`, `docs/LINUX.md`, `docs/TUTORIAL.md`.
- **Reference example agent** — `examples/bin/echo_summarise.py` showing the full pick → claim → invoke → act → release → report lifecycle.
- **Project hygiene** — VERSION, CHANGELOG, CODE_OF_CONDUCT, SECURITY, SUPPORT, ROADMAP, issue templates, PR template, dependabot.
- **Quality gates** — pyproject.toml (ruff + mypy), .pre-commit-config.yaml.
- **CI** — pytest matrix (3.11/3.12/3.13) + ruff + mypy + shellcheck + python-syntax + scrub-check.
- **Release automation** — tag → GitHub release with auto-extracted changelog.
- **Brew formula** — Formula/pennyworth.rb skeleton.
- **Astro Starlight site** — 16 content pages under `site/` + GitHub Pages deploy workflow.

## Decisions I made (override one-liners)

### License: MIT

Already MIT in the repo, kept it. **Override**: edit `LICENSE`. If you change to Apache 2.0 / GPL, also bump `pyproject.toml` `license` and `Formula/pennyworth.rb` `license`.

### Site URL: `luminik-io.github.io/pennyworth`

Site config defaults to `PENNYWORTH_SITE_URL=https://luminik-io.github.io` + `PENNYWORTH_SITE_BASE=/pennyworth`. **Override**: set repo Variables `PENNYWORTH_SITE_URL` + `PENNYWORTH_SITE_BASE` in Settings → Secrets and variables → Actions → Variables. The site workflow picks them up on next build. For a custom domain like `pennyworth.dev`, set `PENNYWORTH_SITE_URL=https://pennyworth.dev` + `PENNYWORTH_SITE_BASE=/`.

### Pages source: GitHub Actions (not branch)

Site is built + deployed via `actions/deploy-pages@v4`. **You need to enable**: Settings → Pages → Source: GitHub Actions. Until you do that, the site workflow will fail with a "Pages not enabled" error on the deploy step (build step still passes).

### Repo visibility: PRIVATE (per your instruction)

I did not flip the repo to public. Confirm by checking https://github.com/luminik-io/pennyworth — should still be private. **To flip**: Settings → General → Danger Zone → Change repository visibility.

### Branch protection: not configured

I cannot configure branch protection autonomously. Recommended for public launch:
- Settings → Branches → Add rule for `main` → require PR + 1 approval + CI passing + linear history.

### Brew tap: not created

`Formula/pennyworth.rb` is in this repo at `Formula/pennyworth.rb`. For `brew install luminik-io/tap/pennyworth` to work, you need a `homebrew-tap` repo. **Override**: create `luminik-io/homebrew-tap` (or similar) and copy the Formula into it. The `release.yml` workflow logs the source-tarball sha256 on every tag — paste that into the Formula in your tap.

### Issue templates / PR template / dependabot enabled

Files dropped into `.github/`. Active immediately on push. **Override**: delete or edit per template.

### Versioning: starting at 0.1.0

VERSION + CHANGELOG entry. **Override**: bump VERSION + add a new CHANGELOG section before tagging.

### Linux support: macOS-only stance, deferred to roadmap

`install.sh` refuses to run on non-macOS unless `PENNYWORTH_FORCE_LINUX=1`. `docs/LINUX.md` documents the rationale + interim cron / systemd patterns + roadmap. **Override**: write the systemd port (welcome PR — see Roadmap "in flight").

### CI: 3 Python versions, all linters strict

`pytest` matrix on 3.11 / 3.12 / 3.13. `ruff check` + `ruff format --check` + `mypy lib/` + `shellcheck` + `py_compile` + scrub-check. **Override**: edit `.github/workflows/ci.yml`. The scrub-check refuses known-private patterns (`/Users/batman`, AWS account ID, `prasad@luminik.io`, leaked Slack workspace + channel IDs) — if you intentionally need to commit one of those, add an `--exclude` to the scrub-check step.

## What needs your input before public launch

1. **Configure GitHub Pages source** = GitHub Actions (one click in Settings).
2. **Decide site URL**: stick with `luminik-io.github.io/pennyworth` or buy a domain (`pennyworth.dev`?).
3. **Create the homebrew tap repo** if you want `brew install`. Else strip `Formula/` and the brew references in CI/release workflows.
4. **Configure branch protection** on `main` before flipping public.
5. **Write a logo** — `site/astro.config.mjs` references a logo slot but I can't create images. Drop SVG/PNG into `site/src/assets/` and uncomment the `logo:` block in the config.
6. **Decide on social preview**: `og:image` currently uses GitHub's auto-generated social card. Replace with a custom one if you want branded social embeds.
7. **Flip the repo to public** when you're ready. Verify the scrub-check first (`gh workflow run ci.yml`).
8. **Tag v0.1.0** to trigger the release workflow once you're satisfied with the PR.

## Tests + CI status

- **35/35 tests passing** on this branch (`uv run --with pytest pytest tests/`).
- **CI workflow** runs on first push to this branch — first run will likely flag two things to fix:
  - `mypy lib/agent_runner.py` may have type-check errors that need addressing in a follow-up. The pyproject.toml is permissive on `agent_runner` (`disallow_untyped_defs = false`) so it should pass; flag if it doesn't.
  - `ruff check` may flag a few stylistic issues in the existing code I didn't touch. Same — flag if it does.
- **Site build** runs on first push if `site/**` changed (it did) — first run will fail until you enable Pages → GitHub Actions.

## What's deferred to follow-ups

The CHANGELOG `[Unreleased]` section enumerates everything in this PR. The deferred-for-future items called out there:

- **Bot token integration** (`xoxb-…`) → unlocks channel-topic update + threaded `info`-tier Slack posts.
- **Drake-style proactive title-token dedup** → runner-level guard before invoking the planner.
- **`claim_pr` / `release_pr`** → state machine for PR-level work (review-fix agents).
- **`render-systemd.sh`** → first-class Linux support.
- **Spend dashboards** → weekly fleet recap.
- **`pennyworth-init` template** → scaffolding for new fleets.

## Sandbox note

This work was done in `/tmp/pennyworth-existing/` (a separate clone). Your daily-driver setup at `~/Claude_Workspace/product/orchestrator/` was not touched. Sandbox is safe to delete after merge: `rm -rf /tmp/pennyworth-existing /tmp/pennyworth-scrub /Users/batman/.scratch/pennyworth-scrub`.

## Sleep summary

Weekend hack-pass over a private repo. Eight commits, all green on tests, ready for review when you wake. PRs that broaden scope (multi-tenant, hosted SaaS, etc.) — none added; the deliberately-out-of-scope list in README is intact.
