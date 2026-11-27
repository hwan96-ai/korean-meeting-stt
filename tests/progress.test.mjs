import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { SQLiteDatabase } from "../lib/runtime-storage.ts";
import { finishJobRemoval, loadJob, updateJob } from "../lib/job-store.ts";
import { FileBucket } from "../lib/runtime-storage.ts";
import { leaseProgress, queueAhead, readProgress, readSpeedSamples, saveProgress } from "../lib/progress-store.ts";
import { PROGRESS_STAGES } from "../lib/progress.ts";
import { expectedSeconds, formatDuration, nextProgress, progressShort, progressView, sampleOf, timingAfter, timingAfterClaim } from "../lib/progress.ts";

const MIN = 60000, T0 = Date.UTC(2026, 10, 25, 7, 0, 0), LEASE = "lease-a";
const drizzle = fileURLToPath(new URL("../drizzle/", import.meta.url));

test("heartbeat progress keeps its speed base only within one lease, stage and total", () => {
  let p = nextProgress(undefined, { stage: "diarizing", done: 7, total: 21 }, LEASE, T0);
  assert.deepEqual(p, { stage: "diarizing", done: 7, total: 21, lease_id: LEASE, reported_at: T0, changed_at: T0 });
  p = nextProgress(p, { stage: "diarizing", done: 7, total: 21 }, LEASE, T0 + 30000);
  assert.equal(p.changed_at, T0);
  assert.equal(p.reported_at, T0 + 30000);
  p = nextProgress(p, { stage: "diarizing", done: 8, total: 21 }, LEASE, T0 + 90000);
  assert.deepEqual(p.base, { done: 8, at: T0 + 90000 });
  p = nextProgress(p, { stage: "diarizing", done: 9, total: 21 }, LEASE, T0 + 180000);
  assert.deepEqual(p.base, { done: 8, at: T0 + 90000 });
  assert.equal(p.changed_at, T0 + 180000);
  assert.equal(nextProgress(p, { stage: "diarizing", done: 9, total: 21 }, "lease-b", T0 + 200000).base, undefined);
  assert.equal(nextProgress(p, { stage: "transcribing", done: 1, total: 300 }, LEASE, T0 + 200000).base, undefined);
  assert.equal(nextProgress(p, { stage: "diarizing", done: 3, total: 21 }, LEASE, T0 + 200000).base, undefined);
});

test("timeline appends state changes, restarts on a new generation and marks a resumed claim", () => {
  const job = { state: "waiting_worker", generation: 1, timing: { generation: 1, events: [{ state: "waiting_worker", at: T0 }] } };
  const claimed = timingAfterClaim(job, "preparing", T0 + MIN);
  assert.deepEqual(claimed.events.map(e => e.state), ["waiting_worker", "preparing"]);
  const running = { state: "diarizing", generation: 1, timing: { generation: 1, events: [...claimed.events, { state: "diarizing", at: T0 + 2 * MIN }] } };
  assert.deepEqual(timingAfterClaim(running, "preparing", T0 + 10 * MIN).events.slice(-2).map(e => e.state), ["resumed", "preparing"]);
  assert.equal(timingAfter(running, "diarizing", 1, T0 + 11 * MIN), running.timing);
  assert.deepEqual(timingAfter(running, "transcribing", 1, T0 + 11 * MIN).events.at(-1), { state: "transcribing", at: T0 + 11 * MIN });
  assert.deepEqual(timingAfter(running, "waiting_worker", 2, T0 + 12 * MIN), { generation: 2, events: [{ state: "waiting_worker", at: T0 + 12 * MIN }] });
  assert.deepEqual(timingAfter({ state: "uploading", generation: 1 }, "waiting_worker", 1, T0).events, [{ state: "waiting_worker", at: T0 }]);
  let long = { state: "preparing", generation: 1, timing: { generation: 1, events: [] } };
  for (let i = 0; i < 60; i++) long = { ...long, state: i % 2 ? "preparing" : "diarizing", timing: timingAfter(long, i % 2 ? "preparing" : "diarizing", 1, T0 + i) };
  assert.equal(long.timing.events.length, 40);
});

