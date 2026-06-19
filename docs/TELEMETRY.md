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

One reason: the community counter on the public impact page. It can show how
many pull requests opted-in Alfred installs have opened, merged, and moved to a
terminal state. Those numbers are useful only if they come from real installs,
so people can opt in to contribute their anonymous totals.

It is not analytics, not crash reporting, not feature tracking, and not tied to
any account. It is a counter.

The richer GitHub activity board on `/impact` is separate. It uses public
GitHub metadata from `luminik-io/alfred-os`: merged PRs, issue flow, additions,
deletions, changed files, and PR URLs. Anonymous telemetry does not send that
detail.

## Exactly what is sent

When enabled, once a day, the reporter POSTs this JSON and nothing else to the
URL in `ALFRED_TELEMETRY_URL`:

```json
{
  "install_id": "a-random-opaque-token",
  "period": "lifetime",
  "prs_opened": 42,
  "prs_merged": 31,
  "prs_reviewed": 18,
  "loc_added": 12873
}
```

The four counts are cumulative lifetime totals, everything the local brain has
ever cached, not a per-day or per-month delta. The collector keeps exactly one
record per `install_id` (latest-wins) and the public aggregate is the sum of
every install's latest counts, so a re-send (every day, or after a calendar
month turns over) never double counts. See
[Honesty about the counts](#honesty-about-the-counts).

| Field | What it is | Where it comes from |
| --- | --- | --- |
| `install_id` | A random URL-safe token generated locally on first opt-in and stored at `$ALFRED_HOME/state/telemetry-install-id`. Not derived from hostname, MAC, user, or email. The collector keys its single per-install record on this. If the token cannot be persisted (read-only state dir), the reporter skips that run rather than mint a fresh ephemeral id, so one host never looks like many installs. | `secrets.token_urlsafe` |
| `period` | The constant `lifetime`. Advisory metadata only: the collector de-duplicates on `install_id` alone, never on this label, so a calendar rollover cannot re-add a constant total. | fixed |
| `prs_opened` | Count of **Alfred-authored** PRs the local fleet-brain has cached. The poller caches every PR it sees, so this counts only rows carrying the `agent:authored` label or an agent branch prefix, never human- or bot-opened PRs. Counted with an exact `COUNT(*)`, never silently capped at the 500-row list limit. | `github_items` (kind = pr, agent-authored) |
| `prs_merged` | Of those Alfred-authored PRs, how many merged. | `github_items` agent-authored, state = merged |
| `prs_reviewed` | Of those Alfred-authored PRs, how many reached a terminal state (merged or closed). The wire name is historical; public pages should label this as terminal PRs unless a future telemetry schema stores explicit review-agent inspections. Never exceeds `prs_opened`. | `github_items` agent-authored, state in (merged, closed) |
| `loc_added` | A file-delta count: one per repo file an agent added or modified. The brain does not store per-line LOC, so this is a file count carrying the wire name `loc_added` for forward compatibility. Public pages must label it as a changed-file proxy, not LOC. | `file_touches` rows |

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

2. Enable telemetry from the Alfred CLI:

   ```sh
   alfred telemetry on --url https://your-worker.example.com/ingest
   ```

   If your collector sets `INGEST_TOKEN`, pass the matching token:

   ```sh
   alfred telemetry on \
     --url https://your-worker.example.com/ingest \
     --token the-same-value-as-the-collector
   ```

   The command writes a managed telemetry block to `~/.alfredrc` and adds the
   `alfred.proof-telemetry` scheduler row to the source checkout's
   `launchd/agents.conf` when Alfred can identify it. Telemetry remains off by
   default; this command is the opt-in action. HTTPS is required for non-local
   endpoints; `http://localhost` is allowed for local Worker tests.

3. Re-run `deploy.sh` so launchd or systemd picks up the scheduler change.
   `alfred-init` also offers this as an opt-in prompt (default No).

## Turning it off

```sh
alfred telemetry off
```

This writes `ALFRED_TELEMETRY_ENABLED=0`, removes the telemetry scheduler row,
and removes the ingest token from the managed telemetry block. The next
scheduled run is a no-op even if an older scheduler row is still loaded.

To also forget the local id:

```sh
alfred telemetry off --delete-install-id
```

Check the current state any time with:

```sh
alfred telemetry status
alfred telemetry status --json
```

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

- `POST /ingest` upserts one install's cumulative counts (latest-wins, keyed by
  `install_id`), writing only that install's record.
- `GET /stats` returns the public totals, derived on read by summing every
  install's latest counts behind a short cache (CORS for your site origin).

It stores one latest snapshot per `install_id` (which both feeds the derived
total and serves as the distinct-install marker), plus a short-lived cache of the
derived totals. There is no incremented running aggregate, so concurrent reports
cannot lose counts. No IPs, no PII. The exact stored shape and the deploy steps
are in [`telemetry/worker/README.md`](../telemetry/worker/README.md).

### Write protection

`POST /ingest` is a server-to-server endpoint. It requires
`Content-Type: application/json` (so a no-preflight cross-origin browser POST is
refused), carries no browser CORS, and refuses any request whose `Origin` header
does not match your `ALLOWED_ORIGIN`, so a visitor's browser cannot post to it
from a web page. The collector also supports an optional shared token: set
`INGEST_TOKEN` on the Worker (and the matching `ALFRED_TELEMETRY_TOKEN` on each
opted-in host) and only hosts with the token can write. A per-IP rate limit, a
per-install count cap, and the latest-wins idempotency are always on.

## Honesty about the counts

This is a public usage counter, so be clear-eyed about what it proves.

- The counts are real aggregates from opted-in installs. They are not silently
  capped: the reporter paginates its local counts rather than truncating at a
  page limit, and re-sends fold in only the increase, so the public total tracks
  genuine cumulative work.
- With the collector's `INGEST_TOKEN` **unset**, the counter is open to
  server-side writes. CORS does not gate `curl` or a script, so a determined
  actor rotating IPs and `install_id`s could push the number up; the rate limit
  and idempotency only raise the cost. If the counter's credibility matters, set
  `INGEST_TOKEN` so only your own opted-in hosts can write. If you run it fully
  open on purpose, present the numbers as best-effort and unverified.

## Reading list

- The reporter: [`lib/proof_telemetry.py`](../lib/proof_telemetry.py) and the
  scheduler wrapper [`bin/proof-telemetry.py`](../bin/proof-telemetry.py).
- The collector: [`telemetry/worker/`](../telemetry/worker/).
- The site counter: `site/src/components/marketing/ImpactCounter.astro` and the
  `/impact` page.
