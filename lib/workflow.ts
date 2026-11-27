import type { OutputMode } from "./contracts";
export function uploadResumeKey(mode:OutputMode,name:string,size:number,identity:string|number){return `meeting-upload:${mode}:${JSON.stringify([name,size,identity])}`;}
export function legacyUploadResumeKey(name:string,size:number,identity:string|number){return `meeting-upload:${name}:${size}:${identity}`;}
export const recordingMode=(session:{output_mode?:OutputMode}):OutputMode=>session.output_mode??"minutes";
