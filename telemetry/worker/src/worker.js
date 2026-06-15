/**
 * Alfred proof-telemetry Worker.
 *
 * Two endpoints, both anonymous and aggregate-only:
 *
 *   POST /ingest   one install reports its CUMULATIVE LIFETIME counts. The
 *                  Worker stores exactly ONE record per install (keyed by
 *                  install_id) and replaces it on every report (latest-wins
 *                  upsert). That upsert is the WHOLE write: ingest touches only
 *                  this one install's key and never a shared running total, so
 *                  two concurrent ingests write disjoint keys and can never lose
 *                  each other's counts. Re-sending the same lifetime total is
 *                  idempotent: the stored record is replaced with an identical
 *                  value, so the derived total is unchanged, forever, no matter
 *                  how many times or for how long an install reports.
 *                  Browser-hostile by design: simple requests are rejected,
 *                  cross-origin browser writes are blocked, an optional shared
 *                  INGEST_TOKEN hardens it, and a per-IP rate limit plus a
 *                  per-install count cap bound forged-id abuse.
 *   GET  /stats    returns the public totals plus a distinct-install count. The
 *                  totals are DERIVED ON READ: the Worker lists the install:*
 *                  keys and sums each install's stored latest counts. Nothing is
 *                  ever incremented, so the public total always equals the sum
 *                  of installs' latest lifetime values by construction, with no
 *                  read-modify-write race to lose counts. A short KV-backed
 *                  cache (STATS_CACHE_TTL_SECONDS) bounds the per-read list cost.
 *                  This is the only route with browser CORS, scoped to
 *                  ALLOWED_ORIGIN so the marketing site can read it.
 *
 * The contract with the agent client (lib/proof_telemetry.py) is
 * "latest-wins per install", not "accumulate per period". The client reports a
 * single cumulative lifetime total; the Worker treats install_id as the unit of
 * de-duplication and the stored counts as that install's current truth, never
 * an increment. The public total is then DERIVED by summing those per-install
 * truths on read, never accumulated on write. The `period` field on the payload
 * is advisory metadata only (the client always sends "lifetime"); it is NOT
 * part of the storage key, so a calendar rollover can never re-add a constant
 * lifetime total.
 *
 * Abuse posture (be honest about a public community counter). /ingest:
 *   - Rejects "simple" requests: the body MUST be Content-Type application/json.
 *     A text/plain or form POST (which a browser can send cross-origin WITHOUT a
 *     CORS preflight) is refused, so a hidden cross-origin browser POST cannot
 *     silently write. This forces a real preflight for any browser caller.
 *   - Never reflects an arbitrary Origin and never falls back to "*", and when a
 *     browser Origin header is present it must match ALLOWED_ORIGIN. A visitor's
 *     tab on another site cannot POST here.
 *   - When INGEST_TOKEN is set, writes also require a matching X-Ingest-Token.
 *     This is the difference between a private counter and an open one.
 *   - A coarse per-IP rate limit and a per-install count cap (MAX_PER_FIELD)
 *     bound how far a single source, or a flood of forged install_ids, can move
 *     the derived total. The latest-wins idempotency means a re-send never
 *     inflates.
 *   With INGEST_TOKEN unset the counter is open to server-side writes by design
 *   (CORS does not gate curl/python); see README.md for the residual surface and
 *   why the display threshold + caps keep it best-effort-honest. Operators who
 *   want a hard gate set INGEST_TOKEN.
 *
 * Storage: a single Workers KV namespace, bound as `TELEMETRY` (see
 * wrangler.toml). No database, no per-install history beyond the one current
 * snapshot needed for the latest-wins upsert.
 *
 * What is stored (the ENTIRE stored shape):
 *   key "install:<id>" -> JSON latest snapshot for one install, the single
 *                         record per install. Replaced on every report:
 *                         { prs_opened, prs_merged, prs_reviewed, loc_added,
 *                           seen_at }. Its presence also IS the distinct-install
 *                         marker, so there is no separate "known install" key.
 *                         These per-install records are the ONLY source of
 *                         truth: /stats sums them on read.
 *   key "stats:cache"  -> OPTIONAL short-lived cache of the derived totals, so a
 *                         burst of /stats reads does not re-list every install
 *                         each time. JSON { ...totals, installs, updated_at }
 *                         with a TTL of STATS_CACHE_TTL_SECONDS. Purely a read
 *                         optimization: deleting it only forces a recompute, it
 *                         can never change the derived answer. It is NEVER a
 *                         write target of /ingest, so it cannot race ingests.
 *   key "rl:<h>:<win>" -> short-lived per-source ingest counter for the rate
 *                         limit. <h> is a KEYED, non-reversible hash of the
 *                         client IP (HMAC-SHA-256 under a server-side salt),
 *                         never the raw IP, and the bucket self-expires after
 *                         the rate-limit window. Because the hash is keyed, a KV
 *                         reader cannot brute-force the dotted-quad space to
 *                         recover the IP from the key without the salt.
 *
 * What is NEVER stored or logged: raw IP addresses, user agents, repo names,
 * file paths, code, commit text, Slack handles, or anything that identifies a
 * person or a machine. The rate-limit key holds only a KEYED one-way hash of the
 * IP with a short TTL, not the address itself, and without the server-side salt
 * the IP cannot be recovered from it. `install_id` is a random opaque token the
 * install generates for itself; the Worker treats it as a bare grouping key and
 * never resolves it to anything.
 *
 * Concurrency: there is no cross-request read-modify-write to race. Each
 * /ingest writes only its own install:<id> key (an idempotent latest-wins
 * upsert), and /stats DERIVES the total by summing those keys. Two installs
 * posting in the same instant write disjoint keys, so neither can clobber the
 * other and no count is ever lost; the public total is, by construction, always
 * the sum of every install's latest stored value. The "stats:cache" key is only
 * a read-side optimization with a short TTL and is never written by /ingest, so
 * it cannot introduce a write race either. The historical incremental-aggregate
 * design (a single "agg" key updated as agg += new - prior) was removed for
 * exactly this reason: Cloudflare KV has no atomic read-modify-write, so two
 * concurrent ingests could both read the same agg and the last put would win,
 * permanently dropping one install's delta. Deriving on read sidesteps that.
 */

