# Batman Parent Issue Template

The body shape Batman's lifecycle parser (`parse_parent_issue` in `lib/batman.py`) expects when you file an `agent:large-feature` issue in `BATMAN_PARENT_REPO`. Mismatching the shape causes the parser to return `children=0 repos=0` silently: Batman drafts a useless plan, posts it to Slack, and the operator spends a polling cycle figuring out what went wrong (see #107 for the silent-zero behaviour).

This doc gives the validated minimal shape, lists the gotchas the parser doesn't surface, and provides a copy-paste template.

## TL;DR (copy this)

```markdown
Bundle: <short-slug-no-spaces>

Repos:
- <owner>/<repo-1>
- <owner>/<repo-2>
- <owner>/<repo-3>

Children:
- <repo-1>: <one-line scope for this repo's PR>
- <repo-2>: <one-line scope for this repo's PR>
- <repo-3>: <one-line scope for this repo's PR>

Rollout order:
- <owner>/<repo-1>
- <owner>/<repo-2>
- <owner>/<repo-3>

Done when:
- All N child PRs merged AND <observable cross-repo invariant>.
```

File the resulting issue in `BATMAN_PARENT_REPO`, label it `agent:large-feature`, and Batman will pick it up on the next firing.

## Canonical inline requirements

### `Repos:` entries should be full `<owner>/<repo>` slugs

```
Repos:
- myorg/backend          ← correct
- myorg/frontend         ← correct
- backend                ← qualified with the parent repo owner, then GH_ORG
- frontend               ← qualified with the parent repo owner, then GH_ORG
```

The canonical `Repos:` parser (`_parse_repo_lines`) keeps `owner/repo` slugs
verbatim. Bare repo names are qualified from the parent issue repo owner first,
then `GH_ORG` when the parser is called without a parent owner. Use `owner/repo`
here when a parent plan spans more than one org.

### `Children:` entries use exact repo tails

```
Children:
- backend: Add /api/v2 endpoint       ← correct for myorg/backend
- frontend: Update settings page      ← correct for myorg/frontend
- core-backend: Add cache invalidation ← correct for myorg/core-backend
- myorg/backend: ...                  ← also works but redundant
```

`_parse_children_lines` extracts `<repo>:` then `<title>` from each bullet. The short name (right-of-`/`) is resolved against the `Repos:` list via `_resolve_child_repo`.
It must match the repo tail exactly. `backend` does not match `core-backend`.

### `Bundle:` slug determines the bundle label

```
Bundle: oauth-rollout        ← becomes `agent:bundle:oauth-rollout`
```

Two gotchas:

1. **Bundle label must be ~50 chars or less** (GitHub validation HTTP 422). Long bundle slugs combined with the `agent:bundle:` prefix overflow. Keep slugs short: `auth-v2`, not `migrate-authentication-from-jwt-to-oauth-across-all-services`.

2. Batman now auto-creates per-bundle labels on target repos before filing child issues. If a target repo forbids label creation for your token, execution will report that repo as failed instead of silently continuing.

## Optional sections

You can add markdown anywhere outside the canonical sections (`## Vision`,
`## Out of scope`, `## References`, etc.). For the inline shape, Batman reads
the sections that start with `Repos:`, `Children:`, `Rollout order:`, and
`Done when:` (case-insensitive). If the canonical sections are missing but the
body contains `## Affected Repos` or `## Acceptance Criteria`, Batman may use
the loose fallback described below.

```markdown
## Vision

Why we're doing this. Multi-paragraph fine.

Bundle: oauth-rollout

Repos:
- myorg/backend
- myorg/frontend

Children:
- backend: Add OAuth2 token-exchange endpoint
- frontend: Wire login flow through the new endpoint

Rollout order:
- myorg/backend
- myorg/frontend

Done when:
- Both PRs merged AND a fresh user can log in via OAuth2 in staging.

## Out of scope

What you're explicitly NOT doing.

## Operator decision needed before plan execution

Anything you want Batman's approval reaction to gate on.
```

## Worked example (validated 2026-05-25)

A real Batman parent issue from a 3-repo product fleet. Posted, parsed, approved end-to-end; Batman drafted children correctly:

```markdown
Bundle: tier-colour-sync

Repos:
- acme/palette
- acme/palette-web
- acme/palette-companion

Children:
- palette: Confirm src/tiers.ts as canonical source-of-truth for the 5 tier
  HSL triples. Add header comment naming the contract.
- palette-web: Sync src/components/OrbStage.astro to the canonical values.
- palette-companion: Sync PaletteCompanion/Techniques.swift to the canonical
  values.

Rollout order:
- acme/palette
- acme/palette-web
- acme/palette-companion

Done when:
- All 3 child PRs merged AND the 5 tier HSL triples are byte-identical
  across tiers.ts, OrbStage.astro, and Techniques.swift.
```

Bundle slug `tier-colour-sync` → `agent:bundle:tier-colour-sync` (27 chars total, well under the GitHub limit).

## Why two parser shapes exist

The canonical lifecycle parser (`parse_parent_issue` in `lib/batman.py`) expects
the inline `Repos:` / `Children:` / `Done when:` blocks documented above.

Batman also accepts a loose Markdown fallback: `## Affected Repos` H2 blocks,
bare repo names, and `## Acceptance Criteria` H3 sections. That fallback lets
imperfect parent issues become reviewable plans, but new parent issues should
use this template. Fully-qualified fallback entries such as `acme/backend` are
preserved as written; bare fallback entries are interpreted inside the parent
repo owner unless you have an explicit repo mapping configured.

Follow this doc for `BATMAN_PARENT_REPO` parent issues.

## Validating before you commit

Run the parser against your draft body without filing the issue:

```sh
python3 - <<'PY'
import sys
sys.path.insert(0, "/path/to/alfred-os/lib")
from batman import parse_parent_issue

BODY = """
Bundle: hello

Repos:
- myorg/backend
- myorg/frontend

Children:
- backend: Build the thing
- frontend: Show the thing

Done when:
- Both PRs merged.
"""

plan = parse_parent_issue(
    body=BODY,
    title="Bundle: hello cross-repo work",
    parent_repo="myorg/specs",
    parent_issue_number=42,
)
print(f"bundle_slug: {plan.bundle_slug}")
print(f"affected_repos: {plan.affected_repos}")
print(f"children: {len(plan.children)}")
for c in plan.children:
    print(f"  {c.repo}: {c.title[:60]}")
PY
```

If `children: 0`, the body shape doesn't match: fix and re-run before filing the GitHub issue.

`bin/doctor.sh --lifecycle` runs this same parser validation with a synthetic parent issue, then checks the Slack approval surface and Claude OAuth token.

## Related

- #107: silent `children=0` when body shape doesn't match (this template is the documented mitigation).
- #115: Batman re-drafts plans on every firing while approval is pending. Combines with body-shape bugs to produce N broken plan posts in a row.
- #116: lifecycle parser no longer drops bare repo names silently. This template still tells you to use full slugs.
- #117: bundle label auto-creation on target repos + ~50-char label-length limit.
- #118: meta-tracker for the broader lifecycle hardening backlog.
- #119: `bin/doctor.sh --lifecycle` synthetic-fire validator.
- #121: `alfred-batman-setup` wizard (would walk operators through this body shape interactively).
