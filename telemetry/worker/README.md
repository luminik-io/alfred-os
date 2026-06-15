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
| `/ingest` | `POST` | One install folds its per-period counts into the running totals. |
| `/stats` | `GET` | Returns the public aggregate totals. CORS-open for your site origin. |
| `/` | `GET` | A small JSON service descriptor. |

### Ingest payload (exactly what an install sends)

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

- `install_id` — a random token the install generates once and persists. It is
  not derived from a hostname, MAC, email, or anything identifying. The Worker
  treats it purely as a grouping key and never resolves it to anything.
- `period` — a coarse bucket label (e.g. `2026-06`). Counts are the install's
  cumulative total **for that period**, not an increment.
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
numbers adds only the difference. This is what lets an install safely post the
same period every day.

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
   - `account_id` — from `wrangler whoami` or the dashboard URL.
   - `vars.ALLOWED_ORIGIN` — your deployed site origin, e.g.
     `https://alfred.example.com`. This is the CORS allow-origin for `/stats`.
   - the two KV ids from step 1.

3. **Deploy:**

   ```sh
   wrangler deploy
   ```

   Wrangler prints the Worker URL, e.g.
   `https://alfred-proof-telemetry.<your-subdomain>.workers.dev`.

4. **Wire the install reporter** (only on hosts you want reporting; still
   off unless `ALFRED_TELEMETRY_ENABLED=1`):

   ```sh
   # in ~/.alfredrc on that host
   ALFRED_TELEMETRY_URL=https://alfred-proof-telemetry.<your-subdomain>.workers.dev/ingest
   ```

5. **Point the site counter at the read endpoint:** set the build-time env var
   when building the site:

   ```sh
   PUBLIC_ALFRED_TELEMETRY_STATS_URL=https://alfred-proof-telemetry.<your-subdomain>.workers.dev/stats npm run build
   ```

### Smoke test after deploy

```sh
# Send a sample payload.
curl -sX POST https://<your-worker-url>/ingest \
  -H 'Content-Type: application/json' \
  -d '{"install_id":"smoke-test-0001","period":"2026-06","prs_opened":3,"prs_merged":2,"prs_reviewed":1,"loc_added":120}'

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
validation, idempotent re-sends, distinct-install counting, summing across
installs, and the HTTP surface (ingest -> stats round-trip, CORS, 400/404).
No network or real Cloudflare account is touched.
