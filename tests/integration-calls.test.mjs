import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

// The token script runs on the author's server only. The public snapshot ships without ops/ at all, so the
// test skips only there; in the private repo a missing script still fails the test.
const tokenHostScript = fileURLToPath(new URL("../ops/integration-calls/token_host.py", import.meta.url));
const publicSnapshot = !existsSync(fileURLToPath(new URL("../ops", import.meta.url)));
import { FileBucket, SQLiteDatabase } from "../lib/runtime-storage.ts";
import { integrationRoute, callFilename, callJobId, applyFills, blankFills } from "../lib/integration.ts";
import { fromLoopbackOnly, bearerMatches } from "../lib/integration-gate.ts";
import { removeJob, loadJob, updateJob, markCallViewed, clearDeletedCallContacts } from "../lib/job-store.ts";
import { CHUNK_BYTES } from "../lib/contracts.ts";

// Synthetic values only: a random token per run and a number from the 555-0100..0199 block kept for fiction.
const TOKEN = randomBytes(32).toString("base64url"), OWNER = "fixture-owner", PHONE = "+1202555" + "0123";
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const drizzle = fileURLToPath(new URL("../drizzle/", import.meta.url));
// What Next's server itself fills in for a direct loopback call (next/dist/server/base-server.js).
const loopback = { host: "127.0.0.1:3187", "x-forwarded-host": "127.0.0.1:3187", "x-forwarded-for": "127.0.0.1", "x-forwarded-proto": "http", "x-forwarded-port": "3187" };

async function fixture(run, token = TOKEN) {
  const dir = await mkdtemp(join(tmpdir(), "meeting-integration-"));
  const db = new SQLiteDatabase(join(dir, "meeting.sqlite"));
  try {
    for (const name of (await readdir(drizzle)).filter(n => /^\d+.*\.sql$/.test(n)).sort()) db.connection.exec(await readFile(join(drizzle, name), "utf8"));
    await db.prepare("INSERT INTO settings(key,value) VALUES('owner',?)").bind(OWNER).run();
    const ctx = { db, bucket: new FileBucket(join(dir, "objects")), token };
    await run({ ctx, db, call: (path, options = {}) => send(ctx, path, options) });
  } finally { db.close(); await rm(dir, { recursive: true, force: true }); }
}
async function send(ctx, path, { method = "GET", headers = {}, json, body, auth = `Bearer ${TOKEN}` } = {}) {
  const all = { ...loopback, ...(auth ? { authorization: auth } : {}), ...(json !== undefined ? { "content-type": "application/json" } : {}), ...headers };
  for (const [k, v] of Object.entries(all)) if (v === undefined) delete all[k];
  const payload = json !== undefined ? JSON.stringify(json) : body;
  if (payload !== undefined && all["content-length"] === undefined) all["content-length"] = String(Buffer.byteLength(payload));
  const request = new Request(`http://127.0.0.1:3187/api/integration/${path}`, { method, headers: all, ...(payload !== undefined ? { body: payload } : {}) });
  const response = await integrationRoute(ctx, request, path.split("/").filter(Boolean));
  const text = await response.text();
  return { status: response.status, body: text ? JSON.parse(text) : null, text, headers: response.headers };
}
const audio = (size, seed = 7) => Buffer.alloc(size, seed);
const callBody = (bytes, over = {}) => ({ external_ref: "call:phone-app-0001", started_at: "2026-11-16T10:30:05+09:00", duration_sec: 95, direction: "in",
  counterpart: { name: "가상 고객", phone_e164: PHONE, crm_contact_id: "contact-1", crm_company_id: "company-1" },
  file: { name: `통화 ${PHONE} 가상 고객.m4a`, size: bytes.length, sha256: sha(bytes), mime: "audio/mp4" }, ...over });
async function upload(call, id, bytes) {
  for (let n = 0; n * CHUNK_BYTES < bytes.length; n++) {
    const part = bytes.subarray(n * CHUNK_BYTES, Math.min(bytes.length, (n + 1) * CHUNK_BYTES));
    const r = await call(`v1/calls/${id}/uploads/${n}`, { method: "PUT", body: part, headers: { "x-chunk-sha256": sha(part), "content-length": String(part.length) } });
    assert.equal(r.status, 200, r.text);
  }
  const checksums = []; for (let n = 0; n * CHUNK_BYTES < bytes.length; n++) checksums.push(sha(bytes.subarray(n * CHUNK_BYTES, Math.min(bytes.length, (n + 1) * CHUNK_BYTES))));
  return checksums;
}
const jobRow = (db, id) => db.connection.prepare("SELECT * FROM jobs WHERE id=?").get(id);
const callRow = (db, ref) => db.connection.prepare("SELECT * FROM integration_calls WHERE external_ref=?").get(ref);

test("door is off without INTEGRATION_TOKEN and refuses a short token", () => fixture(async ({ call }) => {
  const r = await call("v1/calls", { method: "POST", json: {} }); assert.equal(r.status, 404);
}, null).then(() => fixture(async ({ call }) => { assert.equal((await call("v1/calls/x")).status, 503); }, "short")));

test("missing or wrong token: 401 with the same reasonless body", () => fixture(async ({ call }) => {
  const none = await call("v1/calls", { method: "POST", json: {}, auth: null });
  const wrong = await call("v1/calls", { method: "POST", json: {}, auth: `Bearer ${TOKEN.slice(0, -2)}xx` });
  const scheme = await call("v1/calls", { method: "POST", json: {}, auth: `Basic ${TOKEN}` });
  for (const r of [none, wrong, scheme]) { assert.equal(r.status, 401); assert.equal(r.text, none.text); }
  assert.equal(await bearerMatches(TOKEN, `Bearer ${TOKEN}`), true); assert.equal(await bearerMatches(TOKEN, "Bearer "), false); assert.equal(await bearerMatches(TOKEN, null), false);
}));