// A finished job whose steps run back to back after a one-minute wait.
function finished(mode, duration, minutes) {
  const clean = [{ state: "waiting_worker", at: T0 }];
  let t = T0 + MIN;
  for (const [state, m] of minutes) { clean.push({ state, at: t }); t += m * MIN; }
  clean.push({ state: "transcript_ready", at: t });
  return { state: "transcript_ready", duration, speech_mode: mode, timing: { generation: 1, events: clean } };
}

test("history samples need a finished timeline and estimates use only the same speech mode", () => {
  const sample = sampleOf(finished("api/openai/gpt-transcribe/audio-clip-bounds", 600, [["preparing", 1], ["diarizing", 3], ["transcribing", 2], ["aligning", 1]]));
  assert.deepEqual(sample.seconds, { preparing: 60, diarizing: 180, transcribing: 120, aligning: 60 });
  assert.equal(sampleOf({ ...finished("api/x", 600, [["preparing", 1]]), state: "failed" }), null);
  assert.equal(sampleOf({ state: "transcript_ready", duration: 600, speech_mode: "api/x" }), null);
  const other = sampleOf(finished("api/openai/gpt-transcribe/audio-clip-bounds", 1200, [["preparing", 2], ["diarizing", 6], ["transcribing", 4], ["aligning", 3]]));
  const expected = expectedSeconds([sample, other], "api/openai/whatever", 6000);
  assert.equal(expected.diarizing, 1800);
  assert.equal(expected.aligning, 120);
  assert.deepEqual(expectedSeconds([sample], "local/faster-whisper/base/cpu-int8", 6000), {});
});

function running(overrides = {}) {
  return {
    id: "job", state: "diarizing", revision: 5, generation: 1, created_at: T0 - 4 * MIN, updated_at: T0, lease_id: LEASE, lease_until: T0 + 30 * MIN,
    duration: 6000, stage: "diarizing", transcript_revision: 0, segments: [], upload_complete: true,
    timing: { generation: 1, events: [{ state: "waiting_worker", at: T0 - MIN }, { state: "preparing", at: T0 }, { state: "diarizing", at: T0 + MIN }] },
    ...overrides,
  };
}
const API = { apiOrder: true, samples: [], speechMode: "api/openai/gpt-transcribe" };

test("in-stage estimate uses only freshly computed units and the bar follows the API order", () => {
  const progress = { stage: "diarizing", done: 10, total: 21, lease_id: LEASE, reported_at: T0 + 16 * MIN, changed_at: T0 + 16 * MIN, base: { done: 8, at: T0 + 13 * MIN } };
  const view = progressView(running({ progress }), { ...API, now: T0 + 16 * MIN });
  assert.deepEqual(view.steps.map(s => `${s.key}:${s.status}`), ["waiting:done", "preparing:done", "diarizing:current", "transcribing:pending", "aligning:pending", "done:pending"]);
  assert.deepEqual(view.unit, { done: 10, total: 21, noun: "구간" });
  assert.equal(view.stageRemainingMs, 11 * 1.5 * MIN);
  assert.equal(view.stageElapsedMs, 15 * MIN);
  assert.equal(view.jobElapsedMs, 20 * MIN);
  assert.equal(view.totalRemainingMs, null, "no history: no whole-job estimate");
  assert.equal(view.attention, null);
  const local = progressView(running({ progress }), { ...API, apiOrder: false, now: T0 + 16 * MIN });
  assert.deepEqual(local.steps.slice(1, 5).map(s => s.key), ["preparing", "transcribing", "diarizing", "aligning"]);
  assert.equal(progressView(running({ progress: { ...progress, base: undefined } }), { ...API, now: T0 + 16 * MIN }).stageRemainingMs, null);
  assert.equal(progressView(running({ progress: { ...progress, lease_id: "old" } }), { ...API, now: T0 + 16 * MIN }).unit, null);
});

