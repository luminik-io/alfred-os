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

import worker, {
  clampCount,
  clampDependentPrCounts,
  normalizePayload,
  ingest,
  registerInstall,
  forgetInstall,
  computeTotals,
  hashIp,
} from "../src/worker.js";

// --------------------------------------------------------------------------
// In-memory KV stub. Implements just the surface the Worker uses:
//   get(key)                  -> string | null
//   get(key, {type:"json"})   -> parsed | null
//   put(key, value, {expirationTtl}) -> void   (TTL ignored in-memory)
//   delete(key)               -> void
//   list({prefix, cursor})    -> { keys: [{name}], list_complete, cursor }
// list() returns a single complete page (no pagination) for the test scale.
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
    async delete(key) {
      store.delete(key);
    },
    async list(opts) {
      const prefix = (opts && opts.prefix) || "";
      const keys = [...store.keys()]
        .filter((k) => k.startsWith(prefix))
        .map((name) => ({ name }));
      return { keys, list_complete: true, cursor: undefined };
    },
  };
}

// Helper: the derived public total, the way /stats computes it. The old tests
// read these straight off ingest's return value (the running aggregate); under
// the derived-on-read model ingest returns only the install's own snapshot, so
// global assertions go through computeTotals instead.
const totalsOf = (kv) => computeTotals(kv);

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

test("normalizePayload allows larger changed-line totals than row counts", () => {
  const out = normalizePayload({
    install_id: "line-count01",
    period: "lifetime",
    prs_opened: 1,
    lines_changed: 321752,
  });
  assert.equal(out.ok, true);
  assert.equal(out.value.counts.lines_changed, 321752);
  assert.equal(
    normalizePayload({
      install_id: "line-count02",
      period: "lifetime",
      lines_changed: 999999999,
    }).value.counts.lines_changed,
    5000000,
  );
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
  assert.equal(
    normalizePayload({ install_id: "with space!", period: "2026-06" }).ok,
    false,
  );
});

test("normalizePayload defaults a missing or malformed period to 'lifetime'", () => {
  // period is advisory metadata only (never a storage key under the
  // install-keyed model), so its absence is fine: it defaults to "lifetime".
  const noPeriod = normalizePayload({ install_id: "a".repeat(8) });
  assert.equal(noPeriod.ok, true);
  assert.equal(noPeriod.value.period, "lifetime");

  const badPeriod = normalizePayload({ install_id: "a".repeat(8), period: "has space!" });
  assert.equal(badPeriod.ok, true);
  assert.equal(badPeriod.value.period, "lifetime");

  const goodPeriod = normalizePayload({ install_id: "a".repeat(8), period: "lifetime" });
  assert.equal(goodPeriod.value.period, "lifetime");
});

test("normalizePayload accepts a tombstone for an existing install id", () => {
  const payload = normalizePayload({
    install_id: "install-delete0",
    period: "lifetime",
    tombstone: true,
  });
  assert.equal(payload.ok, true);
  assert.equal(payload.value.tombstone, true);
  assert.equal(payload.value.install_id, "install-delete0");
  assert.equal(payload.value.counts, undefined);
});

test("normalizePayload clamps all count fields and keeps id/period", () => {
  const out = normalizePayload({
    install_id: "abcdef12",
    period: "2026-06",
    prs_opened: 5,
    prs_merged: -3,
    prs_reviewed: 2.7,
    issues_opened: 11,
    issues_closed: 12,
    files_changed: 77,
    lines_changed: 123,
    loc_added: 999999999,
    repo: "should-be-ignored",
  });
  assert.equal(out.ok, true);
  assert.deepEqual(out.value, {
    install_id: "abcdef12",
    period: "2026-06",
    counts: {
      prs_opened: 5,
      prs_merged: 0,
      prs_reviewed: 2,
      issues_opened: 11,
      issues_closed: 11,
      files_changed: 77,
      lines_changed: 123,
      loc_added: 100000,
    },
  });
  // No extra keys leaked from the raw payload.
  assert.deepEqual(Object.keys(out.value.counts).sort(), [
    "files_changed",
    "issues_closed",
    "issues_opened",
    "lines_changed",
    "loc_added",
    "prs_merged",
    "prs_opened",
    "prs_reviewed",
  ]);
});

// --------------------------------------------------------------------------
// clampDependentPrCounts: server-side subset invariant (Codex finding).
// prs_merged/prs_reviewed are subsets of prs_opened and may never exceed it.
// The Worker re-enforces this even though the client clamps, so an open-write
// client cannot POST prs_opened:0 with prs_merged>0 and corrupt the total.
// --------------------------------------------------------------------------
test("clampDependentPrCounts clamps dependent counters to prs_opened (incl. opened==0)", () => {
  // The exact invariant violation the finding calls out: opened 0, merged 5.
  assert.deepEqual(
    clampDependentPrCounts({
      prs_opened: 0,
      prs_merged: 5,
      prs_reviewed: 3,
      loc_added: 100,
    }),
    { prs_opened: 0, prs_merged: 0, prs_reviewed: 0, loc_added: 100 },
  );
  // Dependent counters above a non-zero opened are capped at opened.
  assert.deepEqual(
    clampDependentPrCounts({
      prs_opened: 4,
      prs_merged: 9,
      prs_reviewed: 7,
      loc_added: 0,
    }),
    { prs_opened: 4, prs_merged: 4, prs_reviewed: 4, loc_added: 0 },
  );
});

test("clampDependentPrCounts leaves a valid subset payload unchanged", () => {
  const valid = { prs_opened: 10, prs_merged: 7, prs_reviewed: 4, loc_added: 1200 };
  assert.deepEqual(clampDependentPrCounts(valid), valid);
  // It returns a new object and does not mutate the input.
  const input = { prs_opened: 5, prs_merged: 9, prs_reviewed: 1, loc_added: 0 };
  const out = clampDependentPrCounts(input);
  assert.equal(input.prs_merged, 9, "input is not mutated");
  assert.equal(out.prs_merged, 5, "output is clamped");
  assert.notStrictEqual(out, input);
});