test("tunnel-shaped requests are 404 even with the right token; each signal alone is enough", () => fixture(async ({ call }) => {
  const cases = [
    { host: "meetings.example.com" }, { host: "meetings-alt.example.com", "x-forwarded-host": "meetings-alt.example.com" },
    { "x-forwarded-host": "meetings.example.com" }, { "x-forwarded-for": "203.0.113.9" }, { "x-forwarded-for": "127.0.0.1, 203.0.113.9" },
    { "cf-ray": "8f00000000000000-ICN" }, { "cf-connecting-ip": "203.0.113.9" }, { "cdn-loop": "cloudflare" }, { "cf-access-jwt-assertion": "x" },
    { "cf-warp-tag-id": "x" }, { forwarded: "for=203.0.113.9" }, { "x-real-ip": "203.0.113.9" },
    { "x-forwarded-proto": "https" }, { "x-forwarded-port": "443" }, { "x-forwarded-port": "x" }, { "x-forwarded-host": "localhost:3187" },
    { "x-forwarded-server": "edge" }, { "x-forwarded-scheme": "https" }, { via: "1.1 cloudflare" }, { "true-client-ip": "203.0.113.9" },
  ];
  for (const headers of cases) { const r = await call("v1/calls", { method: "POST", json: callBody(audio(10)), headers }); assert.equal(r.status, 404, JSON.stringify(headers)); }
  assert.equal(fromLoopbackOnly(new Headers({ host: "localhost:3187" })), true);
  assert.equal(fromLoopbackOnly(new Headers({ host: "[::1]:3187", "x-forwarded-for": "::1" })), true);
  assert.equal(fromLoopbackOnly(new Headers({})), false);
  assert.equal(fromLoopbackOnly(new Headers({ ...loopback })), true, "exactly what Next fills passes");
  assert.equal(fromLoopbackOnly(new Headers({ ...loopback, "x-forwarded-proto": "https" })), false, "proto alone is enough to refuse");
}));

test("a call becomes one transcript-only job; full number only in integration_calls, never on the job", () => fixture(async ({ call, db }) => {
  const bytes = audio(1000), r = await call("v1/calls", { method: "POST", json: callBody(bytes) });
  assert.equal(r.status, 200, r.text); assert.equal(r.body.created, true); assert.equal(r.body.id, r.body.job_id); assert.deepEqual(r.body.conflicts, []);
  assert.deepEqual(r.body.upload, { chunk_size: CHUNK_BYTES, parts: 1, received: [], complete: false });
  assert.equal(r.body.job_id, await callJobId("call:phone-app-0001"));
  const row = jobRow(db, r.body.job_id), data = JSON.parse(row.data);
  assert.equal(row.owner, OWNER); assert.equal(row.state, "uploading");
  assert.equal(data.kind, "call"); assert.equal(data.output_mode, "transcript"); assert.equal(data.filename, `통화_261116_103005_${r.body.job_id.slice(0, 6)}.m4a`);
  assert.deepEqual(data.call.counterpart, { name: "가상 고객", phone_tail4: "0123", crm_contact_id: "contact-1", crm_company_id: "company-1" });
  assert.equal(data.call.direction, "in"); assert.equal(data.call.duration_sec, 95); assert.equal(data.call.external_ref, "call:phone-app-0001");
  assert.ok(!row.data.includes(PHONE.slice(1)), "full number leaked into job data");
  assert.ok(!r.text.includes(PHONE.slice(1)), "full number leaked into response");
  assert.equal(callRow(db, "call:phone-app-0001").phone_e164, PHONE);
  assert.equal(callFilename("2026-11-16T23:59:59Z", "M4A", "abcdef12-0000"), "통화_261117_085959_abcdef.m4a");
}));

test("same external_ref: no new job, differences come back as conflicts with the number masked", () => fixture(async ({ call, db }) => {
  const bytes = audio(1000), first = await call("v1/calls", { method: "POST", json: callBody(bytes) });
  const again = await call("v1/calls", { method: "POST", json: callBody(bytes) });
  assert.equal(again.status, 200); assert.equal(again.body.created, false); assert.equal(again.body.id, first.body.id); assert.deepEqual(again.body.conflicts, []);
  const changed = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "다른 이름", phone_e164: "+12025550199", crm_contact_id: "contact-2", crm_company_id: null } }) });
  assert.equal(changed.body.created, false); assert.deepEqual(changed.body.filled, []);
  assert.deepEqual(changed.body.conflicts.map(c => c.field).sort(), ["counterpart.crm_contact_id", "counterpart.name", "counterpart.phone_e164"], "a left-out field is no opinion");
  assert.deepEqual(changed.body.conflicts.find(c => c.field === "counterpart.phone_e164"), { field: "counterpart.phone_e164", existing: "끝 0123", proposed: "끝 0199" });
  assert.ok(!changed.text.includes("2025550199") && !changed.text.includes(PHONE.slice(1)));
  assert.equal(JSON.parse(jobRow(db, first.body.id).data).call.counterpart.name, "가상 고객", "conflicts are never written");
  const otherFile = await call("v1/calls", { method: "POST", json: callBody(audio(1000, 9)) });
  assert.equal(otherFile.status, 422); assert.equal(otherFile.body.field, "file.sha256");
  assert.equal(db.connection.prepare("SELECT COUNT(*) AS n FROM jobs").get().n, 1);
}));