// Hard caps. A single install's cumulative count above these is almost
// certainly a bug or abuse, so we clamp rather than trust it. This per-install
// cap is also the primary bound on how much one forged install_id can inflate
// the aggregate. Tuned well above any believable single-host lifetime output.
const MAX_PER_FIELD = 100000;
const COUNT_FIELDS = ["prs_opened", "prs_merged", "prs_reviewed", "loc_added"];

// install_id is operator-generated and opaque. Bound its length and charset so
// a malformed or hostile value cannot blow up a KV key.
const INSTALL_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
// period is advisory metadata only (the client sends "lifetime"); it is never
// part of a storage key, but we still bound it defensively.
const PERIOD_RE = /^[A-Za-z0-9_-]{1,32}$/;

const EMPTY_AGG = {
  prs_opened: 0,
  prs_merged: 0,
  prs_reviewed: 0,
  loc_added: 0,
  installs: 0,
  updated_at: null,
};

// Prefix for the per-install records. Listing this prefix yields every install,
// which is what /stats sums on read. Kept as a constant so the list() prefix and
// the per-key builder cannot drift apart.
const INSTALL_PREFIX = "install:";

// Short-lived cache of the derived totals. Purely a read optimization so a burst
// of /stats reads does not re-list every install each time; it is never written
// by /ingest and deleting it only forces a recompute. Default TTL is small so
// the public number is near-live; override with STATS_CACHE_TTL_SECONDS (0
// disables caching entirely, always recomputing from the install records).
const STATS_CACHE_KEY = "stats:cache";

// Cloudflare Workers KV enforces a HARD MINIMUM of 60 seconds on
// `put(..., { expirationTtl })`: a put with a TTL below 60 is REJECTED, so a
// sub-60 cache write would throw, be swallowed, and the cache would never
// populate, making every /stats recompute by listing all install keys. So the
// usable TTL floor is 60. The default is the floor.
const KV_MIN_EXPIRATION_TTL_SECONDS = 60;
const DEFAULT_STATS_CACHE_TTL_SECONDS = KV_MIN_EXPIRATION_TTL_SECONDS;

