import {z} from "zod";
import {metadataSuggestionsSchema,transcriptSuggestionItemSchema,type Metadata,type MetadataSuggestions,type Segment,type TranscriptSuggestionItem} from "./contracts.ts";

// Suggestions for empty meeting-info fields: the recorder's file-name timestamp, speaker
// names the owner typed in transcript review, and (server only) values a text model read
// from the transcript, each re-checked here against its quote. They stay suggestions
// until the owner saves the meeting information.
export type SuggestionSource="filename"|"speakers"|"transcript";
export type SuggestedKey="datetime"|"attendees"|"organization"|"place"|"meeting_format";
export interface MetadataSuggestion {key:SuggestedKey;value:string;source:SuggestionSource;quote?:string;segment_ids?:string[];
 /** The transcript revision the server read, when it is older than the current one. */ from_revision?:number;}
export const suggestionSourceLabels:Record<SuggestionSource,string>={filename:"파일 이름에서 추정",speakers:"녹취 화자 이름에서 추정",transcript:"녹취 대화에서 추정"};
export const MEETING_FORMATS=["오프라인","온라인","하이브리드"] as const;

// Close to the worker's normalisation (NFC, collapsed whitespace, case-insensitive containment).
// Not identical for a few non-Korean characters (Python casefold and split vs JS toLowerCase and \s);
// a mismatch can only drop an item here, never accept one the worker refused.
const clean=(text:string)=>text.normalize("NFC").split(/\s+/).filter(Boolean).join(" ");
const contains=(haystack:string,needle:string)=>!!needle&&haystack.toLowerCase().includes(needle.toLowerCase());

export const MAX_ATTENDEE_SUGGESTIONS=20; // Mirrors MAX_ATTENDEES in local_worker/meta_inference.py.
// What `metadataSchema` can store, so an accepted suggestion can always be saved.
const VALUE_LIMITS={organization:150,place:200,meeting_format:80,attendee:200} as const;
const looseSuggestions=metadataSuggestionsSchema.extend({items:z.array(z.unknown()).max(25)});

/** Re-checks worker suggestions against the transcript they came with. A failing item is dropped,
 *  a malformed field becomes undefined: neither may reject the transcript result it rides on. */
export function checkTranscriptSuggestions(input:unknown,segments:readonly Pick<Segment,"segment_id"|"text">[],revision:number):MetadataSuggestions|undefined{
 const top=looseSuggestions.safeParse(input);if(!top.success)return undefined;
 const raw=new Map(segments.map(s=>[s.segment_id,s.text])),cleaned=new Map<string,string>();
 const text=(id:string)=>{const value=raw.get(id);if(value===undefined)return undefined;if(!cleaned.has(id))cleaned.set(id,clean(value));return cleaned.get(id)!;};
 const items:TranscriptSuggestionItem[]=[];
 for(const candidate of top.data.status==="ok"?top.data.items:[]){
  const parsed=transcriptSuggestionItemSchema.safeParse(candidate);if(!parsed.success)continue;
  const item=parsed.data,value=clean(item.value),quote=clean(item.quote),cited=item.segment_ids.map(text);
  if(!value||!quote||value.length>VALUE_LIMITS[item.key]||cited.some(t=>t===undefined)||!cited.some(t=>contains(t!,quote)))continue;
  if(item.key==="meeting_format"){if(!(MEETING_FORMATS as readonly string[]).includes(value))continue;}
  else if(item.key==="attendee"){
   // Name, role and company must all be in the quote, not only the first word.
   const name=clean(item.name??value.split(" (")[0]);
   if(!contains(quote,name)||!value.replace(/[()]/g," ").split(" ").filter(Boolean).every(word=>contains(quote,word)))continue;
   if(items.filter(x=>x.key==="attendee").length>=MAX_ATTENDEE_SUGGESTIONS)continue;
   items.push({...item,value,quote,name});continue;
  }
  else if(!contains(quote,value))continue;
  if(items.some(x=>x.key===item.key))continue;
  items.push({...item,value,quote});
 }
 return {...top.data,items,transcript_revision:revision};
}

/** The result route's rule: only a finished meeting-mode transcript carries suggestions. */
export function transcriptSuggestionsFor(input:unknown,result:{state:string;mode:"minutes"|"transcript";segments:readonly Pick<Segment,"segment_id"|"text">[];revision:number}):MetadataSuggestions|undefined{
 if(input===undefined||result.state!=="transcript_ready"||result.mode!=="minutes")return undefined;
 return checkTranscriptSuggestions(input,result.segments,result.revision);
}

