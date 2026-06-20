# The `alfred-shipped-public` emitter

`bin/alfred-shipped-public.py` reads your `$ALFRED_HOME/state/` directory, applies a public field allowlist and a partner-name redaction table, and writes a `weekly.json` feed describing recent merged work. The canonical Alfred site now renders a separate public GitHub board for the `luminik-io/alfred-os` repository from `site/src/data/impact-proof.json`. Use this emitter when you want a shipped-work page for your own private or customer repos, because it scrubs local state before anything is published.

This document explains the schema, the scrub rules, and the emit command.

## When to use it

You have run Alfred for a while and you want a public-facing shipped-work page. Maybe it's part of your build-in-public story. Maybe it's the verifiability bar your customers ask for. Either way the contract is:

- You run the emitter on your own state directory.
- The emitter scrubs aggressively before writing.
- You publish the resulting JSON wherever you want.

The canonical Alfred repository does not render an operator's local emitted feed by default because partner names, customer terms, and internal product codenames vary and should not appear in upstream marketing copy.

## Canonical site product snapshot

The canonical site also has an aggregate-only Luminik product snapshot at
`site/src/data/luminik-product-proof.json`. It is deliberately not a list of
PRs. The refresh script reads a configured repo list and writes only
counts:

```sh
cd site
ALFRED_PRODUCT_PROOF_REPOS="owner/repo-a,owner/repo-b" npm run proof:product
```

The repo list stays in the environment and is never written to the data file.
That lets the public site show real product totals without publishing private
repo names, PR titles, issue titles, branches, prompts, or code.

On the canonical site, `.github/workflows/site.yml` refreshes this aggregate
snapshot on every main-branch deploy, every manual site dispatch, and the daily
site build when `ALFRED_PRODUCT_PROOF_REPOS` is set as a repository variable.
Private repo access comes from the `ALFRED_PRODUCT_PROOF_TOKEN` Actions secret.
When the repo list is configured, that secret is required on main-branch site
builds. Without it, the workflow fails instead of deploying stale totals. Forks
and public PR previews keep using the committed seed file.

## The emitter (`bin/alfred-shipped-public.py`)

The emitter is the privacy boundary. It reads state from `$ALFRED_HOME/state/shipped/prs.json` (and optionally `trend.json`), applies the public allowlist, and writes a JSON file that conforms to the schema below.

### Allowlist

Allow-list driven, not deny-list. Only fields listed in `PR_ALLOWED_FIELDS` are copied:

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

### Partner-name redaction

PR titles often mention the third-party platform an integration targets (event-data vendors, CRMs, mail providers, observability, SSO). These platforms are public companies, but the fact that your install integrates with them is private business context.

The emitter collapses partner names to neutral category words:

| Category | Sample tokens | Replacement |
|---|---|---|
| Event-data vendor | Brella, Cvent, Grip, Swapcard, Whova, Eventbrite, Hopin, Bizzabo, Pheedloop | `vendor` |
| CRM | Salesforce, HubSpot | `CRM` |
| Outreach platform | Apollo, Outreach, Salesloft | `outreach platform` |
| Email provider | Resend, Sendgrid, Mailgun, Postmark | `email provider` |
| Error tracker / telemetry | Sentry, Datadog, Honeycomb | `error tracker` / `telemetry` |
| SSO | WorkOS, Auth0, Clerk | `SSO` |

The token table lives at the top of `bin/alfred-shipped-public.py` and is extensible. Add new integrations there.

### Reviewer scrub

Reviewer entries are collapsed:

- A known agent codename passes through (`lucius`, `ras-al-ghul`, `batman`, ...).
- Anything else (a human GitHub handle, an email, an unknown name) collapses to the literal string `human`.

### Codename scrub

Per-PR `codename` is normalised the same way. Unknown codenames render as `agent` so downstream renderers can still mark the work as machine-driven.

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

## Schema (v1)

JSON Schema 2020-12, `additionalProperties: false` everywhere. The canonical machine-readable schema lives at [`schema/weekly.schema.json`](../schema/weekly.schema.json); a sample feed lives at [`schema/weekly.sample.json`](../schema/weekly.sample.json). If you change the shape in a backward-incompatible way, bump the version and update your renderer to handle both.

```json
{
  "version": 1,
  "generated_at": "ISO 8601 UTC timestamp",
  "operator": "string",
  "window": { "from": "ISO 8601", "to": "ISO 8601" },
  "summary": {
    "prs_merged": "integer >= 0",
    "prs_reverted": "integer >= 0",
    "issues_closed": "integer >= 0",
    "agents_active": "integer >= 0",
    "repos_touched": "integer >= 0",
    "spend_cents": "integer >= 0",
    "merge_clean_pct": "integer 0-100"
  },
  "trend": [
    { "week": "ISO week e.g. 2026-W21", "prs_merged": "integer >= 0" }
  ],
  "prs": [
    {
      "repo": "owner/name",
      "number": "integer >= 1",
      "title": "string (scrubbed)",
      "codename": "string (normalised to known codename or 'agent')",
      "merged_at": "ISO 8601 UTC",
      "lines_added": "integer >= 0",
      "lines_removed": "integer >= 0",
      "files_changed": "integer >= 0",
      "reviewed_by": ["string (normalised to known codename or 'human')"],
      "url": "https URL to the PR"
    }
  ]
}
```

## Suggested cadence

A weekly cron is enough for most installs. Add a launchd unit / systemd timer that runs every Saturday morning and writes the JSON to your publishing target (S3, your site's data dir, a Gist, whatever).

The emitter is idempotent: same state in, same JSON out (modulo the `generated_at` stamp). Safe to run repeatedly.