test("whole-job estimate, finish time and percent come from finished jobs of the same mode", () => {
  const samples = [sampleOf(finished("api/openai/gpt-transcribe/audio-clip-bounds", 600, [["preparing", 1], ["diarizing", 3], ["transcribing", 2], ["aligning", 1]]))];
  const progress = { stage: "diarizing", done: 10, total: 21, lease_id: LEASE, reported_at: T0 + 16 * MIN, changed_at: T0 + 16 * MIN, base: { done: 8, at: T0 + 13 * MIN } };
  const view = progressView(running({ progress }), { ...API, samples, now: T0 + 16 * MIN });
  assert.equal(view.totalRemainingMs, 16.5 * MIN + 20 * MIN + MIN);
  assert.equal(view.finishAt, T0 + 16 * MIN + view.totalRemainingMs);
  assert.equal(view.percent, Math.round(16 / (16 + 37.5) * 100));
  const early = progressView(running(), { ...API, samples, now: T0 + 2 * MIN });
  assert.equal(early.stageRemainingMs, 30 * MIN - MIN, "before two blocks finish, history estimates the stage");
});

test("attention: lost lease, stuck count, and a resume note", () => {
  const progress = { stage: "diarizing", done: 10, total: 21, lease_id: LEASE, reported_at: T0 + 40 * MIN, changed_at: T0 + 16 * MIN, base: { done: 8, at: T0 + 13 * MIN } };
  assert.equal(progressView(running({ progress, lease_until: T0 + 60 * MIN }), { ...API, now: T0 + 40 * MIN }).attention.kind, "slow");
  assert.equal(progressView(running({ progress }), { ...API, now: T0 + 20 * MIN }).attention, null);
  assert.equal(progressView(running({ progress, lease_until: T0 + 19 * MIN }), { ...API, now: T0 + 20 * MIN }).attention.kind, "lease_expired");
  const timing = { generation: 1, events: [...running().timing.events, { state: "resumed", at: T0 + 11 * MIN }, { state: "preparing", at: T0 + 11 * MIN }, { state: "diarizing", at: T0 + 11.5 * MIN }] };
  const view = progressView(running({ timing }), { ...API, now: T0 + 12 * MIN });
  assert.equal(view.resumedAt, T0 + 11 * MIN);
  assert.equal(view.stageElapsedMs, 0.5 * MIN);
  assert.equal(progressView(running({ state: "aligning", timing: { generation: 1, events: [{ state: "aligning", at: T0 }] } }), { ...API, now: T0 + 11 * MIN }).attention.kind, "slow");
});

test("waiting jobs show the queue, finished and failed jobs show nothing", () => {
  const waiting = progressView(running({ state: "waiting_worker", lease_id: undefined, lease_until: undefined }), { ...API, now: T0, queueAhead: 2 });
  assert.equal(waiting.queueAhead, 2);
  assert.equal(waiting.steps[0].status, "current");
  for (const state of ["transcript_ready", "failed", "uploading", "generating"]) assert.equal(progressView(running({ state }), { ...API, now: T0 }), null);
  const meta = progressView(running({ state: "aligning", progress: { stage: "meta_inference", done: 0, total: 0, lease_id: LEASE, reported_at: T0, changed_at: T0 } }), { ...API, now: T0 });
  assert.equal(meta.stageLabel, "회의 정보 추정");
  assert.equal(meta.unit, null);
});

test("list label and duration wording", () => {
  const progress = { stage: "diarizing", done: 8, total: 21, lease_id: LEASE, reported_at: T0, changed_at: T0 };
  assert.equal(progressShort({ state: "diarizing", progress, lease_until: T0 + MIN }, T0), "8/21");
  assert.equal(progressShort({ state: "transcribing", progress, lease_until: T0 + MIN }, T0), null);
  assert.equal(progressShort({ state: "diarizing", progress, lease_until: T0 - 1 }, T0), null);
  assert.equal(formatDuration(20000), "1분 미만");
  assert.equal(formatDuration(37.5 * MIN), "38분");
  assert.equal(formatDuration(65 * MIN), "1시간 5분");
});