/** People the server heard introduce themselves. Offered one by one, never pre-filled. */
export function attendeeCandidates(job:{metadata:Metadata;metadata_suggestions?:MetadataSuggestions;transcript_revision:number;segments:readonly Pick<Segment,"speaker_name">[]}):MetadataSuggestion[]{
 const s=job.metadata_suggestions;if(s?.status!=="ok")return [];
 // Same person = a saved line that is the name or starts with it ("김민수 팀장 (한빛제조)").
 const known=[...job.metadata.attendees.split("\n"),...job.segments.map(x=>x.speaker_name)].map(x=>clean(x).toLowerCase()).filter(Boolean);
 const same=(name:string)=>known.some(line=>line===name||line.startsWith(name+" "));
 return s.items.filter(i=>i.key==="attendee"&&!same(clean(i.name??i.value.split(" (")[0]).toLowerCase()))
  .map(i=>({key:"attendees",value:i.value,source:"transcript",quote:i.quote,segment_ids:i.segment_ids,...(s.transcript_revision!==job.transcript_revision?{from_revision:s.transcript_revision}:{})}));
}

const pad=(n:number)=>String(n).padStart(2,"0");
const PATTERNS:{regex:RegExp;century:boolean}[]=[
  // 2026-11-12 14-00-53, 2026-11-12_14.00, 2026-11-12T14:00
  {regex:/(?<!\d)(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[-:.](\d{2})(?:[-:.](\d{2}))?(?!\d)/,century:true},
  // 20261112_140053, 20261112-1400, 20261112140053
  {regex:/(?<!\d)(\d{4})(\d{2})(\d{2})[ _\-T]?(\d{2})(\d{2})(\d{2})?(?!\d)/,century:true},
  // 음성 261112_140053 (Galaxy voice recorder). The two-digit year form needs a
  // separator and seconds so quote numbers such as 251010-1234 are not dates.
  {regex:/(?<!\d)(\d{2})(\d{2})(\d{2})[ _\-](\d{2})(\d{2})(\d{2})(?!\d)/,century:false},
];

/** Returns the meeting start as "YYYY-MM-DDTHH:MM" (Korea time, form format) or "". */
export function datetimeFromFilename(name:string):string{
  const base=name.replace(/\.[^.]+$/,"");
  for(const {regex,century} of PATTERNS){
    const match=base.match(regex);
    if(!match)continue;
    // GMT20261112-050000 (Zoom) or ...050000Z are UTC, not Korea time. Offer nothing
    // rather than a time that is nine hours off.
    const before=base.slice(0,match.index),after=base.slice((match.index??0)+match[0].length);
    if(/(GMT|UTC)[ _\-]?$/i.test(before)||/^Z(?![a-z])/i.test(after))return "";
    const [year,month,day,hour,minute,second]=match.slice(1).map(v=>v===undefined?0:Number(v));
    const fullYear=century?year:2000+year;
    if(fullYear<2000||fullYear>2099||month<1||month>12||hour>23||minute>59||second>59)continue;
    const daysInMonth=new Date(Date.UTC(fullYear,month,0)).getUTCDate();
    if(day<1||day>daysInMonth)continue;
    return `${fullYear}-${pad(month)}-${pad(day)}T${pad(hour)}:${pad(minute)}`;
  }
  return "";
}

/** Distinct speaker names in order of first speech. Anonymous labels are never used. */
export function attendeesFromSegments(segments:readonly Pick<Segment,"speaker_name"|"start_ms">[]):string{
  const names:string[]=[];
  for(const segment of [...segments].sort((a,b)=>a.start_ms-b.start_ms)){
    const name=segment.speaker_name.trim();
    if(name&&!names.includes(name))names.push(name);
  }
  return names.join("\n");
}

/** Suggestions only for fields the saved metadata leaves empty. */
export function metadataSuggestions(job:{filename:string;metadata:Metadata;segments:readonly Pick<Segment,"speaker_name"|"start_ms">[];metadata_suggestions?:MetadataSuggestions;transcript_revision?:number}):MetadataSuggestion[]{
  const result:MetadataSuggestion[]=[];
  if(!job.metadata.datetime.trim()){const value=datetimeFromFilename(job.filename);if(value)result.push({key:"datetime",value,source:"filename"});}
  if(!job.metadata.attendees.trim()){const value=attendeesFromSegments(job.segments);if(value)result.push({key:"attendees",value,source:"speakers"});}
  const s=job.metadata_suggestions;
  if(s?.status==="ok")for(const key of ["organization","place","meeting_format"] as const){
    const item=s.items.find(i=>i.key===key);
    if(item&&!job.metadata[key].trim())result.push({key,value:item.value,source:"transcript",quote:item.quote,segment_ids:item.segment_ids,
      ...(job.transcript_revision!==undefined&&s.transcript_revision!==job.transcript_revision?{from_revision:s.transcript_revision}:{})});
  }
  return result;
}

/** Fills empty form fields with suggestions; values the owner already typed win. */
export function applySuggestions(form:Metadata,suggestions:readonly MetadataSuggestion[]):Metadata{
  const next={...form};
  for(const s of suggestions)if(!next[s.key].trim())next[s.key]=s.value;
  return next;
}
