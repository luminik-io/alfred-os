# Alfred proof-telemetry Worker

A tiny Cloudflare Worker that collects **anonymous, aggregate-only** usage
counts from Alfred installs that have opted in, and serves the public totals to
the marketing site's `/impact` counter.

It is the server half of an opt-in feature. The install half (the agent-side
reporter) is **off by default** and sends nothing unless the operator sets
`ALFRED_TELEMETRY_ENABLED=1`. See [`docs/TELEMETRY.md`](../../docs/TELEMETRY.md)
for the full privacy contract.

This Worker is **not deployed by the Alfred project**. You deploy your own copy
under your own Cloudflare account if you want to run a counter. Nothing here
phones home to a Luminik-operated endpoint.

## What it does

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/ingest` | `POST` | One install reports its cumulative lifetime counts. The Worker keeps exactly one record per install (latest-wins) and the aggregate is the sum of every install's latest counts. Server-to-server only: requires `application/json`, no browser CORS, Origin allowlist, optional shared token, per-IP rate limit. |
| `/stats` | `GET` | Returns the public aggregate totals. The only route with browser CORS, scoped to your site origin. |
| `/` | `GET` | A small JSON service descriptor. |

### Ingest payload (exactly what an install sends)

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

- `install_id`: a random token the install generates once and persists. It is
  not derived from a hostname, MAC, email, or anything identifying. The Worker
  treats it as the unit of de-duplication (one stored record per install) and
  never resolves it to anything.
- `period`: advisory metadata only. The reporter sends `"lifetime"`. It is NOT
  part of any storage key, so a calendar rollover can never re-add a constant
  total. A missing or malformed value defaults to `"lifetime"`.
- The four count fields are non-negative integers, the install's cumulative
  lifetime total (not an increment). The Worker clamps each to `[0, 100000]`
  (the per-install cap) and ignores any other field in the body.

### What the Worker stores (the entire stored shape)

All state lives in one Workers KV namespace bound as `TELEMETRY`:

| Key | Value | Why it exists |
| --- | --- | --- |
| `agg` | `{prs_opened, prs_merged, prs_reviewed, loc_added, installs, updated_at}` | The public aggregate. The only thing `/stats` reads. |
| `install:<install_id>` | `{prs_opened, prs_merged, prs_reviewed, loc_added, seen_at}` | The single latest snapshot for one install, replaced on every report. It drives the latest-wins upsert, and its presence is also the distinct-install marker (no separate key). |

**Never stored, never logged:** IP addresses, user agents, repo names, file
paths, code, commit text, handles, or anything that identifies a person or
machine.

### Data model: latest-wins per install (no double count)

The Worker stores **one record per install**, keyed by `install_id`, and
replaces it on every report. The public aggregate is the sum over all installs
of each install's latest counts, maintained incrementally as
`aggregate += new - previous_for_that_install`. So:

- The first report from an install adds its full counts.
- A re-send of the same lifetime total adds zero, forever, no matter how often
  or for how long the install reports.
- A re-send with higher numbers adds only the difference.
- A downward correction subtracts, and the aggregate never goes negative.

Because the key is the `install_id` alone (never a per-period bucket), a calendar
rollover cannot re-add a constant lifetime total. This is the core no-double-count
guarantee and what lets an install safely post every day.

### Write protection and the abuse surface

This Worker backs a *public vanity counter*, so be honest with yourself about
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
3. **Optional shared token.** Set `INGEST_TOKEN` (prefer
   `wrangler secret put INGEST_TOKEN`) and every ingest must send a matching
   `X-Ingest-Token` header. Opted-in hosts put their value in
   `ALFRED_TELEMETRY_TOKEN`. This is the difference between a private counter
   (only your hosts can write) and an open one.
4. **Per-IP rate limit.** A coarse fixed-window counter (default 60/hour, set
   `INGEST_RATE_LIMIT`) slows a single source spraying distinct `install_id`s.
5. **Per-install count cap.** Each field is clamped to `[0, 100000]`, so a single
   forged `install_id` can move the aggregate by at most that much, once.
6. **Latest-wins idempotency.** Re-sends from the same `install_id` replace its
   record rather than adding, so they cannot inflate the total.

**Residual surface, stated plainly:** with `INGEST_TOKEN` unset the counter is a
fully public community counter, open to server-side writes by design. The
content-type gate and Origin allowlist stop browsers, but they do not gate
`curl`/`python`: a determined actor who rotates IPs and generates fresh
`install_id`s can still push numbers up, bounded by the per-install count cap and
the rate limit each step. The per-install idempotent upsert, the IP rate limit,
the count caps, and the site's display threshold together bound inflation, but
they make the open counter best-effort, not verified. If the counter's
credibility matters, **set `INGEST_TOKEN`** so only your opted-in hosts can write
(the hard gate). If you intentionally run it fully open, present the numbers as
best-effort and unverified.

### Concurrency note

The aggregate is maintained with a read-modify-write on KV without a
cross-request lock. For a low-frequency proof counter (each install posts at
most once a day) the practical risk of a lost update is negligible, and because
each install's stored record is its full current truth, that install's next
report self-heals any single dropped delta. If you need exactness, port the
aggregate to a Durable Object or D1 transaction; the endpoint contract stays the
same.

## Deploy steps (operator)

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
   - the two KV ids from step 1.

3. **(Recommended) Set a write token** so only your own opted-in hosts can post
   to the counter. Skip this only if you want a fully open public counter:

   ```sh
   wrangler secret put INGEST_TOKEN   # paste a long random value
   ```

4. **Deploy:**

   ```sh
   wrangler deploy
   ```

   Wrangler prints the Worker URL, e.g.
   `https://alfred-proof-telemetry.<your-subdomain>.workers.dev`.

5. **Wire the install reporter** (only on hosts you want reporting; still
   off unless `ALFRED_TELEMETRY_ENABLED=1`):

   ```sh
   # in ~/.alfredrc on that host
   ALFRED_TELEMETRY_URL=https://alfred-proof-telemetry.<your-subdomain>.workers.dev/ingest
   # only if you set INGEST_TOKEN above; must match it exactly
   ALFRED_TELEMETRY_TOKEN=the-same-random-value
   ```

6. **Point the site counter at the read endpoint:** set the build-time env var
   when building the site:

   ```sh
   PUBLIC_ALFRED_TELEMETRY_STATS_URL=https://alfred-proof-telemetry.<your-subdomain>.workers.dev/stats npm run build
   ```

### Smoke test after deploy

```sh
# Send a sample payload. Add -H 'X-Ingest-Token: <your-token>' if you set
# INGEST_TOKEN; without it a token-protected Worker returns 401.
curl -sX POST https://<your-worker-url>/ingest \
  -H 'Content-Type: application/json' \
  -d '{"install_id":"smoke-test-0001","period":"lifetime","prs_opened":3,"prs_merged":2,"prs_reviewed":1,"loc_added":120}'

# Read the public totals back.
curl -s https://<your-worker-url>/stats
```

To clear the smoke-test data, delete the `agg` key and the
`install:<your-install-id>` record from the KV namespace in the dashboard, or
`wrangler kv key delete --binding TELEMETRY agg` and friends.

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
re-adding a constant total), distinct-install counting, summing across installs,
the simple-request rejection (text/plain and form bodies refused), the Origin
allowlist on `/ingest`, the write gate (token accept/reject and open-write
mode), the per-IP rate limit, the `/ingest` CORS lockdown, and the HTTP surface
(ingest -> stats round-trip, scoped `/stats` CORS, 400/404). No network or real
Cloudflare account is touched.
