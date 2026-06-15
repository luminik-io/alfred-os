/**
 * Alfred proof-telemetry Worker.
 *
 * Two endpoints, both anonymous and aggregate-only:
 *
 *   POST /ingest   one install folds its per-period counts into the running
 *                  totals. Idempotent per {install_id, period}: re-sending the
 *                  same pair overwrites that pair's stored counts rather than
 *                  double-counting.
 *   GET  /stats    returns the public aggregate totals plus a distinct-install
 *                  count. CORS-open for GET so the marketing site can read it.
 *
 * Storage: a single Workers KV namespace, bound as `TELEMETRY` (see
 * wrangler.toml). No database, no per-install history beyond the last
 * snapshot needed for idempotency.
 *
 * What is stored (the ENTIRE stored shape):
 *   key "agg"          -> JSON aggregate, the only thing /stats reads:
 *                         { prs_opened, prs_merged, prs_reviewed, loc_added,
 *                           installs, updated_at }
 *   key "i:<id>:<per>" -> JSON last-seen snapshot for one {install_id, period}
 *                         pair, used only to make re-sends idempotent:
 *                         { prs_opened, prs_merged, prs_reviewed, loc_added,
 *                           seen_at }
 *   key "ic:<id>"      -> "1" marker, present once per distinct install_id, used
 *                         to maintain the distinct-install count.
 *
 * What is NEVER stored or logged: IP addresses, user agents, repo names, file
 * paths, code, commit text, Slack handles, or anything that identifies a
 * person or a machine. `install_id` is a random opaque token the install
 * generates for itself; the Worker treats it as a bare grouping key and never
 * resolves it to anything.
 *
 * The aggregate is maintained without a cross-request lock. Two installs
 * posting in the same instant could in principle interleave their
 * read-modify-write of "agg"; for a low-frequency proof counter (each install
 * posts at most once a day) the practical loss is negligible, and the stored
 * per-pair snapshots mean a later re-send self-heals any single dropped delta.
 * If exactness ever matters, swap KV for a Durable Object or D1 transaction;
 * see the README.
 */

// Hard caps. A single install's per-period count above these is almost
// certainly a bug or abuse, so we clamp rather than trust it. Tuned well
// above any believable single-host daily output.
const MAX_PER_FIELD = 100000;
const COUNT_FIELDS = ["prs_opened", "prs_merged", "prs_reviewed", "loc_added"];

// install_id is operator-generated and opaque. Bound its length and charset so
// a malformed or hostile value cannot blow up a KV key.
const INSTALL_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
// period is a coarse bucket label the install picks, e.g. "2026-06" or
// "2026-06-15". Same defensive bounding.
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
  const period = typeof raw.period === "string" ? raw.period : "";
  if (!PERIOD_RE.test(period)) {
    return { ok: false, error: "period missing or malformed" };
  }
  const counts = {};
  for (const field of COUNT_FIELDS) {
    counts[field] = clampCount(raw[field]);
  }
  return { ok: true, value: { install_id: installId, period, counts } };
}

function aggKey() {
  return "agg";
}
function pairKey(installId, period) {
  return `i:${installId}:${period}`;
}
function installKey(installId) {
  return `ic:${installId}`;
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
 * The delta applied is (new counts - previously stored counts for this exact
 * {install_id, period}). So the first send adds the full counts; a re-send of
 * the same period with the same numbers adds zero; a re-send with higher
 * numbers adds only the difference. Counts are treated as the install's
 * cumulative total for that period, never as an increment, which is what makes
 * re-sends safe.
 *
 * Pure-ish: all KV effects go through the passed `kv`. Returns the updated
 * aggregate (without the bookkeeping keys).
 */
export async function ingest(kv, payload, now = new Date()) {
  const { install_id: installId, period, counts } = payload;

  const prior = (await kv.get(pairKey(installId, period), { type: "json" })) || null;
  const agg = await readAgg(kv);

  // Distinct-install accounting: only the first period we ever see from an
  // install increments the install count.
  const isKnownInstall = (await kv.get(installKey(installId))) !== null;
  if (!isKnownInstall) {
    agg.installs += 1;
    await kv.put(installKey(installId), "1");
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

  // Persist the per-pair snapshot (for next-time idempotency) and the new
  // aggregate. Order: snapshot first, then aggregate, so a crash between the
  // two leaves the aggregate trailing (recoverable on next send) rather than
  // ahead (would double count).
  const snapshot = { ...counts, seen_at: iso };
  await kv.put(pairKey(installId, period), JSON.stringify(snapshot));
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

function corsHeaders(env) {
  const origin = (env && env.ALLOWED_ORIGIN) || "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(body, status, env) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      ...corsHeaders(env),
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    const kv = env && env.TELEMETRY;

    if (path === "/stats" && request.method === "GET") {
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env);
      const agg = await readAgg(kv);
      return jsonResponse(publicView(agg), 200, env);
    }

    if (path === "/ingest" && request.method === "POST") {
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env);
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
