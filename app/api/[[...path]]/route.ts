import { z } from "zod";
import { createJobSchema, idSchema, jobMode, metadataSchema, outputModeSchema, segmentsSchema, TEMPLATE_VERSION, type Job, type JobData } from "@/lib/contracts";
import { ApiError, audioResponse, bucket, CHUNK_BYTES, completeUpload, db, deleteJob, device, digest, fail, getJob, markViewed, owner, parse, parts, putSetting, requireApiSpeechAvailable, requireGeneration, requirePcMode, requireWorkerGateway, retryCloudDeletes, setting, speechExecution, sweepCallContacts, unpack, update, uploadPart, workerJob } from "@/lib/server";
import { integrationRoute } from "@/lib/integration";
import { integrationDailyLimit, integrationToken, pushKeys } from "@/lib/runtime";
import { readSubscriptions, removeSubscription, saveSubscription } from "@/lib/push-store";
import { allowedEndpoint, b64uDecode, isP256Public, sendPush } from "@/lib/web-push";
import { transcriptText } from "@/lib/transcript-export";
import {processingConfig,publicHeartbeat,registeredLocation,speechClaimFilter,workerConnection,type WorkerHeartbeat,type WorkerRegistration} from "@/lib/processing";
import {apiPauseClaimGuard,captureApiPause,loadApiPause,publicApiPause} from "@/lib/api-pause";
import {transcriptSuggestionsFor} from "@/lib/meeting-inference";
import {SPEECH_KEYWORDS_SETTING,claimKeywords,speechKeywordsSchema,storedKeywords} from "@/lib/speech-keywords";
import {ACTIVE_STATES,PROGRESS_STAGES,timingAfterClaim,type StoredProgress} from "@/lib/progress";
import {clearProgress,leaseProgress,queueAhead,readProgress,readSpeedSamples,saveProgress} from "@/lib/progress-store";

