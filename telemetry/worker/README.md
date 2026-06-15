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
| `/ingest` | `POST` | One install folds its cumulative counts into the running totals. Server-to-server only: no browser CORS, optional shared token, per-IP rate limit. |
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
  treats it purely as a grouping key and never resolves it to anything.
- `period`: a stable lifetime bucket label (the reporter sends `"lifetime"`).
  Counts are the install's cumulative total for that bucket, not an increment.
- The four count fields are non-negative integers. The Worker clamps each to
  `[0, 100000]` and ignores any other field in the body.

### What the Worker stores (the entire stored shape)

All state lives in one Workers KV namespace bound as `TELEMETRY`:

| Key | Value | Why it exists |
| --- | --- | --- |
| `agg` | `{prs_opened, prs_merged, prs_reviewed, loc_added, installs, updated_at}` | The public aggregate. The only thing `/stats` reads. |
| `i:<install_id>:<period>` | `{prs_opened, prs_merged, prs_reviewed, loc_added, seen_at}` | Last-seen snapshot for one `{install_id, period}` pair, used only to make re-sends idempotent. |
| `ic:<install_id>` | `"1"` | A presence marker, one per distinct install, used to maintain the distinct-install count. |

**Never stored, never logged:** IP addresses, user agents, repo names, file
paths, code, commit text, handles, or anything that identifies a person or
machine.

### Idempotency

Re-sending the same `{install_id, period}` does **not** double count. The Worker
stores the last counts it saw for that pair and applies only the delta on the
next send. A re-send of identical numbers adds zero; a re-send with higher
numbers adds only the difference. The reporter sends a stable `period`
(`"lifetime"`) carrying cumulative totals, so a daily re-send, and a re-send
after a calendar month rolls over, both add nothing unless the lifetime total
actually grew. This is what lets an install safely post every day.

### Write protection and the abuse surface

This Worker backs a *public vanity counter*, so be honest with yourself about
what it can and cannot guarantee. The controls, in layers:

1. **No browser CORS on `/ingest`.** The endpoint never reflects a request
   Origin and never falls back to `*`, so a visitor's browser cannot POST to it
   cross-origin. CORS is scoped to `ALLOWED_ORIGIN` on `GET /stats` only.
2. **Optional shared token.** Set `INGEST_TOKEN` (prefer
   `wrangler secret put INGEST_TOKEN`) and every ingest must send a matching
   `X-Ingest-Token` header. Opted-in hosts put their value in
   `ALFRED_TELEMETRY_TOKEN`. This is the difference between a private counter
   (only your hosts can write) and an open one.
3. **Per-IP rate limit.** A coarse fixed-window counter (default 60/hour, set
   `INGEST_RATE_LIMIT`) slows a single source spraying distinct `install_id`s.
4. **Idempotency.** Re-sends of the same `{install_id, period}` cannot inflate.

**Residual surface, stated plainly:** with `INGEST_TOKEN` unset the counter is
open to server-side writes. CORS does not gate `curl`/`python`, so a determined
actor who rotates IPs and generates fresh `install_id`s can still push numbers
up; the rate limit and idempotency only raise the cost. If the counter's
credibility matters, **set `INGEST_TOKEN`** so only your opted-in hosts can
write. If you intentionally run it fully open, present the numbers as
best-effort and unverified.

### Concurrency note

The aggregate is maintained with a read-modify-write on KV without a
cross-request lock. For a low-frequency proof counter (each install posts at
most once a day) the practical risk of a lost update is negligible, and the
per-pair snapshots self-heal a dropped delta on the next send. If you need
exactness, port the aggregate to a Durable Object or D1 transaction; the
endpoint contract stays the same.

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

To clear the smoke-test data, delete the three keys from the KV namespace in
the dashboard, or `wrangler kv key delete --binding TELEMETRY agg` and friends.

## Local development

```sh
cd telemetry/worker
npm test          # unit tests, no network, in-memory KV stub
wrangler dev      # local Worker against the preview KV namespace
```

## Tests

`npm test` runs `node --test` over `test/`. It covers input clamping, payload
validation, idempotent re-sends, the lifetime no-double-count contract,
distinct-install counting, summing across installs, the write gate (token
accept/reject and open-write mode), the per-IP rate limit, the `/ingest` CORS
lockdown, and the HTTP surface (ingest -> stats round-trip, scoped `/stats`
CORS, 400/404). No network or real Cloudflare account is touched.
