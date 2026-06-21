/**
 * Alfred proof-telemetry Worker.
 *
 * Three endpoints, anonymous and aggregate-only:
 *
 *   POST /register  returns a per-install write token for an anonymous
 *                    install_id. The Worker stores only the token hash. The
 *                    raw token is returned once and stays on the local install.
 *                    Hosted Alfred uses this path before the first report.
 *   POST /ingest   one install reports its CUMULATIVE LIFETIME counts. The
 *                  Worker stores one latest progress record per trusted install
 *                  (keyed by install_id) and replaces it on every trusted report
 *                  (latest-wins upsert). In hosted trusted-counts mode,
 *                  untrusted reports write only an active-install marker, never
 *                  the progress record. That keeps local activity refreshes
 *                  separate from public proof, so an anonymous client cannot
 *                  inflate or erase trusted shipped-work totals. Re-sending the
 *                  same lifetime total is idempotent: the stored record is
 *                  replaced with an identical value, so the derived total is unchanged, forever,
 *                  no matter how many times or for how long an install reports.
 *                  Browser-hostile by design: simple requests are rejected,
 *                  cross-origin browser writes are blocked, hosted writes use
 *                  per-install tokens, and hosted progress totals can require a
 *                  trusted collector token. Self-hosted collectors can use one
 *                  shared INGEST_TOKEN instead.
 *   GET  /stats    returns the public totals plus a trusted-install count. The
 *                  totals are DERIVED ON READ: the Worker lists install:* keys
 *                  for trusted public totals. In hosted trusted-counts mode,
 *                  active:* keys can refresh the timestamp for a trusted
 *                  install, but anonymous active markers are not public proof.
 *                  Nothing is ever
 *                  incremented, so the public progress total always equals the
 *                  sum of trusted installs' latest lifetime values by
 *                  construction, with no read-modify-write race to lose counts.
 *                  A short KV-backed cache (STATS_CACHE_TTL_SECONDS) bounds the
 *                  per-read list cost.
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
 * Abuse posture (be honest about a public aggregate counter). /ingest:
 *   - Rejects "simple" requests: the body MUST be Content-Type application/json.
 *     A text/plain or form POST (which a browser can send cross-origin WITHOUT a
 *     CORS preflight) is refused, so a hidden cross-origin browser POST cannot
 *     silently write. This forces a real preflight for any browser caller.
 *   - Never reflects an arbitrary Origin and never falls back to "*", and when a
 *     browser Origin header is present it must match ALLOWED_ORIGIN. A visitor's
 *     tab on another site cannot POST here.
 *   - Hosted writes use per-install tokens: /register stores only a token hash,
 *     and /ingest verifies the local token on each report. Self-hosted
 *     collectors can set INGEST_TOKEN when one shared token is preferred.
 *   - The hosted public counters can be locked to trusted reporters with
 *     TRUSTED_COUNTS_ONLY=1. Anonymous reports can refresh local activity, but
 *     public PR, issue, file, line, and machine totals only move when the
 *     request carries the trusted collector token. This avoids pretending an
 *     open-source client can keep a global secret.
 *   - A coarse per-IP rate limit and a per-install count cap (MAX_PER_FIELD)
 *     bound open-write self-hosted collectors. In hosted mode,
 *     TRUSTED_COUNTS_ONLY prevents untrusted reports from moving the public
 *     totals at all. The latest-wins idempotency means a re-send never inflates.
 *   With both REQUIRE_INSTALL_TOKEN and INGEST_TOKEN unset the counter is open
 *   to server-side writes by design (CORS does not gate curl/python); see
 *   README.md for the residual surface and why the caps keep it
 *   best-effort-honest.
 *
 * Storage: a single Workers KV namespace, bound as `TELEMETRY` (see
 * wrangler.toml). No database, no per-install history beyond the one current
 * snapshot needed for the latest-wins upsert.
 *
 * What is stored (the ENTIRE stored shape):
 *   key "auth:<id>"    -> JSON token hash for one registered install:
 *                         { token_sha256, created_at }. The raw token is
 *                         returned once from /register and is never stored by
 *                         the Worker.
 *   key "install:<id>" -> JSON latest snapshot for one install, the single
 *                         record per install. Replaced on every report:
 *                         { prs_opened, prs_merged, prs_reviewed,
 *                           issues_opened, issues_closed, files_changed, lines_changed,
 *                           loc_added, seen_at, trusted_reporter }. These
 *                         records are the ONLY source of shipped-work totals:
 *                         /stats sums them on read. In trusted-counts mode,
 *                         untrusted reports never write this key.
 *   key "active:<id>"  -> JSON activity marker:
 *                         { seen_at }. In trusted-counts mode this can refresh
 *                         updated_at for an already-trusted install without
 *                         writing progress totals. It stores no counts and does
 *                         not make an anonymous install public proof.
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
 * person or install. The rate-limit key holds only a KEYED one-way hash of the
 * IP with a short TTL, not the address itself, and without the server-side salt
 * the IP cannot be recovered from it. `install_id` is a random opaque token the
 * install generates for itself; the Worker treats it as a bare grouping key and
 * never resolves it to anything.
 *
 * Concurrency: the COUNTS have no cross-request read-modify-write to race. Each
 * /ingest writes only its own install:<id> or active:<id> key (an idempotent
 * latest-wins upsert), and /stats DERIVES the public totals from trusted
 * install records. Two installs posting in the same instant write disjoint
 * keys, so neither can clobber the other and no count is ever lost; the public
 * total is, by construction, always the sum of every trusted install's latest
 * stored value. The "stats:cache" key is only
 * a read-side optimization with a short TTL and is never written by /ingest, so
 * it cannot introduce a write race either. The historical incremental-aggregate
 * design (a single "agg" key updated as agg += new - prior) was removed for
 * exactly this reason: Cloudflare KV has no atomic read-modify-write, so two
 * concurrent ingests could both read the same agg and the last put would win,
 * permanently dropping one install's delta. Deriving on read sidesteps that.
 *   The ONE remaining read-modify-write is the per-IP rate-limit counter
 * (rl:<h>:<win>), and it is deliberately best-effort: KV has no atomic
 * increment, so a tight burst from one IP can slip past the configured limit
 * (it under-counts, never over-counts). That is safe because the limiter is a
 * coarse speed bump, not the inflation bound. Inflation is bounded WITHOUT the
 * limiter by the per-install idempotent upsert + the MAX_PER_FIELD per-field
 * cap and the derive-on-read total, so an
 * approximate limiter cannot move the credible ceiling. See checkRateLimit and
 * README "Concurrency note" for the full rationale and the rejected
 * atomic-limiter alternatives.
 */

