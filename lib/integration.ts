// Orchestrator-only door for phone call recordings: /api/integration/v1/* (ops/integration-calls/README.md).
// Contract: kept in the author's private orchestrator repo; this file and tests/integration-calls.test.mjs are the public record.
// - Loopback only (integration-gate.ts), then `Authorization: Bearer <INTEGRATION_TOKEN>`.
// - A call becomes an ordinary transcript-only job, so upload, speech, diarization and deletion are the same
//   code paths as the owner's STT jobs (job-store.ts). Only call jobs are reachable through this door.
import { z } from "zod";
import { CHUNK_BYTES, MAX_BYTES, MAX_DURATION, idSchema, metadataSchema, type CallInfo, type Job, type JobData } from "./contracts.ts";
import { ApiError } from "./api-error.ts";
import { boundedBody, clearDeletedCallContacts, digest, finishJobRemoval, finishUpload, listParts, loadJob, partCount, readSetting, removeJob, storePart } from "./job-store.ts";
import { bearerMatches, fromLoopbackOnly, MIN_TOKEN_LENGTH } from "./integration-gate.ts";
import type { Bucket, Database } from "./runtime-types.ts";

// dailyLimit: new calls accepted per Korean calendar day (INTEGRATION_DAILY_CALL_LIMIT, default 200).
export interface IntegrationContext { db: Database; bucket: Bucket; token: string | null | undefined; dailyLimit?: number; }
export const DEFAULT_DAILY_CALL_LIMIT = 200;

// Galaxy call recordings are m4a (older models amr or 3ga). The server worker decodes all of these with PyAV
// (amrnb, amrwb, aac, mp3, opus, vorbis, flac decoders checked on the server, av 19.0.2).
const AUDIO_EXT = /\.(m4a|mp3|wav|webm|ogg|mp4|flac|aac|amr|3gp|3ga)$/i;
// Audio types only. Android reports some audio-only containers as video/mp4 or video/3gpp, so those two are kept.
export const AUDIO_MIME = new Set(["audio/mp4", "audio/m4a", "audio/x-m4a", "audio/mp4a-latm", "audio/aac", "audio/x-aac", "audio/aacp", "audio/mpeg", "audio/mp3", "audio/x-mpeg",
  "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/ogg", "audio/opus", "audio/webm", "audio/flac", "audio/x-flac",
  "audio/amr", "audio/amr-wb", "audio/3gpp", "audio/3gpp2", "video/mp4", "video/3gpp"]);
const mimeType = z.string().max(100).transform(v => v.split(";")[0].trim().toLowerCase()).refine(v => AUDIO_MIME.has(v), "음성 형식이 아닙니다.");
const sha = z.string().regex(/^[a-f0-9]{64}$/);
const crmId = z.string().min(1).max(100).nullable().optional();
export const callRequestSchema = z.object({
  external_ref: z.string().regex(/^call:[A-Za-z0-9._:-]{1,190}$/),
  started_at: z.string().datetime({ offset: true }),
  duration_sec: z.number().int().min(0).max(MAX_DURATION),
  direction: z.enum(["in", "out"]),
  counterpart: z.object({ name: z.string().max(100), phone_e164: z.string().regex(/^\+[1-9]\d{6,14}$/), crm_contact_id: crmId, crm_company_id: crmId }).strict(),
  file: z.object({ name: z.string().min(1).max(255).regex(AUDIO_EXT), size: z.number().int().positive().max(MAX_BYTES), sha256: sha, mime: mimeType }).strict(),
}).strict();
const completeSchema = z.object({ sha256: sha, part_checksums: z.array(sha).min(1).max(128) }).strict();
const revertSchema = z.object({ external_ref: z.string().min(1).max(200) }).strict();

