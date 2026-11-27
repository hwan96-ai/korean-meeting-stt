// Job storage steps shared by the owner routes and the integration routes (ops/integration-calls).
// Everything takes the database and bucket as arguments so tests can run on SQLite + FileBucket.
import { CHUNK_BYTES, type Job, type JobData, type State } from "./contracts.ts";
import { fail } from "./api-error.ts";
import { Sha256 } from "./sha256.ts";
import { FINISHED_STATES, timingAfter } from "./progress.ts";
import { clearProgress, recordSpeedSample } from "./progress-store.ts";
import { clearNotices, deliverNotice, noticeDue, type JobNotice } from "./push-store.ts";
import type { Bucket, Database } from "./runtime-types.ts";

export type Row = {id:string;owner:string;state:State;revision:number;generation:number;data:string;created_at:number;updated_at:number;lease_id:string|null;lease_until:number|null;deleted_at:number|null};
export const unpack = (r: Row): Job => ({output_mode:"minutes",...JSON.parse(r.data),id:r.id,state:r.state,revision:r.revision,generation:r.generation,created_at:r.created_at,updated_at:r.updated_at,lease_id:r.lease_id??undefined,lease_until:r.lease_until??undefined});
export async function digest(bytes: ArrayBuffer | Uint8Array) { return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as BufferSource))].map(b=>b.toString(16).padStart(2,"0")).join(""); }
export async function boundedBody(request:Request,max:number){const reader=request.body?.getReader();if(!reader)return new Uint8Array();const chunks:Uint8Array[]=[];let size=0;for(;;){const next=await reader.read();if(next.done)break;size+=next.value.length;if(size>max){await reader.cancel();fail(413,"too_large","요청 내용이 너무 큽니다.");}chunks.push(next.value);}const joined=new Uint8Array(size);let offset=0;for(const c of chunks){joined.set(c,offset);offset+=c.length;}return joined;}
export async function readSetting(db: Database, key: string) { return (await db.prepare("SELECT value FROM settings WHERE key=?").bind(key).first<{value:string}>())?.value; }
export async function writeSetting(db: Database, key: string, value: string) { await db.prepare("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(key,value).run(); }
export async function loadJob(db: Database, id: string, user: string, allowDeleted=false) {
  const row=await db.prepare(`SELECT * FROM jobs WHERE id=? AND owner=? ${allowDeleted ? "" : "AND deleted_at IS NULL"}`).bind(id,user).first<Row>();
  if (!row) return fail(404,"job_not_found","작업을 찾을 수 없습니다."); return unpack(row);
}
export function dataOf(j: Job): JobData { const {id,state,revision,generation,created_at,updated_at,lease_id,lease_until,...data}=j; void id;void state;void revision;void generation;void created_at;void updated_at;void lease_id;void lease_until;return data; }
// `notice` sends the phone push when the worker finishes or stops a run (lib/push-store.ts); without it nothing is sent.
export async function updateJob(db: Database, j: Job, state: State, extra: Partial<JobData>={}, generation=j.generation, guardLease?:string, notice?:JobNotice) {
  // `call` (integration call info) is owned by lib/integration.ts: every other writer keeps the stored value, so a
  // blank fill there never gets lost to a worker or owner update that read the job a moment earlier.
  const sql=`UPDATE jobs SET state=?, data=CASE WHEN json_type(data,'$.call')='object' THEN json_set(?,'$.call',json(json_extract(data,'$.call'))) ELSE ? END, revision=revision+1, generation=?, updated_at=?${generation!==j.generation?", lease_id=NULL, lease_until=NULL":""} WHERE id=? AND revision=? AND generation=? AND deleted_at IS NULL${guardLease ? " AND lease_id=? AND lease_until>?" : ""}`;
  const changed=state!==j.state||generation!==j.generation,now=Date.now(),timing=timingAfter(j,state,generation,now);
  // progress is never stored in data (it is attached to responses), so drop any copy the caller carries.
  const body=JSON.stringify({...dataOf(j),...extra,timing,progress:undefined});
  const args: (string|number)[]=[state,body,body,generation,now,j.id,j.revision,j.generation]; if(guardLease) args.push(guardLease,Date.now());
  const r=await db.prepare(sql).bind(...args).run(); if(!r.meta.changes) fail(409,"revision_conflict","다른 변경이 먼저 저장되었습니다. 새로 불러온 뒤 다시 저장하세요.");
  // A state change ends the step the worker was counting; its next heartbeat reports the new one.
  if(changed)await clearProgress(db,j.id);
  if(state!==j.state&&generation===j.generation&&FINISHED_STATES.includes(state))await recordSpeedSample(db,{...j,...extra,state,timing});
  const result={...j,...extra,timing,progress:changed?undefined:j.progress,state,revision:j.revision+1,generation,updated_at:now};
  // After the commit and never awaited: a slow or failing push service must not hold up or fail the worker's result.
  if(notice&&noticeDue(j,state,generation,guardLease)){const delivery=deliverNotice(db,result,state,notice).catch(()=>undefined);notice.track?.(delivery);}
  return result;
}
export const partKey=(job:Job,n:number)=>`audio/${job.id}/input/${n}`;
export const partCount=(j:Pick<Job,"byte_size">)=>Math.ceil(j.byte_size/CHUNK_BYTES);
export async function listParts(db: Database, id: string) { return (await db.prepare("SELECT * FROM upload_parts WHERE job_id=? ORDER BY part").bind(id).all<{key:string;part:number;bytes:number;sha256:string}>()).results; }
export async function removeJobAudio(db: Database, bucket: Bucket, id:string){let cursor:string|undefined;do{const listed=await bucket.list({prefix:`audio/${id}/`,cursor});if(listed.objects.length)await bucket.delete(listed.objects.map(o=>o.key));cursor=listed.truncated?listed.cursor:undefined;}while(cursor);await db.prepare("DELETE FROM upload_parts WHERE job_id=?").bind(id).run();await db.prepare("DELETE FROM settings WHERE key=?").bind(`cloud_delete:${id}`).run();}

// One upload part: size, checksum and immutable publication exactly as the owner upload has always done.
export async function storePart(db: Database, bucket: Bucket, j: Job, user: string, rawIndex: string, part: {contentLength: string|null; sha256: string|null; read: (max:number)=>Promise<Uint8Array>}) {
  const id=j.id;
  if(j.state!=="uploading")fail(409,"upload_closed","업로드가 이미 완료되었거나 취소되었습니다.");const n=Number(rawIndex);if(!Number.isInteger(n)||n<0||n>=partCount(j))fail(400,"part_index","잘못된 조각 번호입니다.");
  const expectedSize=Math.min(CHUNK_BYTES,j.byte_size-n*CHUNK_BYTES);if(Number(part.contentLength)!==expectedSize)fail(400,"part_size","조각 크기가 일치하지 않습니다.");
  const content=await part.read(expectedSize);if(content.byteLength!==expectedSize)fail(400,"part_size","조각 크기가 일치하지 않습니다.");const hash=await digest(content);if(hash!==part.sha256)fail(400,"part_checksum","체크섬이 일치하지 않습니다.");const key=partKey(j,n);const stored=await bucket.put(key,content,{onlyIf:{etagDoesNotMatch:"*"},customMetadata:{sha256:hash}});if(!stored){const existing=await bucket.head(key);if(existing?.customMetadata?.sha256!==hash)fail(409,"immutable_part","이미 보존한 음성 조각과 다릅니다. 새 회의로 업로드하세요.");}
  const current=await loadJob(db,id,user,true);if(current.generation!==j.generation||current.state!=="uploading"){await bucket.delete(key);fail(409,"stale_upload","업로드가 취소되었습니다.");}
  const publish=await db.prepare("INSERT INTO upload_parts(key,job_id,part,bytes,sha256) SELECT ?,?,?,?,? FROM jobs WHERE id=? AND owner=? AND generation=? AND state='uploading' AND deleted_at IS NULL ON CONFLICT(key) DO UPDATE SET bytes=excluded.bytes,sha256=excluded.sha256").bind(key,id,n,content.byteLength,hash,id,user,j.generation).run();if(!publish.meta.changes){await bucket.delete(key);fail(409,"stale_upload","업로드가 취소되었습니다.");}return {part:n,sha256:hash};
}

// Finish an upload: every part present, part checksums in order, then the whole-file SHA-256 over the stored bytes.
export async function finishUpload(db: Database, bucket: Bucket, j: Job, input: {sha256:string;part_checksums:string[]}) {
  if(j.state==="cancelled")fail(409,"upload_cancelled","취소된 업로드입니다.");if(j.upload_complete)return j;if(j.state!=="uploading")fail(409,"upload_closed","업로드가 닫혔습니다.");
  const pp=await listParts(db,j.id);if(pp.length!==partCount(j)||pp.reduce((a,p)=>a+p.bytes,0)!==j.byte_size||pp.some((p,n)=>p.part!==n||p.sha256!==input.part_checksums[n]))fail(400,"incomplete_upload","파일 크기 또는 조각 체크섬이 일치하지 않습니다.");
  const whole=await wholeHash(bucket,pp);if(whole!==input.sha256)fail(400,"file_checksum","전체 파일 체크섬이 일치하지 않습니다.");
  return await updateJob(db,j,"waiting_worker",{...input,upload_complete:true,stage:"queued",pending:"transcribe"});
}
async function wholeHash(bucket: Bucket, pp:{key:string}[]) {
  // Streaming hash: the whole file is never held in memory.
  const whole=new Sha256();
  for(const p of pp){const object=await bucket.get(p.key);if(!object)fail(400,"missing_part","업로드 조각이 누락되었습니다.");const reader=object!.body.getReader();for(;;){const c=await reader.read();if(c.done)break;whole.update(c.value);}}
  return whole.hex();
}

// The app's one deletion rule: wipe the job data, drop revisions, delete stored audio (retried later if it fails)
// and leave a marker so the worker deletes its own copy. The jobs UPDATE also fires the migration-0001 trigger
// that clears the call's name and number in the same statement.
export async function removeJob(db: Database, bucket: Bucket, j: Job, user: string) {
  const r=await db.prepare("UPDATE jobs SET data='{}',deleted_at=?,state='cancelled',generation=generation+1,revision=revision+1,lease_id=NULL,lease_until=NULL WHERE id=? AND revision=?").bind(Date.now(),j.id,j.revision).run();if(!r.meta.changes)fail(409,"revision_conflict","작업이 변경되었습니다.");
  return finishJobRemoval(db,bucket,j.id,user,j.generation+1);
}
// The steps after the job row is wiped. Each is idempotent, so an interrupted deletion can simply run them again.
export async function finishJobRemoval(db: Database, bucket: Bucket, id: string, user: string, generation: number) {
  await db.prepare("DELETE FROM revisions WHERE job_id=?").bind(id).run();await clearProgress(db,id);await clearNotices(db,id);
  await writeSetting(db,`cloud_delete:${id}`,id);let cloudPending=false;try{await removeJobAudio(db,bucket,id);}catch{cloudPending=true;}await writeSetting(db,`delete:${id}`,JSON.stringify({id,owner:user,generation}));
  return {deleted:true,cloud_delete_pending:cloudPending,local_delete_pending:true};
}
// The owner opened a call job: record it once inside `call` (kept by updateJob like the rest of `call`).
export async function markCallViewed(db: Database, id: string, at=Date.now()) {
  await db.prepare("UPDATE jobs SET data=json_set(data,'$.call.owner_viewed_at',?) WHERE id=? AND deleted_at IS NULL AND json_type(data,'$.call')='object' AND json_extract(data,'$.call.owner_viewed_at') IS NULL").bind(at,id).run();
}
// Backstop for the trigger (and for jobs deleted before it existed): calls of deleted jobs keep no name or number.
// Runs on the worker heartbeat and on integration requests. The Sites recovery DB may lack the table: skip.
export async function clearDeletedCallContacts(db: Database) {
  try{await db.prepare("UPDATE integration_calls SET phone_e164=NULL,counterpart_name=NULL,job_deleted_at=COALESCE(job_deleted_at,(SELECT deleted_at FROM jobs WHERE jobs.id=integration_calls.job_id)) WHERE (phone_e164 IS NOT NULL OR counterpart_name IS NOT NULL OR job_deleted_at IS NULL) AND job_id IN (SELECT id FROM jobs WHERE deleted_at IS NOT NULL)").run();}
  catch(e){if(!/no such table: integration_calls/.test(String((e as Error)?.message)))throw e;}
}
