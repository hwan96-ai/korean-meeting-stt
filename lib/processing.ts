import {publicApiCapabilities} from "./api-pause.ts";
export type SpeechExecution="pc"|"server";
export interface ProcessingConfig {
  speech_execution:SpeechExecution;
  pc_pairing_enabled:boolean;
  minutes_generation:"worker_config"|"unconfigured";
  notion_export:"local_only";
  owner_scoped_downloads:true;
}
export function readSpeechExecution(value:string|undefined):SpeechExecution{
  if(value===undefined||value===""||value==="pc")return "pc";
  if(value==="server")return "server";
  throw new Error("Invalid MEETING_SPEECH_EXECUTION");
}
export function processingConfig(execution:SpeechExecution):ProcessingConfig{
  return {speech_execution:execution,pc_pairing_enabled:execution==="pc",minutes_generation:execution==="server"?"unconfigured":"worker_config",notion_export:"local_only",owner_scoped_downloads:true};
}
export interface WorkerRegistration {token_hash:string;execution_location?:SpeechExecution;role?:string;}
export interface WorkerHeartbeat {last_seen:number;execution_location?:SpeechExecution;capabilities?:Record<string,string|boolean|number>;device_token_hash?:string;}
export const registeredLocation=(device:WorkerRegistration):SpeechExecution=>device.role==="speech_server"||device.execution_location==="server"?"server":"pc";
export function workerConnection(execution:SpeechExecution,heartbeat:WorkerHeartbeat|null,device:WorkerRegistration|null,now=Date.now()){
  const location=heartbeat?.execution_location??(heartbeat?.capabilities?.execution_location==="server"?"server":"pc");
  const age=heartbeat&&Number.isFinite(heartbeat.last_seen)?now-heartbeat.last_seen:null;
  const fresh=age!==null&&age>=0&&age<=45000;
  const sameDevice=!!device&&(heartbeat?.device_token_hash===device.token_hash||(execution==="pc"&&!heartbeat?.device_token_hash));
  const matches=!!device&&registeredLocation(device)===execution&&location===execution&&sameDevice;
  return {execution_location:execution,reported_location:heartbeat?location:null,last_seen:heartbeat?.last_seen??null,fresh,connected:!!heartbeat&&fresh&&matches,state:!heartbeat?"awaiting_heartbeat":!matches?"wrong_worker":!fresh?"stale":"connected"};
}
export function publicHeartbeat(heartbeat:WorkerHeartbeat|null){return heartbeat?{last_seen:heartbeat.last_seen,execution_location:heartbeat.execution_location??(heartbeat.capabilities?.execution_location==="server"?"server":"pc"),capabilities:publicApiCapabilities(heartbeat.capabilities??{})}:null;}
export const speechClaimFilter=(speechOnly:boolean)=>speechOnly?" AND COALESCE(json_extract(jobs.data,'$.pending'),'transcribe')<>'minutes'":"";