test("a burst of the same ref and a crash between the two inserts still end with one job", () => fixture(async ({ call, db }) => {
  const bytes = audio(500), results = await Promise.all(Array.from({ length: 12 }, () => call("v1/calls", { method: "POST", json: callBody(bytes) })));
  assert.equal(results.filter(r => r.status === 200).length, 12); assert.equal(results.filter(r => r.body.created).length, 1);
  assert.equal(new Set(results.map(r => r.body.id)).size, 1);
  assert.equal(db.connection.prepare("SELECT COUNT(*) AS n FROM jobs").get().n, 1); assert.equal(db.connection.prepare("SELECT COUNT(*) AS n FROM integration_calls").get().n, 1);
  // Crash after the ref row, before the job row: the next request repairs it.
  const ref = "call:crash-2", id = await callJobId(ref), crashBytes = audio(500, 11);
  db.connection.prepare("INSERT INTO integration_calls(external_ref,job_id,created_at,direction,started_at,duration_sec,counterpart_name,phone_e164,crm_contact_id,crm_company_id,file_size,file_sha256,file_mime,file_ext) VALUES(?,?,1,'out','2026-11-16T10:00:00+09:00',5,'가상',?,NULL,NULL,?,?,'audio/mp4','m4a')").run(ref, id, PHONE, crashBytes.length, sha(crashBytes));
  const repaired = await call("v1/calls", { method: "POST", json: callBody(crashBytes, { external_ref: ref, direction: "out", started_at: "2026-11-16T10:00:00+09:00", duration_sec: 5, counterpart: { name: "가상", phone_e164: PHONE } }) });
  assert.equal(repaired.status, 200, repaired.text); assert.equal(repaired.body.created, false); assert.equal(repaired.body.id, id); assert.deepEqual(repaired.body.conflicts, []);
  assert.equal(jobRow(db, id).state, "uploading");
}));

test("input errors are 422 with the field; the number is not echoed", () => fixture(async ({ call }) => {
  const bytes = audio(10);
  const cases = [
    [callBody(bytes, { external_ref: "mail:x" }), "external_ref"], [callBody(bytes, { direction: "both" }), "direction"],
    [callBody(bytes, { started_at: "2026-11-16 10:00" }), "started_at"], [callBody(bytes, { duration_sec: -1 }), "duration_sec"],
    [callBody(bytes, { counterpart: { name: "가상", phone_e164: "202-555-0123" } }), "counterpart.phone_e164"],
    [callBody(bytes, { file: { name: "통화.exe", size: 10, sha256: sha(bytes), mime: "audio/mp4" } }), "file.name"],
    [{ ...callBody(bytes), extra: true }, ""],
  ];
  for (const [json, field] of cases) { const r = await call("v1/calls", { method: "POST", json }); assert.equal(r.status, 422, r.text); if (field) assert.equal(r.body.field, field); assert.ok(!r.text.includes("2025550")); }
  const broken = await call("v1/calls", { method: "POST", body: "{", headers: { "content-type": "application/json" } }); assert.equal(broken.status, 422); assert.equal(broken.body.field, "body");
}));

test("upload: part checksum, declared sha256 and whole-file sha256 are all enforced; then the job waits for the worker", () => fixture(async ({ call, db }) => {
  const bytes = Buffer.concat([audio(CHUNK_BYTES, 3), audio(17, 4)]), created = await call("v1/calls", { method: "POST", json: callBody(bytes) }), id = created.body.id;
  assert.equal(created.body.upload.parts, 2);
  const badPart = await call(`v1/calls/${id}/uploads/1`, { method: "PUT", body: bytes.subarray(CHUNK_BYTES), headers: { "x-chunk-sha256": sha(Buffer.from("x")), "content-length": "17" } });
  assert.equal(badPart.status, 422); assert.equal(badPart.body.field, "x-chunk-sha256");
  const badIndex = await call(`v1/calls/${id}/uploads/2`, { method: "PUT", body: "x", headers: { "x-chunk-sha256": sha(Buffer.from("x")) } });
  assert.equal(badIndex.status, 422); assert.equal(badIndex.body.field, "part");
  const checksums = await upload(call, id, bytes);
  assert.deepEqual((await call(`v1/calls/${id}/uploads`)).body.received, [0, 1]);
  const resend = await call("v1/calls", { method: "POST", json: callBody(bytes) }); assert.deepEqual(resend.body.upload.received, [0, 1], "resume sees received parts");
  const otherSha = await call(`v1/calls/${id}/uploads/complete`, { method: "POST", json: { sha256: sha(Buffer.from("other")), part_checksums: checksums } });
  assert.equal(otherSha.status, 422); assert.equal(otherSha.body.field, "sha256");
  const wrongParts = await call(`v1/calls/${id}/uploads/complete`, { method: "POST", json: { sha256: sha(bytes), part_checksums: [checksums[1], checksums[0]] } });
  assert.equal(wrongParts.status, 422); assert.equal(wrongParts.body.field, "part_checksums");
  const done = await call(`v1/calls/${id}/uploads/complete`, { method: "POST", json: { sha256: sha(bytes), part_checksums: checksums } });
  assert.equal(done.status, 200, done.text); assert.equal(done.body.state, "waiting_worker"); assert.equal(done.body.upload_complete, true);
  const data = JSON.parse(jobRow(db, id).data); assert.equal(data.pending, "transcribe"); assert.equal(data.sha256, sha(bytes));
  const status = await call(`v1/calls/${id}`); assert.equal(status.body.state, "waiting_worker"); assert.equal(status.body.external_ref, "call:phone-app-0001");
  const late = await call(`v1/calls/${id}/uploads/0`, { method: "PUT", body: bytes.subarray(0, 1), headers: { "x-chunk-sha256": sha(bytes.subarray(0, 1)) } }); assert.equal(late.status, 409);
}));