async function withDb(run) {
  const dir = await mkdtemp(join(tmpdir(), "meeting-progress-"));
  const db = new SQLiteDatabase(join(dir, "meeting.sqlite"));
  try {
    for (const name of (await readdir(drizzle)).filter(n => /^\d+.*\.sql$/.test(n)).sort()) db.connection.exec(await readFile(join(drizzle, name), "utf8"));
    await run(db, dir);
  } finally { db.close(); await rm(dir, { recursive: true, force: true }); }
}
const row = (id, state, extra = {}, created = T0, lease = { id: LEASE, until: T0 + 9e12 }) => ({ id, state, created, lease, data: {
  filename: "a.m4a", byte_size: 10, mime_type: "audio/mp4", upload_id: "6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e11", stage: state, transcript_revision: 0, segments: [], upload_complete: true, ...extra } });
async function insert(db, r) {
  await db.prepare("INSERT INTO jobs(id,owner,state,revision,generation,data,created_at,updated_at,lease_id,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)")
    .bind(r.id, "owner", r.state, 1, 1, JSON.stringify(r.data), r.created, r.created, r.lease?.id ?? null, r.lease?.until ?? null).run();
}

test("progress lives in its own row: same-state saves keep the newest count, a state change clears it", async () => withDb(async (db, dir) => {
  await insert(db, row("job-1", "diarizing", { timing: { generation: 1, events: [{ state: "diarizing", at: T0 }] } }));
  const loaded = await loadJob(db, "job-1", "owner");
  await saveProgress(db, "job-1", { stage: "diarizing", done: 4, total: 5 }, LEASE, T0 + 1000);
  await updateJob(db, loaded, "diarizing", { owner_edited_at: T0 });
  assert.equal((await readProgress(db, "job-1")).done, 4, "a save of a job loaded earlier keeps the worker's newer count");
  const stored = await loadJob(db, "job-1", "owner");
  assert.equal(stored.progress, undefined, "never written into jobs.data");
  await updateJob(db, stored, "transcribing", {});
  assert.equal(await readProgress(db, "job-1"), undefined);
  assert.deepEqual((await loadJob(db, "job-1", "owner")).timing.events.map(e => e.state), ["diarizing", "transcribing"]);
  await saveProgress(db, "job-1", { stage: "transcribing", done: 1, total: 9 }, LEASE, T0 + 2000);
  assert.equal(leaseProgress(await readProgress(db, "job-1"), "other-lease"), undefined);
  assert.equal(leaseProgress(await readProgress(db, "job-1"), LEASE).done, 1);
  await finishJobRemoval(db, new FileBucket(join(dir, "objects")), "job-1", "owner", 2);
  assert.equal(await readProgress(db, "job-1"), undefined, "deleting a job removes its progress row");
}));

test("a finished job leaves one speed sample that later edits keep", async () => withDb(async db => {
  const events = [{ state: "waiting_worker", at: T0 }, { state: "preparing", at: T0 + MIN }, { state: "diarizing", at: T0 + 2 * MIN }, { state: "transcribing", at: T0 + 5 * MIN }];
  await insert(db, row("job-2", "transcribing", { duration: 600, timing: { generation: 1, events } }));
  let job = await loadJob(db, "job-2", "owner");
  job = await updateJob(db, job, "aligning", {});
  job = await updateJob(db, job, "transcript_ready", { speech_mode: "api/openai/gpt-transcribe/audio-clip-bounds", duration: 600 });
  let samples = await readSpeedSamples(db);
  assert.equal(samples.length, 1);
  assert.equal(samples[0].v, 2);
  assert.equal(samples[0].seconds.diarizing, 180);
  await updateJob(db, await loadJob(db, "job-2", "owner"), "needs_review", { owner_edited_at: T0 }, 2);
  await updateJob(db, await loadJob(db, "job-2", "owner"), "needs_review", { owner_edited_at: T0 + 1 });
  samples = await readSpeedSamples(db);
  assert.equal(samples.length, 1, "an edit (new generation) neither drops nor duplicates the sample");
}));