// Hard caps. A single install's cumulative count above these is almost
// certainly a bug or abuse, so we clamp rather than trust it. In hosted mode,
// untrusted installs cannot move progress totals at all; this cap remains
// defense in depth for trusted reporters and open self-hosted collectors. Tuned
// well above any believable single-host lifetime output.
const MAX_PER_FIELD = 100000;
const MAX_LINES_CHANGED = 5000000;
const COUNT_FIELDS = [
  "prs_opened",
  "prs_merged",
  "prs_reviewed",
  "issues_opened",
  "issues_closed",
  "files_changed",
  "lines_changed",
  "loc_added",
];
const WINDOW_COUNT_FIELDS = [
  "prs_opened",
  "prs_merged",
  "prs_reviewed",
  "issues_opened",
  "issues_closed",
  "files_changed",
  "lines_changed",
];
const WINDOW_LINE_ACTIVITY_FIELDS = ["prs_merged"];
const FIELD_MAXIMUMS = {
  lines_changed: MAX_LINES_CHANGED,
};
const STALE_COUNT_FIELDS = new Set(["lines_changed"]);

// install_id is client-generated and opaque. Bound its length and charset so
// a malformed or hostile value cannot blow up a KV key.
const INSTALL_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
// period is advisory metadata only (the client sends "lifetime"); it is never
// part of a storage key, but we still bound it defensively.
const PERIOD_RE = /^[A-Za-z0-9_-]{1,32}$/;

const EMPTY_WINDOW = {
  window_days: 30,
  prs_opened: 0,
  prs_merged: 0,
  prs_reviewed: 0,
  issues_opened: 0,
  issues_closed: 0,
  files_changed: 0,
  lines_changed: 0,
};

const EMPTY_AGG = {
  prs_opened: 0,
  prs_merged: 0,
  prs_reviewed: 0,
  issues_opened: 0,
  issues_closed: 0,
  files_changed: 0,
  lines_changed: 0,
  loc_added: 0,
  last_30_days: { ...EMPTY_WINDOW },
  installs: 0,
  updated_at: null,
};

// Prefixes for per-install records. /stats sums install:* for progress totals
// and, in trusted-counts mode, reads active:* markers only to refresh the
// timestamp for trusted installs already counted from install:* records.
const INSTALL_PREFIX = "install:";
const ACTIVITY_PREFIX = "active:";
const AUTH_PREFIX = "auth:";
const TRUSTED_INGEST_HEADER = "X-Alfred-Trusted-Token";
export const STATS_CACHE_SCHEMA_VERSION = 3;

