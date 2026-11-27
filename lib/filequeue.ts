import {MAX_BYTES} from "./contracts.ts";
export type QueueState="waiting"|"checking"|"uploading"|"verifying"|"complete"|"duplicate"|"failed"|"invalid";
export interface QueueFile {name:string;size:number;lastModified:number;}
export interface FileQueueItem<F extends QueueFile=File> {
  id:string;file:F;status:QueueState;progress:number;error?:string;warning?:string;jobId?:string;contentKey?:string;
}
export const fileSignature=(file:QueueFile)=>JSON.stringify([file.name,file.size,file.lastModified]);
export function fileProblem(file:QueueFile,maxBytes=MAX_BYTES):string|undefined{
  if(!Number.isSafeInteger(file.size)||file.size<=0)return "빈 파일은 업로드할 수 없습니다.";
  if(file.size>maxBytes)return "파일별 최대 크기는 512MB입니다.";
  if(file.name.length>255)return "파일 이름은 255자 이내여야 합니다.";
  if(!/\.(m4a|mp3|wav|webm|ogg|mp4|flac|aac)$/i.test(file.name))return "지원하지 않는 형식입니다. M4A, MP3, WAV, WEBM, OGG, MP4, FLAC, AAC 파일을 선택하세요.";
}
export function appendFiles<F extends QueueFile>(current:readonly FileQueueItem<F>[],files:readonly F[],makeId:()=>string){
  // Completed rows belong to a previous attempt. A new selection must recheck
  // their server job (which may have been deleted or cancelled in another tab).
  const seen=new Set(current.filter(row=>row.status!=="complete"&&row.status!=="duplicate").map(row=>row.file));let ignored=0;
  const added:FileQueueItem<F>[]=[];
  for(const file of files){if(seen.has(file)){ignored++;continue;}seen.add(file);const error=fileProblem(file);added.push({id:makeId(),file,status:error?"invalid":"waiting",progress:0,...(error?{error}:{})});}
  const combined=[...current,...added],counts=new Map<string,number>();
  for(const row of combined){const key=fileSignature(row.file);counts.set(key,(counts.get(key)??0)+1);}
  const items=combined.map(row=>({...row,warning:(counts.get(fileSignature(row.file))??0)>1?"이름·크기·수정일이 같은 파일입니다. 내용을 확인해 실제 중복만 제외합니다.":undefined}));
  return {items,added:added.length,ignored};
}
export const queueStateLabels:Record<QueueState,string>={waiting:"대기",checking:"내용 확인 중",uploading:"업로드 중",verifying:"서버 무결성 확인 중",complete:"업로드 완료",duplicate:"동일 내용 · 중복 제외",failed:"업로드 실패",invalid:"파일 확인 필요"};
export function fileSize(bytes:number){return bytes>=1024*1024?`${(bytes/1024/1024).toFixed(1)} MB`:bytes>=1024?`${(bytes/1024).toFixed(1)} KB`:`${bytes} B`;}
export async function sequentialFiles<T>(items:readonly T[],process:(item:T)=>Promise<void>,failed:(item:T,error:unknown)=>void){
  for(const item of items){try{await process(item);}catch(error){failed(item,error);}}
}
