# Alfred proof-telemetry Worker

A tiny Cloudflare Worker that collects **anonymous, aggregate-only** usage
counts from reporting Alfred installs, and serves the public totals to the
marketing site's `/impact` counter.

It is the server half of Alfred's aggregate usage counter. Alfred uses the
hosted collector by default; set `ALFRED_TELEMETRY_ENABLED=0` to opt out. Set
`ALFRED_TELEMETRY_URL` only when you want a self-hosted collector. See
[`docs/TELEMETRY.md`](../../docs/TELEMETRY.md) for the full contract.

This Worker powers Alfred's hosted counter and remains self-hostable for forks
or private counters.

## What it does

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/register` | `POST` | Issues a per-install write token for the given anonymous install id. The hosted collector requires this token before `/ingest` accepts a report. |
| `/ingest` | `POST` | One install reports its cumulative lifetime counts, or asks to remove its stored record during opt-out. The Worker keeps exactly one record per install (latest-wins) and writes only that install's key. Server-to-server only: requires `application/json`, no browser CORS, Origin allowlist, optional shared token, per-IP rate limit. |
| `/stats` | `GET` | Returns the public totals, **derived on read** by summing every install's latest counts (behind a short cache). The only route with browser CORS, scoped to your site origin. |
| `/` | `GET` | A small JSON service descriptor. |

### Ingest payload (exactly what an install sends)

```json
{
  "install_id": "a-random-opaque-token",
  "period": "lifetime",
  "prs_opened": 42,
  "prs_merged": 31,
  "prs_reviewed": 18,
  "issues_opened": 19,
  "issues_closed": 14,
  "files_changed": 1287,
  "lines_changed": 0,
  "loc_added": 1287
}
```

- `install_id`: a random token the install generates once and persists. It is
  not derived from a hostname, MAC, email, or anything identifying. The Worker
  treats it as the unit of de-duplication (one stored record per install) and
  never resolves it to anything.
- `period`: advisory metadata only. The reporter sends `"lifetime"`. It is NOT
  part of any storage key, so a calendar rollover can never re-add a constant
  total. A missing or malformed value defaults to `"lifetime"`.
- The count fields are non-negative integers, the install's cumulative
  lifetime total (not an increment). The Worker clamps each to `[0, 100000]`
  (the per-install cap) and ignores any other field in the body.
- `loc_added` is a legacy alias for `files_changed`. If one is missing, the
  Worker copies the other so old and new reporters aggregate the same file
  count.
- `tombstone: true` with an `install_id` removes that install's stored record.
  `alfred telemetry off` sends this before writing the local opt-out.

### What the Worker stores (the entire stored shape)

All state lives in one Workers KV namespace bound as `TELEMETRY`:

| Key | Value | Why it exists |
| --- | --- | --- |
| `auth:<install_id>` | `{token_sha256, created_at}` | Per-install write credential for the hosted collector. The raw token is returned once from `/register` and is never stored by the Worker. |
| `install:<install_id>` | `{prs_opened, prs_merged, prs_reviewed, issues_opened, issues_closed, files_changed, lines_changed, loc_added, seen_at}` | The single latest snapshot for one install, replaced on every report. It is the only source of truth: `/stats` sums these on read. Its presence is also the distinct-install marker (no separate key). |
| `stats:cache` | `{prs_opened, prs_merged, prs_reviewed, issues_opened, issues_closed, files_changed, lines_changed, loc_added, installs, updated_at}` | Optional short-lived cache of the derived totals (TTL `STATS_CACHE_TTL_SECONDS`, default 300s). A pure read optimization so a burst of `/stats` reads does not re-list every install. Never written by `/ingest`; deleting it only forces a recompute. |

**Never stored, never logged:** IP addresses, user agents, repo names, file
paths, code, commit text, handles, or anything that identifies a person or
install.

### Data model: latest-wins per install, totals derived on read (no double count, no lost count)

The Worker stores **one record per install**, keyed by `install_id`, and
replaces it on every report. `/ingest` writes **only** that one install's key:
there is no shared running aggregate. The public total is **derived on read** by
`/stats`, which lists the `install:*` keys and sums each install's latest counts
(behind a short cache). So:

- The first report from an install contributes its full counts to the sum.
- A re-send of the same lifetime total replaces the record with an identical
  value, so the sum is unchanged, forever, no matter how often or for how long
  the install reports.
- A re-send with higher numbers replaces the record, so only the increase shows
  up in the sum.
- A downward correction replaces the record with the lower value; the sum is
  always over non-negative per-install counts, so the total never goes negative.

Because the key is the `install_id` alone (never a per-period bucket), a calendar
rollover cannot re-add a constant lifetime total. And because the total is summed
from independent per-install records rather than incremented, two installs
reporting at the same instant write disjoint keys and **neither can lose the
other's counts**. The public total always equals the sum of installs' latest
values by construction. This is the core no-double-count, no-lost-count
guarantee and what lets an install safely post every day.

### Write protection and the abuse surface

This Worker backs a public aggregate counter, so be honest with yourself about
what it can and cannot guarantee. The controls, in layers:

1. **No "simple" requests.** `/ingest` requires `Content-Type: application/json`
   and rejects `text/plain` and form content types with `415`. A cross-origin
   browser can fire a `text/plain` POST WITHOUT a CORS preflight (it only hides
   the response), so requiring JSON forces any browser caller into a preflight,
   which closes that silent-write bypass. Server-side clients already send JSON,
   so this costs them nothing.
2. **No browser CORS on `/ingest`, plus an Origin allowlist.** The endpoint
   never reflects a request Origin and never falls back to `*`. Any request that
   carries an `Origin` header (i.e. a browser) must match `ALLOWED_ORIGIN` or it
   is refused with `403`. Server-side callers send no Origin and pass through.
   CORS is scoped to `ALLOWED_ORIGIN` on `GET /stats` only.
3. **Per-install tokens.** Set `REQUIRE_INSTALL_TOKEN=1` and installs must call
   `/register` before `/ingest`. The Worker stores only a hash of the returned
   token. Alfred stores the raw token locally and sends it with each report.
4. **Trusted progress totals.** Set `TRUSTED_COUNTS_ONLY=1` on the hosted
   collector and store `TRUSTED_INGEST_TOKEN` as a Worker secret. Anonymous
   installs still count as active installs, but their self-reported PR, issue,
   file, and line totals do not move the public impact numbers unless the
   request carries `X-Alfred-Trusted-Token`. This is the important guardrail for
   a public OSS project: the client is open-source, so it cannot keep a global
   secret.
5. **Optional shared token.** Set `INGEST_TOKEN` (prefer
   `wrangler secret put INGEST_TOKEN`) when a self-hosted collector should use
   one shared token instead of per-install registration.
6. **Per-IP rate limit (best-effort).** A coarse fixed-window counter (default
   60/hour, set `INGEST_RATE_LIMIT`) slows a single source spraying distinct
   `install_id`s. It is intentionally **approximate under burst**: Cloudflare KV
   has no atomic increment, so a tight burst of concurrent requests from one IP
   can each read the same counter value before any write lands and slip past the
   limit (it under-counts, never over-counts). That is acceptable because this
   limiter is a speed bump, not the inflation bound (see step 6, step 7, and the
   Concurrency note). The bucket key never holds the raw IP: it is a **keyed**
   hash (HMAC-SHA-256 of the IP under a server-side salt, `RATE_LIMIT_SALT`), so
   a KV reader cannot brute-force the ~4 billion dotted-quad space back to the IP
   without the salt. If `RATE_LIMIT_SALT` is unset the Worker fails safe with a
   per-isolate ephemeral random salt (the IP is still unrecoverable from the
   key); set the salt for stable buckets that survive isolate recycling.
7. **Per-install count cap.** Each field is clamped to `[0, 100000]`
   (`lines_changed` has its own higher cap), so a bad trusted reporter cannot
   send absurd values. In hosted mode, untrusted installs cannot move these
   progress totals at all.
8. **Latest-wins idempotency.** Re-sends from the same `install_id` replace its
   record rather than adding, and the total is summed from records on read, so
   they cannot inflate it.

**Residual surface, stated plainly:** per-install tokens block unauthenticated
overwrites and casual server-side writes, but they are not remote attestation.
For the hosted public counter, keep `TRUSTED_COUNTS_ONLY=1` so anonymous installs
can show adoption without being able to alter progress totals. If you turn that
off, or intentionally run a self-hosted collector with both `REQUIRE_INSTALL_TOKEN`
and `INGEST_TOKEN` unset, those numbers are best-effort self-reports.

### Concurrency note

The **counts** have no cross-request read-modify-write to race. `/ingest` writes
only its own `install:<id>` key (an idempotent latest-wins upsert), and `/stats`
derives the total by summing those keys on read. Two installs posting in the same
instant write disjoint keys, so neither can clobber the other and no count is
ever lost. The public total is, by construction, always the sum of every
install's latest stored value.

An earlier design kept a single incremented `agg` key (`agg += new - prior`).
That was racy: Cloudflare KV has no atomic read-modify-write, so two concurrent
ingests could both read the same `agg` and the last `put` would win, permanently
dropping one install's delta. Deriving on read removes that failure mode without
a Durable Object. The `stats:cache` key bounds the per-read list cost and is only
a read optimization (short TTL, never written by `/ingest`), so it cannot
introduce a write race; at worst it serves a value up to `STATS_CACHE_TTL_SECONDS`
old, which is fine for an aggregate counter.

The **one** remaining read-modify-write is the per-IP rate-limit counter
(`rl:<h>:<win>`), and it is deliberately best-effort. KV has no atomic increment,
so a tight burst of concurrent requests from one IP can each read the same value
before any `put` lands and slip past `INGEST_RATE_LIMIT` (it under-counts, never
over-counts). This is safe by design: the limiter is a coarse speed bump, and
inflation is bounded **without** it by the per-install idempotent upsert, the
`MAX_PER_FIELD` cap and the derive-on-read total (see "Residual surface" above),
so an approximate limiter cannot move the credible ceiling. We considered
making it atomic and chose not to: the Cloudflare Workers Rate Limiting binding
only supports a fixed 10s or 60s period and so cannot express this
env-configurable per-hour window, and a Durable Object is unwarranted weight
for a soft speed bump on a free-tier counter whose abuse bound does not depend
on the limiter. A token gate, either per-install or shared, is the write
credential.

## Deploy steps

You need a Cloudflare account (the free plan is enough) and
[`wrangler`](https://developers.cloudflare.com/workers/wrangler/) installed
(`npm install -g wrangler`, then `wrangler login`).

1. **Create the KV namespace** (production and a preview namespace for
   `wrangler dev`):

   ```sh
   cd telemetry/worker
   wrangler kv namespace create TELEMETRY
   wrangler kv namespace create TELEMETRY --preview
   ```

   Each command prints an `id`. Copy them into `wrangler.toml`:
   `id` (production) and `preview_id` (preview).

2. **Fill in `wrangler.toml`:**
   - `account_id`: from `wrangler whoami` or the dashboard URL.
   - `vars.ALLOWED_ORIGIN`: your deployed site origin, e.g.
     `https://alfred.example.com`. This is the CORS allow-origin for `/stats`.
   - `vars.REQUIRE_INSTALL_TOKEN`: keep this at `"1"` when you want installs to
     register before they can write.
   - the two KV ids from step 1.

