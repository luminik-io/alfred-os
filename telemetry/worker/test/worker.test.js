/**
 * Unit tests for the proof-telemetry Worker.
 *
 * Run with the repo's Node (>=18, ships `node:test`):
 *   node --test telemetry/worker/test/
 *
 * No network, no real KV. A tiny in-memory stub stands in for Workers KV.
 */

import test from "node:test";
import assert from "node:assert/strict";

import worker, { clampCount, normalizePayload, ingest } from "../src/worker.js";

// --------------------------------------------------------------------------
// In-memory KV stub. Implements just the surface the Worker uses:
//   get(key)                -> string | null
//   get(key, {type:"json"}) -> parsed | null
//   put(key, value)         -> void
// --------------------------------------------------------------------------
function makeKV() {
  const store = new Map();
  return {
    store,
    async get(key, opts) {
      const raw = store.has(key) ? store.get(key) : null;
      if (raw === null) return null;
      if (opts && opts.type === "json") {
        try {
          return JSON.parse(raw);
        } catch {
          return null;
        }
      }
      return raw;
    },
    async put(key, value) {
      store.set(key, value);
    },
  };
}

const FIXED = new Date("2026-06-15T00:00:00.000Z");

// --------------------------------------------------------------------------
// clampCount
// --------------------------------------------------------------------------
test("clampCount floors negatives and non-numbers to 0", () => {
  assert.equal(clampCount(-5), 0);
  assert.equal(clampCount("nope"), 0);
  assert.equal(clampCount(null), 0);
  assert.equal(clampCount(undefined), 0);
  assert.equal(clampCount(NaN), 0);
  assert.equal(clampCount(Infinity), 0);
});

test("clampCount truncates floats and caps at the max", () => {
  assert.equal(clampCount(3.9), 3);
  assert.equal(clampCount(100000), 100000);
  assert.equal(clampCount(999999999), 100000);
});

// --------------------------------------------------------------------------
// normalizePayload
// --------------------------------------------------------------------------
test("normalizePayload rejects non-objects and bad ids", () => {
  assert.equal(normalizePayload(null).ok, false);
  assert.equal(normalizePayload([]).ok, false);
  assert.equal(normalizePayload("x").ok, false);
  assert.equal(normalizePayload({ period: "2026-06" }).ok, false); // no id
  assert.equal(normalizePayload({ install_id: "short", period: "2026-06" }).ok, false);
  assert.equal(normalizePayload({ install_id: "a".repeat(8) }).ok, false); // no period
  assert.equal(
    normalizePayload({ install_id: "with space!", period: "2026-06" }).ok,
    false,
  );
});

test("normalizePayload clamps all count fields and keeps id/period", () => {
  const out = normalizePayload({
    install_id: "abcdef12",
    period: "2026-06",
    prs_opened: 5,
    prs_merged: -3,
    prs_reviewed: 2.7,
    loc_added: 999999999,
    repo: "should-be-ignored",
  });
  assert.equal(out.ok, true);
  assert.deepEqual(out.value, {
    install_id: "abcdef12",
    period: "2026-06",
    counts: { prs_opened: 5, prs_merged: 0, prs_reviewed: 2, loc_added: 100000 },
  });
  // No extra keys leaked from the raw payload.
  assert.deepEqual(Object.keys(out.value.counts).sort(), [
    "loc_added",
    "prs_merged",
    "prs_opened",
    "prs_reviewed",
  ]);
});

// --------------------------------------------------------------------------
// ingest: aggregation, idempotency, distinct installs, clamping
// --------------------------------------------------------------------------
test("ingest folds the first send into empty totals", async () => {
  const kv = makeKV();
  const payload = normalizePayload({
    install_id: "install-aaaaaaaa",
    period: "2026-06",
    prs_opened: 10,
    prs_merged: 7,
    prs_reviewed: 4,
    loc_added: 1200,
  }).value;

  const agg = await ingest(kv, payload, FIXED);
  assert.equal(agg.prs_opened, 10);
  assert.equal(agg.prs_merged, 7);
  assert.equal(agg.prs_reviewed, 4);
  assert.equal(agg.loc_added, 1200);
  assert.equal(agg.installs, 1);
  assert.equal(agg.updated_at, FIXED.toISOString());
});

test("ingest is idempotent: re-sending the same period does not double count", async () => {
  const kv = makeKV();
  const make = () =>
    normalizePayload({
      install_id: "install-bbbbbbbb",
      period: "2026-06",
      prs_opened: 10,
      prs_merged: 7,
      prs_reviewed: 4,
      loc_added: 1200,
    }).value;

  await ingest(kv, make(), FIXED);
  const agg = await ingest(kv, make(), FIXED); // identical re-send

  assert.equal(agg.prs_opened, 10, "re-send must not double count");
  assert.equal(agg.prs_merged, 7);
  assert.equal(agg.loc_added, 1200);
  assert.equal(agg.installs, 1, "same install must not increment install count");
});

test("ingest applies only the delta when a period's cumulative count grows", async () => {
  const kv = makeKV();
  const first = normalizePayload({
    install_id: "install-cccccccc",
    period: "2026-06",
    prs_opened: 5,
    prs_merged: 3,
    prs_reviewed: 1,
    loc_added: 100,
  }).value;
  const grown = normalizePayload({
    install_id: "install-cccccccc",
    period: "2026-06",
    prs_opened: 8, // +3
    prs_merged: 5, // +2
    prs_reviewed: 1, // +0
    loc_added: 250, // +150
  }).value;

  await ingest(kv, first, FIXED);
  const agg = await ingest(kv, grown, FIXED);

  assert.equal(agg.prs_opened, 8);
  assert.equal(agg.prs_merged, 5);
  assert.equal(agg.prs_reviewed, 1);
  assert.equal(agg.loc_added, 250);
  assert.equal(agg.installs, 1);
});