test("normalizePayload clamps dependent counters server-side", () => {
  // A hostile or buggy open-write client POSTs opened:0 with dependent counts
  // above it. The Worker's normalization must clamp the dependent counters.
  const out = normalizePayload({
    install_id: "abcdef12",
    period: "lifetime",
    prs_opened: 0,
    prs_merged: 5,
    prs_reviewed: 3,
    issues_closed: 5,
    loc_added: 100,
  });
  assert.equal(out.ok, true);
  assert.deepEqual(out.value.counts, {
    prs_opened: 0,
    prs_merged: 0,
    prs_reviewed: 0,
    issues_opened: 0,
    issues_closed: 0,
    files_changed: 100,
    lines_changed: 0,
    loc_added: 100,
  });

  // A normal subset payload is unchanged.
  const valid = normalizePayload({
    install_id: "abcdef12",
    period: "lifetime",
    prs_opened: 10,
    prs_merged: 7,
    prs_reviewed: 4,
    issues_opened: 3,
    issues_closed: 2,
    loc_added: 800,
  });
  assert.deepEqual(valid.value.counts, {
    prs_opened: 10,
    prs_merged: 7,
    prs_reviewed: 4,
    issues_opened: 3,
    issues_closed: 2,
    files_changed: 800,
    lines_changed: 0,
    loc_added: 800,
  });
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

  await ingest(kv, payload, FIXED);
  const agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, 10);
  assert.equal(agg.prs_merged, 7);
  assert.equal(agg.prs_reviewed, 4);
  assert.equal(agg.files_changed, 1200);
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
  await ingest(kv, make(), FIXED); // identical re-send
  const agg = await totalsOf(kv);

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
  await ingest(kv, grown, FIXED);
  const agg = await totalsOf(kv);

  assert.equal(agg.prs_opened, 8);
  assert.equal(agg.prs_merged, 5);
  assert.equal(agg.prs_reviewed, 1);
  assert.equal(agg.loc_added, 250);
  assert.equal(agg.installs, 1);
});

test("lifetime contract: daily re-sends of a stable period never inflate the aggregate", async () => {
  // The reporter sends cumulative lifetime counts into a single fixed period
  // ("lifetime"). Re-sending the same totals every day must add nothing; only a
  // genuine increase in the lifetime total moves the aggregate. This is the
  // server side of the no-double-count contract.
  const kv = makeKV();
  const day = (opened, merged) =>
    normalizePayload({
      install_id: "install-lifetime0",
      period: "lifetime",
      prs_opened: opened,
      prs_merged: merged,
      prs_reviewed: 0,
      loc_added: 0,
    }).value;

  await ingest(kv, day(40, 30), FIXED); // first report
  await ingest(kv, day(40, 30), FIXED); // identical daily re-send: +0
  await ingest(kv, day(40, 30), FIXED); // and again: still +0
  let agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, 40, "identical re-sends must not inflate");
  assert.equal(agg.prs_merged, 30);

  await ingest(kv, day(42, 31), FIXED); // real growth: +2 / +1
  agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, 42, "only the increase is folded in");
  assert.equal(agg.prs_merged, 31);
  assert.equal(agg.installs, 1, "still one install across all re-sends");
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
  await ingest(kv, b, FIXED);
  const agg = await totalsOf(kv);

  assert.equal(agg.prs_opened, 10);
  assert.equal(agg.prs_merged, 5);
  assert.equal(agg.prs_reviewed, 3);
  assert.equal(agg.loc_added, 100);
  assert.equal(agg.installs, 2, "two distinct installs counted");
});

test("forgetInstall removes an install from public totals", async () => {
  const kv = makeKV();
  await ingest(
    kv,
    normalizePayload({ install_id: "install-delete1", prs_opened: 8, prs_merged: 5 }).value,
    FIXED,
  );
  await ingest(
    kv,
    normalizePayload({ install_id: "install-delete2", prs_opened: 3, prs_merged: 1 }).value,
    FIXED,
  );

  await forgetInstall(kv, "install-delete1");
  const totals = await totalsOf(kv);
  assert.equal(totals.prs_opened, 3);
  assert.equal(totals.prs_merged, 1);
  assert.equal(totals.installs, 1);
});

test("forgetInstall removes the install auth token too", async () => {
  const kv = makeKV();
  const registered = await registerInstall(kv, { install_id: "install-delete-auth" }, FIXED);
  assert.equal(registered.ok, true);
  assert.equal(kv.store.has("auth:install-delete-auth"), true);

  await forgetInstall(kv, "install-delete-auth");

  assert.equal(kv.store.has("auth:install-delete-auth"), false);
});

// --------------------------------------------------------------------------
// Concurrent ingest: the whole point of deriving totals on read. Two ingests
// landing "at the same time" must BOTH be reflected in the derived total, with
// no count lost. Under the old incremental-aggregate design two concurrent
// ingests could both read the same "agg" and the last put would win, silently
// dropping one install's delta; deriving on read removes that race entirely.
// --------------------------------------------------------------------------
test("concurrent ingests from two installs are both reflected in the derived total", async () => {
  const kv = makeKV();
  const a = normalizePayload({
    install_id: "install-concur-a0",
    period: "lifetime",
    prs_opened: 7,
    prs_merged: 4,
    prs_reviewed: 2,
    loc_added: 70,
  }).value;
  const b = normalizePayload({
    install_id: "install-concur-b0",
    period: "lifetime",
    prs_opened: 5,
    prs_merged: 3,
    prs_reviewed: 1,
    loc_added: 30,
  }).value;

  // Fire both at once. With derived-on-read there is no shared running total to
  // clobber, so the order they resolve in cannot lose either install's counts.
  await Promise.all([ingest(kv, a, FIXED), ingest(kv, b, FIXED)]);

  const agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, 12, "both installs' prs_opened summed, none lost");
  assert.equal(agg.prs_merged, 7);
  assert.equal(agg.prs_reviewed, 3);
  assert.equal(agg.loc_added, 100);
  assert.equal(agg.installs, 2, "two distinct installs counted");
});

test("interleaved ingests cannot lose a count (the old read-modify-write race)", async () => {
  // Reproduce the EXACT interleaving that broke the old incremental aggregate:
  // both installs are "in flight" before either has finished writing. We drive
  // the two ingests as concurrent promises whose individual KV operations are
  // forced to yield to the event loop between steps, so their writes interleave.
  // Because each ingest writes only its OWN install key and the total is summed
  // on read, the interleaving is harmless: both records exist and both are
  // counted. The old design would have lost whichever delta wrote first.
  const base = makeKV();
  let yields = 0;
  const yieldingKv = {
    store: base.store,
    async get(key, opts) {
      await Promise.resolve();
      yields += 1;
      return base.get(key, opts);
    },
    async put(key, value) {
      await Promise.resolve();
      yields += 1;
      return base.put(key, value);
    },
    async delete(key) {
      await Promise.resolve();
      yields += 1;
      return base.delete(key);
    },
    async list(opts) {
      await Promise.resolve();
      return base.list(opts);
    },
  };

  const mk = (id, opened) =>
    normalizePayload({
      install_id: id,
      period: "lifetime",
      prs_opened: opened,
      prs_merged: 0,
      prs_reviewed: 0,
      loc_added: 0,
    }).value;

  await Promise.all([
    ingest(yieldingKv, mk("install-race-aaaa", 11), FIXED),
    ingest(yieldingKv, mk("install-race-bbbb", 13), FIXED),
  ]);

  assert.ok(yields > 0, "the KV operations actually interleaved via the event loop");
  const agg = await totalsOf(base);
  assert.equal(agg.prs_opened, 24, "no count lost across interleaved writes (11 + 13)");
  assert.equal(agg.installs, 2, "both interleaved installs counted");
});