// Resolve the stats-cache TTL with the KV minimum enforced.
//   unset / "" / malformed / negative -> the default (60, the KV floor)
//   0                                  -> 0 (the documented "disable cache" path,
//                                        preserved: we never call put at all)
//   1..59                              -> CLAMPED UP to 60 (a sub-60 put would be
//                                        rejected by KV and silently lose the
//                                        cache); we log the clamp once per read
//                                        so a misconfiguration is visible.
//   >=60                               -> used as-is (floored to an integer)
// Returning a value below 60 (other than the explicit 0 disable) is never valid
// because KV would reject the put, so this function never does.
function statsCacheTtl(env) {
  const raw = env && env.STATS_CACHE_TTL_SECONDS;
  if (raw === undefined || raw === null || raw === "") {
    return DEFAULT_STATS_CACHE_TTL_SECONDS;
  }
  const n = Number(raw);
  // Malformed or negative: fall back to the default rather than silently
  // disabling caching or emitting a TTL KV would reject.
  if (!Number.isFinite(n) || n < 0) return DEFAULT_STATS_CACHE_TTL_SECONDS;
  const floored = Math.floor(n);
  // 0 keeps its "disable the cache" meaning (no put is ever issued).
  if (floored === 0) return 0;
  // 1..59 cannot be honored: KV rejects an expirationTtl below 60, so the put
  // would throw and the cache would never populate. Clamp up to the KV floor.
  if (floored < KV_MIN_EXPIRATION_TTL_SECONDS) {
    console.warn(
      `STATS_CACHE_TTL_SECONDS=${floored} is below the Cloudflare KV minimum of ` +
        `${KV_MIN_EXPIRATION_TTL_SECONDS}s; clamping up to ${KV_MIN_EXPIRATION_TTL_SECONDS}. ` +
        `Set 0 to disable the cache, or a value >= ${KV_MIN_EXPIRATION_TTL_SECONDS}.`,
    );
    return KV_MIN_EXPIRATION_TTL_SECONDS;
  }
  return floored;
}

/**
 * Coerce one count field: integer, non-negative, clamped to MAX_PER_FIELD.
 * Anything non-numeric becomes 0.
 */
export function clampCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  const floored = Math.floor(n);
  if (floored <= 0) return 0;
  return floored > MAX_PER_FIELD ? MAX_PER_FIELD : floored;
}

/**
 * Validate and normalize an ingest payload. Returns { ok, value } or
 * { ok: false, error }. Never throws on bad input.
 */
export function normalizePayload(raw) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, error: "body must be a JSON object" };
  }
  const installId = typeof raw.install_id === "string" ? raw.install_id : "";
  if (!INSTALL_ID_RE.test(installId)) {
    return { ok: false, error: "install_id missing or malformed" };
  }
  // period is advisory metadata only. Accept it when present and well-formed,
  // default to "lifetime" otherwise; it never becomes part of a storage key.
  const rawPeriod = typeof raw.period === "string" ? raw.period : "";
  const period = PERIOD_RE.test(rawPeriod) ? rawPeriod : "lifetime";
  const counts = {};
  for (const field of COUNT_FIELDS) {
    counts[field] = clampCount(raw[field]);
  }
  return { ok: true, value: { install_id: installId, period, counts } };
}

// One record per install, keyed by install_id. Replaced on every report
// (latest-wins). Its presence is also the distinct-install marker.
function installKey(installId) {
  return `${INSTALL_PREFIX}${installId}`;
}

/**
 * Coerce one stored per-install snapshot into a clean { ...counts, seen_at }.
 * Defensive: a hand-edited or partially written record must never poison the
 * derived total. Unknown/negative/non-numeric counts become 0.
 */
function normalizeSnapshot(stored) {
  const out = {};
  for (const field of COUNT_FIELDS) {
    out[field] = clampCount(stored && stored[field]);
  }
  out.seen_at =
    stored && typeof stored.seen_at === "string" ? stored.seen_at : null;
  return out;
}

