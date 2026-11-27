import test from "node:test";
import assert from "node:assert/strict";
import { createDecipheriv, createECDH, createPublicKey, hkdfSync, verify } from "node:crypto";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { SQLiteDatabase, FileBucket } from "../lib/runtime-storage.ts";
import { finishJobRemoval, loadJob, readSetting, updateJob } from "../lib/job-store.ts";
import { claimNotice, readSubscriptions, removeSubscription, saveSubscription } from "../lib/push-store.ts";
import { noticePayload, NOTICE_STATES } from "../lib/notice.ts";
import { allowedEndpoint, b64uDecode, b64uEncode, encryptPayload, importEcdhPair, sendPush, vapidAuthorization } from "../lib/web-push.ts";

const T0 = Date.UTC(2026, 10, 25, 9, 0, 0), LEASE = "lease-a";
const drizzle = fileURLToPath(new URL("../drizzle/", import.meta.url));
const ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-1", KEY = "server-key-1";

// RFC 8291 Appendix A worked example.
const RFC = {
  plaintext: "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24",
  asPublic: "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
  asPrivate: "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
  uaPublic: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
  salt: "DGv6ra1nlYgDCS1FRnbzlw",
  auth: "BTBZMqHH6r4Tts7J_aSIgg",
  header: "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
  ciphertext: "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ",
};

test("aes128gcm encryption matches the RFC 8291 worked example byte for byte", async () => {
  const body = await encryptPayload(b64uDecode(RFC.plaintext), { p256dh: RFC.uaPublic, auth: RFC.auth }, { salt: b64uDecode(RFC.salt), serverKeys: await importEcdhPair(RFC.asPublic, RFC.asPrivate) });
  assert.equal(body.length, 144);
  assert.equal(b64uEncode(body.slice(0, 86)), RFC.header);
  assert.equal(b64uEncode(body.slice(86)), RFC.ciphertext);
});

// The receiving browser's side, written separately with node:crypto.
function decrypt(body, ua, auth) {
  const salt = body.subarray(0, 16), rs = body.readUInt32BE(16), idlen = body[20], asPublic = body.subarray(21, 21 + idlen), sealed = body.subarray(21 + idlen);
  assert.equal(rs, 4096); assert.equal(idlen, 65);
  const secret = ua.computeSecret(asPublic);
  const ikm = Buffer.from(hkdfSync("sha256", secret, auth, Buffer.concat([Buffer.from("WebPush: info\0"), ua.getPublicKey(), asPublic]), 32));
  const cek = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  const d = createDecipheriv("aes-128-gcm", cek, nonce); d.setAuthTag(sealed.subarray(-16));
  const plain = Buffer.concat([d.update(sealed.subarray(0, -16)), d.final()]);
  assert.equal(plain.at(-1), 2, "last-record delimiter");
  return plain.subarray(0, -1).toString("utf8");
}
function browser() { const ua = createECDH("prime256v1"); ua.generateKeys(); const auth = Buffer.alloc(16, 7); return { ua, auth, target: { endpoint: ENDPOINT, p256dh: b64uEncode(ua.getPublicKey()), auth: b64uEncode(auth) } }; }
function vapidKeys() { const k = createECDH("prime256v1"); k.generateKeys(); return { publicKey: b64uEncode(k.getPublicKey()), privateKey: b64uEncode(k.getPrivateKey()), subject: "https://meetings.example.test" }; }

test("a sent push decrypts on the browser side and carries a valid VAPID token for the push service origin", async () => {
  const { ua, auth, target } = browser(), keys = vapidKeys();
  const seen = [];
  const status = await sendPush(target, JSON.stringify({ body: "안녕" }), keys, async (url, init) => { seen.push({ url, init }); return new Response(null, { status: 201 }); }, T0);
  assert.equal(status, 201);
  const { url, init } = seen[0];
  assert.equal(url, ENDPOINT);
  assert.equal(init.redirect, "error");
  assert.equal(init.headers["Content-Encoding"], "aes128gcm");
  assert.equal(JSON.parse(decrypt(Buffer.from(init.body), ua, auth)).body, "안녕");
  const [, token, k] = init.headers.Authorization.match(/^vapid t=([^,]+), k=(.+)$/);
  assert.equal(k, keys.publicKey);
  const [h, c, s] = token.split(".");
  const claims = JSON.parse(Buffer.from(b64uDecode(c)).toString());
  assert.deepEqual(claims, { aud: "https://fcm.googleapis.com", exp: Math.floor(T0 / 1000) + 12 * 3600, sub: keys.subject });
  const pub = b64uDecode(keys.publicKey);
  const key = createPublicKey({ key: { kty: "EC", crv: "P-256", x: b64uEncode(pub.slice(1, 33)), y: b64uEncode(pub.slice(33)) }, format: "jwk" });
  assert.ok(verify("sha256", Buffer.from(`${h}.${c}`), { key, dsaEncoding: "ieee-p1363" }, Buffer.from(b64uDecode(s))));
  assert.match(await vapidAuthorization("https://web.push.apple.com/x", keys, T0), /^vapid t=/);
});