test("ingest counts distinct installs and sums across them", async () => {
  const kv = makeKV();
  const a = normalizePayload({
    install_id: "install-dddddddd",
    period: "2026-06",
    prs_opened: 4,
    prs_merged: 2,
    prs_reviewed: 1,
    loc_added: 40,
  }).value;
  const b = normalizePayload({
    install_id: "install-eeeeeeee",
    period: "2026-06",
    prs_opened: 6,
    prs_merged: 3,
    prs_reviewed: 2,
    loc_added: 60,
  }).value;

  await ingest(kv, a, FIXED);
  const agg = await ingest(kv, b, FIXED);

  assert.equal(agg.prs_opened, 10);
  assert.equal(agg.prs_merged, 5);
  assert.equal(agg.prs_reviewed, 3);
  assert.equal(agg.loc_added, 100);
  assert.equal(agg.installs, 2, "two distinct installs counted");
});

test("ingest counts one install across multiple periods only once", async () => {
  const kv = makeKV();
  const june = normalizePayload({
    install_id: "install-ffffffff",
    period: "2026-06",
    prs_opened: 3,
    prs_merged: 1,
    prs_reviewed: 0,
    loc_added: 10,
  }).value;
  const july = normalizePayload({
    install_id: "install-ffffffff",
    period: "2026-07",
    prs_opened: 5,
    prs_merged: 2,
    prs_reviewed: 1,
    loc_added: 20,
  }).value;

  await ingest(kv, june, FIXED);
  const agg = await ingest(kv, july, FIXED);

  assert.equal(agg.prs_opened, 8, "both periods sum");
  assert.equal(agg.prs_merged, 3);
  assert.equal(agg.installs, 1, "same install across two periods still one install");
});

test("ingest never pushes a total negative on a downward correction", async () => {
  const kv = makeKV();
  const high = normalizePayload({
    install_id: "install-99999999",
    period: "2026-06",
    prs_opened: 10,
    prs_merged: 5,
    prs_reviewed: 0,
    loc_added: 0,
  }).value;
  const corrected = normalizePayload({
    install_id: "install-99999999",
    period: "2026-06",
    prs_opened: 2, // corrected downward
    prs_merged: 1,
    prs_reviewed: 0,
    loc_added: 0,
  }).value;

  await ingest(kv, high, FIXED);
  const agg = await ingest(kv, corrected, FIXED);

  assert.equal(agg.prs_opened, 2);
  assert.equal(agg.prs_merged, 1);
  assert.ok(agg.prs_opened >= 0 && agg.prs_merged >= 0);
});

// --------------------------------------------------------------------------
// fetch: HTTP surface
// --------------------------------------------------------------------------
function req(method, path, body) {
  const init = { method };
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    init.headers = { "Content-Type": "application/json" };
  }
  return new Request(`https://telemetry.example.com${path}`, init);
}

test("POST /ingest then GET /stats round-trips the aggregate", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };

  const postRes = await worker.fetch(
    req("POST", "/ingest", {
      install_id: "install-11112222",
      period: "2026-06",
      prs_opened: 9,
      prs_merged: 6,
      prs_reviewed: 3,
      loc_added: 800,
    }),
    env,
  );
  assert.equal(postRes.status, 200);
  const postBody = await postRes.json();
  assert.equal(postBody.ok, true);
  assert.equal(postBody.totals.prs_opened, 9);

  const getRes = await worker.fetch(req("GET", "/stats"), env);
  assert.equal(getRes.status, 200);
  assert.equal(
    getRes.headers.get("Access-Control-Allow-Origin"),
    "https://alfred.example.com",
  );
  const stats = await getRes.json();
  assert.deepEqual(stats, {
    prs_opened: 9,
    prs_merged: 6,
    prs_reviewed: 3,
    loc_added: 800,
    installs: 1,
    updated_at: stats.updated_at,
  });
  assert.equal(typeof stats.updated_at, "string");
});

test("POST /ingest rejects malformed bodies with 400", async () => {
  const env = { TELEMETRY: makeKV() };

  const badJson = await worker.fetch(req("POST", "/ingest", "{not json"), env);
  assert.equal(badJson.status, 400);

  const badPayload = await worker.fetch(
    req("POST", "/ingest", { period: "2026-06" }),
    env,
  );
  assert.equal(badPayload.status, 400);
});

test("OPTIONS preflight returns CORS headers", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(req("OPTIONS", "/ingest"), env);
  assert.equal(res.status, 204);
  assert.equal(
    res.headers.get("Access-Control-Allow-Origin"),
    "https://alfred.example.com",
  );
});

test("GET /stats on an empty store returns zeroed totals", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(req("GET", "/stats"), env);
  assert.equal(res.status, 200);
  const stats = await res.json();
  assert.deepEqual(stats, {
    prs_opened: 0,
    prs_merged: 0,
    prs_reviewed: 0,
    loc_added: 0,
    installs: 0,
    updated_at: null,
  });
});

test("unknown route returns 404", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(req("GET", "/nope"), env);
  assert.equal(res.status, 404);
});