/**
 * Compute the public totals by listing every install:* record and summing its
 * stored latest counts. This is the ONLY source of the public number, and it is
 * race-free: it reads independent per-install keys and never any shared running
 * total, so concurrent ingests can never make it lose a count. The returned
 * shape matches EMPTY_AGG: { ...COUNT_FIELDS, installs, updated_at }, where
 * `installs` is the distinct record count and `updated_at` is the most recent
 * `seen_at` across all records.
 *
 * Cost: one list() per 1000 keys plus one get() per install. For the expected
 * scale (tens to low-hundreds of installs, gated by the site display threshold)
 * this is cheap, and /stats caches the result for STATS_CACHE_TTL_SECONDS so a
 * read burst does not re-list each time. KV list() is paginated via `cursor`;
 * we follow it so a namespace larger than one page is summed in full.
 */
export async function computeTotals(kv) {
  const totals = { ...EMPTY_AGG };
  let cursor;
  // Bound the number of list pages we will walk so a pathologically large
  // namespace (far beyond any believable opt-in fleet) cannot run the read
  // unbounded. Each page is up to 1000 keys; 1000 pages is a million installs,
  // orders of magnitude past the expected scale.
  const MAX_LIST_PAGES = 1000;
  for (let page = 0; page < MAX_LIST_PAGES; page++) {
    const listed = await kv.list({ prefix: INSTALL_PREFIX, cursor });
    const keys = (listed && listed.keys) || [];
    for (const entry of keys) {
      const name = entry && entry.name;
      if (typeof name !== "string") continue;
      const stored = await kv.get(name, { type: "json" });
      if (!stored || typeof stored !== "object") continue;
      const snap = normalizeSnapshot(stored);
      totals.installs += 1;
      for (const field of COUNT_FIELDS) {
        totals[field] += snap[field];
      }
      if (
        snap.seen_at &&
        (totals.updated_at === null || snap.seen_at > totals.updated_at)
      ) {
        totals.updated_at = snap.seen_at;
      }
    }
    if (listed && listed.list_complete === false && listed.cursor) {
      cursor = listed.cursor;
    } else {
      break;
    }
  }
  return totals;
}

/**
 * Read the public totals, derived from the per-install records, behind a short
 * KV-backed cache. The cache (STATS_CACHE_KEY) is a pure read optimization with
 * a TTL of STATS_CACHE_TTL_SECONDS: a hit avoids re-listing every install, a
 * miss recomputes from scratch and refreshes it. The cache is NEVER written by
 * /ingest, so it cannot race a write; the worst it can do is serve a value up to
 * the TTL stale, which for a vanity counter is fine. Setting the TTL to 0
 * disables the cache and always recomputes.
 */
export async function readStats(kv, env) {
  const ttl = statsCacheTtl(env);
  if (ttl > 0) {
    const cached = await kv.get(STATS_CACHE_KEY, { type: "json" });
    if (cached && typeof cached === "object") {
      const totals = { ...EMPTY_AGG };
      for (const field of COUNT_FIELDS) {
        const n = Number(cached[field]);
        totals[field] = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
      }
      const installs = Number(cached.installs);
      totals.installs =
        Number.isFinite(installs) && installs > 0 ? Math.floor(installs) : 0;
      totals.updated_at =
        typeof cached.updated_at === "string" ? cached.updated_at : null;
      return totals;
    }
  }
  const totals = await computeTotals(kv);
  if (ttl > 0) {
    // Refresh the cache. Best-effort: a failed cache write only costs a recompute
    // next read, never correctness.
    try {
      await kv.put(STATS_CACHE_KEY, JSON.stringify(totals), {
        expirationTtl: ttl,
      });
    } catch {
      /* cache write is best-effort; ignore */
    }
  }
  return totals;
}

/**
 * Upsert one normalized payload as this install's latest snapshot, idempotently.
 *
 * Latest-wins per install: the stored record is REPLACED with the new counts, so
 * a re-send of the same lifetime total stores an identical value (the derived
 * total is unchanged), a re-send with higher numbers stores the higher value,
 * and a downward correction stores the lower value. Counts are the install's
 * cumulative lifetime total, never an increment. The record is keyed only by
 * install_id, so no calendar bucket can ever re-add a constant total.
 *
 * CRUCIALLY this writes ONLY the install's own key (plus an optional cache
 * invalidation). It never touches a shared running total, so two concurrent
 * ingests write disjoint keys and neither can lose the other's counts. The
 * public total is derived on read by summing every install record (see
 * computeTotals), so it always equals the sum of installs' latest values.
 *
 * Returns the stored snapshot for this install (not the global total; the global
 * total is derived separately so the write stays a single cheap, race-free put).
 */