export const dynamic = "force-dynamic";
const json=(value:unknown,status=200)=>Response.json(value,{status,headers:{"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"}});
const version=z.number().int().positive();
const leaseFields={lease_id:idSchema,generation:version};
const SPEECH_STATES:readonly string[]=ACTIVE_STATES.filter(s=>s!=="generating");
const sha=z.string().regex(/^[a-f0-9]{64}$/);
async function handle(request:Request) {
  try {
    const path=new URL(request.url).pathname.replace(/^\/api\/?/,"").split("/").filter(Boolean);
    const method=request.method;
    // Orchestrator door: loopback + its own bearer token, checked before any owner or worker authentication.
    if(path[0]==="integration")return await integrationRoute({get db(){return db();},get bucket(){return bucket();},token:integrationToken(),dailyLimit:integrationDailyLimit()},request,path.slice(1));
    if(path[0]==="worker") { await requireWorkerGateway(request); return await workerRoute(request,path.slice(1)); }
    const user=await owner(request);
    if(path[0]==="config"&&method==="GET")return json(processingConfig(speechExecution()));
    if(path[0]==="session"&&method==="GET") {
      const rawHeartbeat=await setting("heartbeat"),rawDevice=await setting("device"),heartbeat:WorkerHeartbeat|null=rawHeartbeat?JSON.parse(rawHeartbeat):null,registration:WorkerRegistration|null=rawDevice?JSON.parse(rawDevice):null,now=Date.now(),execution=speechExecution();
      return json({owner:user,config:processingConfig(execution),server_time:now,progress_samples:await readSpeedSamples(db()),worker:publicHeartbeat(heartbeat),worker_status:workerConnection(execution,heartbeat,registration,now),api_status:publicApiPause(await loadApiPause(db())),cloud_deletions_pending:await retryCloudDeletes(),limits:{max_bytes:512*1024*1024,chunk_bytes:CHUNK_BYTES,max_duration_seconds:14400},subscription_speech_supported:false,notion_export:"local_only"});
    }
    if(path[0]==="push")return await pushRoute(request,path.slice(1));
    if(path[0]==="pair"&&method==="POST") {
      requirePcMode();
      await parse(request,z.object({}).strict());const code=crypto.randomUUID().replaceAll("-","").slice(0,12).toUpperCase();await putSetting("pair",JSON.stringify({hash:await digest(new TextEncoder().encode(code)),owner:user,expires:Date.now()+300000}));return json({code,expires_at:Date.now()+300000});
    }
    if(path[0]==="device"&&method==="DELETE") {requirePcMode();await putSetting("device","");return json({revoked:true});}
    if(path[0]==="profiles"&&method==="GET")return json(JSON.parse(await setting("profiles")??"[]"));
    if(path[0]==="profiles"&&path[1]&&method==="DELETE"){requirePcMode();const person=idSchema.parse(path[1]);await putSetting(`profile_delete:${person}`,JSON.stringify({person_id:person}));return json({local_delete_pending:true});}
    if(path[0]==="speech-keywords"&&!path[1]&&method==="GET")return json({keywords:storedKeywords(await setting(SPEECH_KEYWORDS_SETTING))});
    if(path[0]==="speech-keywords"&&!path[1]&&method==="PUT"){const input=await parse(request,z.object({keywords:speechKeywordsSchema}).strict());await putSetting(SPEECH_KEYWORDS_SETTING,JSON.stringify(input.keywords));return json({keywords:input.keywords});}
    if(path[0]!=="jobs") return fail(404,"not_found","경로를 찾을 수 없습니다.");
    if(!path[1]) {
      if(method==="GET") {const mode=outputModeSchema.parse(new URL(request.url).searchParams.get("output_mode")??"minutes");const r=await db().prepare("SELECT id,state,created_at,json_extract(data,'$.metadata') AS metadata,json_extract(data,'$.filename') AS filename,COALESCE(json_extract(data,'$.output_mode'),'minutes') AS output_mode,json_extract(data,'$.kind') AS kind,json_extract(data,'$.call') AS call,p.value AS progress,lease_id,lease_until FROM jobs LEFT JOIN settings AS p ON p.key='progress:'||jobs.id WHERE owner=? AND deleted_at IS NULL AND COALESCE(json_extract(data,'$.output_mode'),'minutes')=? ORDER BY created_at DESC LIMIT 200").bind(user,mode).all<{id:string;state:string;created_at:number;metadata:string;filename:string;output_mode:string;kind:string|null;call:string|null;progress:string|null;lease_id:string|null;lease_until:number|null}>();return json(r.results.map(({kind,call,progress,lease_id,lease_until,...r})=>{const current=progress?leaseProgress(JSON.parse(progress) as StoredProgress,lease_id):undefined;return {...r,metadata:JSON.parse(r.metadata),...(kind?{kind}:{}),...(call?{call:JSON.parse(call)}:{}),...(current?{progress:current,lease_until}:{})};}));}
      if(method==="POST") {
        const input=await parse(request,createJobSchema);const existing=await db().prepare("SELECT * FROM jobs WHERE id=? AND owner=?").bind(input.upload_id,user).first();if(existing){if(existing.deleted_at!==null)fail(409,"upload_id_retired","삭제된 업로드입니다. 새 업로드로 시작하세요.");const previous=unpack(existing as never);if(jobMode(previous)!==input.output_mode)fail(409,"upload_mode_conflict","다른 출력 방식의 작업입니다. 새 업로드로 시작하세요.");if(previous.state==="cancelled"&&!previous.upload_complete)fail(409,"upload_cancelled","취소된 업로드입니다. 새 업로드로 시작하세요.");return json(previous);}
        if(!/\.(m4a|mp3|wav|webm|ogg|mp4|flac|aac)$/i.test(input.filename))fail(400,"audio_format","지원 음성 파일 형식이 아닙니다.");
        const id=input.upload_id;const now=Date.now();const data:JobData={...input,stage:"uploading",transcript_revision:0,segments:[],upload_complete:false};
        await db().prepare("INSERT INTO jobs(id,owner,state,revision,generation,data,created_at,updated_at) VALUES(?,?,'uploading',1,1,?,?,?)").bind(id,user,JSON.stringify(data),now,now).run();return json(await getJob(id,user),201);
      }
    }
    const id=idSchema.parse(path[1]);let j=await getJob(id,user,method==="DELETE");
    // Any owner access to a call job (except deleting it) marks it as seen: the orchestrator may no longer revert it.
    if(method!=="DELETE")await markViewed(j);
    if(path.length===2) {
      if(method==="GET"){const progress=leaseProgress(await readProgress(db(),j.id),j.lease_id);return json({...j,...(progress?{progress}:{}),...(j.state==="waiting_worker"?{queue_ahead:await queueAhead(db(),j,user)}:{})});}
      if(method==="DELETE")return json(await deleteJob(j,user));
    }
    const action=path[2];
    if(jobMode(j)==="transcript"&&["minutes","download","export-bundle","metadata"].includes(action))fail(409,"transcript_only_job","STT 작업은 녹취만 출력합니다.");
    if(action==="uploads") {
      if(method==="GET")return json({parts:await parts(id),chunk_bytes:CHUNK_BYTES});
      if(path[3]==="complete"&&method==="POST") {const input=await parse(request,z.object({sha256:sha,part_checksums:z.array(sha).min(1).max(128)}).strict());return json(await completeUpload(j,input));}
      if(path[3]&&method==="PUT")return json(await uploadPart(j,user,path[3],request));
    }
    if(action==="audio"&&method==="GET")return await audioResponse(j,request);
    if(action==="original"&&method==="GET")return await audioResponse(j,request,true);
    if(action==="transcript.txt"&&method==="GET") {if(!j.transcript_revision||!j.segments.length)fail(409,"no_transcript","저장된 녹취 결과가 아직 없습니다.");const revision=new URL(request.url).searchParams.get("revision");if(revision!==null&&revision!==String(j.transcript_revision))fail(409,"transcript_conflict","저장된 녹취 버전이 변경되었습니다. 새로 불러온 뒤 내려받으세요.");return new Response(transcriptText(j),{headers:{"Content-Type":"text/plain; charset=utf-8","Content-Disposition":`attachment; filename="transcript-${id}-v${j.transcript_revision}.txt"`,"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","X-Transcript-Revision":String(j.transcript_revision)}});}
    if(action==="export-bundle"&&method==="GET")return new Response(JSON.stringify(j,null,2),{headers:{"Content-Type":"application/json; charset=utf-8","Content-Disposition":`attachment; filename="meeting-${id}-reviewed.json"`,"Cache-Control":"private, no-store"}});
    if(action==="revisions"&&method==="GET") {
      if(path[3]&&path[4]){if(!["minutes","transcript"].includes(path[3]))fail(400,"revision_kind","버전 유형을 확인하세요.");const r=await db().prepare("SELECT data FROM revisions WHERE job_id=? AND kind=? AND revision=?").bind(id,path[3],Number(path[4])).first<{data:string}>();if(!r)fail(404,"revision_missing","해당 버전이 없습니다.");return json(JSON.parse(r!.data));}
      const r=await db().prepare("SELECT kind,revision,created_at FROM revisions WHERE job_id=? ORDER BY created_at DESC LIMIT 100").bind(id).all();return json(r.results);
    }
    if(action==="metadata"&&method==="PATCH") {const input=await parse(request,z.object({expected_revision:version,metadata:metadataSchema}).strict());if(input.expected_revision!==j.revision)fail(409,"revision_conflict","메타데이터 버전 충돌입니다.");
        // Speech work does not read the meeting info, so a save while the audio is queued or running keeps its lease,
        // generation and checkpoints. Minutes work does read it, so those jobs still start over below.
        if(j.pending==="transcribe"&&!j.unresolved_external&&(j.state==="waiting_worker"||SPEECH_STATES.includes(j.state)))return json(await update(j,j.state,{metadata:input.metadata,owner_edited_at:Date.now()}));
        const state=j.unresolved_external?"unknown_outcome":j.segments.length?"needs_review":j.upload_complete&&j.pending?"waiting_worker":j.state;return json(await update(j,state,{metadata:input.metadata,owner_edited_at:Date.now(),...(j.segments.length?{pending:undefined}:{})},j.state==="uploading"?j.generation:j.generation+1));}
    if(action==="transcript"&&method==="PATCH") {
      const input=await parse(request,z.object({expected_revision:version,segments:segmentsSchema}).strict());if(input.expected_revision!==j.transcript_revision)fail(409,"transcript_conflict","녹취 버전이 변경되었습니다.");if(!j.upload_complete)fail(409,"no_audio","음성을 먼저 업로드하세요.");
      if(j.duration&&input.segments.some(s=>s.end_ms>Math.ceil(j.duration!*1000)+100))fail(400,"segment_duration","구간이 음성 길이를 벗어납니다.");
      const revision=j.transcript_revision+1;j=await update(j,"transcript_ready",{segments:input.segments,transcript_revision:revision,pending:undefined,error:undefined,profile_requests:undefined,stage:"reviewed",owner_edited_at:Date.now()},j.generation+1);
      await db().prepare("INSERT INTO revisions(key,job_id,kind,revision,data,created_at) SELECT ?,?,'transcript',?,?,? FROM jobs WHERE id=? AND deleted_at IS NULL").bind(`${id}:transcript:${revision}`,id,revision,JSON.stringify(input.segments),Date.now(),id).run();return json(j);
    }
    if(action==="minutes"&&method==="POST") {requireGeneration();const input=await parse(request,z.object({transcript_revision:version}).strict());if(j.state==="unknown_outcome"||j.unresolved_external)fail(409,"unknown_outcome","PC에서 이전 외부 요청의 성공 여부를 확인하고 해소해야 새로 작성할 수 있습니다.");if(j.pending==="transcribe")fail(409,"speech_pending","새 음성 분석이 끝난 뒤 최신 녹취를 검토하고 작성하세요.");if(input.transcript_revision!==j.transcript_revision)fail(409,"transcript_conflict","최신 검토 녹취를 선택하세요.");if(!j.segments.length)fail(400,"empty_transcript","검토한 녹취가 필요합니다.");if(j.segments.some(s=>s.review_status==="unreviewed"))fail(400,"review_required","모든 녹취 구간을 검토 완료 또는 불확실로 표시하고 저장하세요.");return json(await update(j,"waiting_worker",{pending:"minutes",stage:"queued_minutes",error:undefined},j.generation+1));}
    if(action==="minutes"&&method==="PATCH") {const input=await parse(request,z.object({expected_revision:version,content:z.string().min(1).max(250000)}).strict());if(!j.draft||input.expected_revision!==j.draft.revision)fail(409,"draft_conflict","회의록 버전이 변경되었습니다.");const draft={...j.draft!,revision:j.draft!.revision+1,content:input.content};j=await update(j,"minutes_ready",{draft,pending:undefined,owner_edited_at:Date.now()},j.generation+1);await db().prepare("INSERT INTO revisions(key,job_id,kind,revision,data,created_at) SELECT ?,?,'minutes',?,?,? FROM jobs WHERE id=? AND deleted_at IS NULL").bind(`${id}:minutes:${draft.revision}`,id,draft.revision,JSON.stringify(draft),Date.now(),id).run();return json(j);}
    if(action==="download"&&method==="GET") {if(!j.draft)fail(404,"no_draft","회의록이 아직 없습니다.");return new Response(j.draft!.content,{headers:{"Content-Type":"text/markdown; charset=utf-8","Content-Disposition":`attachment; filename="meeting-${id}.md"`,"Cache-Control":"private, no-store"}});}
    if(action==="cancel"&&method==="POST") {await parse(request,z.object({}).strict());return json(await update(j,"cancelled",{pending:undefined,stage:"cancelled"},j.generation+1));}
    if(action==="reprocess-speech"&&method==="POST") {const input=await parse(request,z.object({expected_revision:version}).strict());await requireApiSpeechAvailable();if(input.expected_revision!==j.revision)fail(409,"revision_conflict","작업이 변경되었습니다. 최신 작업을 불러온 뒤 재분석하세요.");if(j.state==="unknown_outcome"||j.unresolved_external)fail(409,"unknown_outcome","이전 외부 요청을 PC에서 확인하기 전에는 음성을 재분석할 수 없습니다.");if(!j.upload_complete)fail(409,"incomplete_upload","음성 업로드를 먼저 완료하세요.");if(!["transcript_ready","needs_review","minutes_ready","failed","needs_setup","cancelled"].includes(j.state)||(j.lease_until??0)>Date.now())fail(409,"active_job","진행 중인 작업이 끝나거나 취소 상태가 확인된 뒤 재분석하세요.");return json(await update(j,"waiting_worker",{pending:"transcribe",stage:"queued_speech",speech_complete:false,diarization_complete:false,profile_requests:undefined,error:undefined,owner_edited_at:Date.now()},j.generation+1));}
    if(action==="retry"&&method==="POST") {await parse(request,z.object({}).strict());await requireApiSpeechAvailable();if(j.state==="unknown_outcome"||j.unresolved_external)fail(409,"unknown_outcome","로컬 기록에서 외부 요청 결과를 먼저 확인해야 합니다.");if(!j.upload_complete)fail(409,"incomplete_upload","음성 업로드를 완료하세요.");const task=jobMode(j)==="transcript"?"transcribe":j.pending??(j.stage==="generating"||j.error?.stage==="generating"?"minutes":j.speech_complete!==true||j.diarization_complete!==true?"transcribe":j.segments.length?"minutes":"transcribe");if(task==="minutes")requireGeneration();return json(await update(j,"waiting_worker",{pending:task,stage:task==="minutes"?"queued_minutes":"queued",error:undefined},j.generation+1));}
    if(action==="profiles"&&method==="POST") {requirePcMode();const input=await parse(request,z.object({person_id:idSchema.optional(),segment_ids:z.array(idSchema).min(1).max(30),display_name:z.string().min(1).max(100),role:z.string().max(100),company:z.string().max(100),confirmed_single_speaker:z.literal(true)}).strict());if(input.person_id){const people=JSON.parse(await setting("profiles")??"[]") as {person_id:string}[];if(!people.some(p=>p.person_id===input.person_id))fail(400,"profile_missing","기존 화자 프로필을 찾을 수 없습니다.");if(await setting(`profile_delete:${input.person_id}`))fail(409,"profile_delete_pending","삭제 대기 중인 화자입니다.");}if(input.segment_ids.some(s=>!j.segments.find(x=>x.segment_id===s&&x.review_status==="confirmed"&&!x.overlap)))fail(400,"unknown_segment","원음을 듣고 확인한 단독 발화 구간만 등록할 수 있습니다.");if((j.profile_requests?.length??0)>=30)fail(400,"profile_queue_full","기존 화자 등록을 PC에서 완료한 뒤 추가하세요.");const req={...input,request_id:crypto.randomUUID()};return json(await update(j,j.state,{profile_requests:[...(j.profile_requests??[]),req]}));}
    return fail(404,"not_found","경로를 찾을 수 없습니다.");
  } catch(error) {if(error instanceof ApiError)return json({error:{code:error.code,message:error.message}},error.status);if(error instanceof z.ZodError)return json({error:{code:"invalid_input",message:"입력 형식을 확인하세요."}},400);return json({error:{code:"internal_error",message:"처리 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."}},500);}
}

