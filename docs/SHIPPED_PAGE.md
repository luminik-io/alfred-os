# The `/shipped/` page

`alfred.luminik.io/shipped/` shows a weekly roundup of the pull requests the operator's Alfred fleet has merged. The point is verifiability. The landing page claims Alfred ships while you are away. The shipped page is the audit trail.

This document explains the data flow, the two render modes, and how to populate the feed for your own fleet.

## What renders

- **Summary card** with the headline numbers for the current week: PRs merged, PRs reverted, issues closed, agents active, repos touched, LLM spend, merge-clean percent.
- **Trend** as an inline-SVG sparkline of PRs merged per ISO week, last 12 weeks. No JS, no canvas, no external library.
- **PR table**, one row per merged PR: repo and number, title, agent codename, merge time, lines added/removed, files changed. Each row links to the PR on GitHub.
- **Last-updated stamp** with the feed's `generated_at` timestamp and schema version.

## Two modes

The page detects which mode to render from the data file at `site/src/data/shipped/weekly.json`:

### Operator mode

You run Alfred. You generate the feed each week (or on whatever cadence you want):

```bash
alfred shipped --emit-public-json site/src/data/shipped/weekly.json
```

The next site build picks up the new file and renders the headline numbers, trend, and PR table. The build is hermetic. The site itself never reads `~/.alfred/state/` and never calls out to GitHub.

### Cold-fork mode

You cloned `alfred-os` and have not yet run Alfred long enough to have a real feed. The placeholder file ships with `operator: "your-org"` and `summary.prs_merged: 47` against `your-org/your-backend` style placeholder repos. The page detects this and renders the cold-fork explainer instead of pretending the placeholder data is yours.

The cold-fork explainer reads:

> This page shows the operator's own fleet. No public mock data is rendered. After you have run Alfred for a week, generate your own feed: `alfred shipped --emit-public-json site/src/data/shipped/weekly.json`.

The detection is intentionally simple: zero merged PRs in `summary.prs_merged` means cold-fork. Replace the file with a feed that has real PRs and the page lights up.

## The emitter (`bin/alfred-shipped-public.py`)

The emitter is the privacy boundary. It reads state from `$ALFRED_HOME/state/shipped/prs.json` (and optionally `trend.json`), applies the public allowlist, and writes a JSON file that conforms to `site/src/data/shipped/schema.json`.

### Allowlist

The emitter is allow-list driven, not deny-list. Only the fields explicitly listed in `PR_ALLOWED_FIELDS` are copied to the public feed:

```
repo, number, title, codename, merged_at,
lines_added, lines_removed, files_changed,
reviewed_by, url
```

PR diffs, issue bodies, author emails, comments, labels, and any other state are dropped silently. New additions to the allowlist need a deliberate code change.

### Repo scrub

A PR passes through only when its repo:

1. Matches the public slug format `owner/name`.
2. Does NOT match the built-in private-name patterns. The emitter ships with a denylist for internal product-repo basenames; any owner/name whose name segment matches is dropped, and the bare-name `alfred` basename is denied under any owner (the public `alfred-os` repo carries the `-os` suffix and stays through).
3. Is in `--public-allowlist` (if any is set) OR no allowlist is set.

Any title containing one of those private tokens has it rewritten in place to a `your-` placeholder.

### Reviewer scrub

Reviewer entries are collapsed:

- A known agent codename passes through (`lucius`, `ras-al-ghul`, `batman`, ...).
- Anything else (a human GitHub handle, an email, an unknown name) collapses to the literal string `human`.

### Codename scrub

Per-PR `codename` is normalised the same way. Unknown codenames render as `agent` so the table still visually marks the work as machine-driven, not as a human merge.

## CLI

```text
alfred-shipped-public.py
  --emit-public-json PATH          required, file path or '-' for stdout
  --state DIR                      override $ALFRED_HOME/state
  --operator NAME                  override $ALFRED_PUBLIC_OPERATOR
  --public-allowlist REPO          repeatable, overrides $ALFRED_PUBLIC_REPO_ALLOWLIST
  --since YYYY-MM-DD               window start, UTC
  --until YYYY-MM-DD               window end, UTC
  --summary-extra PATH             JSON with prs_reverted/issues_closed/agents_active/spend_cents
  --quiet                          suppress informational stderr logs
```

Env vars (12-factor):

- `ALFRED_HOME` (default `~/.alfred`)
- `ALFRED_PUBLIC_OPERATOR` (default `your-org`)
- `ALFRED_PUBLIC_REPO_ALLOWLIST` (default empty, comma-separated)

## Schema

`site/src/data/shipped/schema.json` is the source of truth for the public contract. It is JSON Schema 2020-12 with `additionalProperties: false` on every object so a leaked field surfaces as a validation failure in CI.

Schema version is `1`. If you change the shape of the feed in a backward-incompatible way, bump the version and update the site renderer to handle both.

## Weekly cron

A typical operator runs the emitter from a weekly launchd / systemd unit. The shipped repo already has `bin/shipped-summary-weekly.sh` for the Slack post; you can chain the public emit alongside it:

```bash
# in shipped-summary-weekly.sh, or a sibling script
ALFRED_PUBLIC_OPERATOR=your-org \
  alfred-shipped-public.py \
    --emit-public-json "$ALFRED_HOME/exports/shipped-weekly.json" \
    --quiet
```

Then commit the resulting JSON to your fork of `alfred-os` (or whichever Astro site renders it) and rebuild.

## Tests

`tests/test_alfred_shipped_public.py` covers:

- Empty state yields a schema-valid feed with zero PRs and a flat 12-week trend.
- Full state computes summary, sorts PRs by merged_at descending, and routes the data through the public allowlist.
- Private repos are dropped, private tokens in titles are rewritten, human reviewer handles collapse to `human`, unknown codenames collapse to `agent`.
- Fields outside the field allowlist (`diff`, `body`, `issue_body`, `author_email`) are dropped.
- The CLI works against a temp `~/.alfred/state/` and supports `--emit-public-json -` (stdout) and `--emit-public-json PATH`.
- The shipped sample `weekly.json` validates against `schema.json`.

Run:

```bash
pytest tests/test_alfred_shipped_public.py -v
```

## Privacy posture summary

| Concern | How it's handled |
| --- | --- |
| Operator home path leak | Emitter only reads `$ALFRED_HOME/state/`, never the broader workspace; output paths are operator-supplied. |
| Private repo name leak | Hard-coded private-pattern denylist, plus optional allowlist via `--public-allowlist`. |
| PR diff leak | Diffs are never read. Only the allowlisted PR metadata fields are emitted. |
| Issue body leak | Issue bodies are never read. Only the closed-issue count enters via `--summary-extra`. |
| Reviewer identity leak | Human handles collapse to `human`. Only known agent codenames pass through. |
| AWS / Slack / GitHub secret leak | None of those fields are in the allowlist. `bash bin/scrub-check.sh` catches accidental literal-value leaks in committed files. |
