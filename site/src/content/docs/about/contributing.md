---
title: Contributing
description: How to propose changes, the design constraints we hold, the PR review flow.
---

Full guide at [`CONTRIBUTING.md`](https://github.com/luminik-io/alfred-os/blob/main/CONTRIBUTING.md). The shape:

## Read first

- [Architecture](/concepts/architecture/): the design rationale.
- [Roadmap](/about/roadmap/): what's in flight and what's out.
- The constraints in [Architecture → "What this rules out"](/concepts/architecture/#what-this-rules-out).

PRs that fit get reviewed. PRs that broaden scope get politely declined.

## Local dev

```sh
git clone https://github.com/luminik-io/alfred-os.git
cd alfred-os
uv run --with pytest pytest tests/         # 35 tests, ~2s
uv run --with 'ruff>=0.6' ruff check .
uv run --with 'mypy>=1.10' mypy lib/
```

Pre-commit (recommended):

```sh
brew install pre-commit
pre-commit install
```

## What we accept

- **Bug fixes**: always welcome. File the bug first if you didn't.
- **Test coverage**: always welcome. Alfred aims for 100% on `agent_runner.py` over time.
- **Doc fixes / clarifications**: always welcome.
- **New examples** under `examples/bin/`: welcome if they show a useful pattern not already covered.
- **In-flight roadmap items**: welcome if you've sketched the design in the related issue first.
- **New `agent_runner` primitives**: welcome if there's a clear pattern in real fleets that justifies framework-level support.

## What we don't accept

- **Multi-tenant patterns.** No.
- **Web UI / dashboard.** No.
- **Long-running orchestration loops.** No.
- **Hosted-service hooks.** No.
- **Renaming / moving things for aesthetic reasons** without a migration story.
- **Adding Python deps to `pyproject.toml`** without justifying why the stdlib doesn't do it.

## Commit + PR conventions

- Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`.
- One topic per PR. Stack PRs for related changes.
- Update `CHANGELOG.md` under `Next` for any user-visible change.
- Update `docs/` and `site/` for any user-visible change.
- The PR template in [`.github/PULL_REQUEST_TEMPLATE.md`](https://github.com/luminik-io/alfred-os/blob/main/.github/PULL_REQUEST_TEMPLATE.md) has the verification checklist.

## Codename proposals

For a production codename that is specific to your own fleet, keep it in your fleet repo. Alfred is the framework; codenames are fleet-specific unless they demonstrate a reusable pattern.

For a new example codename in `examples/bin/`, open a feature request issue with the codename + role + 100-line sketch. We'll respond with the design call before you write the PR.

## Review flow

PRs go through:

1. CI (pytest + ruff + mypy + shellcheck + scrub-check) on every push.
2. CodeRabbit (auto-installed on the repo) for prose-style review.
3. Codex (auto-installed) for code-level review.
4. Maintainer human review for scope alignment + design.

Expect 1-3 weeks for non-urgent changes. If you need faster review for a serious bug, label the PR `severity:p0`.

## License

By submitting a PR you agree your contribution is licensed under the project's MIT license. No CLA, no DCO sign-off required (we may add one if the project ever grows beyond solo-maintainer).