test("the door reaches call jobs only", () => fixture(async ({ call, db }) => {
  const owners = "11111111-1111-4111-8111-111111111111";
  db.connection.prepare("INSERT INTO jobs(id,owner,state,data,created_at,updated_at) VALUES(?,?,'uploading',?,0,0)").run(owners, OWNER, JSON.stringify({ output_mode: "transcript", filename: "회의.wav", byte_size: 10, segments: [], transcript_revision: 0 }));
  for (const path of [`v1/calls/${owners}`, `v1/calls/${owners}/uploads`, "v1/calls/not-a-uuid", "v1/other", "v2/calls"]) assert.equal((await call(path)).status, 404, path);
}));

test("revert deletes by the app's rule; repeats, owner edits and owner deletes are not reverted", () => fixture(async ({ call, ctx, db }) => {
  const bytes = audio(2000), created = await call("v1/calls", { method: "POST", json: callBody(bytes) }), id = created.body.id;
  const checksums = await upload(call, id, bytes); await call(`v1/calls/${id}/uploads/complete`, { method: "POST", json: { sha256: sha(bytes), part_checksums: checksums } });
  assert.ok((await ctx.bucket.list({ prefix: `audio/${id}/` })).objects.length);
  const r = await call("v1/revert", { method: "POST", json: { external_ref: "call:phone-app-0001" } });
  assert.deepEqual(r.body, { reverted: true, reason: "deleted" });
  const row = jobRow(db, id); assert.ok(row.deleted_at); assert.equal(row.data, "{}"); assert.equal(row.state, "cancelled");
  assert.equal((await ctx.bucket.list({ prefix: `audio/${id}/` })).objects.length, 0);
  assert.ok(db.connection.prepare("SELECT value FROM settings WHERE key=?").get(`delete:${id}`), "worker copy deletion is scheduled");
  const ref = callRow(db, "call:phone-app-0001"); assert.equal(ref.phone_e164, null); assert.equal(ref.counterpart_name, null); assert.ok(ref.reverted_at);
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:phone-app-0001" } })).body, { reverted: false, reason: "already_reverted" });
  const again = await call("v1/calls", { method: "POST", json: callBody(bytes) }); assert.equal(again.status, 409); assert.equal(again.body.field, "external_ref");
  assert.equal((await call(`v1/calls/${id}`)).status, 404);
  assert.equal((await call("v1/revert", { method: "POST", json: { external_ref: "call:unknown" } })).status, 404);

  const editedBytes = audio(2000, 21), edited = await call("v1/calls", { method: "POST", json: callBody(editedBytes, { external_ref: "call:edited" }) });
  db.connection.prepare("UPDATE jobs SET data=json_set(data,'$.owner_edited_at',123) WHERE id=?").run(edited.body.id);
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:edited" } })).body, { reverted: false, reason: "changed_by_person" });
  assert.equal(jobRow(db, edited.body.id).deleted_at, null);

  const deletedBytes = audio(2000, 22), deleted = await call("v1/calls", { method: "POST", json: callBody(deletedBytes, { external_ref: "call:owner-deleted" }) });
  await removeJob(db, ctx.bucket, await loadJob(db, deleted.body.id, OWNER), OWNER);
  assert.equal(callRow(db, "call:owner-deleted").phone_e164, null, "owner deletion also clears the number");
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:owner-deleted" } })).body, { reverted: false, reason: "already_deleted" });
  assert.equal((await call("v1/calls", { method: "POST", json: callBody(deletedBytes, { external_ref: "call:owner-deleted" }) })).status, 409);
}));

test("re-send fills blanks (CRM ids, empty name) on the job and the table; worker and owner updates keep them", () => fixture(async ({ call, db }) => {
  const bytes = audio(800), first = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "", phone_e164: PHONE } }) }), id = first.body.id;
  const fill = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "가상 고객", phone_e164: PHONE, crm_contact_id: "contact-9", crm_company_id: "company-9" } }) });
  assert.equal(fill.status, 200); assert.equal(fill.body.created, false); assert.deepEqual(fill.body.conflicts, []);
  assert.deepEqual(fill.body.filled.sort(), ["counterpart.crm_company_id", "counterpart.crm_contact_id", "counterpart.name"]);
  const stored = callRow(db, "call:phone-app-0001"); assert.equal(stored.crm_contact_id, "contact-9"); assert.equal(stored.counterpart_name, "가상 고객");
  // A writer that read the job before the fill (stale copy) must not wipe it.
  const stale = { ...(await loadJob(db, id, OWNER)) }; stale.call = { ...stale.call, counterpart: { name: "", phone_tail4: "0123", crm_contact_id: null, crm_company_id: null } };
  await updateJob(db, stale, "uploading", { stage: "uploading" });
  const after = JSON.parse(jobRow(db, id).data);
  assert.deepEqual(after.call.counterpart, { name: "가상 고객", phone_tail4: "0123", crm_contact_id: "contact-9", crm_company_id: "company-9" });
  assert.equal(after.stage, "uploading");
  const again = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "다른", phone_e164: PHONE, crm_contact_id: "contact-1" } }) });
  assert.deepEqual(again.body.filled, []); assert.deepEqual(again.body.conflicts.map(c => c.field).sort(), ["counterpart.crm_contact_id", "counterpart.name"]);
  assert.equal(JSON.parse(jobRow(db, id).data).call.counterpart.crm_contact_id, "contact-9", "filled values are never overwritten");
}));