test("the public total always equals the sum of installs' latest lifetime values", async () => {
  // The invariant the derived-on-read model guarantees: regardless of report
  // order, re-sends, or growth, GET /stats equals the sum over install records
  // of each install's CURRENT stored counts.
  const kv = makeKV();
  const send = (id, opened, merged) =>
    ingest(
      kv,
      normalizePayload({
        install_id: id,
        period: "lifetime",
        prs_opened: opened,
        prs_merged: merged,
        prs_reviewed: 0,
        loc_added: 0,
      }).value,
      FIXED,
    );

  await send("install-inv-aaaa", 5, 2);
  await send("install-inv-bbbb", 9, 4);
  await send("install-inv-aaaa", 8, 3); // a grows (latest-wins replace)
  await send("install-inv-cccc", 1, 0);
  await send("install-inv-bbbb", 9, 4); // b idempotent re-send

  // Independently sum the stored install records and compare to the derived API.
  let sumOpened = 0;
  let sumMerged = 0;
  let count = 0;
  for (const [key, value] of kv.store.entries()) {
    if (!key.startsWith("install:")) continue;
    const rec = JSON.parse(value);
    sumOpened += rec.prs_opened;
    sumMerged += rec.prs_merged;
    count += 1;
  }

  const agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, sumOpened, "derived total equals sum of records");
  assert.equal(agg.prs_merged, sumMerged);
  assert.equal(agg.installs, count);
  // Concretely: a=8/3, b=9/4, c=1/0 -> 18 opened, 7 merged, 3 installs.
  assert.equal(agg.prs_opened, 18);
  assert.equal(agg.prs_merged, 7);
  assert.equal(agg.installs, 3);
});

test("install-keyed model: a changed period does not re-add a constant lifetime total", async () => {
  // The storage key is install_id ONLY; period is advisory metadata. Even if
  // the period label changes between reports (it never does in practice, the
  // client always sends "lifetime"), the same install's cumulative total must
  // be treated latest-wins, not summed into a fresh bucket. This is the core
  // no-double-count guard: only the install's record drives the aggregate.
  const kv = makeKV();
  const report = (period, opened, merged) =>
    normalizePayload({
      install_id: "install-ffffffff",
      period,
      prs_opened: opened,
      prs_merged: merged,
      prs_reviewed: 0,
      loc_added: 0,
    }).value;

  await ingest(kv, report("2026-06", 8, 3), FIXED);
  // A different period label carrying the SAME cumulative total must add zero.
  await ingest(kv, report("2026-07", 8, 3), FIXED);
  const agg = await totalsOf(kv);

  assert.equal(agg.prs_opened, 8, "a new period label must not re-add the lifetime total");
  assert.equal(agg.prs_merged, 3);
  assert.equal(agg.installs, 1, "the same install is still one install");

  // Only the per-install record exists, keyed by install_id (no per-period key).
  assert.ok(kv.store.has("install:install-ffffffff"), "one record per install");
  assert.equal(
    [...kv.store.keys()].filter((k) => k.startsWith("i:")).length,
    0,
    "no per-period pair keys remain",
  );
});

test("install-keyed model: the per-install record is replaced, not appended", async () => {
  const kv = makeKV();
  const report = (opened) =>
    normalizePayload({
      install_id: "install-replace0",
      period: "lifetime",
      prs_opened: opened,
      prs_merged: 0,
      prs_reviewed: 0,
      loc_added: 0,
    }).value;

  await ingest(kv, report(5), FIXED);
  await ingest(kv, report(9), FIXED);
  await ingest(kv, report(12), FIXED);

  // Exactly one record for this install, holding the latest total.
  const installRecords = [...kv.store.keys()].filter((k) => k === "install:install-replace0");
  assert.equal(installRecords.length, 1, "exactly one record per install");
  const snapshot = JSON.parse(kv.store.get("install:install-replace0"));
  assert.equal(snapshot.prs_opened, 12, "the record holds the latest cumulative total");
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
  await ingest(kv, corrected, FIXED);
  const agg = await totalsOf(kv);

  assert.equal(agg.prs_opened, 2);
  assert.equal(agg.prs_merged, 1);
  assert.ok(agg.prs_opened >= 0 && agg.prs_merged >= 0);
});

test("ingest clamps a dependent counter above prs_opened in the stored record (defense in depth)", async () => {
  // Drive ingest with raw counts that VIOLATE the subset invariant, bypassing
  // normalizePayload, to prove the stored per-install record is clamped by
  // ingest itself. opened:0, merged:5 must be stored as merged:0.
  const kv = makeKV();
  await ingest(
    kv,
    {
      install_id: "install-invariant",
      period: "lifetime",
      counts: { prs_opened: 0, prs_merged: 5, prs_reviewed: 3, loc_added: 100 },
    },
    FIXED,
  );

  const snapshot = JSON.parse(kv.store.get("install:install-invariant"));
  assert.equal(snapshot.prs_opened, 0);
  assert.equal(snapshot.prs_merged, 0, "stored merged clamped to opened (0)");
  assert.equal(snapshot.prs_reviewed, 0, "stored reviewed clamped to opened (0)");
  assert.equal(snapshot.loc_added, 100, "an independent counter is untouched");

  const agg = await totalsOf(kv);
  assert.equal(agg.prs_opened, 0);
  assert.equal(agg.prs_merged, 0, "the derived aggregate never reflects the violation");
  assert.equal(agg.prs_reviewed, 0);
  assert.equal(agg.loc_added, 100);
});

test("POST /ingest stores opened:0/merged:5 as merged:0 and leaves a valid payload unchanged", async () => {
  // End-to-end over the HTTP surface: an open-write (no INGEST_TOKEN) client
  // POSTs an invariant-violating body. The server must clamp it before storing,
  // so GET /stats can never report merged > opened. A second, valid subset
  // payload from another install must round-trip unchanged.
  const env = { TELEMETRY: makeKV() };

  const bad = await worker.fetch(
    req("POST", "/ingest", {
      install_id: "install-badsubset",
      period: "lifetime",
      prs_opened: 0,
      prs_merged: 5,
      prs_reviewed: 3,
      loc_added: 0,
    }),
    env,
  );
  assert.equal(bad.status, 200);
  const badBody = await bad.json();
  assert.equal(badBody.totals.prs_opened, 0);
  assert.equal(badBody.totals.prs_merged, 0, "merged clamped to opened (0) before aggregation");
  assert.equal(badBody.totals.prs_reviewed, 0);

  const good = await worker.fetch(
    req("POST", "/ingest", {
      install_id: "install-goodsubset",
      period: "lifetime",
      prs_opened: 8,
      prs_merged: 5,
      prs_reviewed: 2,
      loc_added: 300,
    }),
    env,
  );
  assert.equal(good.status, 200);

  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  // Only the valid install contributes PR counts; the bad one added merged 0.
  assert.equal(stats.prs_opened, 8, "8 + 0");
  assert.equal(stats.prs_merged, 5, "valid subset payload unchanged, bad one clamped");
  assert.equal(stats.prs_reviewed, 2);
  assert.equal(stats.loc_added, 300);
  assert.equal(stats.installs, 2);
});