// Phone push for finished jobs (lib/push-store.ts). Owner only, like every route after owner(); off without VAPID keys.
const pushEndpoint=z.string().max(1000).refine(allowedEndpoint,"push service endpoint");
const b64key=(check:(bytes:Uint8Array)=>boolean)=>z.string().max(200).refine(v=>{try{return check(b64uDecode(v));}catch{return false;}});
async function pushRoute(request:Request,path:string[]) {
  const keys=pushKeys(),method=request.method;
  if(!path[0]&&method==="GET")return json({available:!!keys,public_key:keys?.publicKey??null,devices:(await readSubscriptions(db())).length});
  if(!keys)fail(409,"push_unconfigured","서버에 휴대폰 알림이 설정되지 않았습니다.");
  if(path[0]==="subscribe"&&method==="POST"){const input=await parse(request,z.object({endpoint:pushEndpoint,keys:z.object({p256dh:b64key(isP256Public),auth:b64key(b=>b.length===16)}).strict()}).strict());return json({subscribed:true,devices:await saveSubscription(db(),{endpoint:input.endpoint,...input.keys},keys!.publicKey)});}
  if(path[0]==="unsubscribe"&&method==="POST"){const input=await parse(request,z.object({endpoint:z.string().max(1000)}).strict());return json({subscribed:false,devices:await removeSubscription(db(),input.endpoint)});}
  if(path[0]==="test"&&method==="POST"){
    const input=await parse(request,z.object({endpoint:z.string().max(1000)}).strict());const target=(await readSubscriptions(db())).find(s=>s.endpoint===input.endpoint&&s.server_key===keys!.publicKey);if(!target)fail(404,"push_not_subscribed","이 기기는 알림을 켜지 않았습니다.");
    let status=0;try{status=await sendPush(target!,JSON.stringify({title:"회의록",body:"시험 알림입니다. 작업이 끝나면 이렇게 알려 드립니다.",tag:"push-test",url:"/"}),keys!);}catch{/* reported as failed */}
    if(status===404||status===410){await removeSubscription(db(),target!.endpoint);return json({result:"gone"});}
    return json({result:status>=200&&status<300?"sent":"failed",status});
  }
  return fail(404,"not_found","경로를 찾을 수 없습니다.");
}

