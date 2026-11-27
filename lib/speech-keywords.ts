import {z} from "zod";
import { utf16 } from "./text-schema.ts";

// Terms the owner expects to hear (product names, acronyms). The worker sends them
// to the speech API as `keywords` hints. These caps are application guards, kept
// equal to local_worker/api_speech.py; the provider documents no limit.
export const MAX_KEYWORDS=100;
export const MAX_KEYWORD_CHARS=40;
export const MAX_KEYWORDS_TOTAL_CHARS=1500;
export const SPEECH_KEYWORDS_SETTING="speech_keywords";

// The provider rejects the whole request for <, > or a line break. Control and
// invisible format characters are refused as well.
const FORBIDDEN=/[<>\p{C}]/u;
const fold=(term:string)=>term.toLowerCase();

export const speechKeywordsSchema=z.array(
  utf16(MAX_KEYWORD_CHARS, 1)
    .refine(term=>term===term.trim()&&!/\s{2,}/.test(term)&&!FORBIDDEN.test(term),"용어에 쓸 수 없는 문자가 있습니다."),
).max(MAX_KEYWORDS)
  .refine(terms=>new Set(terms.map(fold)).size===terms.length,"같은 용어가 두 번 있습니다.")
  .refine(terms=>terms.reduce((sum,term)=>sum+term.length,0)<=MAX_KEYWORDS_TOTAL_CHARS,"용어 전체 길이가 너무 깁니다.");

/** One term per line or comma. Spacing is tidied and repeats are dropped. */
export function keywordsFromText(text:string):string[]{
  const terms:string[]=[],seen=new Set<string>();
  for(const raw of text.normalize("NFC").split(/[\n\r,\u2028\u2029]+/)){
    const term=raw.replace(/\s+/g," ").trim();
    if(!term||seen.has(fold(term)))continue;
    seen.add(fold(term));
    terms.push(term);
  }
  return terms;
}

/** A sentence the owner can act on, or "" when the list can be saved. */
export function keywordsProblem(terms:readonly string[]):string{
  if(terms.length>MAX_KEYWORDS)return `용어는 ${MAX_KEYWORDS}개까지 저장할 수 있습니다. 지금 ${terms.length}개입니다.`;
  const long=terms.find(term=>term.length>MAX_KEYWORD_CHARS);
  if(long)return `한 용어는 ${MAX_KEYWORD_CHARS}자까지입니다: ${long.slice(0,20)}…`;
  const bad=terms.find(term=>FORBIDDEN.test(term));
  if(bad)return `< 또는 > 같은 기호와 보이지 않는 문자는 쓸 수 없습니다: ${bad.replace(/\p{C}/gu,"")}`;
  if(terms.reduce((sum,term)=>sum+term.length,0)>MAX_KEYWORDS_TOTAL_CHARS)return `용어 전체 길이는 ${MAX_KEYWORDS_TOTAL_CHARS}자까지입니다.`;
  return "";
}

/** What a claim hands the worker: terms for speech jobs only, never for minutes generation. */
export function claimKeywords(job:{pending?:string},raw:string|null|undefined):string[]{
  return job.pending==="minutes"?[]:storedKeywords(raw);
}

/** Stored value to a list. Anything unreadable means no terms, never an error. */
export function storedKeywords(raw:string|null|undefined):string[]{
  if(!raw)return [];
  try{const parsed=speechKeywordsSchema.safeParse(JSON.parse(raw));return parsed.success?parsed.data:[];}catch{return [];}
}