// --------------------------------------------------------------------------
// /stats derived-on-read cache: a pure read optimization that can never change
// the derived answer, only avoid recomputing it within the TTL.
// --------------------------------------------------------------------------
test("GET /stats writes a derived-totals cache and serves it on the next read", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-cache001", prs_opened: 4 }).value,
    FIXED,
  );

  // First read populates the cache.
  const first = await worker.fetch(req("GET", "/stats"), env);
  assert.equal((await first.json()).prs_opened, 4);
  assert.ok(kv.store.has("stats:cache"), "first /stats read populates the cache");

  // Tamper the cache to a sentinel value: a cache HIT must return it verbatim,
  // proving the second read came from the cache and did not re-list installs.
  kv.store.set(
    "stats:cache",
    JSON.stringify({
      prs_opened: 999,
      prs_merged: 0,
      prs_reviewed: 0,
      loc_added: 0,
      installs: 1,
      updated_at: FIXED.toISOString(),
    }),
  );
  const second = await worker.fetch(req("GET", "/stats"), env);
  assert.equal((await second.json()).prs_opened, 999, "served from cache");
});

test("GET /stats cache hit preserves aggregate totals above the per-install cap", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  kv.store.set(
    "stats:cache",
    JSON.stringify({
      prs_opened: 120000,
      prs_merged: 110000,
      prs_reviewed: 115000,
      issues_opened: 130000,
      issues_closed: 125000,
      files_changed: 140000,
      lines_changed: 150000,
      loc_added: 140000,
      installs: 2,
      updated_at: FIXED.toISOString(),
    }),
  );

  const res = await worker.fetch(req("GET", "/stats"), env);
  const stats = await res.json();

  assert.equal(stats.prs_opened, 120000);
  assert.equal(stats.prs_merged, 110000);
  assert.equal(stats.prs_reviewed, 115000);
  assert.equal(stats.issues_opened, 130000);
  assert.equal(stats.issues_closed, 125000);
  assert.equal(stats.files_changed, 140000);
  assert.equal(stats.lines_changed, 150000);
  assert.equal(stats.loc_added, 140000);
  assert.equal(stats.installs, 2);
});

test("GET /stats preserves legacy cached loc_added as files_changed", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  kv.store.set(
    "stats:cache",
    JSON.stringify({
      prs_opened: 3,
      prs_merged: 2,
      prs_reviewed: 1,
      loc_added: 321,
      installs: 1,
      updated_at: FIXED.toISOString(),
    }),
  );

  const res = await worker.fetch(req("GET", "/stats"), env);
  const stats = await res.json();

  assert.equal(stats.files_changed, 321);
  assert.equal(stats.loc_added, 321);
});

test("a new ingest invalidates the stats cache so the next /stats recomputes", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-cache002", prs_opened: 4 }).value,
    FIXED,
  );
  await worker.fetch(req("GET", "/stats"), env); // populate cache
  assert.ok(kv.store.has("stats:cache"));

  // A fresh report through the HTTP path must drop the cache so the number is
  // not stuck behind the TTL.
  await worker.fetch(
    req("POST", "/ingest", { install_id: "install-cache003", prs_opened: 6 }),
    env,
  );
  assert.equal(kv.store.has("stats:cache"), false, "ingest invalidated the cache");

  const res = await worker.fetch(req("GET", "/stats"), env);
  const body = await res.json();
  assert.equal(body.prs_opened, 10, "recomputed total reflects both installs");
  assert.equal(body.installs, 2);
});

test("POST /ingest tombstone removes an install and invalidates cached stats", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-delete3", prs_opened: 7, prs_merged: 4 }).value,
    FIXED,
  );
  await worker.fetch(req("GET", "/stats"), env); // populate cache
  assert.equal(kv.store.has("stats:cache"), true);

  const res = await worker.fetch(
    req("POST", "/ingest", { install_id: "install-delete3", tombstone: true }),
    env,
  );
  assert.equal(res.status, 200);
  assert.equal(kv.store.has("install:install-delete3"), false);
  assert.equal(kv.store.has("stats:cache"), false);
  const body = await res.json();
  assert.equal(body.totals.prs_opened, 0);
  assert.equal(body.totals.installs, 0);
});

test("STATS_CACHE_TTL_SECONDS=0 disables the cache (always recompute)", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv, STATS_CACHE_TTL_SECONDS: "0" };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-cache004", prs_opened: 3 }).value,
    FIXED,
  );
  const res = await worker.fetch(req("GET", "/stats"), env);
  assert.equal((await res.json()).prs_opened, 3);
  assert.equal(kv.store.has("stats:cache"), false, "no cache key written when TTL is 0");
});

// --------------------------------------------------------------------------
// Stats-cache TTL must honor the Cloudflare KV minimum (Codex finding #1).
// Workers KV REJECTS put(..., { expirationTtl }) below 60 seconds. A sub-60 TTL
// would make every cache write throw (swallowed in readStats), so the cache
// would never populate and /stats would re-list all install keys on every read.
// A KV stub that records the put TTL (and rejects < 60, like the real KV) lets
// us assert the Worker never writes the cache below the floor.
// --------------------------------------------------------------------------
function makeTtlRecordingKV() {
  const kv = makeKV();
  kv.putTtls = [];
  const innerPut = kv.put.bind(kv);
  kv.put = async (key, value, opts) => {
    const ttl = opts && opts.expirationTtl;
    if (key === "stats:cache") {
      kv.putTtls.push(ttl);
      // Mirror Cloudflare KV: a TTL below 60 is rejected outright.
      if (typeof ttl === "number" && ttl < 60) {
        throw new Error(`KV PUT failed: expirationTtl of ${ttl} is below the 60s minimum`);
      }
    }
    return innerPut(key, value, opts);
  };
  return kv;
}

test("stats cache write uses the free-tier-friendly default TTL", async () => {
  // Default (no STATS_CACHE_TTL_SECONDS): the put must use a TTL >= 60 so KV
  // accepts it and the cache actually populates. Hosted Alfred uses 300s so a
  // continuously hit public page stays below the free KV list/day cap.
  const kv = makeTtlRecordingKV();
  const env = { TELEMETRY: kv };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-ttl00001", prs_opened: 5 }).value,
    FIXED,
  );
  const res = await worker.fetch(req("GET", "/stats"), env);
  assert.equal((await res.json()).prs_opened, 5);
  assert.ok(kv.store.has("stats:cache"), "cache populated under the default TTL");
  assert.ok(kv.putTtls.length >= 1, "a cache put was attempted");
  for (const ttl of kv.putTtls) {
    assert.equal(ttl, 300, "default stats-cache TTL is 300s");
  }
});