async function workerRoute(request:Request,path:string[]) {
  if(path[0]==="pair"&&request.method==="POST") {
    requirePcMode();
    const input=await parse(request,z.object({code:z.string().regex(/^[A-F0-9]{12}$/),device_token:z.string().min(40).max(200)}).strict());const raw=await setting("pair");if(!raw)fail(403,"invalid_pair","연결 코드를 다시 만드세요.");const p=JSON.parse(raw!);if(p.expires<Date.now()||p.hash!==await digest(new TextEncoder().encode(input.code)))fail(403,"invalid_pair","연결 코드가 만료되었거나 일치하지 않습니다.");
    const removed=await db().prepare("DELETE FROM settings WHERE key='pair' AND value=?").bind(raw).run();if(!removed.meta.changes)fail(409,"pair_used","이미 사용된 연결 코드입니다.");await putSetting("device",JSON.stringify({owner:p.owner,token_hash:await digest(new TextEncoder().encode(input.device_token))}));return json({paired:true,owner:p.owner});
  }
  const d=await device(request);
  const serverDevice=speechExecution()==="server"||registeredLocation(d)==="server";
  if(serverDevice&&(path[0]==="profiles"||path[0]==="profile-requests"||path[2]==="profile-audio"))fail(409,"profiles_pc_only","화자 기준 음성 관리는 기존 PC에서만 지원합니다.");
  if(path[0]==="heartbeat"&&request.method==="POST") {
    const input=await parse(request,z.object({capabilities:z.record(z.union([z.string().max(300),z.boolean(),z.number()])).default({}),execution_location:z.enum(["pc","server"]).optional(),lease:z.object({job_id:idSchema,...leaseFields}).optional(),progress:z.object({stage:z.enum(PROGRESS_STAGES),done:z.number().int().min(0).max(100000),total:z.number().int().min(0).max(100000)}).strict().refine(p=>p.done<=p.total,"done exceeds total").optional(),deleted_job_ids:z.array(idSchema).max(200).default([]),deleted_profile_ids:z.array(idSchema).max(500).default([])}).strict());
    const location=input.execution_location??(input.capabilities.execution_location==="server"?"server":"pc");if(location!==registeredLocation(d))fail(403,"worker_location_mismatch","등록된 작업기 위치와 응답 위치가 다릅니다.");
    if(serverDevice&&input.deleted_profile_ids.length)fail(409,"profiles_pc_only","서버 작업기는 PC 화자 자료 삭제를 확인할 수 없습니다.");
    const apiPause=serverDevice?await captureApiPause(db(),input.capabilities,d.token_hash):await loadApiPause(db());
    if(input.progress&&!input.lease)fail(400,"progress_without_lease","진행 상황은 임대한 작업에만 보낼 수 있습니다.");
    if(input.lease){const x=input.lease,now=Date.now();await workerJob(request,x.job_id,x.lease_id,x.generation);await db().prepare("UPDATE jobs SET lease_until=? WHERE id=? AND lease_id=? AND generation=? AND deleted_at IS NULL").bind(now+180000,x.job_id,x.lease_id,x.generation).run();
      // Its own settings row: no revision bump (owner saves would conflict) and no rewrite of the transcript JSON.
      if(input.progress)await saveProgress(db(),x.job_id,input.progress,x.lease_id,now);}
    await sweepCallContacts();await putSetting("heartbeat",JSON.stringify({last_seen:Date.now(),execution_location:location,capabilities:input.capabilities,device_token_hash:d.token_hash}));for(const id of input.deleted_job_ids)await db().prepare("DELETE FROM settings WHERE key=?").bind(`delete:${id}`).run();for(const id of input.deleted_profile_ids)await db().prepare("DELETE FROM settings WHERE key=?").bind(`profile_delete:${id}`).run();const deletions=await db().prepare("SELECT value FROM settings WHERE key LIKE 'delete:%'").all<{value:string}>();const profileDeletions=serverDevice?{results:[]}:await db().prepare("SELECT value FROM settings WHERE key LIKE 'profile_delete:%'").all<{value:string}>();return json({ok:true,accepts_progress:true,deletions:deletions.results.map(r=>JSON.parse(r.value)),profile_deletions:profileDeletions.results.map(r=>JSON.parse(r.value)),api_pause:{blocked:apiPause?.blocked??false,block_id:apiPause?.block_id??null}});
  }
  if(path[0]==="claim"&&request.method==="POST") {
    const input=await parse(request,z.object({speech_only:z.boolean().optional()}).strict());const pause=await loadApiPause(db());if(pause?.blocked)return json({job:null,api_blocked:true,api_block_reason:pause.reason});const speechOnly=speechExecution()==="server"||registeredLocation(d)==="server"||input.speech_only===true,filter=speechClaimFilter(speechOnly);const active=await db().prepare("SELECT id FROM jobs WHERE owner=? AND deleted_at IS NULL AND lease_until>? AND state IN ('preparing','transcribing','diarizing','aligning','generating') LIMIT 1").bind(d.owner,Date.now()).first();if(active)return json({job:null});
    const row=await db().prepare(`SELECT * FROM jobs WHERE owner=? AND deleted_at IS NULL AND (state='waiting_worker' OR (state IN ('preparing','transcribing','diarizing','aligning','generating') AND lease_until<?))${filter} ORDER BY created_at LIMIT 1`).bind(d.owner,Date.now()).first();if(!row)return json({job:null});let j=unpack(row as never);if(jobMode(j)==="transcript"&&j.pending==="minutes")fail(409,"transcript_only_job","STT 작업은 회의록을 생성할 수 없습니다.");
    // Read before the lease is taken, so a failed read never strands a leased job.
    const speechKeywords=claimKeywords(j,await setting(SPEECH_KEYWORDS_SETTING));const lease=crypto.randomUUID();
    const claimedState=j.pending==="minutes"?"generating":"preparing",claimedAt=Date.now();
    const r=await db().prepare("UPDATE jobs SET lease_id=?,lease_until=?,state=?,revision=revision+1,updated_at=?,data=json_set(data,'$.timing',json(?)) WHERE id=? AND revision=? AND generation=? AND deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM jobs AS active WHERE active.owner=? AND active.id<>? AND active.deleted_at IS NULL AND active.lease_until>? AND active.state IN ('preparing','transcribing','diarizing','aligning','generating'))"+apiPauseClaimGuard).bind(lease,claimedAt+180000,claimedState,claimedAt,JSON.stringify(timingAfterClaim(j,claimedState,claimedAt)),j.id,j.revision,j.generation,d.owner,j.id,claimedAt).run();if(!r.meta.changes)return json({job:null});await clearProgress(db(),j.id);j=await getJob(j.id,d.owner);
    // Sibling of the job, never stored on it: the worker freezes it once a clip has paid text.
    return json({job:j,speech_keywords:speechKeywords});
  }
  if(path[0]==="profiles"&&request.method==="POST") {const input=await parse(request,z.object({profiles:z.array(z.object({person_id:idSchema,display_name:z.string().max(100),role:z.string().max(100),company:z.string().max(100),profile_revision:version,model_id:z.string().max(200)}).strict()).max(500)}).strict());await putSetting("profiles",JSON.stringify(input.profiles));return json({ok:true});}
  if(path[0]==="profile-requests"&&request.method==="GET") {
    const rows=await db().prepare("SELECT id,generation,json_extract(data,'$.transcript_revision') AS transcript_revision,json_extract(data,'$.profile_requests') AS requests FROM jobs WHERE owner=? AND deleted_at IS NULL AND json_array_length(json_extract(data,'$.profile_requests'))>0 ORDER BY updated_at LIMIT 10").bind(d.owner).all<{id:string;generation:number;transcript_revision:number;requests:string}>();
    const requests=[];for(const row of rows.results){for(const request of JSON.parse(row.requests) as NonNullable<JobData["profile_requests"]>){if(requests.length>=10)break;const placeholders=request.segment_ids.map(()=>"?").join(",");const selected=await db().prepare(`SELECT s.value FROM jobs AS j,json_each(j.data,'$.segments') AS s WHERE j.id=? AND j.owner=? AND j.generation=? AND j.deleted_at IS NULL AND json_extract(s.value,'$.segment_id') IN (${placeholders})`).bind(row.id,d.owner,row.generation,...request.segment_ids).all<{value:string}>();requests.push({job_id:row.id,generation:row.generation,transcript_revision:row.transcript_revision,request,segments:selected.results.map(s=>JSON.parse(s.value))});}}return json({requests});
  }
  if(path[0]==="profile-requests"&&path[1]==="complete"&&request.method==="POST") {
    const input=await parse(request,z.object({job_id:idSchema,generation:version,request_id:idSchema}).strict());const j=await getJob(input.job_id,d.owner);if(j.generation!==input.generation)fail(409,"stale_profile_request","녹취가 변경되었습니다.");await update(j,j.state,{profile_requests:(j.profile_requests??[]).filter(r=>r.request_id!==input.request_id)});return json({ok:true});
  }
  if(path[0]==="jobs"&&path[1]) {
    const id=idSchema.parse(path[1]);
    if(path[2]==="audio"&&request.method==="GET") {const url=new URL(request.url);const j=await workerJob(request,id,url.searchParams.get("lease_id")??"",Number(url.searchParams.get("generation")));return await audioResponse(j,request);}
    if(path[2]==="export-bundle"&&request.method==="GET"){if(speechExecution()==="server"||registeredLocation(d)==="server")fail(409,"speech_only_worker","서버 음성 작업기는 Notion 내보내기를 실행하지 않습니다.");const j=await getJob(id,d.owner);if(jobMode(j)==="transcript")fail(409,"transcript_only_job","STT 작업은 녹취만 출력합니다.");return json(j);}
    if(path[2]==="profile-audio"&&request.method==="GET") {const url=new URL(request.url),j=await getJob(id,d.owner);if(j.generation!==Number(url.searchParams.get("generation"))||!j.profile_requests?.some(r=>r.request_id===url.searchParams.get("request_id")))fail(409,"stale_profile_request","화자 등록 요청이 만료되었습니다.");return await audioResponse(j,request);}
    if(path[2]==="resolve-outcome"&&request.method==="POST") {const input=await parse(request,z.object({generation:version,confirmed_no_result:z.literal(true),request_id:z.string().min(1).max(200)}).strict());const j=await getJob(id,d.owner);if(j.generation!==input.generation)fail(409,"stale_resolution","작업이 변경되었습니다.");if(!j.unresolved_external)fail(409,"no_unknown_outcome","확인할 불명확 요청이 없습니다.");return json(await update(j,"failed",{unresolved_external:undefined,stage:"external_outcome_resolved",error:{code:"confirmed_no_result",message:"PC에서 이전 요청 미완료를 확인했습니다. 명시적으로 다시 시도할 수 있습니다.",retryable:true,stage:j.unresolved_external!.stage}},j.generation+1));}
    if(path[2]==="result"&&request.method==="POST") {
      const input=await parse(request,z.object({...leaseFields,input_revision:z.number().int().min(0),state:z.enum(["preparing","transcribing","diarizing","aligning","needs_setup","needs_review","transcript_ready","minutes_ready","failed","unknown_outcome","rate_limited"]),stage:z.string().max(80),segments:segmentsSchema.optional(),duration:z.number().positive().max(14400).optional(),speech_mode:z.string().max(200).optional(),generation_mode:z.string().max(200).optional(),speech_complete:z.boolean().optional(),diarization_complete:z.boolean().optional(),local_received:z.boolean().optional(),usage:z.unknown().optional(),error:z.object({code:z.string().max(100),message:z.string().max(1000),retryable:z.boolean(),stage:z.string().max(80)}).strict().optional(),draft:z.object({content:z.string().max(250000),model_id:z.string().max(200),generation_mode:z.string().max(200),missing_fields:z.array(z.string()).max(20),template_version:z.literal(TEMPLATE_VERSION)}).strict().optional(),metadata_suggestions:z.unknown().optional()}).strict());
      const j=await workerJob(request,id,input.lease_id,input.generation);if(input.input_revision!==j.transcript_revision)fail(409,"stale_transcript","녹취가 변경되었습니다.");
      if((speechExecution()==="server"||registeredLocation(d)==="server")&&(input.draft||input.state==="minutes_ready"))fail(409,"speech_only_worker","서버 음성 작업기는 회의록 결과를 저장할 수 없습니다.");
      if(jobMode(j)==="transcript"&&(input.draft||input.state==="minutes_ready"))fail(409,"transcript_only_job","STT 작업에는 회의록 결과를 저장할 수 없습니다.");
      if(input.state==="minutes_ready"&&!input.draft)fail(400,"missing_draft","완료한 회의록 내용이 필요합니다.");
      if(input.draft&&input.state!=="minutes_ready")fail(400,"invalid_draft_stage","완료한 회의록만 결과로 저장할 수 있습니다.");
      if(input.segments){const duration=input.duration??j.duration;if(!duration||input.segments.some(s=>s.end_ms>Math.ceil(duration*1000)+100))fail(400,"segment_duration","녹취 구간이 실제 음성 길이를 벗어납니다.");if(!["transcript_ready","needs_review"].includes(input.state))fail(400,"invalid_transcript_stage","완료한 녹취만 결과로 저장할 수 있습니다.");}
      if(input.state==="transcript_ready"&&!(input.segments?.length||j.segments.length))fail(400,"empty_transcript","완료한 녹취 구간이 필요합니다.");
      const {lease_id,generation,input_revision,state,draft,metadata_suggestions,...extra}=input;void generation;void input_revision;
      if(state==="transcript_ready"&&(!input.speech_complete||!input.diarization_complete))fail(400,"incomplete_speech","전사·화자 분석·정렬 완료를 확인해야 합니다.");
      const next={...extra,...(state==="unknown_outcome"?{unresolved_external:{stage:input.stage,input_revision:j.transcript_revision,generation:j.generation}}:{}),...(input.segments?{transcript_revision:j.transcript_revision+1,profile_requests:undefined}:{}),...(draft?{draft:{...draft,revision:(j.draft?.revision??0)+1,transcript_revision:j.transcript_revision}}:{}),...(["transcript_ready","minutes_ready","needs_review"].includes(state)?{pending:undefined}:{}),
        // Suggestions belong to the transcript they arrived with: a new transcript replaces or clears them.
        // The field is parsed apart from the strict schema, so a bad suggestion is dropped, never the transcript.
        ...(input.segments?{metadata_suggestions:transcriptSuggestionsFor(metadata_suggestions,{state,mode:jobMode(j),segments:input.segments,revision:j.transcript_revision+1})}:{})};
      const result=await update(j,state,next,j.generation,lease_id);
      if(input.segments)await db().prepare("INSERT OR IGNORE INTO revisions(key,job_id,kind,revision,data,created_at) SELECT ?,?,'transcript',?,?,? FROM jobs WHERE id=? AND generation=? AND deleted_at IS NULL").bind(`${id}:transcript:${result.transcript_revision}`,id,result.transcript_revision,JSON.stringify(input.segments),Date.now(),id,j.generation).run();
      if(result.draft&&draft)await db().prepare("INSERT OR IGNORE INTO revisions(key,job_id,kind,revision,data,created_at) SELECT ?,?,'minutes',?,?,? FROM jobs WHERE id=? AND generation=? AND deleted_at IS NULL").bind(`${id}:minutes:${result.draft.revision}`,id,result.draft.revision,JSON.stringify(result.draft),Date.now(),id,j.generation).run();
      if(!["preparing","transcribing","diarizing","aligning"].includes(state))await db().prepare("UPDATE jobs SET lease_id=NULL,lease_until=NULL WHERE id=? AND generation=? AND revision=?").bind(j.id,j.generation,result.revision).run();return json(result);
    }
  }
  return fail(404,"not_found","지원하지 않는 PC 작업입니다.");
}
export { handle as GET,handle as POST,handle as PATCH,handle as PUT,handle as DELETE };