3. **Set a rate-limit salt** so the per-IP rate-limit bucket keys
   are keyed and the client IP cannot be recovered from KV. If you skip this the
   Worker fails safe with a per-isolate ephemeral salt, but buckets then reset on
   isolate recycle, so set it for stable rate limiting:

   ```sh
   wrangler secret put RATE_LIMIT_SALT   # paste a long random value
   ```

   Optional shared-token mode for private self-hosted collectors:

   ```sh
   wrangler secret put INGEST_TOKEN   # paste a long random value
   ```

   **(Optional) Tune the stats cache.** `STATS_CACHE_TTL_SECONDS` controls how
   long `GET /stats` caches the derived totals. It defaults to `300`, which keeps
   a continuously hit public impact page well under the free KV list/day limit
   while still updating within minutes. Cloudflare KV rejects a cache write below
   60 seconds, so a value of `1`-`59` is clamped UP to `60` (and logged) rather
   than silently never caching. Set `0` to disable the cache and recompute on
   every read. Higher values cut list cost on read bursts at the price of
   staleness; the cache can never change the derived answer.

4. **Deploy:**

   ```sh
   wrangler deploy
   ```

   Wrangler prints the Worker URL, e.g.
   `https://alfred-proof-telemetry.<your-subdomain>.workers.dev`.