// Short-lived cache of the derived totals. Purely a read optimization so a burst
// of /stats reads does not re-list every install each time; it is never written
// by /ingest and deleting it only forces a recompute. The hosted default is
// tuned for the Cloudflare free tier: near-live enough for a public impact page,
// but high enough that steady traffic does not turn into more than 1000 KV
// list() operations per day. Override with STATS_CACHE_TTL_SECONDS (0 disables
// caching entirely, always recomputing from the install records).
const STATS_CACHE_KEY = "stats:cache";

// Cloudflare Workers KV enforces a HARD MINIMUM of 60 seconds on
// `put(..., { expirationTtl })`: a put with a TTL below 60 is REJECTED, so a
// sub-60 cache write would throw, be swallowed, and the cache would never
// populate, making every /stats recompute by listing all install keys. So the
// usable TTL floor is 60. The hosted default is five minutes, which caps a
// continuously hit public page at 288 derived-total recomputes per day.
const KV_MIN_EXPIRATION_TTL_SECONDS = 60;
const DEFAULT_STATS_CACHE_TTL_SECONDS = 300;

// Resolve the stats-cache TTL with the KV minimum enforced.
//   unset / "" / malformed / negative -> the default (300)
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
export function clampCount(value, max = MAX_PER_FIELD) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  const floored = Math.floor(n);
  if (floored <= 0) return 0;
  return floored > max ? max : floored;
}

// Dependent PR counters that are, by definition, SUBSETS of prs_opened: every
// merged or reviewed PR is first an opened one. Neither may exceed prs_opened.
const DEPENDENT_PR_FIELDS = ["prs_merged", "prs_reviewed"];

/**
 * Enforce the PR-count invariant SERVER-SIDE: prs_merged and prs_reviewed are
 * subsets of prs_opened, so neither may exceed it. The client (lib/proof_
 * telemetry.py) already clamps these, but the Worker re-enforces it as defense
 * in depth: in open-write mode (no INGEST_TOKEN) a buggy or malicious client
 * could POST prs_opened:0 with prs_merged>0, and without this clamp the Worker
 * would store and aggregate that invariant-violating payload, corrupting the
 * public total. Clamping unconditionally (including the prs_opened==0 case)
 * means the stored per-install record can never reflect a violation, so the
 * derived aggregate can never reflect one either.
 *
 * Operates on an already-clamped counts object (each field a non-negative
 * integer via clampCount). Returns a NEW object; never mutates the input.
 */
export function clampDependentPrCounts(counts) {
  const opened = clampCount(counts && counts.prs_opened);
  const out = { ...counts, prs_opened: opened };
  for (const field of DEPENDENT_PR_FIELDS) {
    const value = clampCount(counts && counts[field]);
    out[field] = value > opened ? opened : value;
  }
  return out;
}

function clampDependentIssueCounts(counts) {
  const opened = clampCount(counts && counts.issues_opened);
  const closed = clampCount(counts && counts.issues_closed);
  return {
    ...counts,
    issues_opened: opened,
    issues_closed: closed > opened ? opened : closed,
  };
}

function clampDependentCounts(counts) {
  return clampDependentIssueCounts(clampDependentPrCounts(counts));
}

function normalizeCountFields(raw) {
  const counts = {};
  for (const field of COUNT_FIELDS) {
    counts[field] = clampCount(raw && raw[field], FIELD_MAXIMUMS[field] || MAX_PER_FIELD);
  }
  // `loc_added` was the original wire name for file touches. Keep it as an alias
  // so older reporters and newer UI code agree on the same file-count total.
  if (raw && raw.files_changed === undefined && raw.loc_added !== undefined) {
    counts.files_changed = counts.loc_added;
  }
  if (raw && raw.loc_added === undefined && raw.files_changed !== undefined) {
    counts.loc_added = counts.files_changed;
  }
  return counts;
}

function normalizeWindowCounts(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = { ...EMPTY_WINDOW };
  const windowDays = clampCount(raw.window_days, 366);
  out.window_days = windowDays > 0 ? windowDays : 30;
  for (const field of WINDOW_COUNT_FIELDS) {
    out[field] = clampCount(raw[field], FIELD_MAXIMUMS[field] || MAX_PER_FIELD);
  }
  return out;
}

function normalizeAggregateWindowCounts(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = { ...EMPTY_WINDOW };
  const windowDays = clampCount(raw.window_days, 366);
  out.window_days = windowDays > 0 ? windowDays : 30;
  for (const field of WINDOW_COUNT_FIELDS) {
    const n = Number(raw[field]);
    out[field] = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  }
  return out;
}