test("file.mime must be an audio type; parameters are dropped; amr and 3ga recordings are accepted", () => fixture(async ({ call, db }) => {
  const bytes = audio(10);
  for (const mime of ["text/html; charset=<script>", "application/octet-stream", "image/png", ""]) {
    const r = await call("v1/calls", { method: "POST", json: callBody(bytes, { file: { name: "a.m4a", size: 10, sha256: sha(bytes), mime } }) });
    assert.equal(r.status, 422, mime); assert.equal(r.body.field, "file.mime");
  }
  const amr = audio(10, 31), ok = await call("v1/calls", { method: "POST", json: callBody(amr, { external_ref: "call:amr", file: { name: "녹음.amr", size: 10, sha256: sha(amr), mime: "Audio/AMR; rate=8000" } }) });
  assert.equal(ok.status, 200, ok.text); assert.equal(callRow(db, "call:amr").file_mime, "audio/amr"); assert.match(JSON.parse(jobRow(db, ok.body.id).data).filename, /\.amr$/);
  const g3 = audio(10, 32), ga = await call("v1/calls", { method: "POST", json: callBody(g3, { external_ref: "call:3ga", file: { name: "녹음.3ga", size: 10, sha256: sha(g3), mime: "audio/3gpp" } }) });
  assert.equal(ga.status, 200, ga.text);
}));

test("owner deletion still works where the integration table does not exist (Sites recovery DB)", async () => {
  const dir = await mkdtemp(join(tmpdir(), "meeting-no-table-")), db = new SQLiteDatabase(join(dir, "m.sqlite"));
  try {
    db.connection.exec(await readFile(join(drizzle, "0000_sturdy_storm.sql"), "utf8"));
    db.connection.prepare("INSERT INTO jobs(id,owner,state,data,created_at,updated_at) VALUES('j',?,'transcript_ready','{}',0,0)").run(OWNER);
    const result = await removeJob(db, new FileBucket(join(dir, "objects")), await loadJob(db, "j", OWNER), OWNER);
    assert.equal(result.deleted, true); assert.ok(jobRow(db, "j").deleted_at);
  } finally { db.close(); await rm(dir, { recursive: true, force: true }); }
});

test("deleting a job by any code clears the number in the same statement; the sweep covers rows without the trigger", () => fixture(async ({ call, db }) => {
  const made = await call("v1/calls", { method: "POST", json: callBody(audio(10, 41), { external_ref: "call:old-delete" }) });
  db.connection.prepare("UPDATE jobs SET data='{}',deleted_at=1 WHERE id=?").run(made.body.id); // exactly what the old release's delete runs
  assert.equal(callRow(db, "call:old-delete").phone_e164, null, "trigger"); assert.equal(callRow(db, "call:old-delete").counterpart_name, null);
  // A DB where the trigger is missing (or rows from before it): the heartbeat / request sweep clears them.
  db.connection.exec("DROP TRIGGER integration_calls_clear_on_job_delete");
  const other = await call("v1/calls", { method: "POST", json: callBody(audio(10, 42), { external_ref: "call:old-delete-2" }) });
  db.connection.prepare("UPDATE jobs SET data='{}',deleted_at=1 WHERE id=?").run(other.body.id);
  assert.equal(callRow(db, "call:old-delete-2").phone_e164, PHONE);
  await clearDeletedCallContacts(db);
  assert.equal(callRow(db, "call:old-delete-2").phone_e164, null); assert.equal(callRow(db, "call:old-delete-2").counterpart_name, null);
}));

test("an unknown failure is a 500 that logs where and what kind, without request content", () => fixture(async ({ ctx }) => {
  const logged = [], original = console.error; console.error = (...args) => logged.push(args.join(" "));
  try {
    const broken = { ...ctx, db: { prepare() { throw new TypeError("boom " + PHONE); } } };
    const r = await send(broken, "v1/calls", { method: "POST", json: callBody(audio(10)) });
    assert.equal(r.status, 500); assert.equal(r.body.code, "internal_error");
  } finally { console.error = original; }
  assert.equal(logged.length, 1); const entry = JSON.parse(logged[0]);
  assert.deepEqual(entry, { event: "integration_internal_error", where: "POST /api/integration/v1/calls", kind: "TypeError" });
  assert.ok(!logged[0].includes("2025550"));
}));

test("review: an opened call is not reverted (viewed_by_owner); an edited one stays changed_by_person", () => fixture(async ({ call, db }) => {
  const a = await call("v1/calls", { method: "POST", json: callBody(audio(30, 51), { external_ref: "call:viewed" }) });
  await markCallViewed(db, a.body.id, 111); await markCallViewed(db, a.body.id, 222);
  assert.equal(JSON.parse(jobRow(db, a.body.id).data).call.owner_viewed_at, 111, "first view only");
  // A stale writer does not erase the mark (it lives inside `call`).
  const stale = await loadJob(db, a.body.id, OWNER); delete stale.call.owner_viewed_at; await updateJob(db, stale, "uploading", {});
  assert.equal(JSON.parse(jobRow(db, a.body.id).data).call.owner_viewed_at, 111);
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:viewed" } })).body, { reverted: false, reason: "viewed_by_owner" });
  assert.equal(jobRow(db, a.body.id).deleted_at, null);
  const b = await call("v1/calls", { method: "POST", json: callBody(audio(30, 52), { external_ref: "call:both" }) });
  await markCallViewed(db, b.body.id); db.connection.prepare("UPDATE jobs SET data=json_set(data,'$.owner_edited_at',5) WHERE id=?").run(b.body.id);
  assert.equal((await call("v1/revert", { method: "POST", json: { external_ref: "call:both" } })).body.reason, "changed_by_person");
}));

