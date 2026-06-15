# Telemetry

Alfred has an **opt-in, off-by-default** telemetry reporter. This page is the
full contract: exactly what it sends, exactly what it never sends, the single
switch that controls it, and how to run your own collector.

If you do nothing, telemetry is off and stays off. There is no hidden default,
no "anonymous by default" that is really on, and no second switch.

## The one rule

Telemetry runs only when `ALFRED_TELEMETRY_ENABLED=1`.

- Unset: OFF (this is the default for every install).
- Set to anything other than the single character `1` (including `true`, `yes`,
  `on`): OFF.
- Set to `1`: ON, and only then.

Remove the variable and the reporter is a no-op again on the next run. There is
no separate "disable" step and no cached state that keeps it running.

## Why it exists

One reason: a public proof counter. The marketing site shows an aggregate like
"Alfred has opened N pull requests and merged M across K installs." That number
is only credible if it comes from real installs, so installs that want to be
counted can opt in to contribute their anonymous totals.

It is not analytics, not crash reporting, not feature tracking, and not tied to
any account. It is a counter.

## Exactly what is sent

When enabled, once a day, the reporter POSTs this JSON and nothing else to the
URL in `ALFRED_TELEMETRY_URL`:

```json
{
  "install_id": "a-random-opaque-token",
  "period": "2026-06",
  "prs_opened": 42,
  "prs_merged": 31,
  "prs_reviewed": 18,
  "loc_added": 12873
}
```

| Field | What it is | Where it comes from |
| --- | --- | --- |
| `install_id` | A random URL-safe token generated locally on first opt-in and stored at `$ALFRED_HOME/state/telemetry-install-id`. Not derived from hostname, MAC, user, or email. | `secrets.token_urlsafe` |
| `period` | The current month, UTC, e.g. `2026-06`. A coarse bucket so the server can de-duplicate re-sends. | the clock |
| `prs_opened` | Count of PRs the local fleet-brain has cached. | `github_items` (kind = pr) |
| `prs_merged` | Of those, how many merged. | `github_items` state = merged |
| `prs_reviewed` | Of those, how many reached a terminal state (merged or closed), which in Alfred's flow means they went through review. Never exceeds `prs_opened`. | `github_items` state in (merged, closed) |
| `loc_added` | A file-delta count: one per repo file an agent added or modified. The brain does not store per-line LOC, so this is a file count carrying the wire name `loc_added` for forward compatibility. | `file_touches` rows |

Counts are clamped to `[0, 100000]` before sending. The server clamps again.

## Exactly what is never sent

- IP addresses (the collector does not store them either)
- Hostnames, usernames, emails, or any machine identifier
- Repo names, organization names, or URLs
- File paths, file contents, diffs, or commit messages
- Branch names, PR titles, or issue text
- Slack handles, channel names, or codenames
- LLM prompts, responses, or token counts
- Anything that could identify a person, a company, or a machine

The `install_id` is the only persistent identifier, and it is random. The
server uses it only to avoid double-counting and to count distinct installs. It
is never resolved to anything.

## Turning it on

1. Decide where the counts go. Either deploy the bundled collector
   (see below) or point at any endpoint that accepts the payload above.

2. Add both variables to `~/.alfredrc`:

   ```sh
   ALFRED_TELEMETRY_ENABLED=1
   ALFRED_TELEMETRY_URL=https://your-worker.example.com/ingest
   ```

   If `ALFRED_TELEMETRY_URL` is missing, the reporter no-ops even with the
   switch on, it will not guess a host.

3. Uncomment the `proof-telemetry` line in `launchd/agents.conf` (copied from
   `agents.conf.example`) and re-run `deploy.sh` so the scheduler picks it up.
   `alfred-init` also offers this as an opt-in prompt (default No).

## Turning it off

Remove `ALFRED_TELEMETRY_ENABLED` from `~/.alfredrc` (or set it to anything but
`1`). The next scheduled run is a no-op. Optionally re-comment the
`proof-telemetry` line in `agents.conf` so the job is not even loaded.

To also forget the local id, delete
`$ALFRED_HOME/state/telemetry-install-id`.

## See what would be sent, without sending

```sh
ALFRED_TELEMETRY_ENABLED=1 python3 bin/proof-telemetry.py --dry-run
```

This builds the payload from your local counts and prints it. It does not POST.
With the switch off, `--dry-run` generates nothing and prints
`[PROOF-TELEMETRY-DISABLED]`.

## Running your own collector

Alfred does not operate a telemetry endpoint, and nothing in a default install
phones home. The server half is a self-hostable Cloudflare Worker in
[`telemetry/worker/`](../telemetry/worker/):

- `POST /ingest` folds one install's per-period counts into running aggregates,
  idempotently per `{install_id, period}`.
- `GET /stats` returns the public aggregate totals (CORS for your site origin).

It stores only the aggregate, a last-seen snapshot per `{install_id, period}`
(for idempotency), and a presence marker per install (for the distinct count).
No IPs, no PII. The exact stored shape and the deploy steps are in
[`telemetry/worker/README.md`](../telemetry/worker/README.md).

## Reading list

- The reporter: [`lib/proof_telemetry.py`](../lib/proof_telemetry.py) and the
  scheduler wrapper [`bin/proof-telemetry.py`](../bin/proof-telemetry.py).
- The collector: [`telemetry/worker/`](../telemetry/worker/).
- The site counter: `site/src/components/marketing/ImpactCounter.astro` and the
  `/impact` page.
