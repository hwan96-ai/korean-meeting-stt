import {CHUNK_BYTES,jobMode,type Job,type Metadata,type OutputMode} from "./contracts.ts";
import {legacyUploadResumeKey,uploadResumeKey} from "./workflow.ts";
export interface UploadSettings {mode:OutputMode;meeting?:{meeting_type:"customer"|"weekly";developer_requests:boolean;metadata:Metadata};}
export interface UploadSource {blob:Blob;name:string;identity:string|number;kind:"file"|"recording";settings:UploadSettings;}
export interface UploadProgress {status:"checking"|"uploading"|"verifying";progress:number;}
export interface UploadResult {jobId:string;contentKey:string;duplicate:boolean;}
interface UploadTransport {
  request<T>(path:string,method?:string,body?:unknown):Promise<T>;
  put(path:string,body:Blob,sha256:string):Promise<void>;
  hash(blob:Blob):Promise<string>;
  storage:Pick<Storage,"getItem"|"setItem"|"removeItem">;
  uuid():string;
  progress(update:UploadProgress):void;
  completed?(contentKey:string):string|undefined;
}
type Part={part:number;sha256:string;};
function collision(error:unknown){return typeof error==="object"&&error!==null&&"code"in error&&["upload_id_retired","upload_cancelled","upload_mode_conflict"].includes(String(error.code));}
export async function uploadFile(source:UploadSource,t:UploadTransport):Promise<UploadResult>{
  const {blob,name,identity,kind,settings}=source,{mode}=settings;
  const meeting=settings.meeting?{...settings.meeting,metadata:{...settings.meeting.metadata}}:undefined;
  t.progress({status:"checking",progress:0});
  // Content is checked before choosing a job. The same hash is reused at completion.
  const whole=await t.hash(blob),key=uploadResumeKey(mode,name,blob.size,`v2:${identity}:${whole}`);
  const duplicate=t.completed?.(key);
  if(duplicate){
    let saved:Job|undefined;
    try{saved=await t.request<Job>(`jobs/${duplicate}`);}catch(error){if(!(typeof error==="object"&&error!==null&&"code"in error&&error.code==="job_not_found"))throw error;}
    if(saved&&saved.upload_complete&&saved.state!=="cancelled"&&saved.sha256===whole&&saved.byte_size===blob.size&&saved.filename===name&&jobMode(saved)===mode){t.storage.setItem(key,duplicate);return {jobId:duplicate,contentKey:key,duplicate:true};}
  }
  const previous=[uploadResumeKey(mode,name,blob.size,`${identity}:${whole}`),uploadResumeKey(mode,name,blob.size,identity),...(mode==="minutes"?[legacyUploadResumeKey(name,blob.size,identity)]:[])];
  let id=t.storage.getItem(key),legacyKey:string|undefined;
  if(!id)for(const candidate of previous){const saved=t.storage.getItem(candidate);if(saved){id=saved;legacyKey=candidate;break;}}
  if(!id)id=t.uuid();
  const input={output_mode:mode,...(mode==="minutes"?meeting:{}),input_kind:kind,filename:name,byte_size:blob.size,mime_type:blob.type||"application/octet-stream"};
  let created:Job|undefined,parts:Part[]=[],checks=new Map<number,string>();
  for(let attempt=0;attempt<2;attempt++){
    // Only a fresh UUID or a previously proven v2 association is persisted
    // before network work. A legacy candidate must not be promoted on failure.
    if(!legacyKey)t.storage.setItem(key,id);
    let mismatched=false;
    try{created=await t.request<Job>("jobs","POST",{...input,upload_id:id});}
    catch(error){if(attempt===0&&collision(error)){id=t.uuid();legacyKey=undefined;continue;}throw error;}
    if(created.id!==id)throw Error("업로드 작업 식별자가 일치하지 않습니다.");
    if(jobMode(created)!==mode||created.byte_size!==blob.size||created.filename!==name||created.state==="cancelled")mismatched=true;
    else if(created.upload_complete)mismatched=created.sha256!==whole;
    else{
      parts=(await t.request<{parts:Part[]}>(`jobs/${id}/uploads`)).parts;
      // Old metadata-only resume keys are candidates, never proof of identical content.
      // Check every saved part before writing anything to an existing job.
      checks=new Map();
      for(const part of parts){
        if(!Number.isInteger(part.part)||part.part<0||part.part>=Math.ceil(blob.size/CHUNK_BYTES)){mismatched=true;break;}
        const hash=await t.hash(blob.slice(part.part*CHUNK_BYTES,(part.part+1)*CHUNK_BYTES));checks.set(part.part,hash);
        if(hash!==part.sha256){mismatched=true;break;}
      }
      // A legacy metadata-only key cannot prove the contents of missing parts.
      // Equal saved prefixes do not justify attaching a different file's tail
      // to that old job. Migrate incomplete legacy candidates to a fresh job;
      // content-bound jobs created by this uploader retain normal part resume.
      if(legacyKey&&checks.size!==Math.ceil(blob.size/CHUNK_BYTES))mismatched=true;
    }
    if(!mismatched){t.storage.setItem(key,id);break;}
    if(attempt===1)throw Error("이전 업로드와 파일 내용이 다릅니다. 재개 정보를 안전하게 확인하지 못했습니다.");
    id=t.uuid();legacyKey=undefined;created=undefined;parts=[];checks=new Map();
  }
  if(!created)throw Error("업로드 작업을 시작하지 못했습니다.");
  if(!created.upload_complete){
    const checksums:string[]=[];t.progress({status:"uploading",progress:0});
    for(let n=0;n<Math.ceil(blob.size/CHUNK_BYTES);n++){
      const part=blob.slice(n*CHUNK_BYTES,(n+1)*CHUNK_BYTES),sha256=checks.get(n)??await t.hash(part);checksums.push(sha256);
      if(!parts.some(p=>p.part===n&&p.sha256===sha256))await t.put(`jobs/${id}/uploads/${n}`,part,sha256);
      t.progress({status:"uploading",progress:Math.round(Math.min(blob.size,(n+1)*CHUNK_BYTES)/blob.size*100)});
    }
    t.progress({status:"verifying",progress:100});
    created=await t.request<Job>(`jobs/${id}/uploads/complete`,"POST",{sha256:whole,part_checksums:checksums});
    if(!created.upload_complete||created.sha256!==whole||created.id!==id||jobMode(created)!==mode)throw Error("서버의 업로드 완료 확인이 일치하지 않습니다.");
  }
  // A file batch may be reselected after refresh. Retain its content-bound job
  // association so already completed files are verified, not uploaded twice.
  // Recording keeps the existing single-submit cleanup behavior.
  try{if(kind==="recording")t.storage.removeItem(key);if(legacyKey&&t.storage.getItem(legacyKey)===id)t.storage.removeItem(legacyKey);}catch{/* Completed server data remains authoritative if local cleanup is unavailable. */}
  return {jobId:id,contentKey:key,duplicate:false};
}