5. **Wire the install reporter** for a self-hosted counter:

   ```sh
   alfred telemetry on --url https://alfred-proof-telemetry.<your-subdomain>.workers.dev/ingest
   ```

   For the hosted Alfred collector, users just run `alfred telemetry on` or keep
   the default install settings. `alfred telemetry off` removes this install's
   stored record, then opts out locally. The `proof-telemetry` scheduler row can
   stay installed; after opt-out it exits cleanly and sends nothing.

6. **Point the site counter at the read endpoint:** set the build-time env var
   when building the site:

   ```sh
   PUBLIC_ALFRED_TELEMETRY_STATS_URL=https://alfred-proof-telemetry.<your-subdomain>.workers.dev/stats npm run build
   ```

### Smoke test after deploy

```sh
# Register an anonymous install id. The hosted collector requires this before
# ingest accepts a report.
TOKEN="$(
  curl -sX POST https://<your-worker-url>/register \
    -H 'Content-Type: application/json' \
    -d '{"install_id":"smoke-test-0001"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
)"

# Send a sample payload.
curl -sX POST https://<your-worker-url>/ingest \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"install_id":"smoke-test-0001","period":"lifetime","prs_opened":3,"prs_merged":2,"prs_reviewed":1,"issues_opened":4,"issues_closed":3,"files_changed":120,"lines_changed":0,"loc_added":120}'

# Read the public totals back.
curl -s https://<your-worker-url>/stats
```