test("GET /stats allows short public response caching", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv, ALLOWED_ORIGIN: "https://alfred.luminik.io" };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-cache-hdr", prs_opened: 5 }).value,
    FIXED,
  );

  const res = await worker.fetch(req("GET", "/stats"), env);

  assert.equal(res.status, 200);
  assert.equal(
    res.headers.get("Cache-Control"),
    "public, max-age=60, stale-while-revalidate=240",
  );
});

test("a sub-60 STATS_CACHE_TTL_SECONDS is clamped up to 60 (KV would reject below)", async () => {
  // 1..59 cannot be honored: KV rejects the put. The Worker must clamp up to 60
  // so the cache still populates rather than silently never caching.
  for (const raw of ["1", "30", "59"]) {
    const kv = makeTtlRecordingKV();
    const env = { TELEMETRY: kv, STATS_CACHE_TTL_SECONDS: raw };
    await ingest(
      kv,
      normalizePayload({ install_id: `install-ttlc${raw.padStart(4, "0")}`, prs_opened: 2 }).value,
      FIXED,
    );
    const res = await worker.fetch(req("GET", "/stats"), env);
    assert.equal((await res.json()).prs_opened, 2);
    assert.ok(kv.store.has("stats:cache"), `cache populated for STATS_CACHE_TTL_SECONDS=${raw}`);
    for (const ttl of kv.putTtls) {
      assert.ok(ttl >= 60, `TTL ${ttl} (from ${raw}) must be clamped up to >= 60`);
    }
  }
});

test("a >=60 STATS_CACHE_TTL_SECONDS is used verbatim", async () => {
  const kv = makeTtlRecordingKV();
  const env = { TELEMETRY: kv, STATS_CACHE_TTL_SECONDS: "120" };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-ttl00120", prs_opened: 1 }).value,
    FIXED,
  );
  await worker.fetch(req("GET", "/stats"), env);
  assert.deepEqual(kv.putTtls, [120], "an above-floor TTL passes through unchanged");
});

test("STATS_CACHE_TTL_SECONDS=0 never attempts a cache put at all", async () => {
  // The disable contract: with TTL 0 the Worker must not call put for the cache
  // key, so there is nothing for KV to reject and no cache key is written.
  const kv = makeTtlRecordingKV();
  const env = { TELEMETRY: kv, STATS_CACHE_TTL_SECONDS: "0" };
  await ingest(
    kv,
    normalizePayload({ install_id: "install-ttl00000", prs_opened: 7 }).value,
    FIXED,
  );
  const res = await worker.fetch(req("GET", "/stats"), env);
  assert.equal((await res.json()).prs_opened, 7);
  assert.equal(kv.putTtls.length, 0, "no cache put issued when the cache is disabled");
  assert.equal(kv.store.has("stats:cache"), false, "no cache key written when TTL is 0");
});

test("GET /stats sums many installs derived-on-read (no incremented aggregate)", async () => {
  const kv = makeKV();
  const env = { TELEMETRY: kv };
  for (let i = 0; i < 5; i++) {
    await worker.fetch(
      req("POST", "/ingest", {
        install_id: `install-many-${String(i).padStart(4, "0")}`,
        prs_opened: 2,
        prs_merged: 1,
      }),
      env,
    );
  }
  const res = await worker.fetch(req("GET", "/stats"), env);
  const body = await res.json();
  assert.equal(body.prs_opened, 10, "5 installs x 2 opened, summed on read");
  assert.equal(body.prs_merged, 5);
  assert.equal(body.installs, 5);
  // No legacy "agg" key is ever written under the derived model.
  assert.equal(kv.store.has("agg"), false, "no incremented aggregate key exists");
});

// --------------------------------------------------------------------------
// fetch: HTTP surface
// --------------------------------------------------------------------------
function req(method, path, body, headers = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    init.headers = { "Content-Type": "application/json", ...headers };
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
      issues_opened: 5,
      issues_closed: 4,
      files_changed: 800,
      lines_changed: 1200,
      loc_added: 800,
    }),
    env,
  );
  assert.equal(postRes.status, 200);
  const postBody = await postRes.json();
  assert.equal(postBody.ok, true);
  assert.equal(postBody.totals.prs_opened, 9);

  const getRes = await worker.fetch(
    req("GET", "/stats", undefined, { Origin: "https://alfred.example.com" }),
    env,
  );
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
    issues_opened: 5,
    issues_closed: 4,
    files_changed: 800,
    lines_changed: 1200,
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

test("OPTIONS preflight on /stats returns the scoped CORS origin", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(
    req("OPTIONS", "/stats", undefined, { Origin: "https://alfred.example.com" }),
    env,
  );
  assert.equal(res.status, 204);
  assert.equal(
    res.headers.get("Access-Control-Allow-Origin"),
    "https://alfred.example.com",
  );
  // Only GET is advertised for the read endpoint; POST is not a browser caller.
  assert.match(res.headers.get("Access-Control-Allow-Methods") || "", /GET/);
  assert.doesNotMatch(res.headers.get("Access-Control-Allow-Methods") || "", /POST/);
});

test("GET /stats allows localhost preview origins", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(
    req("GET", "/stats", undefined, { Origin: "http://127.0.0.1:4327" }),
    env,
  );
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:4327");
});

test("GET /stats does not allow arbitrary browser origins", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(
    req("GET", "/stats", undefined, { Origin: "https://random.example.com" }),
    env,
  );
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
});

test("OPTIONS preflight on /ingest carries NO allow-origin (browser POST blocked)", async () => {
  // A browser preflight for a cross-origin POST to /ingest must fail: with no
  // Access-Control-Allow-Origin the browser will not send the actual request,
  // so a visitor's tab cannot inflate the public counter.
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(req("OPTIONS", "/ingest"), env);
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
});

test("POST /ingest response never carries an allow-origin, even with ALLOWED_ORIGIN set", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(
    req("POST", "/ingest", {
      install_id: "install-cors0001",
      period: "lifetime",
      prs_opened: 1,
      prs_merged: 0,
      prs_reviewed: 0,
      loc_added: 0,
    }),
    env,
  );
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
});

test("GET /stats never advertises a wildcard origin", async () => {
  // With ALLOWED_ORIGIN unset, /stats stays readable server-side but does not
  // hand out "*"; only browser cross-origin reads are gated.
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(req("GET", "/stats"), env);
  assert.equal(res.status, 200);
  assert.notEqual(res.headers.get("Access-Control-Allow-Origin"), "*");
});

// --------------------------------------------------------------------------
// /ingest simple-request rejection (Content-Type gate)
// --------------------------------------------------------------------------
function ingestReqWithCtype(body, contentType) {
  const init = { method: "POST", body: JSON.stringify(body) };
  init.headers = contentType === undefined ? {} : { "Content-Type": contentType };
  return new Request("https://telemetry.example.com/ingest", init);
}

test("ingest rejects a text/plain body (simple POST bypass blocked)", async () => {
  // A cross-origin browser can fire a text/plain POST with NO preflight; the
  // Worker must refuse it so a hidden browser write cannot reach KV.
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(ingestReqWithCtype(SAMPLE_PAYLOAD, "text/plain"), env);
  assert.equal(res.status, 415);
  const body = await res.json();
  assert.match(body.error, /application\/json/);
});