test("only browser vendors' push services are accepted as endpoints", async () => {
  for (const ok of [ENDPOINT, "https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/x", "https://wns2-par02p.notify.windows.com/w/?token=x"]) assert.ok(allowedEndpoint(ok), ok);
  for (const bad of ["http://fcm.googleapis.com/x", "https://127.0.0.1:3187/api", "https://localhost:9222/json", "https://fcm.googleapis.com.evil.test/x", "https://evilfcm.googleapis.com.test/x", "https://fcm.googleapis.com:8443/x", "https://u:p@fcm.googleapis.com/x", "not a url"]) assert.equal(allowedEndpoint(bad), false, bad);
  await assert.rejects(sendPush({ ...browser().target, endpoint: "https://127.0.0.1/x" }, "{}", vapidKeys(), async () => { throw new Error("must not fetch"); }));
});

async function withDb(run) {
  const dir = await mkdtemp(join(tmpdir(), "meeting-push-"));
  const db = new SQLiteDatabase(join(dir, "meeting.sqlite"));
  try {
    for (const name of (await readdir(drizzle)).filter(n => /^\d+.*\.sql$/.test(n)).sort()) db.connection.exec(await readFile(join(drizzle, name), "utf8"));
    await run(db, dir);
  } finally { db.close(); await rm(dir, { recursive: true, force: true }); }
}
async function insert(db, id, state, extra = {}) {
  const data = { filename: "a.m4a", byte_size: 10, mime_type: "audio/mp4", upload_id: "6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e11", stage: state, transcript_revision: 0, segments: [], upload_complete: true, metadata: { organization: "가나전자" }, ...extra };
  await db.prepare("INSERT INTO jobs(id,owner,state,revision,generation,data,created_at,updated_at,lease_id,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)")
    .bind(id, "owner", state, 1, 1, JSON.stringify(data), T0, T0, LEASE, Date.now() + 9e9).run();
  return loadJob(db, id, "owner");
}
// A recording sender; `deliveries` lets the test wait for the background send.
function recorder(status = 201) {
  const sent = [], deliveries = [];
  return { sent, deliveries, notice: { publicKey: KEY, send: async (target, payload) => { sent.push({ target, payload: JSON.parse(payload) }); return typeof status === "function" ? status(target) : status; }, track: p => deliveries.push(p) }, settle: () => Promise.all(deliveries) };
}
const SEGMENT = { segment_id: "11111111-1111-4111-8111-111111111111", start_ms: 0, end_ms: 1000, speaker: "A", text: "비밀 녹취 문장", review_status: "unreviewed" };

test("the worker's finishing result notifies each device once; owner actions never do", async () => withDb(async db => {
  await saveSubscription(db, { endpoint: ENDPOINT, p256dh: "p", auth: "a" }, KEY, T0);
  const r = recorder();
  const job = await insert(db, "job-1", "aligning");
  const done = await updateJob(db, job, "transcript_ready", { segments: [SEGMENT] }, job.generation, LEASE, r.notice);
  await r.settle();
  assert.equal(r.sent.length, 1);
  assert.deepEqual(Object.keys(r.sent[0].payload).sort(), ["body", "tag", "title", "url"]);
  assert.equal(r.sent[0].payload.body, "가나전자: 녹취가 준비되었습니다.");
  assert.doesNotMatch(JSON.stringify(r.sent[0].payload), /비밀 녹취/);
  assert.equal(r.sent[0].payload.tag, "job-job-1");

  // The same result arriving again in the same run (claim already taken) sends nothing more.
  assert.equal(await claimNotice(db, "job-1", 1, "transcript_ready"), false);
  // Owner review save: new generation, no lease.
  await updateJob(db, done, "needs_review", {}, done.generation + 1, undefined, r.notice);
  // Worker progress inside a run is not a notice state.
  const other = await insert(db, "job-2", "preparing");
  const moved = await updateJob(db, other, "diarizing", {}, other.generation, LEASE, r.notice);
  // A same-state save is no change.
  await updateJob(db, moved, "diarizing", {}, moved.generation, LEASE, r.notice);
  // resolve-outcome style: failed with generation + 1.
  await updateJob(db, moved, "failed", {}, moved.generation + 1, undefined, r.notice).catch(() => undefined);
  await r.settle();
  assert.equal(r.sent.length, 1);
}));