export async function ingest(kv, payload, now = new Date()) {
  const { install_id: installId, counts } = payload;

  const iso = now.toISOString();
  const snapshot = {};
  for (const field of COUNT_FIELDS) {
    snapshot[field] = clampCount(counts[field]);
  }
  snapshot.seen_at = iso;

  // The entire write: replace this install's record. No shared-state read,
  // no read-modify-write, nothing another ingest could clobber.
  await kv.put(installKey(installId), JSON.stringify(snapshot));

  // Invalidate the derived-stats cache so the next /stats recomputes and this
  // report shows up promptly rather than waiting out the TTL. Best-effort: if
  // the delete fails the value is at worst TTL-stale, never wrong. This is a
  // delete, not a write of a total, so it still introduces no cross-ingest race.
  try {
    await kv.delete(STATS_CACHE_KEY);
  } catch {
    /* cache invalidation is best-effort; ignore */
  }

  return snapshot;
}

function publicView(agg) {
  return {
    prs_opened: agg.prs_opened,
    prs_merged: agg.prs_merged,
    prs_reviewed: agg.prs_reviewed,
    loc_added: agg.loc_added,
    installs: agg.installs,
    updated_at: agg.updated_at,
  };
}

// CORS is for the marketing site reading GET /stats from a browser. It is the
// ONLY route a browser legitimately calls cross-origin, so it is the only route
// that gets an Access-Control-Allow-Origin. POST /ingest has no browser caller
// (the install reporter is server-side urllib, which CORS does not gate), so it
// returns NO allow-origin header: a browser preflight or cross-origin POST from
// a visitor's tab fails and cannot inflate the public counter. We never reflect
// an arbitrary request Origin, and we never fall back to "*".
function statsCorsHeaders(env) {
  const origin = (env && env.ALLOWED_ORIGIN) || "";
  const headers = {
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  // Only advertise an allow-origin when the operator configured one. With it
  // unset, /stats is still readable server-side; only browser cross-origin
  // reads are gated, which is the safe default for a public read endpoint.
  if (origin) headers["Access-Control-Allow-Origin"] = origin;
  return headers;
}

// jsonResponse(body, status, env, { cors }): `cors` is an explicit headers
// object (e.g. statsCorsHeaders) for routes that should carry CORS, omitted for
// routes (like /ingest) that must not.
function jsonResponse(body, status, env, opts = {}) {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  };
  if (opts.cors) Object.assign(headers, opts.cors);
  return new Response(JSON.stringify(body), { status, headers });
}

/**
 * Constant-time-ish string compare. Avoids leaking the token length/contents
 * via early-exit timing on the ingest auth path. Tokens are short and this runs
 * at most once per request, so the cost is irrelevant.
 */
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Reject "simple" requests on /ingest.
 *
 * A cross-origin browser POST with a "simple" Content-Type
 * (text/plain, application/x-www-form-urlencoded, multipart/form-data) does NOT
 * trigger a CORS preflight, so the browser fires it and only hides the
 * RESPONSE; the Worker would still parse the body and write KV. By REQUIRING
 * Content-Type: application/json we force any cross-origin browser caller into a
 * preflight (which /ingest fails, see the OPTIONS handler), closing the
 * simple-POST write bypass. Server-side clients (urllib) already send JSON, so
 * this costs them nothing.
 *
 * Returns { ok: true } or { ok: false, status, error }.
 */
function checkContentType(request) {
  const raw = request.headers.get("Content-Type") || "";
  // Strip any "; charset=..." parameter and compare case-insensitively.
  const mediaType = raw.split(";", 1)[0].trim().toLowerCase();
  if (mediaType !== "application/json") {
    return {
      ok: false,
      status: 415,
      error: "Content-Type must be application/json",
    };
  }
  return { ok: true };
}

