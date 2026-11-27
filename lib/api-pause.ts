import type {Database} from "./runtime-types.ts";
export const API_PAUSE_KEY="api_pause";
const messages:Record<string,string>={
 credit_balance_exhausted:"API 제공자가 크레딧 한도 오류를 반환해 음성 처리를 중단했습니다.",
 insufficient_quota:"API 크레딧 또는 사용 한도를 확인해야 하므로 음성 처리를 중단했습니다.",
 billing_limit:"API 결제 한도 오류로 음성 처리를 중단했습니다.",
 ambiguous_429:"API 사용 한도를 안전하게 판별할 수 없어 추가 호출을 중단했습니다.",
 transient_429:"API 사용 제한으로 추가 호출을 중단했습니다.",
 unknown_outcome:"이전 API 요청의 완료 여부가 불명확해 자동 재전송을 중단했습니다.",
 provider_configuration:"API 연결 설정을 확인해야 하므로 음성 처리를 중단했습니다.",
 response_invalid:"API 응답을 확인해야 하므로 추가 호출을 중단했습니다.",
 circuit_storage:"비용 차단 상태를 안전하게 확인할 수 없어 추가 호출을 중단했습니다.",
 api_paused:"API 음성 처리가 안전을 위해 중단되었습니다.",
};
export interface ApiPause {blocked:boolean;block_id:string;reason:string;blocked_at:number;resolved_at?:number;}
type Capabilities=Record<string,string|boolean|number>;
const uuid=(value:unknown):value is string=>typeof value==="string"&&/^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(value);
const reason=(value:unknown)=>typeof value==="string"&&Object.hasOwn(messages,value)?value:"api_paused";
export function decodeApiPause(raw:string|undefined):ApiPause|null{
 if(!raw)return null;
 try{const value=JSON.parse(raw);if(typeof value.blocked!=="boolean"||typeof value.block_id!=="string"||!Number.isFinite(value.blocked_at))throw Error("invalid");return {blocked:value.blocked,block_id:value.block_id,reason:reason(value.reason),blocked_at:value.blocked_at,...(Number.isFinite(value.resolved_at)?{resolved_at:value.resolved_at}:{})};}
 catch{return {blocked:true,block_id:"invalid-stored-state",reason:"circuit_storage",blocked_at:0};}
}
export function nextApiPause(current:ApiPause|null,caps:Capabilities,now=Date.now(),newId=()=>crypto.randomUUID()):ApiPause|null{
 if(caps.api_blocked===true){
  const blockId=uuid(caps.api_block_id)?caps.api_block_id:current?.blocked?current.block_id:newId();
  return {blocked:true,block_id:blockId,reason:reason(caps.api_block_reason),blocked_at:current?.blocked&&current.block_id===blockId?current.blocked_at:now};
 }
 if(current?.blocked&&caps.api_blocked===false&&uuid(caps.api_resolved_block_id)&&caps.api_resolved_block_id===current.block_id)return {...current,blocked:false,resolved_at:now};
 return current;
}
export function publicApiPause(pause:ApiPause|null){return {blocked:pause?.blocked??false,reason:pause?.blocked?pause.reason:null,message:pause?.blocked?`${messages[reason(pause.reason)]} 결제 조건과 이전 요청을 확인한 뒤 운영자가 명시적으로 재개해야 합니다.`:"",balance:"unknown" as const,...(pause?.blocked?{blocked_at:pause.blocked_at}:{})};}
export function publicApiCapabilities(caps:Capabilities){
 const {api_block_id,api_resolved_block_id,api_block_reason,...safe}=caps;void api_block_id;void api_resolved_block_id;
 return {...safe,...(api_block_reason!==undefined?{api_block_reason:reason(api_block_reason)}:{})};
}
export async function loadApiPause(db:Database){return decodeApiPause((await db.prepare("SELECT value FROM settings WHERE key=?").bind(API_PAUSE_KEY).first<{value:string}>())?.value);}
export async function captureApiPause(db:Database,caps:Capabilities,tokenHash:string,now=Date.now(),newId=()=>crypto.randomUUID()){
 for(let attempt=0;attempt<5;attempt++){
  const row=await db.prepare("SELECT value FROM settings WHERE key=?").bind(API_PAUSE_KEY).first<{value:string}>(),current=decodeApiPause(row?.value),next=nextApiPause(current,caps,now,newId);
  if(JSON.stringify(next)===JSON.stringify(current))return current;
  if(!next)return current;
  const value=JSON.stringify(next);
  const guard="EXISTS (SELECT 1 FROM settings AS registered WHERE registered.key='device' AND json_extract(registered.value,'$.token_hash')=? AND (json_extract(registered.value,'$.execution_location')='server' OR json_extract(registered.value,'$.role')='speech_server'))";
  const result=row?
   await db.prepare(`UPDATE settings SET value=? WHERE key=? AND value=? AND ${guard}`).bind(value,API_PAUSE_KEY,row.value,tokenHash).run():
   await db.prepare(`INSERT OR IGNORE INTO settings(key,value) SELECT ?,? WHERE ${guard}`).bind(API_PAUSE_KEY,value,tokenHash).run();
  if(result.meta.changes)return next;
  const registered=await db.prepare("SELECT value FROM settings WHERE key='device'").first<{value:string}>();
  if(!registered||JSON.parse(registered.value).token_hash!==tokenHash)throw Error("api_pause_device_changed");
 }
 throw Error("api_pause_state_conflict");
}
// This guard is part of the atomic lease UPDATE, not only its preceding read.
export const apiPauseClaimGuard=" AND NOT EXISTS (SELECT 1 FROM settings AS api_guard WHERE api_guard.key='api_pause' AND json_extract(api_guard.value,'$.blocked')=1)";