test("every stop state notifies; call jobs show no name or number", async () => withDb(async db => {
  assert.deepEqual([...NOTICE_STATES].sort(), ["failed", "minutes_ready", "needs_review", "needs_setup", "rate_limited", "transcript_ready", "unknown_outcome"]);
  await saveSubscription(db, { endpoint: ENDPOINT, p256dh: "p", auth: "a" }, KEY, T0);
  const r = recorder();
  let n = 0;
  for (const state of ["needs_review", "failed", "unknown_outcome", "rate_limited", "needs_setup"]) {
    const job = await insert(db, `job-s${n++}`, "transcribing");
    await updateJob(db, job, state, {}, job.generation, LEASE, r.notice);
  }
  const call = await insert(db, "job-call", "aligning", { kind: "call", call: { external_ref: "r", direction: "in", started_at: "2026-11-25T09:00:00Z", duration_sec: 60, counterpart: { name: "홍길동", phone_tail4: "1234", crm_contact_id: null, crm_company_id: null } } });
  await updateJob(db, call, "transcript_ready", {}, call.generation, LEASE, r.notice);
  await r.settle();
  assert.equal(r.sent.length, 6);
  assert.equal(r.sent.at(-1).payload.body, "통화 녹취: 녹취가 준비되었습니다.");
  assert.doesNotMatch(JSON.stringify(r.sent.map(s => s.payload)), /홍길동|1234/);
  assert.equal(noticePayload({ id: "x", filename: "f", metadata: {} }, "transcribing"), null);
  assert.equal(noticePayload({ id: "x", filename: "통화 홍길동 01012345678.m4a", output_mode: "transcript", metadata: {} }, "transcript_ready").body, "STT 녹취: 녹취가 준비되었습니다.");
}));

test("a failing push service never fails the result, and a gone subscription is dropped", async () => withDb(async db => {
  await saveSubscription(db, { endpoint: ENDPOINT, p256dh: "p", auth: "a" }, KEY, T0);
  await saveSubscription(db, { endpoint: "https://web.push.apple.com/gone", p256dh: "p", auth: "a" }, KEY, T0 + 1);
  const throwing = { send: async () => { throw new Error("network down"); }, deliveries: [] };
  const job = await insert(db, "job-3", "aligning");
  const delivery = [];
  const result = await updateJob(db, job, "transcript_ready", {}, job.generation, LEASE, { publicKey: KEY, send: throwing.send, track: p => delivery.push(p) });
  assert.equal(result.state, "transcript_ready");
  await Promise.all(delivery);
  assert.equal((await readSubscriptions(db)).length, 2, "a network error keeps the device");

  const r = recorder(target => target.endpoint.endsWith("/gone") ? 410 : 201);
  const next = await insert(db, "job-4", "aligning");
  await updateJob(db, next, "failed", {}, next.generation, LEASE, r.notice);
  await r.settle();
  assert.deepEqual((await readSubscriptions(db)).map(s => s.endpoint), [ENDPOINT]);
}));

test("subscriptions: same endpoint replaces, five devices at most; deleting a job clears its notice marks", async () => withDb(async (db, dir) => {
  for (let i = 0; i < 7; i++) await saveSubscription(db, { endpoint: `${ENDPOINT}-${i}`, p256dh: "p", auth: "a" }, T0 + i);
  assert.equal(await saveSubscription(db, { endpoint: `${ENDPOINT}-6`, p256dh: "new", auth: "a" }, T0 + 9), 5);
  const list = await readSubscriptions(db);
  assert.equal(list[0].p256dh, "new");
  assert.equal(list.filter(s => s.endpoint === `${ENDPOINT}-6`).length, 1);
  assert.equal(await removeSubscription(db, `${ENDPOINT}-6`), 4);

  assert.equal(await claimNotice(db, "job-5", 1, "failed"), true);
  assert.equal(await claimNotice(db, "job-5", 2, "failed"), true, "a new run notifies again");
  assert.equal(await claimNotice(db, "job-50", 1, "failed"), true);
  await finishJobRemoval(db, new FileBucket(join(dir, "objects")), "job-5", "owner", 3);
  assert.equal(await readSetting(db, "push_sent:job-5:1:failed"), undefined);
  assert.equal(await readSetting(db, "push_sent:job-5:2:failed"), undefined);
  assert.ok(await readSetting(db, "push_sent:job-50:1:failed"), "another job whose id starts the same keeps its mark");
}));

test("no device means no notice record; subscriptions made with an older server key are dropped, not sent to", async () => withDb(async db => {
  const r = recorder();
  const job = await insert(db, "job-6", "aligning");
  await updateJob(db, job, "transcript_ready", {}, job.generation, LEASE, r.notice);
  await r.settle();
  assert.equal(await readSetting(db, "push_sent:job-6:1:transcript_ready"), undefined);

  await saveSubscription(db, { endpoint: `${ENDPOINT}-old`, p256dh: "p", auth: "a" }, "server-key-0", T0);
  await saveSubscription(db, { endpoint: ENDPOINT, p256dh: "p", auth: "a" }, KEY, T0 + 1);
  const next = await insert(db, "job-7", "aligning");
  await updateJob(db, next, "failed", {}, next.generation, LEASE, r.notice);
  await r.settle();
  assert.deepEqual(r.sent.map(s => s.target.endpoint), [ENDPOINT]);
  assert.deepEqual((await readSubscriptions(db)).map(s => s.endpoint), [ENDPOINT]);
}));
