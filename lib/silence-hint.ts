import type {Segment} from "./contracts.ts";

// An API transcript shows the range of the audio clip that was sent, not where
// the words are. A clip with no detected speaker is sent whole so nothing is
// missed, and a few words then look like a long utterance. Ordinary Korean
// speech in these transcripts runs at about six characters a second.
export const SILENCE_HINT_MIN_MS=10000;
export const SILENCE_HINT_MAX_CHARS_PER_SECOND=1.5;

/** True when a clip-range segment is long but holds very little text. */
export function likelyMostlySilent(segment:Pick<Segment,"start_ms"|"end_ms"|"text"|"timing_provenance">):boolean{
  if(segment.timing_provenance!=="audio_clip_bounds")return false;
  const length=segment.end_ms-segment.start_ms;
  if(length<SILENCE_HINT_MIN_MS)return false;
  return segment.text.replace(/\s/g,"").length/(length/1000)<SILENCE_HINT_MAX_CHARS_PER_SECOND;
}