test("ingest rejects a form-encoded body (another simple Content-Type)", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(
    ingestReqWithCtype(SAMPLE_PAYLOAD, "application/x-www-form-urlencoded"),
    env,
  );
  assert.equal(res.status, 415);
});

test("ingest rejects a missing Content-Type", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(ingestReqWithCtype(SAMPLE_PAYLOAD, undefined), env);
  assert.equal(res.status, 415);
});

test("ingest accepts application/json with a charset parameter", async () => {
  // A real server-side client may append "; charset=utf-8"; that is still JSON.
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(
    ingestReqWithCtype(SAMPLE_PAYLOAD, "application/json; charset=utf-8"),
    env,
  );
  assert.equal(res.status, 200);
});

// --------------------------------------------------------------------------
// /ingest Origin allowlist (browser-origin requests are gated)
// --------------------------------------------------------------------------
test("ingest rejects a browser Origin that does not match ALLOWED_ORIGIN", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const req = new Request("https://telemetry.example.com/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "https://evil.example.com" },
    body: JSON.stringify(SAMPLE_PAYLOAD),
  });
  const res = await worker.fetch(req, env);
  assert.equal(res.status, 403);
  const body = await res.json();
  assert.match(body.error, /origin/);
});

test("ingest allows a browser Origin that matches ALLOWED_ORIGIN", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const req = new Request("https://telemetry.example.com/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "https://alfred.example.com" },
    body: JSON.stringify(SAMPLE_PAYLOAD),
  });
  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
});

test("ingest allows a browser Origin listed in comma-separated ALLOWED_ORIGIN", async () => {
  const env = {
    TELEMETRY: makeKV(),
    ALLOWED_ORIGIN: "https://site.example.com, https://alfred.example.com",
  };
  const req = new Request("https://telemetry.example.com/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "https://alfred.example.com" },
    body: JSON.stringify(SAMPLE_PAYLOAD),
  });
  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
});

test("ingest allows a server-side caller (no Origin header) regardless of ALLOWED_ORIGIN", async () => {
  const env = { TELEMETRY: makeKV(), ALLOWED_ORIGIN: "https://alfred.example.com" };
  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD), env);
  assert.equal(res.status, 200, "urllib sends no Origin and must pass");
});

// --------------------------------------------------------------------------
// /ingest write gate: optional shared token
// --------------------------------------------------------------------------
function ingestReq(body, token, trustedToken) {
  const init = { method: "POST", body: JSON.stringify(body) };
  init.headers = { "Content-Type": "application/json" };
  if (token !== undefined) init.headers["X-Ingest-Token"] = token;
  if (trustedToken !== undefined) init.headers["X-Alfred-Trusted-Token"] = trustedToken;
  return new Request("https://telemetry.example.com/ingest", init);
}

const SAMPLE_PAYLOAD = {
  install_id: "install-token001",
  period: "lifetime",
  prs_opened: 3,
  prs_merged: 2,
  prs_reviewed: 1,
  loc_added: 50,
};

test("ingest accepts writes when no INGEST_TOKEN is configured (open-write mode)", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD), env);
  assert.equal(res.status, 200);
});

test("ingest rejects a missing token when INGEST_TOKEN is configured", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_TOKEN: "s3cr3t-token" };
  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD), env);
  assert.equal(res.status, 401);
  const body = await res.json();
  assert.match(body.error, /token/);
});

test("ingest rejects a wrong token when INGEST_TOKEN is configured", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_TOKEN: "s3cr3t-token" };
  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, "wrong-token"), env);
  assert.equal(res.status, 401);
});

test("ingest accepts the correct token when INGEST_TOKEN is configured", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_TOKEN: "s3cr3t-token" };
  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, "s3cr3t-token"), env);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
});

test("the token check runs before the body is parsed (no work for unauthorized callers)", async () => {
  // A bad-token request with a garbage body still 401s, not 400: auth precedes
  // parsing, so an unauthorized caller cannot probe the JSON validator.
  const env = { TELEMETRY: makeKV(), INGEST_TOKEN: "s3cr3t-token" };
  const badBody = new Request("https://telemetry.example.com/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Ingest-Token": "nope" },
    body: "{not json",
  });
  const res = await worker.fetch(badBody, env);
  assert.equal(res.status, 401);
});

function registerReq(body = {}, token, trustedToken) {
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  if (token !== undefined) init.headers["X-Ingest-Token"] = token;
  if (trustedToken !== undefined) init.headers["X-Alfred-Trusted-Token"] = trustedToken;
  return new Request("https://telemetry.example.com/register", init);
}

test("register issues a per-install token and stores only its hash", async () => {
  const kv = makeKV();
  const result = await registerInstall(kv, { install_id: "install-reg001" }, FIXED);
  assert.equal(result.ok, true);
  assert.equal(result.install_id, "install-reg001");
  assert.ok(result.token.length >= 32);

  const stored = JSON.parse(kv.store.get("auth:install-reg001"));
  assert.equal(typeof stored.token_sha256, "string");
  assert.notEqual(stored.token_sha256, result.token);
  assert.equal(stored.created_at, FIXED.toISOString());
});

test("POST /register returns credentials for the install", async () => {
  const env = { TELEMETRY: makeKV() };
  const res = await worker.fetch(registerReq({ install_id: "install-reg002" }), env);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.install_id, "install-reg002");
  assert.ok(body.token);
  assert.equal(body.ingest, "/ingest");
});

test("register refuses to overwrite an existing install token", async () => {
  const kv = makeKV();
  const first = await registerInstall(kv, { install_id: "install-reg003" }, FIXED);
  assert.equal(first.ok, true);
  const stored = kv.store.get("auth:install-reg003");

  const second = await registerInstall(kv, { install_id: "install-reg003" }, FIXED);
  assert.equal(second.ok, false);
  assert.equal(second.status, 409);
  assert.equal(kv.store.get("auth:install-reg003"), stored);
});

test("POST /register returns 409 for an already-registered install", async () => {
  const env = { TELEMETRY: makeKV() };
  const first = await worker.fetch(registerReq({ install_id: "install-reg004" }), env);
  assert.equal(first.status, 200);

  const second = await worker.fetch(registerReq({ install_id: "install-reg004" }), env);
  assert.equal(second.status, 409);
  assert.match((await second.json()).error, /already registered/);
});

test("POST /register can rotate an existing token with the current install token", async () => {
  const env = { TELEMETRY: makeKV(), REQUIRE_INSTALL_TOKEN: "1" };
  const first = await (await worker.fetch(registerReq({ install_id: "install-reg005" }), env)).json();

  const rotated = await worker.fetch(
    registerReq({ install_id: "install-reg005" }, first.token),
    env,
  );
  assert.equal(rotated.status, 200);
  const rotatedBody = await rotated.json();
  assert.ok(rotatedBody.token);
  assert.notEqual(rotatedBody.token, first.token);

  const stale = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, install_id: "install-reg005" }, first.token),
    env,
  );
  assert.equal(stale.status, 401);
  const ok = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, install_id: "install-reg005" }, rotatedBody.token),
    env,
  );
  assert.equal(ok.status, 200);
});

