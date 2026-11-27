import test from "node:test";
import assert from "node:assert/strict";
import {applySuggestions,attendeesFromSegments,datetimeFromFilename,metadataSuggestions} from "../lib/meeting-inference.ts";
import {metadataSchema} from "../lib/contracts.ts";

test("recorder file names give the meeting start in form format",()=>{
  assert.equal(datetimeFromFilename("음성 261112_140053.m4a"),"2026-11-12T14:00");
  assert.equal(datetimeFromFilename("20261112_093015.m4a"),"2026-11-12T09:30");
  assert.equal(datetimeFromFilename("Recording 20261112-1400.mp3"),"2026-11-12T14:00");
  assert.equal(datetimeFromFilename("회의 2026-11-12 14-05-09.wav"),"2026-11-12T14:05");
  assert.equal(datetimeFromFilename("2026-11-12T08:15.webm"),"2026-11-12T08:15");
});

test("names without a complete, valid date and time give nothing",()=>{
  for(const name of ["회의-녹음.webm","고객미팅.m4a","20261112.m4a","261112.m4a","음성 261131_140053.m4a","음성 260229_100000.m4a","20261112_250000.m4a","261112140053.m4a","phone 01012345678.m4a","음성 261112_1400533.m4a","견적서 251010-1234.m4a","회의 900101_1200.m4a","GMT20261112-050000_Recording.m4a","Recording_20261112T050000Z.m4a","UTC 2026-11-12 05-00.m4a"])
    assert.equal(datetimeFromFilename(name),"",name);
  assert.equal(datetimeFromFilename("음성 280229_100000.m4a"),"2028-02-29T10:00");
});

test("attendees come only from speaker names, in order of first speech",()=>{
  const segments=[{speaker_name:"",start_ms:0},{speaker_name:"이수진 과장 (고객사)",start_ms:9000},{speaker_name:"김민수 팀장 (가나소프트)",start_ms:3000},{speaker_name:" 김민수 팀장 (가나소프트) ",start_ms:12000}];
  assert.equal(attendeesFromSegments(segments),"김민수 팀장 (가나소프트)\n이수진 과장 (고객사)");
  assert.equal(attendeesFromSegments([{speaker_name:"",start_ms:0}]),"");
});

test("suggestions skip fields the owner already saved and never override typed values",()=>{
  const empty=metadataSchema.parse({});
  const job={filename:"음성 261112_140053.m4a",metadata:empty,segments:[{speaker_name:"김민수",start_ms:0}]};
  assert.deepEqual(metadataSuggestions(job),[{key:"datetime",value:"2026-11-12T14:00",source:"filename"},{key:"attendees",value:"김민수",source:"speakers"}]);
  assert.deepEqual(metadataSuggestions({...job,metadata:{...empty,datetime:"2026-11-10T10:00:00+09:00",attendees:"직접 입력"}}),[]);
  const form=applySuggestions({...empty,attendees:"사용자 입력"},metadataSuggestions(job));
  assert.equal(form.datetime,"2026-11-12T14:00");
  assert.equal(form.attendees,"사용자 입력");
});

import {attendeeCandidates,checkTranscriptSuggestions} from "../lib/meeting-inference.ts";
import {metadataSuggestionsSchema} from "../lib/contracts.ts";

const seg=(segment_id,text,speaker_name="")=>({segment_id,text,speaker_name,start_ms:0});
const A="11111111-1111-4111-8111-111111111111",B="22222222-2222-4222-8222-222222222222";
const transcript=[seg(A,"안녕하세요,  저는 한빛제조 김민수 팀장입니다."),seg(B,"오늘은 판교 본사 3층 회의실에서 뵙네요.")];
const wire=items=>metadataSuggestionsSchema.parse({status:"ok",model_id:"text-model-x",created_at:1,items});

test("server suggestions keep only items whose quote is in the cited segment",()=>{
  const result=checkTranscriptSuggestions(wire([
    {key:"organization",value:"한빛제조",segment_ids:[A],quote:"저는 한빛제조 김민수"},
    {key:"place",value:"판교 본사",segment_ids:[A],quote:"판교 본사 3층"},
    {key:"meeting_format",value:"전화",segment_ids:[B],quote:"회의실에서 뵙네요"},
    {key:"meeting_format",value:"오프라인",segment_ids:[B],quote:"회의실에서   뵙네요"},
    {key:"attendee",value:"김민수 팀장 (한빛제조)",segment_ids:[A],quote:"저는 한빛제조 김민수 팀장입니다."},
    {key:"attendee",value:"박서연",segment_ids:["33333333-3333-4333-8333-333333333333"],quote:"박서연"},
  ]),transcript,4);
  assert.equal(result.transcript_revision,4);
  assert.deepEqual(result.items.map(i=>[i.key,i.value]),[["organization","한빛제조"],["meeting_format","오프라인"],["attendee","김민수 팀장 (한빛제조)"]]);
  assert.equal(checkTranscriptSuggestions({status:"failed",reason:"meta_unknown_outcome",created_at:1,items:[]},transcript,4).items.length,0);
});

test("the wire schema refuses free provider text and unknown fields",()=>{
  assert.throws(()=>metadataSuggestionsSchema.parse({status:"failed",reason:"Provider said: quota exceeded",created_at:1,items:[]}));
  assert.throws(()=>metadataSuggestionsSchema.parse({status:"ok",created_at:1,items:[],raw:"x"}));
  assert.throws(()=>metadataSuggestionsSchema.parse({status:"ok",created_at:1,items:[{key:"datetime",value:"x",segment_ids:[A],quote:"x"}]}));
});