function normalizeStaleFields(raw) {
  if (!raw || !Array.isArray(raw.stale_fields)) return [];
  const out = [];
  for (const field of raw.stale_fields) {
    if (typeof field !== "string" || !STALE_COUNT_FIELDS.has(field)) continue;
    if (!out.includes(field)) out.push(field);
  }
  return out;
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
  if (raw.tombstone === true) {
    return {
      ok: true,
      value: { install_id: installId, period, tombstone: true },
    };
  }
  const counts = normalizeCountFields(raw);
  const last30Days = normalizeWindowCounts(raw.last_30_days);
  const staleFields = normalizeStaleFields(raw);
  // Enforce subset invariants server-side. A buggy or hostile open-write client
  // could POST dependent counters above their base count; clamp them here so the
  // normalized counts that flow into the stored record can never violate the
  // invariant.
  const invariant = clampDependentCounts(counts);
  const value = {
    ok: true,
    value: { install_id: installId, period, counts: invariant },
  };
  if (last30Days) {
    value.value.last_30_days = last30Days;
  }
  if (staleFields.length > 0) {
    value.value.stale_fields = staleFields;
  }
  return value;
}

// One progress record per install, keyed by install_id. Replaced on each
// count-bearing report (latest-wins).
function installKey(installId) {
  return `${INSTALL_PREFIX}${installId}`;
}

function activityKey(installId) {
  return `${ACTIVITY_PREFIX}${installId}`;
}

function authKey(installId) {
  return `${AUTH_PREFIX}${installId}`;
}

function base64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function randomToken(byteLength = 32) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  const bytes = new Uint8Array(digest);
  let hex = "";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

export async function registerInstall(kv, raw, now = new Date(), opts = {}) {
  if (raw !== null && (typeof raw !== "object" || Array.isArray(raw))) {
    return { ok: false, status: 400, error: "body must be a JSON object" };
  }
  const requestedId = raw && typeof raw.install_id === "string" ? raw.install_id : "";
  const installId = requestedId || randomToken(16);
  if (!INSTALL_ID_RE.test(installId)) {
    return { ok: false, status: 400, error: "install_id missing or malformed" };
  }
  const existing = await kv.get(authKey(installId), { type: "json" });
  if (existing && typeof existing === "object" && opts.replaceExisting !== true) {
    return { ok: false, status: 409, error: "install already registered" };
  }
  const token = randomToken(32);
  const token_sha256 = await sha256Hex(token);
  await kv.put(
    authKey(installId),
    JSON.stringify({
      token_sha256,
      created_at: now.toISOString(),
    }),
  );
  return { ok: true, install_id: installId, token };
}

/**
 * Coerce one stored per-install snapshot into a clean { ...counts, seen_at }.
 * Defensive: a hand-edited or partially written record must never poison the
 * derived total. Unknown/negative/non-numeric counts become 0.
 */
function normalizeSnapshot(stored) {
  const clamped = normalizeCountFields(stored);
  // Re-enforce subset invariants on read too: a hand-edited or legacy record
  // with dependent counts above their base count must not poison the derived
  // total.
  const out = clampDependentCounts(clamped);
  out.last_30_days = normalizeWindowCounts(stored && stored.last_30_days) || { ...EMPTY_WINDOW };
  out.seen_at =
    stored && typeof stored.seen_at === "string" ? stored.seen_at : null;
  out.trusted_reporter = Boolean(stored && stored.trusted_reporter === true);
  return out;
}

function hasFreshRollingWindow(snap, now = new Date()) {
  if (!snap || typeof snap !== "object") return false;
  if (!snap.last_30_days || typeof snap.last_30_days !== "object") return false;
  const seenAtMs = Date.parse(snap.seen_at || "");
  if (!Number.isFinite(seenAtMs)) return false;
  const windowDays = clampCount(snap.last_30_days.window_days, 366) || 30;
  return now.getTime() - seenAtMs <= windowDays * 24 * 60 * 60 * 1000;
}

function trustedCountsOnly(env) {
  const raw = env && env.TRUSTED_COUNTS_ONLY;
  if (raw === undefined || raw === null || raw === "") return false;
  return !["0", "false", "no", "off"].includes(String(raw).trim().toLowerCase());
}