test("POST /register can recover a hosted trusted reporter with the trusted token", async () => {
  const env = {
    TELEMETRY: makeKV(),
    REQUIRE_INSTALL_TOKEN: "1",
    TRUSTED_INGEST_TOKEN: "trusted-secret",
  };
  const first = await (await worker.fetch(registerReq({ install_id: "install-reg006" }), env)).json();

  const withoutProof = await worker.fetch(registerReq({ install_id: "install-reg006" }), env);
  assert.equal(withoutProof.status, 409);

  const wrongTrusted = await worker.fetch(
    registerReq({ install_id: "install-reg006" }, undefined, "wrong-secret"),
    env,
  );
  assert.equal(wrongTrusted.status, 401);

  const recovered = await worker.fetch(
    registerReq({ install_id: "install-reg006" }, undefined, "trusted-secret"),
    env,
  );
  assert.equal(recovered.status, 200);
  const recoveredBody = await recovered.json();
  assert.ok(recoveredBody.token);
  assert.notEqual(recoveredBody.token, first.token);

  const stale = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, install_id: "install-reg006" }, first.token),
    env,
  );
  assert.equal(stale.status, 401);
  const ok = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, install_id: "install-reg006" }, recoveredBody.token),
    env,
  );
  assert.equal(ok.status, 200);
});

test("ingest requires the registered install token when REQUIRE_INSTALL_TOKEN is set", async () => {
  const env = { TELEMETRY: makeKV(), REQUIRE_INSTALL_TOKEN: "1" };

  const missing = await worker.fetch(ingestReq(SAMPLE_PAYLOAD), env);
  assert.equal(missing.status, 401);

  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();
  const wrong = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, "wrong-token"), env);
  assert.equal(wrong.status, 401);

  const ok = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token), env);
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).ok, true);
});

test("registered install token gates tombstone deletes too", async () => {
  const env = { TELEMETRY: makeKV(), REQUIRE_INSTALL_TOKEN: "1" };
  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();
  await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token), env);

  const badDelete = await worker.fetch(
    ingestReq({ install_id: SAMPLE_PAYLOAD.install_id, tombstone: true }, "wrong-token"),
    env,
  );
  assert.equal(badDelete.status, 401);
  assert.equal((await worker.fetch(req("GET", "/stats"), env).then((r) => r.json())).installs, 1);

  const goodDelete = await worker.fetch(
    ingestReq({ install_id: SAMPLE_PAYLOAD.install_id, tombstone: true }, registered.token),
    env,
  );
  assert.equal(goodDelete.status, 200);
  assert.equal((await worker.fetch(req("GET", "/stats"), env).then((r) => r.json())).installs, 0);

  const staleToken = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, prs_opened: 99 }, registered.token),
    env,
  );
  assert.equal(staleToken.status, 401);

  const reRegistered = await (
    await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)
  ).json();
  assert.ok(reRegistered.token);
  assert.notEqual(reRegistered.token, registered.token);
});

test("trusted-counts mode ignores untrusted self-reported progress totals", async () => {
  const env = { TELEMETRY: makeKV(), REQUIRE_INSTALL_TOKEN: "1", TRUSTED_COUNTS_ONLY: "1" };
  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();

  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token), env);
  assert.equal(res.status, 200);

  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  assert.equal(stats.installs, 1);
  assert.equal(stats.prs_opened, 0);
  assert.equal(stats.prs_merged, 0);
  assert.equal(stats.files_changed, 0);
});

test("trusted-counts mode accepts progress totals from the trusted collector token", async () => {
  const env = {
    TELEMETRY: makeKV(),
    REQUIRE_INSTALL_TOKEN: "1",
    TRUSTED_COUNTS_ONLY: "1",
    TRUSTED_INGEST_TOKEN: "trusted-secret",
  };
  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();

  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token, "trusted-secret"), env);
  assert.equal(res.status, 200);

  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  assert.equal(stats.installs, 1);
  assert.equal(stats.prs_opened, 3);
  assert.equal(stats.prs_merged, 2);
  assert.equal(stats.files_changed, 50);
});

test("trusted-counts mode keeps trusted totals after an untrusted refresh", async () => {
  const env = {
    TELEMETRY: makeKV(),
    REQUIRE_INSTALL_TOKEN: "1",
    TRUSTED_COUNTS_ONLY: "1",
    TRUSTED_INGEST_TOKEN: "trusted-secret",
  };
  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();

  const trusted = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token, "trusted-secret"), env);
  assert.equal(trusted.status, 200);

  const untrustedRefresh = await worker.fetch(
    ingestReq({ ...SAMPLE_PAYLOAD, prs_opened: 99999, prs_merged: 99999 }, registered.token),
    env,
  );
  assert.equal(untrustedRefresh.status, 200);

  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  assert.equal(stats.installs, 1);
  assert.equal(stats.prs_opened, 3);
  assert.equal(stats.prs_merged, 2);
  assert.equal(stats.files_changed, 50);
});

test("a wrong trusted collector token is rejected", async () => {
  const env = {
    TELEMETRY: makeKV(),
    REQUIRE_INSTALL_TOKEN: "1",
    TRUSTED_COUNTS_ONLY: "1",
    TRUSTED_INGEST_TOKEN: "trusted-secret",
  };
  const registered = await (await worker.fetch(registerReq({ install_id: SAMPLE_PAYLOAD.install_id }), env)).json();

  const res = await worker.fetch(ingestReq(SAMPLE_PAYLOAD, registered.token, "wrong-secret"), env);
  assert.equal(res.status, 401);
  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  assert.equal(stats.installs, 0);
});

// --------------------------------------------------------------------------
// /ingest per-IP rate limit
// --------------------------------------------------------------------------
function ingestReqFromIp(body, ip) {
  return new Request("https://telemetry.example.com/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
    body: JSON.stringify(body),
  });
}

test("ingest rate-limits a single IP after the configured maximum", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_RATE_LIMIT: "3" };
  const ip = "203.0.113.7";
  for (let i = 0; i < 3; i++) {
    const ok = await worker.fetch(
      ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: `install-rl${i}00000` }, ip),
      env,
    );
    assert.equal(ok.status, 200, `request ${i} within the limit should pass`);
  }
  const blocked = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-rlblocked" }, ip),
    env,
  );
  assert.equal(blocked.status, 429, "the request over the limit should be rejected");
});

test("the rate limit is per-IP: a different IP is unaffected", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_RATE_LIMIT: "1" };
  const first = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-ipaaaaaa" }, "203.0.113.1"),
    env,
  );
  assert.equal(first.status, 200);
  const blockedSame = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-ipaaaaab" }, "203.0.113.1"),
    env,
  );
  assert.equal(blockedSame.status, 429);
  const otherIp = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-ipbbbbbb" }, "203.0.113.2"),
    env,
  );
  assert.equal(otherIp.status, 200, "a separate IP has its own window");
});