type CallRow = { external_ref: string; job_id: string; created_at: number; direction: "in" | "out"; started_at: string; duration_sec: number; counterpart_name: string | null; phone_e164: string | null; crm_contact_id: string | null; crm_company_id: string | null; file_size: number; file_sha256: string; file_mime: string; file_ext: string; reverting_at: number | null; reverted_at: number | null; job_deleted_at: number | null };

class IntegrationError extends Error {
  status: number; code: string; field?: string; extra?: Record<string, unknown>;
  constructor(status: number, code: string, message: string, field?: string, extra?: Record<string, unknown>) { super(message); this.status = status; this.code = code; this.field = field; this.extra = extra; }
}
const reject = (status: number, code: string, message: string, field?: string, extra?: Record<string, unknown>): never => { throw new IntegrationError(status, code, message, field, extra); };
const headers = { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" };
const json = (value: unknown, status = 200) => Response.json(value, { status, headers });
const notFound = () => json({ error: "경로를 찾을 수 없습니다." }, 404);

// Shared upload/complete errors keep their meaning; input-type failures become the contract's 422 with a field.
const FIELD_OF: Record<string, string> = { part_index: "part", part_size: "content-length", part_checksum: "x-chunk-sha256", incomplete_upload: "part_checksums", file_checksum: "sha256", missing_part: "part_checksums", invalid_json: "body", too_large: "body" };
function respondError(error: unknown, where: string) {
  if (error instanceof IntegrationError) return json({ error: error.message, code: error.code, ...(error.field ? { field: error.field } : {}), ...(error.extra ?? {}) }, error.status);
  if (error instanceof ApiError) {
    if (error.status === 400) return json({ error: error.message, code: error.code, field: FIELD_OF[error.code] ?? "body" }, 422);
    return json({ error: error.message, code: error.code }, error.status);
  }
  if (error instanceof z.ZodError) {
    const field = error.issues[0]?.path.join(".") || "body";
    return json({ error: `입력 형식을 확인하세요: ${field}`, code: "invalid_input", field }, 422);
  }
  // Unknown failure: log where and what kind, never the request body, token or number.
  const kind = error instanceof Error ? error.name : typeof error, code = (error as { code?: unknown })?.code;
  console.error(JSON.stringify({ event: "integration_internal_error", where, kind, ...(typeof code === "string" ? { code } : {}) }));
  return json({ error: "처리 중 오류가 발생했습니다. 잠시 후 다시 시도하세요.", code: "internal_error" }, 500);
}

async function parseJson<S extends z.ZodTypeAny>(request: Request, schema: S): Promise<z.output<S>> {
  if (Number(request.headers.get("content-length") ?? 0) > 1024 * 1024) reject(413, "too_large", "요청 내용이 너무 큽니다.", "body");
  let value: unknown;
  try { value = JSON.parse(new TextDecoder().decode(await boundedBody(request, 1024 * 1024))); } catch (e) { if (e instanceof ApiError) throw e; return reject(422, "invalid_json", "JSON 형식이 아닙니다.", "body"); }
  return schema.parse(value);
}

// Same ref, same job id: a crash between the two inserts, or two requests at once, still end with one job.
export async function callJobId(ref: string) {
  const h = await digest(new TextEncoder().encode(`meeting-integration-call:${ref}`));
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-5${h.slice(13, 16)}-${((parseInt(h[16], 16) & 3) | 8).toString(16)}${h.slice(17, 20)}-${h.slice(20, 32)}`;
}
// The recorder's own file name often carries the other person's number or name, so it is never stored.
// The job gets "통화_YYMMDD_HHMMSS_<6 hex of the job id>.<ext>" in Korean time instead: the warehouse export reads
// the date pattern, and the job-id part keeps two calls that start in the same second apart.
export function callFilename(startedAt: string, ext: string, jobId: string) {
  const kst = new Date(new Date(startedAt).getTime() + 9 * 3600_000).toISOString();
  return `통화_${kst.slice(2, 4)}${kst.slice(5, 7)}${kst.slice(8, 10)}_${kst.slice(11, 13)}${kst.slice(14, 16)}${kst.slice(17, 19)}_${jobId.slice(0, 6)}.${ext.toLowerCase()}`;
}
export const tail4 = (phone: string | null) => phone ? phone.slice(-4) : "";

function callData(row: CallRow, jobId: string): JobData {
  const call: CallInfo = { external_ref: row.external_ref, direction: row.direction, started_at: row.started_at, duration_sec: row.duration_sec,
    counterpart: { name: row.counterpart_name ?? "", phone_tail4: tail4(row.phone_e164), crm_contact_id: row.crm_contact_id, crm_company_id: row.crm_company_id } };
  return { output_mode: "transcript", meeting_type: "customer", developer_requests: false, input_kind: "file", metadata: metadataSchema.parse({}),
    filename: callFilename(row.started_at, row.file_ext, jobId), byte_size: row.file_size, mime_type: row.file_mime, upload_id: jobId,
    stage: "uploading", transcript_revision: 0, segments: [], upload_complete: false, kind: "call", call };
}

// Re-sent ref (contract 0-3/0-5): blanks are filled (CRM ids, an empty name); a value that differs from a non-blank
// stored one is reported in `conflicts`, never written. A field the new request leaves out or blank is no opinion.
// The number shows only its last four digits.
type CallInput = z.output<typeof callRequestSchema>;
export function blankFills(row: CallRow, input: CallInput) {
  const fills: { field: string; column: "counterpart_name" | "crm_contact_id" | "crm_company_id"; path: string; value: string }[] = [];
  if (!row.counterpart_name && input.counterpart.name) fills.push({ field: "counterpart.name", column: "counterpart_name", path: "$.call.counterpart.name", value: input.counterpart.name });
  if (row.crm_contact_id === null && input.counterpart.crm_contact_id) fills.push({ field: "counterpart.crm_contact_id", column: "crm_contact_id", path: "$.call.counterpart.crm_contact_id", value: input.counterpart.crm_contact_id });
  if (row.crm_company_id === null && input.counterpart.crm_company_id) fills.push({ field: "counterpart.crm_company_id", column: "crm_company_id", path: "$.call.counterpart.crm_company_id", value: input.counterpart.crm_company_id });
  return fills;
}
function conflictsOf(row: CallRow, input: CallInput) {
  const pairs: [string, unknown, unknown][] = [
    ["direction", row.direction, input.direction], ["started_at", row.started_at, input.started_at], ["duration_sec", row.duration_sec, input.duration_sec],
    ["counterpart.phone_e164", row.phone_e164, input.counterpart.phone_e164],
    ["counterpart.name", row.counterpart_name || null, input.counterpart.name || null],
    ["counterpart.crm_contact_id", row.crm_contact_id, input.counterpart.crm_contact_id ?? null], ["counterpart.crm_company_id", row.crm_company_id, input.counterpart.crm_company_id ?? null],
  ];
  // started_at is compared as a moment: "+09:00" and "Z" spellings of the same instant are the same value.
  const same = (field: string, a: unknown, b: unknown) => field === "started_at" ? Date.parse(a as string) === Date.parse(b as string) : a === b;
  return pairs.filter(([field, a, b]) => a !== null && b !== null && !same(field, a, b)).map(([field, existing, proposed]) => field === "counterpart.phone_e164"
    ? { field, existing: `끝 ${tail4(existing as string | null)}`, proposed: `끝 ${tail4(proposed as string)}` }
    : { field, existing, proposed });
}
// The job keeps its own copy of the call info. Other writers cannot clobber it: updateJob carries the stored
// `call` over (job-store.ts), so this json_set needs no revision bump and never races the worker's lease.
export async function applyFills(db: Database, row: CallRow, fills: ReturnType<typeof blankFills>) {
  const applied: string[] = [];
  for (const f of fills) {
    // Never on a deleted job: its name and number were cleared on purpose.
    const r = await db.prepare(`UPDATE integration_calls SET ${f.column}=? WHERE external_ref=? AND reverted_at IS NULL AND reverting_at IS NULL AND (${f.column} IS NULL OR ${f.column}='') AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=integration_calls.job_id AND jobs.deleted_at IS NOT NULL)`).bind(f.value, row.external_ref).run();
    if (!r.meta.changes) continue;
    await db.prepare("UPDATE jobs SET data=json_set(data,?,?),updated_at=? WHERE id=? AND deleted_at IS NULL AND json_type(data,'$.call')='object'").bind(f.path, f.value, Date.now(), row.job_id).run();
    applied.push(f.field);
  }
  return applied;
}
// Start of the current Korean calendar day, in epoch milliseconds.
const kstDayStart = (now: number) => { const kst = now + 9 * 3600_000; return kst - (kst % 86_400_000) - 9 * 3600_000; };
async function liveDuplicate(db: Database, sha256: string) {
  // Only live calls hold their file: a reverted ref or a job the owner deleted does not (and its id is not told).
  return db.prepare("SELECT job_id FROM integration_calls WHERE file_sha256=? AND reverted_at IS NULL AND job_deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=integration_calls.job_id AND jobs.deleted_at IS NOT NULL)").bind(sha256).first<{ job_id: string }>();
}
const duplicateFile = (jobId: string) => reject(422, "duplicate_file", "같은 녹음 파일이 다른 external_ref로 이미 들어와 있습니다.", "file.sha256", { reason: "duplicate_file", job_id: jobId });

async function ownerOf(db: Database) { const owner = await readSetting(db, "owner"); if (!owner) reject(503, "owner_unset", "소유자 설정이 없습니다."); return owner!; }
async function callRow(db: Database, by: "external_ref" | "job_id", value: string) {
  return db.prepare(`SELECT * FROM integration_calls WHERE ${by}=?`).bind(value).first<CallRow>();
}
async function jobDeletedAt(db: Database, id: string) { return (await db.prepare("SELECT deleted_at FROM jobs WHERE id=?").bind(id).first<{ deleted_at: number | null }>()); }
async function liveCallJob(db: Database, rawId: string, owner: string): Promise<{ row: CallRow; job: Job }> {
  const parsed = idSchema.safeParse(rawId); if (!parsed.success) return reject(404, "job_not_found", "작업을 찾을 수 없습니다.");
  const row = await callRow(db, "job_id", parsed.data); if (!row || row.reverted_at || row.reverting_at) return reject(404, "job_not_found", "작업을 찾을 수 없습니다.");
  return { row, job: await loadJob(db, row.job_id, owner) };
}
const uploadState = async (db: Database, job: Job) => ({ chunk_size: CHUNK_BYTES, parts: partCount(job), received: (await listParts(db, job.id)).map(p => p.part), complete: job.upload_complete });

async function createCall(ctx: IntegrationContext, request: Request) {
  const input = await parseJson(request, callRequestSchema), db = ctx.db, owner = await ownerOf(db);
  await clearDeletedCallContacts(db);
  const jobId = await callJobId(input.external_ref), now = Date.now(), ext = AUDIO_EXT.exec(input.file.name)![1].toLowerCase();
  if (!await callRow(db, "external_ref", input.external_ref)) {
    // A new ref: the same recording may not be live twice, and intake per day is capped.
    const dup = await liveDuplicate(db, input.file.sha256); if (dup) duplicateFile(dup.job_id);
    const limit = ctx.dailyLimit ?? DEFAULT_DAILY_CALL_LIMIT, today = await db.prepare("SELECT COUNT(*) AS n FROM integration_calls WHERE created_at>=?").bind(kstDayStart(now)).first<{ n: number }>();
    if ((today?.n ?? 0) >= limit) reject(429, "daily_limit", `오늘 받은 통화가 한도(${limit}건)에 닿았습니다. 내일 다시 보내세요.`);
  }
  let inserted;
  try { inserted = await db.prepare("INSERT INTO integration_calls(external_ref,job_id,created_at,direction,started_at,duration_sec,counterpart_name,phone_e164,crm_contact_id,crm_company_id,file_size,file_sha256,file_mime,file_ext) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(external_ref) DO NOTHING")
    .bind(input.external_ref, jobId, now, input.direction, input.started_at, input.duration_sec, input.counterpart.name, input.counterpart.phone_e164, input.counterpart.crm_contact_id ?? null, input.counterpart.crm_company_id ?? null, input.file.size, input.file.sha256, input.file.mime, ext).run(); }
  catch (e) {
    // Another ref won the race for this recording (partial unique index on live file_sha256).
    if (!/UNIQUE constraint failed: integration_calls\.file_sha256/.test(String((e as Error)?.message))) throw e;
    const dup = await liveDuplicate(db, input.file.sha256); return duplicateFile(dup?.job_id ?? "");
  }
  const created = inserted.meta.changes === 1, row = (await callRow(db, "external_ref", input.external_ref))!;
  if (row.reverted_at || row.reverting_at) reject(409, "call_retired", "이미 되돌린 통화입니다. 새 external_ref로 보내세요.", "external_ref");
  if (!created && (row.file_sha256 !== input.file.sha256 || row.file_size !== input.file.size)) reject(422, "file_mismatch", "같은 external_ref에 다른 녹음 파일입니다.", "file.sha256");
  const deleted = await jobDeletedAt(db, row.job_id);
  if (deleted?.deleted_at != null) reject(409, "call_retired", "소유자가 삭제한 통화입니다. 다시 받지 않습니다.", "external_ref");
  const filled = !created ? await applyFills(db, row, blankFills(row, input)) : [];
  const current = filled.length ? (await callRow(db, "external_ref", input.external_ref))! : row;
  if (!deleted) await db.prepare("INSERT OR IGNORE INTO jobs(id,owner,state,revision,generation,data,created_at,updated_at) VALUES(?,?,'uploading',1,1,?,?,?)").bind(current.job_id, owner, JSON.stringify(callData(current, current.job_id)), now, now).run();
  const job = await loadJob(db, row.job_id, owner);
  return json({ id: job.id, job_id: job.id, created, conflicts: created ? [] : conflictsOf(current, input), filled, state: job.state, upload: await uploadState(db, job) });
}

function callStatus(row: CallRow, job: Job) {
  return { id: job.id, job_id: job.id, external_ref: row.external_ref, state: job.state, stage: job.stage, upload_complete: job.upload_complete,
    transcript_ready: job.state === "transcript_ready", transcript_revision: job.transcript_revision, segments: job.segments.length, duration: job.duration ?? null,
    ...(job.error ? { error: { code: job.error.code, retryable: job.error.retryable } } : {}) };
}

// Revert (contract 0-6): a call made through this door is deleted by the app's ordinary deletion rule.
// Not when the owner has edited it (changed_by_person) or even opened it (viewed_by_owner). Before deleting, the
// row is marked `reverting_at`; a retry after an interruption finishes the job and answers reverted:true.
async function revert(ctx: IntegrationContext, request: Request) {
  const input = await parseJson(request, revertSchema), db = ctx.db, owner = await ownerOf(db);
  await clearDeletedCallContacts(db);
  const row = await callRow(db, "external_ref", input.external_ref);
  if (!row) return json({ error: "그 external_ref로 만든 것이 없습니다." }, 404);
  if (row.reverted_at) return json({ reverted: false, reason: "already_reverted" });
  const done = async () => { await db.prepare("UPDATE integration_calls SET reverted_at=?,phone_e164=NULL,counterpart_name=NULL WHERE external_ref=? AND reverted_at IS NULL").bind(Date.now(), row.external_ref).run(); return json({ reverted: true, reason: "deleted" }); };
  const state = await db.prepare("SELECT deleted_at,generation FROM jobs WHERE id=?").bind(row.job_id).first<{ deleted_at: number | null; generation: number }>();
  if (!state) return await done(); // the job row was never written (crash between the two inserts)
  if (row.reverting_at) {
    if (state.deleted_at != null) { await finishJobRemoval(db, ctx.bucket, row.job_id, owner, state.generation); return await done(); }
    // The job is still there: the owner may have opened or edited it since the interrupted attempt. Check again;
    // if so, drop the marker so the call is an ordinary live call again.
    const pending = await loadJob(db, row.job_id, owner), refusal = pending.owner_edited_at ? "changed_by_person" : pending.call?.owner_viewed_at ? "viewed_by_owner" : null;
    if (refusal) { await db.prepare("UPDATE integration_calls SET reverting_at=NULL WHERE external_ref=? AND reverted_at IS NULL").bind(row.external_ref).run(); return json({ reverted: false, reason: refusal }); }
    await removeJob(db, ctx.bucket, pending, owner);
    return await done();
  }
  if (state.deleted_at != null) return json({ reverted: false, reason: "already_deleted" });
  const job = await loadJob(db, row.job_id, owner);
  if (job.owner_edited_at) return json({ reverted: false, reason: "changed_by_person" });
  if (job.call?.owner_viewed_at) return json({ reverted: false, reason: "viewed_by_owner" });
  await db.prepare("UPDATE integration_calls SET reverting_at=? WHERE external_ref=? AND reverting_at IS NULL").bind(Date.now(), row.external_ref).run();
  await removeJob(db, ctx.bucket, job, owner);
  return await done();
}

export async function integrationRoute(ctx: IntegrationContext, request: Request, path: string[]) {
  try {
    // Off unless configured; through the public tunnel it looks like any unknown path, even with a valid token.
    if (!ctx.token || !fromLoopbackOnly(request.headers)) return notFound();
    if (ctx.token.length < MIN_TOKEN_LENGTH) return json({ error: "통합 설정을 확인하세요." }, 503);
    if (!await bearerMatches(ctx.token, request.headers.get("authorization"))) return Response.json({ error: "인증이 필요합니다." }, { status: 401, headers: { ...headers, "WWW-Authenticate": "Bearer" } });
    if (path[0] !== "v1") return notFound();
    const [resource, id, action, extra] = path.slice(1), method = request.method;
    if (resource === "revert" && !id && method === "POST") return await revert(ctx, request);
    if (resource !== "calls") return notFound();
    if (!id && method === "POST") return await createCall(ctx, request);
    if (!id) return notFound();
    const owner = await ownerOf(ctx.db), { row, job } = await liveCallJob(ctx.db, id, owner);
    if (!action && method === "GET") return json(callStatus(row, job));
    if (action !== "uploads") return notFound();
    if (!extra && method === "GET") return json(await uploadState(ctx.db, job));
    if (extra === "complete" && method === "POST") {
      const input = await parseJson(request, completeSchema);
      if (input.sha256 !== row.file_sha256) reject(422, "file_checksum", "POST /calls에서 알린 sha256과 다릅니다.", "sha256");
      const done = await finishUpload(ctx.db, ctx.bucket, job, input);
      return json({ id: done.id, job_id: done.id, created: false, conflicts: [], state: done.state, upload_complete: done.upload_complete });
    }
    if (extra && extra !== "complete" && method === "PUT")
      return json(await storePart(ctx.db, ctx.bucket, job, owner, extra, { contentLength: request.headers.get("content-length"), sha256: request.headers.get("x-chunk-sha256"), read: max => boundedBody(request, max) }));
    return notFound();
  } catch (error) { return respondError(error, `${request.method} /api/integration/${path.map(p => /^[0-9a-f-]{36}$/i.test(p) ? ":id" : p).join("/")}`); }
}
