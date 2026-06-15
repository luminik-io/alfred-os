/**
 * Alfred proof-telemetry Worker.
 *
 * Two endpoints, both anonymous and aggregate-only:
 *
 *   POST /ingest   one install reports its CUMULATIVE LIFETIME counts. The
 *                  Worker stores exactly ONE record per install (keyed by
 *                  install_id) and replaces it on every report (latest-wins
 *                  upsert). The public aggregate is the sum over all installs
 *                  of each install's latest counts, maintained incrementally as
 *                  aggregate += new - previous_for_this_install. Re-sending the
 *                  same lifetime total is therefore idempotent: it adds zero,
 *                  forever, no matter how many times or for how long an install
 *                  reports. Browser-hostile by design: simple requests are
 *                  rejected, cross-origin browser writes are blocked, an
 *                  optional shared INGEST_TOKEN hardens it, and a per-IP rate
 *                  limit plus a per-install count cap bound forged-id abuse.
 *   GET  /stats    returns the public aggregate totals plus a distinct-install
 *                  count. This is the only route with browser CORS, scoped to
 *                  ALLOWED_ORIGIN so the marketing site can read it.
 *
 * The contract with the agent client (lib/proof_telemetry.py) is
 * "latest-wins per install", not "accumulate per period". The client reports a
 * single cumulative lifetime total; the Worker treats install_id as the unit of
 * de-duplication and the stored counts as that install's current truth, never
 * an increment. The `period` field on the payload is advisory metadata only
 * (the client always sends "lifetime"); it is NOT part of the storage key, so a
 * calendar rollover can never re-add a constant lifetime total.
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
 *     the aggregate. The latest-wins idempotency means a re-send never inflates.
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
 *   key "agg"          -> JSON aggregate, the only thing /stats reads:
 *                         { prs_opened, prs_merged, prs_reviewed, loc_added,
 *                           installs, updated_at }
 *   key "install:<id>" -> JSON latest snapshot for one install, the single
 *                         record per install. Replaced on every report:
 *                         { prs_opened, prs_merged, prs_reviewed, loc_added,
 *                           seen_at }. Its presence also IS the distinct-install
 *                         marker, so there is no separate "known install" key.
 *   key "rl:<h>:<win>" -> short-lived per-source ingest counter for the rate
 *                         limit. <h> is a non-reversible hash of the client IP,
 *                         never the raw IP, and the bucket self-expires after
 *                         the rate-limit window.
 *
 * What is NEVER stored or logged: raw IP addresses, user agents, repo names,
 * file paths, code, commit text, Slack handles, or anything that identifies a
 * person or a machine. The rate-limit key holds only a one-way hash of the IP
 * with a short TTL, not the address itself. `install_id` is a random opaque
 * token the install generates for itself; the Worker treats it as a bare
 * grouping key and never resolves it to anything.
 *
 * The aggregate is maintained without a cross-request lock. Two installs
 * posting in the same instant could in principle interleave their
 * read-modify-write of "agg"; for a low-frequency proof counter (each install
 * posts at most once a day) the practical loss is negligible, and because each
 * install's record is the full current truth, the very next report from that
 * install self-heals any single dropped delta. If exactness ever matters, swap
 * KV for a Durable Object or D1 transaction; see the README.
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

function aggKey() {
  return "agg";
}
// One record per install, keyed by install_id. Replaced on every report
// (latest-wins). Its presence is also the distinct-install marker.
function installKey(installId) {
  return `install:${installId}`;
}

async function readAgg(kv) {
  const stored = await kv.get(aggKey(), { type: "json" });
  if (!stored || typeof stored !== "object") {
    return { ...EMPTY_AGG };
  }
  const agg = { ...EMPTY_AGG };
  for (const field of COUNT_FIELDS) {
    const n = Number(stored[field]);
    agg[field] = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  }
  const installs = Number(stored.installs);
  agg.installs = Number.isFinite(installs) && installs > 0 ? Math.floor(installs) : 0;
  agg.updated_at = typeof stored.updated_at === "string" ? stored.updated_at : null;
  return agg;
}

/**
 * Fold one normalized payload into the running aggregate, idempotently.
 *
 * Latest-wins per install. The delta applied is
 * (new counts - the counts currently stored for THIS install_id). So the first
 * report from an install adds its full counts; a re-send of the same lifetime
 * total adds zero; a re-send with higher numbers adds only the difference; a
 * downward correction subtracts. Counts are the install's cumulative lifetime
 * total, never an increment, which is what makes every re-send safe regardless
 * of how often or for how long the install reports. The stored record is keyed
 * only by install_id, so no calendar bucket can ever re-add a constant total.
 *
 * Pure-ish: all KV effects go through the passed `kv`. Returns the updated
 * aggregate (without the bookkeeping keys).
 */
export async function ingest(kv, payload, now = new Date()) {
  const { install_id: installId, counts } = payload;

  const prior = (await kv.get(installKey(installId), { type: "json" })) || null;
  const agg = await readAgg(kv);

  // Distinct-install accounting: the per-install record's presence IS the
  // marker, so the first time we see an install (no prior record) is the only
  // time we increment.
  if (!prior) {
    agg.installs += 1;
  }

  for (const field of COUNT_FIELDS) {
    const priorVal = prior ? clampCount(prior[field]) : 0;
    const delta = counts[field] - priorVal;
    const next = agg[field] + delta;
    // Floor at zero defensively; a downward correction should never push the
    // public total negative.
    agg[field] = next > 0 ? next : 0;
  }

  const iso = now.toISOString();
  agg.updated_at = iso;

  // Persist the install's new latest snapshot (for next-time idempotency) and
  // the new aggregate. Order: snapshot first, then aggregate, so a crash
  // between the two leaves the aggregate trailing (recoverable on the install's
  // next report) rather than ahead (which would double count).
  const snapshot = { ...counts, seen_at: iso };
  await kv.put(installKey(installId), JSON.stringify(snapshot));
  await kv.put(aggKey(), JSON.stringify(agg));

  return agg;
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

// Non-reversible 32-bit FNV-1a hash, hex-encoded. Used so the rate-limit KV key
// never contains the raw client IP, keeping the "no IP stored" promise intact
// while still bucketing per source. A short window TTL drops it regardless.
function hashIp(ip) {
  let h = 0x811c9dc5;
  for (let i = 0; i < ip.length; i++) {
    h ^= ip.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16);
}

async function checkRateLimit(kv, request, env, now = Date.now()) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (!ip) return { ok: true }; // no IP to key on; rely on the other guards
  const max = rateLimitMax(env);
  const window = Math.floor(now / 1000 / RATE_LIMIT_WINDOW_SECONDS);
  // Key on a hash of the IP plus the window so it self-expires and the raw IP
  // is never written to KV. The TTL drops the bucket after the window anyway.
  const key = `rl:${hashIp(ip)}:${window}`;
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
      const agg = await readAgg(kv);
      return jsonResponse(publicView(agg), 200, env, { cors });
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
      const agg = await ingest(kv, parsed.value);
      return jsonResponse({ ok: true, totals: publicView(agg) }, 200, env);
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