// --------------------------------------------------------------------------
// Keyed rate-limit hash (Codex finding #3). The bucket key is derived with an
// HMAC over the IP under a server-side salt, so a KV reader cannot brute-force
// the dotted-quad space back to the IP without the salt. These tests assert the
// keyed-hash properties directly via the exported hashIp.
// --------------------------------------------------------------------------
test("hashIp: two different IPs map to different buckets", async () => {
  const env = { RATE_LIMIT_SALT: "server-side-secret" };
  const a = await hashIp("203.0.113.7", env);
  const b = await hashIp("203.0.113.8", env);
  assert.notEqual(a, b, "distinct IPs must land in distinct buckets");
});

test("hashIp: the same IP under the same salt is stable", async () => {
  const env = { RATE_LIMIT_SALT: "server-side-secret" };
  const a = await hashIp("198.51.100.4", env);
  const b = await hashIp("198.51.100.4", env);
  assert.equal(a, b, "a stable salt buckets a source consistently within a window");
});

test("hashIp: the hash is keyed, so a different salt changes the bucket for the same IP", async () => {
  // This is the property that makes the key non-reversible: the digest depends
  // on the secret salt, not on the IP alone, so it cannot be precomputed/brute-
  // forced from the public dotted-quad space without knowing the salt.
  const ip = "192.0.2.55";
  const h1 = await hashIp(ip, { RATE_LIMIT_SALT: "salt-one" });
  const h2 = await hashIp(ip, { RATE_LIMIT_SALT: "salt-two" });
  assert.notEqual(h1, h2, "the same IP under different salts must hash differently");
});

test("hashIp: the digest never contains or equals the raw IP", async () => {
  const ip = "203.0.113.42";
  const h = await hashIp(ip, { RATE_LIMIT_SALT: "server-side-secret" });
  assert.ok(!h.includes(ip), "the bucket hash must not embed the raw IP");
  assert.notEqual(h, ip);
  assert.match(h, /^[0-9a-f]+$/, "hex-encoded HMAC truncation");
});

test("hashIp: an unset salt fails safe with a per-isolate ephemeral salt", async () => {
  // With no RATE_LIMIT_SALT configured the Worker still produces a keyed hash
  // (under a per-isolate random salt), so the IP is still not embedded in the
  // key. Within one isolate the ephemeral salt is stable, so the bucket is
  // consistent enough to rate-limit.
  const ip = "198.51.100.9";
  const h1 = await hashIp(ip, {});
  const h2 = await hashIp(ip, {});
  assert.ok(!h1.includes(ip), "even without a configured salt the IP is not in the key");
  assert.match(h1, /^[0-9a-f]+$/);
  assert.equal(h1, h2, "the per-isolate ephemeral salt is stable within the isolate");
});

test("the rate limit still enforces with a configured RATE_LIMIT_SALT", async () => {
  // End-to-end: the keyed-hash path is wired through checkRateLimit and the
  // bucket key written to KV is the rl:<hmac>:<window> form, never the raw IP.
  const kv = makeKV();
  const env = { TELEMETRY: kv, INGEST_RATE_LIMIT: "2", RATE_LIMIT_SALT: "deploy-secret" };
  const ip = "203.0.113.200";
  for (let i = 0; i < 2; i++) {
    const ok = await worker.fetch(
      ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: `install-salt${i}0000` }, ip),
      env,
    );
    assert.equal(ok.status, 200);
  }
  const blocked = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-saltblock" }, ip),
    env,
  );
  assert.equal(blocked.status, 429, "the keyed-hash bucket still counts per source");

  // The KV bucket key must not contain the raw IP.
  const rlKeys = [...kv.store.keys()].filter((k) => k.startsWith("rl:"));
  assert.ok(rlKeys.length >= 1, "a rate-limit bucket key was written");
  for (const k of rlKeys) {
    assert.ok(!k.includes(ip), "the rate-limit KV key must not embed the raw IP");
  }
});

// The per-IP limiter is documented as best-effort: KV has no atomic increment,
// so a concurrent burst from one IP can slip past the limit (Codex finding on
// worker.js:639). These two tests pin the intended contract: serial traffic is
// still enforced (the common case), and a concurrent burst is tolerated without
// error AND cannot inflate the public total beyond the per-install cap, because
// inflation is bounded by latest-wins idempotency, not by the limiter.
test("rate limit enforces on serial traffic (the common, non-burst case)", async () => {
  const env = { TELEMETRY: makeKV(), INGEST_RATE_LIMIT: "2" };
  const ip = "203.0.113.55";
  // Serial requests resolve their read-modify-write one at a time, so the
  // counter is exact here and the limit holds precisely.
  for (let i = 0; i < 2; i++) {
    const ok = await worker.fetch(
      ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: `install-ser${i}00000` }, ip),
      env,
    );
    assert.equal(ok.status, 200, `serial request ${i} within the limit should pass`);
  }
  const blocked = await worker.fetch(
    ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: "install-serblock0" }, ip),
    env,
  );
  assert.equal(blocked.status, 429, "serial traffic over the limit is rejected exactly");
});

test("concurrent burst from one IP is tolerated best-effort and cannot inflate the total", async () => {
  // Fire a burst concurrently: the non-atomic KV read-modify-write means some
  // requests can read the same counter value and slip past the limit. The
  // contract is NOT that the limit is exact under burst (it is documented
  // best-effort); it is that the Worker never errors and the public total stays
  // bounded by the per-install latest-wins records, not by request volume.
  const env = { TELEMETRY: makeKV(), INGEST_RATE_LIMIT: "2" };
  const ip = "203.0.113.66";
  const burst = Array.from({ length: 8 }, (_, i) =>
    worker.fetch(
      ingestReqFromIp({ ...SAMPLE_PAYLOAD, install_id: `install-burst${i}0000` }, ip),
      env,
    ),
  );
  const results = await Promise.all(burst);
  for (const r of results) {
    assert.ok(
      r.status === 200 || r.status === 429,
      "every burst request resolves cleanly (200 or 429), never an error",
    );
  }
  // The credibility bound: even if every burst write landed, the public total is
  // the SUM of distinct install records, each capped at MAX_PER_FIELD and
  // idempotent. Re-sends from the same install replace rather than add, so the
  // total can never exceed (distinct installs) * cap regardless of the limiter.
  const stats = await (await worker.fetch(req("GET", "/stats"), env)).json();
  const distinctInstalls = stats.installs;
  assert.ok(distinctInstalls <= 8, "at most one record per distinct install_id");
  assert.ok(
    stats.prs_opened <= distinctInstalls * 100000,
    "the derived total is bounded by per-install caps, not by request count",
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
    issues_opened: 0,
    issues_closed: 0,
    files_changed: 0,
    lines_changed: 0,
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