/**
 * Compute the public totals by listing every install:* progress record and, in
 * trusted-counts mode, only trusting records written by a trusted collector.
 * install:* is the ONLY source of shipped-work totals. active:* can refresh
 * `updated_at` for a trusted install, but anonymous active markers are not
 * public proof and do not change public totals. The
 * returned shape matches EMPTY_AGG: { ...COUNT_FIELDS, installs, updated_at },
 * where `installs` is the distinct install count and `updated_at` is the most
 * recent `seen_at` across all records.
 *
 * Cost: one list() per 1000 keys plus one get() per install. For the expected
 * scale (tens to low-hundreds of installs)
 * this is cheap, and /stats caches the result for STATS_CACHE_TTL_SECONDS so a
 * read burst does not re-list each time. KV list() is paginated via `cursor`;
 * we follow it so a namespace larger than one page is summed in full.
 */
export async function computeTotals(kv, env, now = new Date()) {
  const totals = { ...EMPTY_AGG, last_30_days: { ...EMPTY_WINDOW } };
  const countsNeedTrust = trustedCountsOnly(env);
  const countedInstalls = new Set();
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
      if (countsNeedTrust && !snap.trusted_reporter) continue;
      const installId = name.slice(INSTALL_PREFIX.length);
      countedInstalls.add(installId);
      totals.installs += 1;
      for (const field of COUNT_FIELDS) {
        totals[field] += snap[field];
      }
      if (hasFreshRollingWindow(snap, now)) {
        totals.last_30_days.window_days = clampCount(snap.last_30_days.window_days, 366) || 30;
        for (const field of WINDOW_COUNT_FIELDS) {
          totals.last_30_days[field] += snap.last_30_days[field];
        }
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
  if (countsNeedTrust) {
    cursor = undefined;
    for (let page = 0; page < MAX_LIST_PAGES; page++) {
      const listed = await kv.list({ prefix: ACTIVITY_PREFIX, cursor });
      const keys = (listed && listed.keys) || [];
      for (const entry of keys) {
        const name = entry && entry.name;
        if (typeof name !== "string") continue;
        const installId = name.slice(ACTIVITY_PREFIX.length);
        if (!installId) continue;
        if (!countedInstalls.has(installId)) continue;
        const stored = await kv.get(name, { type: "json" });
        const seenAt = stored && typeof stored.seen_at === "string" ? stored.seen_at : null;
        if (seenAt && (totals.updated_at === null || seenAt > totals.updated_at)) {
          totals.updated_at = seenAt;
        }
      }
      if (listed && listed.list_complete === false && listed.cursor) {
        cursor = listed.cursor;
      } else {
        break;
      }
    }
  }
  return totals;
}

/**
 * Read the public totals behind a short KV-backed cache. The cache
 * (STATS_CACHE_KEY) is a pure read optimization with a TTL of
 * STATS_CACHE_TTL_SECONDS: a hit avoids re-listing install/activity records, a
 * miss recomputes from scratch and refreshes it. The cache is NEVER written by
 * /ingest, so it cannot race a write; the worst it can do is serve a value up to
 * the TTL stale, which is fine for an aggregate counter. Setting the TTL to 0
 * disables the cache and always recomputes.
 */
export async function readStats(kv, env) {
  const ttl = statsCacheTtl(env);
  if (ttl > 0) {
    const cached = await kv.get(STATS_CACHE_KEY, { type: "json" });
    if (
      cached &&
      typeof cached === "object" &&
      cached.cache_schema_version === STATS_CACHE_SCHEMA_VERSION
    ) {
      const totals = { ...EMPTY_AGG, last_30_days: { ...EMPTY_WINDOW } };
      for (const field of COUNT_FIELDS) {
        const n = Number(cached[field]);
        totals[field] = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
      }
      const hasFilesChanged = Object.prototype.hasOwnProperty.call(
        cached,
        "files_changed",
      );
      const hasLocAdded = Object.prototype.hasOwnProperty.call(cached, "loc_added");
      if (!hasFilesChanged && hasLocAdded) totals.files_changed = totals.loc_added;
      if (!hasLocAdded && hasFilesChanged) totals.loc_added = totals.files_changed;
      const cachedWindow = normalizeAggregateWindowCounts(cached.last_30_days);
      totals.last_30_days = cachedWindow || { ...EMPTY_WINDOW };
      const installs = Number(cached.installs);
      totals.installs =
        Number.isFinite(installs) && installs > 0 ? Math.floor(installs) : 0;
      totals.updated_at =
        typeof cached.updated_at === "string" ? cached.updated_at : null;
      return totals;
    }
  }
  const totals = await computeTotals(kv, env);
  if (ttl > 0) {
    // Refresh the cache. Best-effort: a failed cache write only costs a recompute
    // next read, never correctness.
    try {
      await kv.put(
        STATS_CACHE_KEY,
        JSON.stringify({
          ...totals,
          cache_schema_version: STATS_CACHE_SCHEMA_VERSION,
        }),
        {
          expirationTtl: ttl,
        },
      );
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
export async function ingest(kv, payload, now = new Date(), opts = {}) {
  const { install_id: installId, counts } = payload;

  const iso = now.toISOString();
  const isTrustedReporter = opts.trustedReporter === true;
  const countsNeedTrust = opts.trustedCountsOnly === true;
  if (countsNeedTrust && !isTrustedReporter) {
    const snapshot = { seen_at: iso };
    await kv.put(activityKey(installId), JSON.stringify(snapshot));
    try {
      await kv.delete(STATS_CACHE_KEY);
    } catch {
      /* cache invalidation is best-effort; ignore */
    }
    return {
      ...normalizeCountFields({}),
      last_30_days: { ...EMPTY_WINDOW },
      seen_at: iso,
      trusted_reporter: false,
    };
  }

  // Clamp every field (non-negative integer, capped) AND re-enforce the subset
  // invariant on the STORED record: prs_merged/prs_reviewed never exceed
  // prs_opened. This runs even if a caller reached ingest with counts that did
  // not pass through normalizePayload, so the per-install record the aggregate
  // sums can never reflect an invariant-violating payload.
  const clamped = normalizeCountFields(counts);
  const snapshot = clampDependentCounts(clamped);
  snapshot.last_30_days = normalizeWindowCounts(payload.last_30_days) || { ...EMPTY_WINDOW };
  const staleFields = Array.isArray(payload.stale_fields) ? payload.stale_fields : [];
  if (staleFields.includes("lines_changed")) {
    let previous = null;
    try {
      previous = await kv.get(installKey(installId), { type: "json" });
    } catch {
      previous = null;
    }
    const previousSnapshot = normalizeSnapshot(previous);
    const canPreservePrevious = !countsNeedTrust || previousSnapshot.trusted_reporter === true;
    const hasRollingLineActivity = WINDOW_LINE_ACTIVITY_FIELDS.some(
      (field) => snapshot.last_30_days[field] > 0,
    );
    if (snapshot.lines_changed === 0) {
      snapshot.lines_changed = clampCount(
        canPreservePrevious ? previousSnapshot.lines_changed : 0,
        FIELD_MAXIMUMS.lines_changed,
      );
    }
    snapshot.last_30_days.lines_changed = clampCount(
      canPreservePrevious && hasRollingLineActivity ? previousSnapshot.last_30_days.lines_changed : 0,
      FIELD_MAXIMUMS.lines_changed,
    );
  }
  snapshot.seen_at = iso;
  snapshot.trusted_reporter = isTrustedReporter;

  // The entire write: replace this install's record. No shared-state read,
  // no read-modify-write, nothing another ingest could clobber.
  await kv.put(installKey(installId), JSON.stringify(snapshot));
  if (countsNeedTrust) {
    await kv.put(activityKey(installId), JSON.stringify({ seen_at: iso }));
  }

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

export async function forgetInstall(kv, installId) {
  await kv.delete(installKey(installId));
  await kv.delete(activityKey(installId));
  await kv.delete(authKey(installId));
  try {
    await kv.delete(STATS_CACHE_KEY);
  } catch {
    /* cache invalidation is best-effort; ignore */
  }
}

function publicView(agg) {
  return {
    prs_opened: agg.prs_opened,
    prs_merged: agg.prs_merged,
    prs_reviewed: agg.prs_reviewed,
    issues_opened: agg.issues_opened,
    issues_closed: agg.issues_closed,
    files_changed: agg.files_changed,
    lines_changed: agg.lines_changed,
    loc_added: agg.loc_added,
    last_30_days: normalizeAggregateWindowCounts(agg.last_30_days) || { ...EMPTY_WINDOW },
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
function configuredStatsOrigins(env) {
  const raw = (env && env.ALLOWED_ORIGIN) || "";
  return String(raw)
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
}

function isLocalDevOrigin(origin) {
  try {
    const url = new URL(origin);
    const hostname = url.hostname.replace(/^\[(.*)\]$/, "$1");
    return (
      url.protocol === "http:" &&
      ["localhost", "127.0.0.1", "::1"].includes(hostname)
    );
  } catch {
    return false;
  }
}

function allowedStatsOrigin(request, env) {
  const origin = request.headers.get("Origin") || "";
  if (!origin) return "";
  if (configuredStatsOrigins(env).includes(origin)) return origin;
  if (isLocalDevOrigin(origin)) return origin;
  return "";
}

function statsCorsHeaders(request, env) {
  const origin = allowedStatsOrigin(request, env);
  const headers = {
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  // Only advertise an allow-origin when the site owner configured one. With it
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
    "Cache-Control": opts.cacheControl || "no-store",
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
  if (configuredStatsOrigins(env).includes(origin)) return { ok: true };
  return { ok: false, status: 403, error: "origin not allowed" };
}

/**
 * Shared-token write gate. Self-hosted collectors can set INGEST_TOKEN when
 * one shared credential is preferred. Hosted Alfred normally leaves this unset
 * and uses per-install tokens issued by /register.
 *
 * Returns { ok: true } or { ok: false, status, error }.
 */
function checkSharedIngestToken(request, env) {
  const expected = env && env.INGEST_TOKEN;
  if (!expected) return { configured: false, ok: true };
  const provided = request.headers.get("X-Ingest-Token") || "";
  if (!safeEqual(provided, expected)) {
    return {
      configured: true,
      ok: false,
      status: 401,
      error: "ingest token missing or invalid",
    };
  }
  return { configured: true, ok: true };
}

function checkTrustedReporterToken(request, env) {
  const expected = env && env.TRUSTED_INGEST_TOKEN;
  if (!expected) return { configured: false, trusted: false, ok: true };
  const provided = request.headers.get(TRUSTED_INGEST_HEADER) || "";
  if (!provided) return { configured: true, trusted: false, ok: true };
  if (!safeEqual(provided, expected)) {
    return {
      configured: true,
      trusted: false,
      ok: false,
      status: 401,
      error: "trusted token missing or invalid",
    };
  }
  return { configured: true, trusted: true, ok: true };
}

function installTokenRequired(env) {
  const raw = env && env.REQUIRE_INSTALL_TOKEN;
  if (raw === undefined || raw === null || raw === "") return false;
  return !["0", "false", "no", "off"].includes(String(raw).trim().toLowerCase());
}

function requestInstallToken(request) {
  const auth = request.headers.get("Authorization") || "";
  const match = auth.match(/^Bearer\s+(.+)$/i);
  if (match) return match[1].trim();
  return (request.headers.get("X-Ingest-Token") || "").trim();
}

async function checkInstallToken(kv, request, env, installId) {
  const stored = await kv.get(authKey(installId), { type: "json" });
  if (!stored || typeof stored !== "object") {
    if (installTokenRequired(env)) {
      return { ok: false, status: 401, error: "install is not registered" };
    }
    return { ok: true }; // open-write self-host mode
  }
  const expectedHash = typeof stored.token_sha256 === "string" ? stored.token_sha256 : "";
  const token = requestInstallToken(request);
  if (!expectedHash || !token) {
    return { ok: false, status: 401, error: "install token missing or invalid" };
  }
  const actualHash = await sha256Hex(token);
  if (!safeEqual(actualHash, expectedHash)) {
    return { ok: false, status: 401, error: "install token missing or invalid" };
  }
  return { ok: true };
}

// Per-IP rate limit on /ingest. A coarse fixed-window counter in KV keeps a
// single source from hammering the endpoint to spray distinct install_ids. The
// limit is intentionally generous (a legitimate host posts once a day) and only
// engages when the platform gives us a client IP; it is a speed bump on top of
// the token + count cap + idempotency, not the primary control.
//
// APPROXIMATE UNDER BURST, BY DESIGN. This is the one read-modify-write in the
// Worker, and Cloudflare KV has no atomic increment: a burst of concurrent
// requests from one IP can each read the same `current` before any put lands,
// so the effective ceiling under a tight burst is higher than the configured
// limit (it under-counts, never over-counts). That is acceptable here because
// the limiter is explicitly best-effort and is NOT what protects hosted progress
// totals; TRUSTED_COUNTS_ONLY + TRUSTED_INGEST_TOKEN does that. In open
// self-hosted mode, the limiter-independent bound is the per-install
// latest-wins idempotency + the MAX_PER_FIELD per-field cap + the
// derive-on-read total. A token gate, either per-install or shared, is the real
// write credential. An atomic limiter (the Workers Rate
// Limiting binding or a Durable Object) was considered and deliberately NOT
// adopted: the binding's
// fixed 10s/60s period cannot express this env-configurable per-hour window,
// and a Durable Object is unwarranted weight for a soft speed bump on a
// free-tier counter whose abuse bound does not depend on the limiter.
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
// bump; set RATE_LIMIT_SALT when stable buckets should survive isolate recycle.
let _ephemeralSalt = null;
function ephemeralSalt() {
  if (_ephemeralSalt === null) {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    _ephemeralSalt = bytes;
  }
  return _ephemeralSalt;
}

// Resolve the HMAC key material: the configured RATE_LIMIT_SALT when
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
  // Best-effort read-modify-write: KV has no atomic increment, so concurrent
  // requests from one IP can read the same `current` and the puts coalesce,
  // letting a tight burst exceed `max`. This is a deliberate tradeoff (see the
  // RATE_LIMIT_WINDOW_SECONDS comment): the limiter is a coarse speed bump, and
  // inflation is bounded by the per-install idempotent upsert + MAX_PER_FIELD
  // cap + derive-on-read total in open self-hosted mode regardless of how many
  // requests slip past here; hosted progress totals require the trusted token.
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
        return new Response(null, { status: 204, headers: statsCorsHeaders(request, env) });
      }
      // No CORS headers: the browser treats this as a failed preflight.
      return new Response(null, { status: 204 });
    }

    const kv = env && env.TELEMETRY;

    if (path === "/stats" && request.method === "GET") {
      const cors = statsCorsHeaders(request, env);
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env, { cors });
      // Totals are derived on read (sum of every install record), behind a short
      // cache. Never an incremented running total, so no race can lose counts.
      const totals = await readStats(kv, env);
      return jsonResponse(publicView(totals), 200, env, {
        cors,
        cacheControl: "public, max-age=60, stale-while-revalidate=240",
      });
    }

    if (path === "/register" && request.method === "POST") {
      if (!kv) return jsonResponse({ error: "telemetry store unavailable" }, 503, env);

      const ctype = checkContentType(request);
      if (!ctype.ok) return jsonResponse({ error: ctype.error }, ctype.status, env);

      const origin = checkOrigin(request, env);
      if (!origin.ok) return jsonResponse({ error: origin.error }, origin.status, env);

      const limited = await checkRateLimit(kv, request, env);
      if (!limited.ok) return jsonResponse({ error: limited.error }, limited.status, env);

      let raw;
      try {
        raw = await request.json();
      } catch {
        return jsonResponse({ error: "invalid JSON" }, 400, env);
      }
      const requestedId = raw && typeof raw.install_id === "string" ? raw.install_id : "";
      const trustedAuth = checkTrustedReporterToken(request, env);
      if (!trustedAuth.ok) {
        return jsonResponse({ error: trustedAuth.error }, trustedAuth.status, env);
      }
      let replaceExisting = trustedAuth.trusted === true;
      if (!replaceExisting && INSTALL_ID_RE.test(requestedId)) {
        const installAuth = await checkInstallToken(kv, request, env, requestedId);
        replaceExisting = installAuth.ok === true;
      }
      const registered = await registerInstall(kv, raw, new Date(), { replaceExisting });
      if (!registered.ok) {
        return jsonResponse({ error: registered.error }, registered.status || 400, env);
      }
      return jsonResponse(
        {
          ok: true,
          install_id: registered.install_id,
          token: registered.token,
          ingest: "/ingest",
        },
        200,
        env,
      );
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

      const sharedAuth = checkSharedIngestToken(request, env);
      if (!sharedAuth.ok) return jsonResponse({ error: sharedAuth.error }, sharedAuth.status, env);

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
      const trustedAuth = checkTrustedReporterToken(request, env);
      if (!trustedAuth.ok) {
        return jsonResponse({ error: trustedAuth.error }, trustedAuth.status, env);
      }
      if (!sharedAuth.configured) {
        const installAuth = await checkInstallToken(kv, request, env, parsed.value.install_id);
        if (!installAuth.ok) {
          return jsonResponse({ error: installAuth.error }, installAuth.status, env);
        }
      }
      if (parsed.value.tombstone) {
        await forgetInstall(kv, parsed.value.install_id);
        const totals = await computeTotals(kv, env);
        return jsonResponse({ ok: true, totals: publicView(totals) }, 200, env);
      }
      await ingest(kv, parsed.value, new Date(), {
        trustedReporter: trustedAuth.trusted || sharedAuth.configured,
        trustedCountsOnly: trustedCountsOnly(env),
      });
      // Report the fresh derived total back. ingest just invalidated the stats
      // cache, so this recomputes from the install records and includes the
      // write we just made. computeTotals (not readStats) so the response always
      // reflects this report rather than a possibly-cached older value.
      const totals = await computeTotals(kv, env);
      return jsonResponse({ ok: true, totals: publicView(totals) }, 200, env);
    }

    if (path === "/" && request.method === "GET") {
      return jsonResponse(
        {
          service: "alfred-proof-telemetry",
          endpoints: { register: "POST /register", ingest: "POST /ingest", stats: "GET /stats" },
          privacy: "anonymous aggregate counts only; no PII, no repo names, no IP storage",
        },
        200,
        env,
      );
    }

    return jsonResponse({ error: "not found" }, 404, env);
  },
};
