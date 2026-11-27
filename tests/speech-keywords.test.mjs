import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {MAX_KEYWORDS,MAX_KEYWORD_CHARS,MAX_KEYWORDS_TOTAL_CHARS,claimKeywords,keywordsFromText,keywordsProblem,speechKeywordsSchema,storedKeywords} from "../lib/speech-keywords.ts";
import {likelyMostlySilent} from "../lib/silence-hint.ts";

const CR=String.fromCharCode(13),LF=String.fromCharCode(10),NUL=String.fromCharCode(0),ZWSP=String.fromCharCode(0x200b);

test("typed terms become one tidy term per line or comma",()=>{
  assert.deepEqual(keywordsFromText(["  회의록 ","STT, stt,화자   분리","","premium plan"].join(CR+LF)),["회의록","STT","화자 분리","premium plan"]);
  assert.deepEqual(keywordsFromText(""),[]);
  // Decomposed Hangul pasted from another OS is stored composed.
  assert.deepEqual(keywordsFromText("회의록".normalize("NFD")),["회의록"]);
});

test("a list the speech API would reject cannot be saved",()=>{
  assert.equal(keywordsProblem(["회의록","STT"]),"");
  assert.equal(speechKeywordsSchema.safeParse(["회의록","STT","premium plan"]).success,true);
  const cases=[["a<b"],["x>y"],["숨김"+ZWSP+"문자"],["제어"+NUL+"문자"],["가".repeat(MAX_KEYWORD_CHARS+1)],Array.from({length:MAX_KEYWORDS+1},(_,n)=>`용어${n}`),Array.from({length:40},(_,n)=>String(n).padStart(3,"0")+"가".repeat(37))];
  for(const terms of cases){
    assert.notEqual(keywordsProblem(terms),"",JSON.stringify(terms).slice(0,40));
    assert.equal(speechKeywordsSchema.safeParse(terms).success,false,JSON.stringify(terms).slice(0,40));
  }
  assert.ok(cases[6].join("").length>MAX_KEYWORDS_TOTAL_CHARS);
  // The server also refuses what the form would have tidied away.
  for(const terms of [["줄"+LF+"바꿈"],["줄"+CR+"바꿈"],[" 회의록"],["유휴  장비"],[""],["회의록","회의록"],["STT","stt"],[7],"회의록"])
    assert.equal(speechKeywordsSchema.safeParse(terms).success,false,JSON.stringify(terms));
});

test("the web and the worker use the same caps",()=>{
  const worker=readFileSync(new URL("../local_worker/api_speech.py",import.meta.url),"utf8");
  for(const [name,value] of Object.entries({MAX_KEYWORDS,MAX_KEYWORD_CHARS,MAX_KEYWORDS_TOTAL_CHARS}))
    assert.equal(Number(worker.match(new RegExp(String.raw`^${name} = (\d+)$`,"m"))?.[1]),value,name);
});

test("a claim hands terms to speech jobs only",()=>{
  const raw=JSON.stringify(["회의록","STT"]);
  assert.deepEqual(claimKeywords({pending:"transcribe"},raw),["회의록","STT"]);
  assert.deepEqual(claimKeywords({},raw),["회의록","STT"]);
  assert.deepEqual(claimKeywords({pending:"minutes"},raw),[]);
  assert.deepEqual(claimKeywords({pending:"transcribe"},undefined),[]);
});

test("an unreadable stored list means no terms",()=>{
  assert.deepEqual(storedKeywords(JSON.stringify(["회의록","STT"])),["회의록","STT"]);
  for(const raw of [undefined,null,"","not json","{}",JSON.stringify(["a<b"]),JSON.stringify(["회의록",7])])assert.deepEqual(storedKeywords(raw),[]);
});

test("only long clip-range segments with almost no text are marked as possibly silent",()=>{
  const clip=(seconds,text,timing_provenance="audio_clip_bounds")=>({start_ms:60000,end_ms:60000+seconds*1000,text,timing_provenance});
  assert.equal(likelyMostlySilent(clip(29,"이런 게 있는지 좀 알 수 있을 것 같아요")),true);
  assert.equal(likelyMostlySilent(clip(15,"네.")),true);
  // Ordinary speech is about six characters a second.
  assert.equal(likelyMostlySilent(clip(29,"가".repeat(150))),false);
  assert.equal(likelyMostlySilent(clip(12,"가 나 다 라 마 바 사 아 자 차 카 타 파 하 가 나 다 라")),false);
  // Short clips, provider-timed, word-timed and hand-edited ranges are never marked.
  assert.equal(likelyMostlySilent(clip(9,"네.")),false);
  for(const provenance of ["provider_segment","asr_words","asr_segments","manual"])assert.equal(likelyMostlySilent(clip(29,"네.",provenance)),false);
  // A segment saved before timing provenance existed is never marked.
  assert.equal(likelyMostlySilent({start_ms:0,end_ms:29000,text:"네."}),false);
});
