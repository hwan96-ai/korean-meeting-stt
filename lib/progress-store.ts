// Where progress lives. Worker progress and speed history sit in small settings rows, never in jobs.data:
// a heartbeat every 30 seconds must not rewrite a job that already holds a long transcript, and an owner
// save of the job must not overwrite progress the worker reported a moment later.
import type { Job } from "./contracts.ts";
import type { Database } from "./runtime-types.ts";
import { ACTIVE_STATES, nextProgress, sampleOf, type ProgressReport, type SpeedSample, type StoredProgress } from "./progress.ts";

const progressKey = (id: string) => `progress:${id}`;
const SPEED_SAMPLES = "speed_samples";
const KEEP_SAMPLES = 20;

async function readJson<T>(db: Database, key: string): Promise<T | undefined> {
  const row = await db.prepare("SELECT value FROM settings WHERE key=?").bind(key).first<{ value: string }>();
  if (!row) return undefined;
  try { return JSON.parse(row.value) as T; } catch { return undefined; }
}
async function writeJson(db: Database, key: string, value: unknown) {
  await db.prepare("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(key, JSON.stringify(value)).run();
}

export async function readProgress(db: Database, id: string) { return readJson<StoredProgress>(db, progressKey(id)); }
export async function saveProgress(db: Database, id: string, report: ProgressReport, leaseId: string, now: number) {
  const next = nextProgress(await readProgress(db, id), report, leaseId, now);
  await writeJson(db, progressKey(id), next);
  return next;
}
export async function clearProgress(db: Database, id: string) { await db.prepare("DELETE FROM settings WHERE key=?").bind(progressKey(id)).run(); }
// Progress counts only for the lease that reported it; a reclaimed or finished job shows none.
export function leaseProgress(progress: StoredProgress | undefined, leaseId: string | null | undefined) {
  return progress && leaseId && progress.lease_id === leaseId ? progress : undefined;
}

// Newest first. Written once when a job finishes, so later edits of that job (which start a new
// generation and a new timeline) keep its step times.
export async function readSpeedSamples(db: Database): Promise<SpeedSample[]> {
  const value = await readJson<SpeedSample[]>(db, SPEED_SAMPLES);
  return Array.isArray(value) ? value.filter(sample => sample?.v === 2) : [];
}
export async function recordSpeedSample(db: Database, job: Pick<Job, "state" | "timing" | "duration" | "speech_mode">) {
  const sample = sampleOf(job);
  if (!sample) return;
  await writeJson(db, SPEED_SAMPLES, [sample, ...await readSpeedSamples(db)].slice(0, KEEP_SAMPLES));
}

// Jobs the worker takes before this one, in claim order: every running job (an expired lease is claimed
// first) and older waiting ones.
export async function queueAhead(db: Database, j: Pick<Job, "id" | "created_at">, user: string) {
  const active = ACTIVE_STATES.map(() => "?").join(",");
  const row = await db.prepare(`SELECT COUNT(*) AS n FROM jobs WHERE owner=? AND deleted_at IS NULL AND id<>? AND (state IN (${active}) OR (state='waiting_worker' AND created_at<?))`)
    .bind(user, j.id, ...ACTIVE_STATES, j.created_at).first<{ n: number }>();
  return row?.n ?? 0;
}