test("transcript suggestions fill only empty fields, mark an older transcript and never pre-fill attendees",()=>{
  const empty=metadataSchema.parse({});
  const ms={...wire([{key:"organization",value:"한빛제조",segment_ids:[A],quote:"한빛제조"},{key:"attendee",value:"김민수 팀장",name:"김민수",segment_ids:[A],quote:"김민수 팀장"}]),transcript_revision:2};
  const job={filename:"회의.m4a",metadata:empty,segments:transcript,metadata_suggestions:ms,transcript_revision:2};
  assert.deepEqual(metadataSuggestions(job),[{key:"organization",value:"한빛제조",source:"transcript",quote:"한빛제조",segment_ids:[A]}]);
  assert.equal(metadataSuggestions({...job,transcript_revision:3})[0].from_revision,2);
  assert.deepEqual(metadataSuggestions({...job,metadata:{...empty,organization:"직접 입력"}}),[]);
  assert.equal(attendeeCandidates(job).length,1);
  assert.equal(attendeeCandidates({...job,metadata:{...empty,attendees:"김민수 팀장 (한빛제조)"}}).length,0);
  assert.equal(attendeeCandidates({...job,segments:[seg(A,"x","김민수")]}).length,0);
  assert.equal(applySuggestions(empty,metadataSuggestions(job)).organization,"한빛제조");
});

import {transcriptSuggestionsFor} from "../lib/meeting-inference.ts";

test("a bad suggestion never rejects the transcript: oversize items and malformed fields are dropped",()=>{
  const C="44444444-4444-4444-8444-444444444444";
  const emoji="회의실 "+"😀".repeat(296),segs=[seg(C,emoji),...transcript];
  const ok={status:"ok",model_id:"m",created_at:1,items:[{key:"place",value:"회의실",segment_ids:[C],quote:emoji},{key:"organization",value:"한빛제조",segment_ids:[A],quote:"한빛제조"}]};
  const kept=transcriptSuggestionsFor(ok,{state:"transcript_ready",mode:"minutes",segments:segs,revision:2});
  assert.deepEqual(kept.items.map(i=>i.key),["organization"]);
  for(const raw of ["x",{...ok,raw:"x"},{...ok,items:"x"},{...ok,status:"weird"}])
    assert.equal(transcriptSuggestionsFor(raw,{state:"transcript_ready",mode:"minutes",segments:segs,revision:2}),undefined);
  assert.equal(transcriptSuggestionsFor(ok,{state:"transcript_ready",mode:"transcript",segments:segs,revision:2}),undefined);
  assert.equal(transcriptSuggestionsFor(ok,{state:"needs_review",mode:"minutes",segments:segs,revision:2}),undefined);
  assert.equal(transcriptSuggestionsFor(undefined,{state:"transcript_ready",mode:"minutes",segments:segs,revision:2}),undefined);
});

test("suggestions that could not be saved or are not in the quote are dropped",()=>{
  const D="55555555-5555-4555-8555-555555555555",long="가".repeat(151);
  const segs=[seg(D,`저는 ${long} 소속, Acme John Smith 입니다`)];
  const raw={status:"ok",created_at:1,items:[
    {key:"organization",value:long,segment_ids:[D],quote:`저는 ${long} 소속`},
    {key:"attendee",value:"John Smith CEO (InventedCorp)",name:"John Smith",segment_ids:[D],quote:"Acme John Smith"},
    {key:"attendee",value:"John Smith",name:"John Smith",segment_ids:[D],quote:"Acme John Smith"}]};
  const out=transcriptSuggestionsFor(raw,{state:"transcript_ready",mode:"minutes",segments:segs,revision:1});
  assert.deepEqual(out.items.map(i=>i.value),["John Smith"]);
});

test("a saved attendee hides only the same person, not someone sharing a first name",()=>{
  const empty=metadataSchema.parse({});
  const ms={status:"ok",created_at:1,transcript_revision:1,items:[{key:"attendee",value:"John Doe",name:"John Doe",segment_ids:[A],quote:"John Doe"}]};
  const job={filename:"x.m4a",metadata:{...empty,attendees:"John Smith"},segments:transcript,metadata_suggestions:ms,transcript_revision:1};
  assert.equal(attendeeCandidates(job).length,1);
  assert.equal(attendeeCandidates({...job,metadata:{...empty,attendees:"John Doe CTO (Acme)"}}).length,0);
});

test("at most 20 attendee candidates are kept, matching the worker cap",()=>{
  const ids=Array.from({length:25},(_,i)=>`66666666-6666-4666-8666-${String(i).padStart(12,"0")}`);
  const segs=ids.map((id,i)=>seg(id,`저는 참석자${i}입니다`));
  const raw={status:"ok",created_at:1,items:ids.map((id,i)=>({key:"attendee",value:`참석자${i}`,name:`참석자${i}`,segment_ids:[id],quote:`저는 참석자${i}입니다`}))};
  const out=transcriptSuggestionsFor(raw,{state:"transcript_ready",mode:"minutes",segments:segs,revision:1});
  assert.equal(out.items.length,20);
});