test("review: the same recording under another ref is 422 duplicate_file with the live job id; after revert it may come again", () => fixture(async ({ call, db }) => {
  const bytes = audio(40, 61), first = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:one" }) });
  const dup = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:two" }) });
  assert.equal(dup.status, 422); assert.equal(dup.body.code, "duplicate_file"); assert.equal(dup.body.reason, "duplicate_file"); assert.equal(dup.body.field, "file.sha256"); assert.equal(dup.body.job_id, first.body.id);
  assert.equal(callRow(db, "call:two"), undefined);
  // Race: two new refs with the same file at once -> one 200, the other 422 (partial unique index).
  const raced = await Promise.all(["call:r1", "call:r2"].map(ref => call("v1/calls", { method: "POST", json: callBody(audio(40, 62), { external_ref: ref }) })));
  assert.deepEqual(raced.map(r => r.status).sort(), [200, 422]); assert.equal(raced.find(r => r.status === 422).body.code, "duplicate_file");
  await call("v1/revert", { method: "POST", json: { external_ref: "call:one" } });
  assert.equal((await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:three" }) })).status, 200);
}));

test("review: daily intake limit gives 429; a re-sent ref is not counted as new", () => fixture(async ({ ctx }) => {
  const limited = { ...ctx, dailyLimit: 2 }, send2 = (path, o) => send(limited, path, o);
  for (const n of [1, 2]) assert.equal((await send2("v1/calls", { method: "POST", json: callBody(audio(20, 70 + n), { external_ref: `call:day-${n}` }) })).status, 200);
  const third = await send2("v1/calls", { method: "POST", json: callBody(audio(20, 79), { external_ref: "call:day-3" }) });
  assert.equal(third.status, 429); assert.equal(third.body.code, "daily_limit");
  assert.equal((await send2("v1/calls", { method: "POST", json: callBody(audio(20, 71), { external_ref: "call:day-1" }) })).status, 200, "re-send still answers");
  // Yesterday's rows do not count.
  ctx.db.connection.prepare("UPDATE integration_calls SET created_at=created_at-86400000").run();
  assert.equal((await send2("v1/calls", { method: "POST", json: callBody(audio(20, 79), { external_ref: "call:day-3" }) })).status, 200);
}));

test("review: a fill never writes a name back onto a deleted job's row", () => fixture(async ({ call, db }) => {
  const bytes = audio(25, 81), made = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "", phone_e164: PHONE } }) });
  // Owner deletes between the request's checks and its fill: simulate by deleting, then calling the fill path directly through a re-send.
  db.connection.prepare("UPDATE jobs SET data='{}',deleted_at=1 WHERE id=?").run(made.body.id);
  const r = await call("v1/calls", { method: "POST", json: callBody(bytes, { counterpart: { name: "가상 고객", phone_e164: PHONE, crm_contact_id: "c" } }) });
  assert.equal(r.status, 409); const row = callRow(db, "call:phone-app-0001");
  assert.equal(row.counterpart_name, null); assert.equal(row.phone_e164, null); assert.equal(row.crm_contact_id, null);
  // The race itself: the fill step runs after the owner's delete landed.
  const input = { ...callBody(bytes), counterpart: { name: "가상 고객", phone_e164: PHONE, crm_contact_id: "c", crm_company_id: "d" } };
  assert.equal(blankFills(row, input).length, 3);
  assert.deepEqual(await applyFills(db, row, blankFills(row, input)), []);
  const after = callRow(db, "call:phone-app-0001"); assert.equal(after.counterpart_name, null); assert.equal(after.crm_contact_id, null);
}));

test("review: an interrupted revert (marked reverting) finishes on retry with reverted:true", () => fixture(async ({ call, ctx, db }) => {
  const bytes = audio(60, 91), made = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:half" }) }), id = made.body.id;
  const checksums = await upload(call, id, bytes); await call(`v1/calls/${id}/uploads/complete`, { method: "POST", json: { sha256: sha(bytes), part_checksums: checksums } });
  // Crash right after the job row was wiped: marker set, revisions/audio/worker marker not done yet.
  db.connection.prepare("UPDATE integration_calls SET reverting_at=1 WHERE external_ref='call:half'").run();
  db.connection.prepare("UPDATE jobs SET data='{}',deleted_at=2,state='cancelled',generation=generation+1 WHERE id=?").run(id);
  assert.ok((await ctx.bucket.list({ prefix: `audio/${id}/` })).objects.length, "audio still there");
  assert.equal((await call(`v1/calls/${id}`)).status, 404, "a reverting call is no longer reachable");
  assert.equal((await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:half" }) })).status, 409);
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:half" } })).body, { reverted: true, reason: "deleted" });
  assert.equal((await ctx.bucket.list({ prefix: `audio/${id}/` })).objects.length, 0);
  assert.ok(db.connection.prepare("SELECT value FROM settings WHERE key=?").get(`delete:${id}`));
  assert.ok(callRow(db, "call:half").reverted_at);
  // Marked but the job row not yet wiped: the retry deletes it fully.
  const other = await call("v1/calls", { method: "POST", json: callBody(audio(60, 92), { external_ref: "call:half-2" }) });
  db.connection.prepare("UPDATE integration_calls SET reverting_at=1 WHERE external_ref='call:half-2'").run();
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: "call:half-2" } })).body, { reverted: true, reason: "deleted" });
  assert.ok(jobRow(db, other.body.id).deleted_at);
}));

test("review: revert of a ref whose job row was never written (stub) is reverted:true", () => fixture(async ({ call, db }) => {
  const ref = "call:stub-only", id = await callJobId(ref);
  db.connection.prepare("INSERT INTO integration_calls(external_ref,job_id,created_at,direction,started_at,duration_sec,counterpart_name,phone_e164,file_size,file_sha256,file_mime,file_ext) VALUES(?,?,1,'in','2026-11-16T10:00:00+09:00',1,'가상',?,1,?,'audio/mp4','m4a')").run(ref, id, PHONE, sha(Buffer.from("stub")));
  assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: ref } })).body, { reverted: true, reason: "deleted" });
  const row = callRow(db, ref); assert.ok(row.reverted_at); assert.equal(row.phone_e164, null); assert.equal(row.counterpart_name, null);
  assert.equal(jobRow(db, id), undefined);
}));