/**
 * Enforce the Origin allowlist for any request that carries an Origin header.
 *
 * Server-side clients (urllib) send no Origin, so they are unaffected. A browser
 * always sets Origin on a cross-origin request; if one reaches /ingest its
 * Origin must equal ALLOWED_ORIGIN (and even then the request already had to be
 * application/json, which forced a preflight that /ingest fails). This is a
 * defence-in-depth check on top of the no-preflight-CORS lockdown: an Origin
 * that is present and does NOT match is refused outright.
 *
 * Returns { ok: true } or { ok: false, status, error }.
 */
function checkOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return { ok: true }; // no Origin: a server-side (non-browser) caller
  const allowed = (env && env.ALLOWED_ORIGIN) || "";
  if (allowed && origin === allowed) return { ok: true };
  return { ok: false, status: 403, error: "origin not allowed" };
}

/**
 * Ingest write gate. When INGEST_TOKEN is configured on the Worker, /ingest
 * must present a matching `X-Ingest-Token` header (opted-in hosts send their
 * ALFRED_TELEMETRY_TOKEN there). When INGEST_TOKEN is unset the counter is
 * deliberately open to server-side writes (the application/json + Origin lock,
 * rate limit, per-install count cap, and latest-wins idempotency are the
 * remaining guards); see README.md for the residual abuse surface.
 *
 * Returns { ok: true } or { ok: false, status, error }.
 */
function checkIngestToken(request, env) {
  const expected = env && env.INGEST_TOKEN;
  if (!expected) return { ok: true }; // open-write mode, by configuration
  const provided = request.headers.get("X-Ingest-Token") || "";
  if (!safeEqual(provided, expected)) {
    return { ok: false, status: 401, error: "ingest token missing or invalid" };
  }
  return { ok: true };
}

// Per-IP rate limit on /ingest. A coarse fixed-window counter in KV keeps a
// single source from hammering the endpoint to spray distinct install_ids. The
// limit is intentionally generous (a legitimate host posts once a day) and only
// engages when the platform gives us a client IP; it is a speed bump on top of
// the token + count cap + idempotency, not the primary control.
const RATE_LIMIT_WINDOW_SECONDS = 3600;
const RATE_LIMIT_MAX_PER_WINDOW = 60;

function rateLimitMax(env) {
  const n = Number(env && env.INGEST_RATE_LIMIT);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : RATE_LIMIT_MAX_PER_WINDOW;
}

// Width (hex chars) of the truncated rate-limit bucket hash. 32 hex chars is
// 128 bits, far more than enough to avoid bucket collisions across a realistic
// number of source IPs while keeping the key short.
const RL_HASH_HEX_WIDTH = 32;

// Per-isolate ephemeral salt, generated lazily the FIRST time we need a keyed
// hash WITHOUT a configured RATE_LIMIT_SALT. It lives only in this isolate's
// memory and is never persisted, so even with it the IP cannot be recovered
// from a KV key by anyone reading KV. The trade-off vs a configured secret: an
// ephemeral salt rotates whenever the isolate recycles, so a source's bucket
// can reset early (it under-counts, never over-counts) and two isolates bucket
// the same IP differently. That is an acceptable fail-safe for a coarse speed
// bump; an operator who wants stable buckets sets RATE_LIMIT_SALT.
let _ephemeralSalt = null;
function ephemeralSalt() {
  if (_ephemeralSalt === null) {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    _ephemeralSalt = bytes;
  }
  return _ephemeralSalt;
}

// Resolve the HMAC key material: the operator-configured RATE_LIMIT_SALT when
// set (a Worker secret/var), else a per-isolate ephemeral random salt. Either
// way the key material is server-side only and never written to KV, so the IP
// cannot be brute-forced back out of the bucket key by a KV reader.
function rateLimitSaltBytes(env) {
  const configured = env && typeof env.RATE_LIMIT_SALT === "string" ? env.RATE_LIMIT_SALT : "";
  if (configured) return new TextEncoder().encode(configured);
  return ephemeralSalt();
}

// KEYED, non-reversible hash of the client IP for the rate-limit bucket key.
// HMAC-SHA-256 over the IP using a server-side secret salt, hex-encoded and
// truncated. Unlike the old unsalted 32-bit FNV-1a, the key cannot be reversed
// by brute-forcing the ~4 billion dotted-quad space: without the secret salt an
// attacker with KV read access cannot recompute the HMAC, so the IP stays
// unrecoverable. The raw IP is never written to KV; only this keyed digest is,
// and the window TTL drops the bucket regardless.
export async function hashIp(ip, env) {
  const keyBytes = rateLimitSaltBytes(env);
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(ip));
  const bytes = new Uint8Array(mac);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex.slice(0, RL_HASH_HEX_WIDTH);
}

