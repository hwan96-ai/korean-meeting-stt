import type { Segment } from "./contracts";
const singleLine=(value:string)=>value.replace(/[\r\n\u0000-\u001f\u007f]/g," ");
export function transcriptTime(milliseconds:number){
  const ms=Math.max(0,Math.floor(milliseconds));
  const pad=(n:number)=>String(n).padStart(2,"0");
  return `${pad(Math.floor(ms/3600000))}:${pad(Math.floor(ms/60000)%60)}:${pad(Math.floor(ms/1000)%60)}.${String(ms%1000).padStart(3,"0")}`;
}
export function transcriptText(saved:{filename:string;transcript_revision:number;segments:readonly Segment[]}){
  const lines=[`녹취 · 저장 버전 v${saved.transcript_revision}`,`원본: ${singleLine(saved.filename)}`,"시간은 저장된 구간 범위이며 원문과 화자는 검토가 필요할 수 있습니다.",""];
  for(const segment of saved.segments){
    const speaker=singleLine(segment.speaker_name.trim()||segment.local_speaker_id||"미확인 화자");
    lines.push(`[${transcriptTime(segment.start_ms)} → ${transcriptTime(segment.end_ms)}] ${speaker}${segment.overlap?" · 겹침 발화":""}`,segment.text.replace(/\r\n?/g,"\n"),"");
  }
  return "\uFEFF"+lines.join("\n");
}