test("review: started_at is compared as a moment, and two calls in the same second get different file names", () => fixture(async ({ call, db }) => {
  const bytes = audio(15, 95), a = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:t1" }) });
  const same = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:t1", started_at: "2026-11-16T01:30:05Z" }) });
  assert.deepEqual(same.body.conflicts, []);
  const later = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:t1", started_at: "2026-11-16T01:30:06Z" }) });
  assert.deepEqual(later.body.conflicts.map(c => c.field), ["started_at"]);
  const b = await call("v1/calls", { method: "POST", json: callBody(audio(15, 96), { external_ref: "call:t2" }) });
  const names = [a.body.id, b.body.id].map(id => JSON.parse(jobRow(db, id).data).filename);
  assert.notEqual(names[0], names[1]); for (const n of names) assert.match(n, /^통화_261116_103005_[0-9a-f]{6}\.m4a$/);
}));

test("review: token_host.py create twice, rotate, check, and a failed second swap leave both files unchanged", { skip: publicSnapshot && "public snapshot: ops/ is not published" }, async () => {
  const dir = await mkdtemp(join(tmpdir(), "meeting-token-")), script = tokenHostScript;
  const app = join(dir, "app.env"), orch = join(dir, "orch", "integrations.env");
  const { mkdir, writeFile } = await import("node:fs/promises"); await mkdir(join(dir, "orch")); await writeFile(app, 'MEETING_DATA_DIR="/x"\nMEETING_OWNER_ID="o"\n');
  const env = { ...process.env, APP_ENV: app, ORCH_ENV: orch, FILE_OWNER: "-" };
  const run = (action, pre = "") => spawnSync("python", ["-I", "-c", `${pre}\nimport runpy,sys\np=sys.argv[1]\nsys.argv=['x',sys.argv[2]]\nrunpy.run_path(p,run_name='__main__')`, script, action], { env, encoding: "utf8" });
  const value = (file, key) => (readFileSync(file, "utf8").split(/\r?\n/).find(l => l.startsWith(key + "=")) ?? "").split("=").slice(1).join("=").replaceAll('"', "");
  const { readFileSync } = await import("node:fs");
  try {
    assert.equal(run("create").status, 0);
    const t1 = value(app, "INTEGRATION_TOKEN"); assert.equal(t1.length, 43); assert.equal(value(orch, "MEETING_INTEGRATION_TOKEN"), t1);
    const again = run("create"); assert.notEqual(again.status, 0); assert.match(again.stderr + again.stdout, /already present/); assert.equal(value(app, "INTEGRATION_TOKEN"), t1);
    const checked = run("check"); assert.equal(checked.status, 0); assert.match(checked.stdout, /'same_token': True/); assert.ok(!checked.stdout.includes(t1), "check never prints the token");
    assert.equal(run("rotate").status, 0); const t2 = value(app, "INTEGRATION_TOKEN");
    const { readdirSync } = await import("node:fs"), backups = () => readdirSync(dir).filter(n => n.startsWith("app.env.before-integration-"));
    assert.equal(backups().length, 2, "create and rotate each left a backup");
    assert.ok(backups().every(n => /^app\.env\.before-integration-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}$/.test(n)), backups().join(",")); assert.notEqual(t2, t1); assert.equal(value(orch, "MEETING_INTEGRATION_TOKEN"), t2);
    // Same-second runs (frozen clock): backup names still differ, nothing is overwritten.
    const frozen = "import datetime as _d\nclass _F(_d.datetime):\n    @classmethod\n    def now(cls, tz=None): return cls(2026,11,16,12,0,0,123456,tzinfo=tz)\n_d.datetime=_F";
    assert.equal(run("rotate", frozen).status, 0); assert.equal(run("rotate", frozen).status, 0);
    assert.equal(backups().filter(n => n.includes("20261116T120000.123456Z")).length, 2, "two backups in the same microsecond are both kept");
    const t3 = value(app, "INTEGRATION_TOKEN"); assert.equal(value(orch, "MEETING_INTEGRATION_TOKEN"), t3);
    const before = [readFileSync(app, "utf8"), readFileSync(orch, "utf8")];
    const failed = run("rotate", "import os\n_r=os.replace\n_n=[0]\ndef _f(a,b):\n    _n[0]+=1\n    if _n[0]==2: raise OSError('simulated')\n    return _r(a,b)\nos.replace=_f");
    assert.notEqual(failed.status, 0); assert.deepEqual([readFileSync(app, "utf8"), readFileSync(orch, "utf8")], before, "both files unchanged");
    assert.equal((await import("node:fs")).readdirSync(join(dir, "orch")).filter(n => n.startsWith(".integrations")).length, 0, "no temp files left");
    for (const r of [run("check"), failed]) assert.ok(!(r.stdout + r.stderr).includes(t2) && !(r.stdout + r.stderr).includes(t3));
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("re-review: a reverting retry re-checks owner view and edit, and then leaves the call live", () => fixture(async ({ call, db }) => {
  for (const [ref, seed, mark, reason] of [["call:rv-viewed", 101, "view", "viewed_by_owner"], ["call:rv-edited", 102, "edit", "changed_by_person"]]) {
    const made = await call("v1/calls", { method: "POST", json: callBody(audio(30, seed), { external_ref: ref }) }), id = made.body.id;
    db.connection.prepare("UPDATE integration_calls SET reverting_at=1 WHERE external_ref=?").run(ref); // interrupted before the job was wiped
    if (mark === "view") await markCallViewed(db, id); else db.connection.prepare("UPDATE jobs SET data=json_set(data,'$.owner_edited_at',9) WHERE id=?").run(id);
    assert.deepEqual((await call("v1/revert", { method: "POST", json: { external_ref: ref } })).body, { reverted: false, reason });
    assert.equal(jobRow(db, id).deleted_at, null, "kept");
    const row = callRow(db, ref); assert.equal(row.reverting_at, null, "marker dropped"); assert.equal(row.reverted_at, null);
    assert.equal((await call(`v1/calls/${id}`)).status, 200, "reachable again");
  }
}));

test("re-review: a file whose job the owner deleted is not a duplicate, and its job id is not told", () => fixture(async ({ call, ctx, db }) => {
  const bytes = audio(35, 111), first = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:gone" }) });
  await removeJob(db, ctx.bucket, await loadJob(db, first.body.id, OWNER), OWNER);
  assert.ok(callRow(db, "call:gone").job_deleted_at, "trigger marks the row");
  const again = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:gone-again" }) });
  assert.equal(again.status, 200, again.text); assert.equal(again.body.created, true); assert.notEqual(again.body.id, first.body.id);
  assert.ok(!again.text.includes(first.body.id), "the deleted job id is not in the answer");
  // While that new one is live, the same file under a third ref is a duplicate of the live job only.
  const third = await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:gone-third" }) });
  assert.equal(third.status, 422); assert.equal(third.body.job_id, again.body.id); assert.ok(!third.text.includes(first.body.id));
  // Rows deleted before the trigger existed: the sweep marks them too, so they stop holding the file.
  db.connection.exec("DROP TRIGGER integration_calls_clear_on_job_delete");
  db.connection.prepare("UPDATE jobs SET data='{}',deleted_at=5 WHERE id=?").run(again.body.id);
  await clearDeletedCallContacts(db);
  assert.equal(callRow(db, "call:gone-again").job_deleted_at, 5);
  assert.equal((await call("v1/calls", { method: "POST", json: callBody(bytes, { external_ref: "call:gone-fourth" }) })).status, 200);
}));

test("re-review: dropping integration_calls needs the trigger dropped first (RUNBOOK)", () => fixture(async ({ db }) => {
  db.connection.prepare("INSERT INTO jobs(id,owner,state,data,created_at,updated_at) VALUES('x',?,'transcript_ready','{}',0,0)").run(OWNER);
  db.connection.exec("DROP TABLE integration_calls");
  assert.throws(() => db.connection.prepare("UPDATE jobs SET deleted_at=1 WHERE id='x'").run(), /no such table/);
  db.connection.exec("DROP TRIGGER integration_calls_clear_on_job_delete");
  db.connection.prepare("UPDATE jobs SET deleted_at=1 WHERE id='x'").run();
  assert.ok(jobRow(db, "x").deleted_at);
}));

test("OCI migration applies 0001 on a fresh and on an existing 0000 database, and is idempotent", async () => {
  const dir = await mkdtemp(join(tmpdir(), "meeting-migrate-")), script = fileURLToPath(new URL("../scripts/migrate-oci.mjs", import.meta.url));
  const run = data => spawnSync(process.execPath, ["--experimental-strip-types", "--no-warnings", script], { env: { ...process.env, MEETING_DATA_DIR: data, MEETING_OWNER_ID: OWNER }, encoding: "utf8" });
  try {
    const fresh = join(dir, "fresh"); for (let i = 0; i < 2; i++) { const r = run(fresh); assert.equal(r.status, 0, r.stderr); }
    const existing = join(dir, "existing"), { mkdir } = await import("node:fs/promises"); await mkdir(existing);
    const { DatabaseSync } = await import("node:sqlite"); let db = new DatabaseSync(join(existing, "meeting.sqlite"));
    db.exec(await readFile(join(drizzle, "0000_sturdy_storm.sql"), "utf8")); db.exec("CREATE TABLE runtime_migrations(name TEXT PRIMARY KEY NOT NULL)");
    db.prepare("INSERT INTO runtime_migrations VALUES('0000_sturdy_storm.sql')").run(); db.prepare("INSERT INTO settings VALUES('owner',?)").run(OWNER);
    db.prepare("INSERT INTO jobs(id,owner,state,data,created_at,updated_at) VALUES('old',?,'transcript_ready','{}',1,1)").run(OWNER); db.close();
    const r = run(existing); assert.equal(r.status, 0, r.stderr);
    db = new DatabaseSync(join(existing, "meeting.sqlite"));
    try {
      assert.deepEqual(db.prepare("SELECT name FROM runtime_migrations ORDER BY name").all().map(x => x.name), ["0000_sturdy_storm.sql", "0001_integration_calls.sql"]);
      assert.equal(db.prepare("SELECT COUNT(*) AS n FROM jobs").get().n, 1);
      const columns = db.prepare("PRAGMA table_info(integration_calls)").all().map(c => `${c.name}:${c.type}:${c.notnull}:${c.pk}`);
      assert.deepEqual(columns, ["external_ref:TEXT:1:1", "job_id:TEXT:1:0", "created_at:INTEGER:1:0", "direction:TEXT:1:0", "started_at:TEXT:1:0", "duration_sec:INTEGER:1:0", "counterpart_name:TEXT:0:0", "phone_e164:TEXT:0:0", "crm_contact_id:TEXT:0:0", "crm_company_id:TEXT:0:0", "file_size:INTEGER:1:0", "file_sha256:TEXT:1:0", "file_mime:TEXT:1:0", "file_ext:TEXT:1:0", "reverting_at:INTEGER:0:0", "reverted_at:INTEGER:0:0", "job_deleted_at:INTEGER:0:0"]);
      assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='trigger' AND name='integration_calls_clear_on_job_delete'").get().n, 1);
      assert.deepEqual(db.prepare("PRAGMA index_list(integration_calls)").all().filter(i => i.unique).map(i => i.name).sort(), ["integration_calls_job_id_unique", "integration_calls_live_file_unique", "sqlite_autoindex_integration_calls_1"]);
    } finally { db.close(); }
  } finally { await rm(dir, { recursive: true, force: true }); }
});