To clear the smoke-test data, delete the `install:<your-install-id>` record from
the KV namespace in the dashboard, or
`wrangler kv key delete --binding TELEMETRY install:smoke-test-0001`. The total
is derived from the install records, so removing the record removes its
contribution; the short-lived `stats:cache` key expires on its own (or delete it
to recompute immediately).

## Local development

```sh
cd telemetry/worker
npm test          # unit tests, no network, in-memory KV stub
wrangler dev      # local Worker against the preview KV namespace
```

## Tests

`npm test` runs `node --test` over `test/`. It covers input clamping, payload
validation, the latest-wins-per-install upsert and its idempotent re-sends, the
lifetime no-double-count contract (including a changed period label not
re-adding a constant total), distinct-install counting, derived-on-read summing
across installs, concurrent and interleaved ingests both landing in the derived
total (no count lost), the "total equals the sum of installs' latest values"
invariant, the derived-stats cache (populate, serve-from-cache, invalidate on
ingest, and `STATS_CACHE_TTL_SECONDS=0` disabling it), the simple-request
rejection (text/plain and form bodies refused), the Origin allowlist on
`/ingest`, the write gate (token accept/reject and open-write mode), the per-IP
rate limit (including the keyed HMAC bucket hash: two IPs map to distinct
buckets, the hash is salt-keyed and non-reversible, and the raw IP never appears
in the KV key), the `/ingest` CORS lockdown, and the HTTP surface (ingest ->
stats round-trip, scoped `/stats` CORS, 400/404). No network or real Cloudflare
account is touched.
