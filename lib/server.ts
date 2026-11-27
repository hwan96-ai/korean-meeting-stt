import { browserIdentity, fixedOwner, publicOrigin, pushKeys, runtimeBucket, runtimeDatabase, speechExecution, workerGateway } from "@/lib/runtime";
import { sendPush } from "./web-push.ts";
import type { JobNotice } from "./push-store.ts";
import { z } from "zod";
import { CHUNK_BYTES, MAX_BYTES, type Job, type JobData, type State } from "./contracts";
import {audioAttachment,audioType,parseRange} from "./media";
import {streamParts} from "./stream-parts";
import {registeredLocation,type WorkerRegistration} from "./processing";
import {loadApiPause,publicApiPause} from "./api-pause";
export {speechExecution};
import { ApiError, fail } from "./api-error.ts";
import { boundedBody, clearDeletedCallContacts, digest, finishUpload, listParts, loadJob, markCallViewed, readSetting, removeJob, removeJobAudio, storePart, unpack, updateJob, writeSetting } from "./job-store.ts";
export { ApiError, fail, boundedBody, digest, unpack };
export function db() { return runtimeDatabase(); }
export function bucket() { return runtimeBucket(); }
export async function requireWorkerGateway(request: Request) { if (!await workerGateway(request)) fail(401,"worker_gateway","PC 서비스 접근 인증이 필요합니다."); }
export function requirePcMode(){if(speechExecution()==="server")fail(409,"server_managed_worker","서버 음성 처리 모드에서는 PC 연결과 해제를 변경할 수 없습니다.");}
export function requireGeneration(){if(speechExecution()==="server")fail(503,"generation_unconfigured","서버의 회의록 문장 생성은 아직 설정되지 않았습니다. 녹취와 TXT 다운로드를 이용하세요.");}
export async function requireApiSpeechAvailable(){const pause=await loadApiPause(db());if(pause?.blocked)fail(409,"api_paused",publicApiPause(pause).message);}
export async function setting(key: string) { return readSetting(db(),key); }
export async function putSetting(key: string, value: string) { await writeSetting(db(),key,value); }
export async function owner(request: Request) {
  const identity = await browserIdentity(request.headers), id = identity?.userId;
  if (!id) fail(401,"sign_in_required","소유자 로그인이 필요합니다.");
  if (!fixedOwner) await db().prepare("INSERT OR IGNORE INTO settings(key,value) VALUES('owner',?)").bind(id).run();
  if (await setting("owner") !== id) fail(403,"owner_only","사이트 소유자만 접근할 수 있습니다.");
  if (request.method !== "GET") csrf(request);
  return id!;
}
export function csrf(request: Request) {
  const origin = request.headers.get("origin");
  if (origin !== publicOrigin(request) || request.headers.get("x-meeting-client") !== "1") fail(403,"csrf","같은 사이트에서 시작한 요청만 허용합니다.");
}
export async function parse<S extends z.ZodTypeAny>(request: Request, schema: S): Promise<z.output<S>> {
  const length = Number(request.headers.get("content-length") ?? 0); if (length > 8*1024*1024) fail(413,"too_large","요청 내용이 너무 큽니다.");
  const raw = new TextDecoder().decode(await boundedBody(request,8*1024*1024));
  let value; try { value=JSON.parse(raw); } catch { return fail(400,"invalid_json","JSON 형식이 아닙니다."); }
  const result=schema.safeParse(value); if (!result.success) return fail(400,"invalid_input",result.error.issues.map(x=>`${x.path.join(".")}: ${x.message}`).join("; ").slice(0,1000)); return result.data;
}
export async function getJob(id: string, user: string, allowDeleted=false) { return loadJob(db(),id,user,allowDeleted); }
// Phone push sender when the VAPID keys are configured (lib/push-store.ts decides whether a change notifies).
export function pushNotice(): JobNotice|undefined { const keys=pushKeys(); return keys?{publicKey:keys.publicKey,send:(target,payload)=>sendPush(target,payload,keys)}:undefined; }
export async function update(j: Job, state: State, extra: Partial<JobData>={}, generation=j.generation, guardLease?:string) { return updateJob(db(),j,state,extra,generation,guardLease,pushNotice()); }
export async function parts(id: string) { return listParts(db(),id); }
export async function cleanupDeletedAudio(id:string){await removeJobAudio(db(),bucket(),id);}
export async function retryCloudDeletes(){const pending=await db().prepare("SELECT value FROM settings WHERE key LIKE 'cloud_delete:%' LIMIT 10").all<{value:string}>();let remaining=0;for(const row of pending.results){try{await cleanupDeletedAudio(row.value);}catch{remaining++;}}return remaining;}
export async function uploadPart(j: Job, user: string, rawIndex: string, request: Request) { return storePart(db(),bucket(),j,user,rawIndex,{contentLength:request.headers.get("content-length"),sha256:request.headers.get("x-chunk-sha256"),read:max=>boundedBody(request,max)}); }
export async function completeUpload(j: Job, input: {sha256:string;part_checksums:string[]}) { return finishUpload(db(),bucket(),j,input); }
export async function deleteJob(j: Job, user: string) { return removeJob(db(),bucket(),j,user); }
export async function markViewed(j: Job) { if(j.kind==="call"&&!j.call?.owner_viewed_at)await markCallViewed(db(),j.id); }
export async function sweepCallContacts() { await clearDeletedCallContacts(db()); }
export async function audioResponse(j: Job, request: Request, attachment=false) {
  if(!j.upload_complete)fail(409,"incomplete_upload","음성 업로드를 먼저 완료하세요.");
  const pp=await parts(j.id); const total=j.byte_size; const range=parseRange(request.headers.get("range"),total);if(!range)return new Response(null,{status:416,headers:{"Content-Range":`bytes */${total}`}});const {start,end}=range,status=range.partial?206:200;
  let position=0;const selected=pp.map(p=>{const a=position;position+=p.bytes;return {...p,a,b:position-1};}).filter(p=>p.b>=start&&p.a<=end);
  const stream=streamParts(selected,async p=>{const object=await bucket().get(p.key,{range:{offset:Math.max(0,start-p.a),length:Math.min(p.b,end)-Math.max(p.a,start)+1}});if(!object)throw Error("missing_audio");return object.body;});
  return new Response(stream,{status,headers:{"Content-Type":audioType(j.filename),"Content-Length":String(end-start+1),"Accept-Ranges":"bytes",...(attachment?{"Content-Disposition":audioAttachment(j.filename,j.id)}:{}),...(status===206?{"Content-Range":`bytes ${start}-${end}/${total}`} : {}),"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Content-Security-Policy":"default-src 'none'"}});
}
export async function device(request:Request) {
  const token=request.headers.get("authorization")?.replace(/^Bearer /,""); if(!token||token.length<32)fail(401,"device_auth","PC 연결 인증이 필요합니다.");
  const stored=await setting("device");if(!stored)return fail(403,"device_unpaired","작업기 인증 설정이 필요합니다.");const d=JSON.parse(stored) as WorkerRegistration&{owner:string};if(d.token_hash!==await digest(new TextEncoder().encode(token!)))fail(403,"device_revoked","작업기 인증이 만료되었거나 폐기되었습니다.");if(d.owner!==await setting("owner"))fail(403,"device_owner","작업기 소유자 설정이 일치하지 않습니다.");if(speechExecution()==="server"&&registeredLocation(d)!=="server")fail(403,"server_worker_required","서버에 등록된 음성 작업기만 접근할 수 있습니다.");return d;
}
export async function workerJob(request:Request,id:string,lease:string,generation:number) {
  const d=await device(request);const j=await getJob(id,d.owner);if(j.lease_id!==lease||(j.lease_until??0)<Date.now()||j.generation!==generation||j.state==="cancelled")fail(409,"stale_lease","작업 임대가 만료되었거나 취소되었습니다.");if((speechExecution()==="server"||registeredLocation(d)==="server")&&j.pending==="minutes")fail(409,"speech_only_worker","서버 음성 작업기는 회의록 생성 작업을 처리하지 않습니다.");return j;
}
export { CHUNK_BYTES, MAX_BYTES };