test("only first runs become speed samples: a resume or a retry reuses checkpoints", () => {
  const events = [{ state: "waiting_worker", at: T0 }, { state: "preparing", at: T0 + MIN }, { state: "diarizing", at: T0 + 2 * MIN },
    { state: "resumed", at: T0 + 8 * 60 * MIN }, { state: "preparing", at: T0 + 8 * 60 * MIN }, { state: "diarizing", at: T0 + 8 * 60 * MIN + MIN },
    { state: "transcript_ready", at: T0 + 8 * 60 * MIN + 4 * MIN }];
  assert.equal(sampleOf({ state: "transcript_ready", duration: 3600, speech_mode: "api/x", timing: { generation: 1, events } }), null);
  const clean = events.filter((e, i) => i < 3).concat([{ state: "transcript_ready", at: T0 + 5 * MIN }]);
  assert.equal(sampleOf({ state: "transcript_ready", duration: 3600, speech_mode: "api/x", timing: { generation: 1, events: clean } }).seconds.diarizing, 180);
  assert.equal(sampleOf({ state: "transcript_ready", duration: 3600, speech_mode: "api/x", timing: { generation: 4, events: clean } }), null, "a retried job");
});

test("queue count includes running jobs whose lease ran out, which the claim takes first", async () => withDb(async db => {
  await insert(db, row("stale", "diarizing", {}, T0, { id: "old", until: T0 - 1 }));
  await insert(db, row("older", "waiting_worker", {}, T0 + 1, null));
  await insert(db, row("mine", "waiting_worker", {}, T0 + 2, null));
  await insert(db, row("newer", "waiting_worker", {}, T0 + 3, null));
  await insert(db, row("done", "transcript_ready", {}, T0, null));
  assert.equal(await queueAhead(db, { id: "mine", created_at: T0 + 2 }, "owner"), 2);
  assert.equal(await queueAhead(db, { id: "older", created_at: T0 + 1 }, "owner"), 1);
  assert.equal(await queueAhead(db, { id: "mine", created_at: T0 + 2 }, "someone-else"), 0);
}));

test("slow warning waits four unit times when units are long", () => {
  const unit = 5 * MIN, base = { done: 2, at: T0 }, changed = T0 + 2 * unit;
  const progress = { stage: "diarizing", done: 4, total: 21, lease_id: LEASE, reported_at: changed, changed_at: changed, base };
  const job = running({ progress, lease_until: T0 + 9e12 });
  assert.equal(progressView(job, { ...API, now: changed + 15 * MIN }).attention, null, "15 minutes is under 4 x 5 minutes");
  assert.equal(progressView(job, { ...API, now: changed + 21 * MIN }).attention.kind, "slow");
});

test("every stage the worker reports is one the web accepts", async () => {
  const source = await readFile(fileURLToPath(new URL("../local_worker/runner.py", import.meta.url)), "utf8");
  const stages = [...source.matchAll(/set_progress\("([a-z_]+)"/g)].map(m => m[1]);
  assert.ok(stages.length >= 5);
  for (const stage of stages) assert.ok(PROGRESS_STAGES.includes(stage), stage);
});

test("samples written by the first release (no version) are ignored", async () => withDb(async db => {
  const old = { mode: "api/x", duration: 6000, seconds: { diarizing: 1, transcribing: 700 } };
  await db.prepare("INSERT INTO settings(key,value) VALUES('speed_samples',?)").bind(JSON.stringify([old, { ...old, v: 2 }])).run();
  assert.deepEqual((await readSpeedSamples(db)).map(s => s.v), [2]);
}));