async function checkRateLimit(kv, request, env, now = Date.now()) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (!ip) return { ok: true }; // no IP to key on; rely on the other guards
  const max = rateLimitMax(env);
  const window = Math.floor(now / 1000 / RATE_LIMIT_WINDOW_SECONDS);
  // Key on a KEYED hash of the IP plus the window so it self-expires and the raw
  // IP is never written to KV AND cannot be recovered from the key without the
  // server-side salt. The TTL drops the bucket after the window anyway.
  const key = `rl:${await hashIp(ip, env)}:${window}`;
  const current = Number(await kv.get(key)) || 0;
  if (current >= max) {
    return { ok: false, status: 429, error: "rate limit exceeded" };
  }
  await kv.put(key, String(current + 1), { expirationTtl: RATE_LIMIT_WINDOW_SECONDS });
  return { ok: true };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // CORS preflight: only /stats has a legitimate browser caller. We answer the
    // preflight WITHOUT an allow-origin for any other path (including /ingest),
    // so a browser cross-origin POST to /ingest fails its preflight and cannot
    // inflate the counter from a visitor's tab. Because /ingest requires
    // application/json (a non-simple Content-Type), any cross-origin browser
    // POST MUST preflight first, and that preflight gets no allow-origin here.
    if (request.method === "OPTIONS") {
      if (path === "/stats") {
        return new Response(null, { status: 204, headers: statsCorsHeaders(env) });
      }
      // No CORS headers: the browser treats this as a failed preflight.
      return new Response(null, { status: 204 });
    }

    const kv = env && env.TELEMETRY;

    if (path === "/stats" && request.method === "GET") {
      const cors = statsCorsHeaders(env);
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env, { cors });
      // Totals are derived on read (sum of every install record), behind a short
      // cache. Never an incremented running total, so no race can lose counts.
      const totals = await readStats(kv, env);
      return jsonResponse(publicView(totals), 200, env, { cors });
    }

    if (path === "/ingest" && request.method === "POST") {
      // No CORS headers on /ingest, ever: this is a server-to-server endpoint.
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env);

      // Reject "simple" requests so a no-preflight cross-origin browser POST
      // cannot silently write. Server-side clients send application/json.
      const ctype = checkContentType(request);
      if (!ctype.ok) return jsonResponse({ error: ctype.error }, ctype.status, env);

      // Any caller that DOES present an Origin (i.e. a browser) must match the
      // allowlist. Server-side callers send no Origin and pass through.
      const origin = checkOrigin(request, env);
      if (!origin.ok) return jsonResponse({ error: origin.error }, origin.status, env);

      const auth = checkIngestToken(request, env);
      if (!auth.ok) return jsonResponse({ error: auth.error }, auth.status, env);

      const limited = await checkRateLimit(kv, request, env);
      if (!limited.ok) return jsonResponse({ error: limited.error }, limited.status, env);

      let raw;
      try {
        raw = await request.json();
      } catch {
        return jsonResponse({ error: "invalid JSON" }, 400, env);
      }
      const parsed = normalizePayload(raw);
      if (!parsed.ok) {
        return jsonResponse({ error: parsed.error }, 400, env);
      }
      await ingest(kv, parsed.value);
      // Report the fresh derived total back. ingest just invalidated the stats
      // cache, so this recomputes from the install records and includes the
      // write we just made. computeTotals (not readStats) so the response always
      // reflects this report rather than a possibly-cached older value.
      const totals = await computeTotals(kv);
      return jsonResponse({ ok: true, totals: publicView(totals) }, 200, env);
    }

    if (path === "/" && request.method === "GET") {
      return jsonResponse(
        {
          service: "alfred-proof-telemetry",
          endpoints: { ingest: "POST /ingest", stats: "GET /stats" },
          privacy: "anonymous aggregate counts only; no PII, no repo names, no IP storage",
        },
        200,
        env,
      );
    }

    return jsonResponse({ error: "not found" }, 404, env);
  },
};
