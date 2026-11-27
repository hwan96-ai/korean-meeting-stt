const types:Record<string,string>={m4a:"audio/mp4",mp3:"audio/mpeg",wav:"audio/wav",webm:"audio/webm",ogg:"audio/ogg",mp4:"audio/mp4",flac:"audio/flac",aac:"audio/aac",amr:"audio/amr","3gp":"audio/3gpp","3ga":"audio/3gpp"};
export const audioType=(filename:string)=>types[filename.toLowerCase().split(".").pop()??""]??"application/octet-stream";
export function audioAttachment(filename:string,id:string){
 const extension=filename.toLowerCase().split(".").pop()??"";
 const fallback=`audio-${id.replace(/[^a-zA-Z0-9-]/g,"").slice(0,80)||"original"}.${types[extension]?extension:"bin"}`;
 const safe=filename.toWellFormed().replace(/[\x00-\x1f\x7f/\\]/g,"_").replace(/^\.+/,"").trim()||fallback;
 const encoded=encodeURIComponent(safe).replace(/[!'()*]/g,c=>`%${c.charCodeAt(0).toString(16).toUpperCase()}`);
 return `attachment; filename="${fallback}"; filename*=UTF-8''${encoded}`;
}
export function parseRange(value:string|null,total:number):{start:number;end:number;partial:boolean}|null{
 if(!value)return {start:0,end:total-1,partial:false};const m=/^bytes=(\d*)-(\d*)$/.exec(value);if(!m||(!m[1]&&!m[2]))return null;
 let start:number,end:number;if(!m[1]){const tail=Number(m[2]);if(!Number.isSafeInteger(tail)||tail<=0)return null;start=Math.max(0,total-tail);end=total-1;}else{start=Number(m[1]);end=m[2]?Math.min(Number(m[2]),total-1):total-1;}
 if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||start>end||start>=total)return null;return {start,end,partial:true};
}
