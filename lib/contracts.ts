import { z } from "zod";
import { isoDatetimeOffset, utf16 } from "./text-schema.ts";
import type { JobTiming, StoredProgress } from "./progress";
export const MAX_BYTES = 512 * 1024 * 1024;
export const CHUNK_BYTES = 4 * 1024 * 1024;
export const MAX_DURATION = 4 * 60 * 60;
export const TEMPLATE_VERSION = "2026-10-28.1";
// Any 8-4-4-4-12 hex id, as zod 3 accepted; zod 4's z.uuid() would also check the version and variant bits.
export const idSchema = z.guid();
export const outputModeSchema = z.enum(["minutes", "transcript"]);
export type OutputMode = z.infer<typeof outputModeSchema>;
export const jobMode = (job: {output_mode?: OutputMode}): OutputMode => job.output_mode ?? "minutes";
export const metadataSchema = z.object({
  organization: utf16(150).default(""), datetime: z.union([z.literal(""),isoDatetimeOffset()]).default(""),
  place: utf16(200).default(""), meeting_format: utf16(80).default(""),
  author: utf16(100).default(""), attendees: utf16(4000).default(""), week: utf16(30).default(""),
}).strict();
export type Metadata = z.infer<typeof metadataSchema>;
export const segmentSchema = z.object({
  segment_id: idSchema, start_ms: z.number().int().min(0).max(MAX_DURATION * 1000),
  end_ms: z.number().int().min(1).max(MAX_DURATION * 1000), text: utf16(8000),
  local_speaker_id: utf16(100, 1), proposed_person_id: utf16(100).nullable().default(null),
  confirmed_person_id: utf16(100).nullable().default(null), speaker_name: utf16(200).default(""),
  overlap: z.boolean().default(false), review_status: z.enum(["unreviewed", "confirmed", "uncertain"]).default("unreviewed"),
  timing_provenance: z.enum(["audio_clip_bounds","provider_segment","asr_words","asr_segments","manual"]).optional(),
}).strict().refine(s => s.end_ms > s.start_ms, "종료 시각은 시작 시각보다 뒤여야 합니다.");
export type Segment = z.infer<typeof segmentSchema>;
export const segmentsSchema = z.array(segmentSchema).max(20000).refine(ss => new Set(ss.map(s => s.segment_id)).size === ss.length, "구간 ID 중복");
export const createJobSchema = z.object({
  output_mode: outputModeSchema.default("minutes"),
  meeting_type: z.enum(["customer", "weekly"]).optional(), developer_requests: z.boolean().default(false), input_kind: z.enum(["file", "recording"]),
  metadata: metadataSchema.optional(), filename: utf16(255, 1), byte_size: z.number().int().positive().max(MAX_BYTES), mime_type: utf16(100), upload_id: idSchema,
}).strict().superRefine((input,ctx)=>{
  if(input.output_mode==="minutes"){
    if(!input.meeting_type)ctx.addIssue({code:z.ZodIssueCode.custom,path:["meeting_type"],message:"회의 유형을 선택하세요."});
    if(!input.metadata)ctx.addIssue({code:z.ZodIssueCode.custom,path:["metadata"],message:"회의 정보가 필요합니다."});
  }
  if(input.output_mode==="transcript"&&input.developer_requests)ctx.addIssue({code:z.ZodIssueCode.custom,path:["developer_requests"],message:"STT 작업은 회의록 요청을 포함할 수 없습니다."});
  if(input.output_mode==="transcript"&&input.metadata&&Object.values(input.metadata).some(value=>value.trim()))ctx.addIssue({code:z.ZodIssueCode.custom,path:["metadata"],message:"STT 작업에는 회의 정보를 입력하지 않습니다."});
}).transform(input=>({...input,meeting_type:input.meeting_type??"customer" as const,metadata:input.metadata??metadataSchema.parse({})}));
export type State = "uploading" | "waiting_worker" | "preparing" | "transcribing" | "diarizing" | "aligning" | "needs_setup" | "needs_review" | "transcript_ready" | "generating" | "minutes_ready" | "failed" | "cancelled" | "unknown_outcome" | "rate_limited";
// Meeting info the server read from the transcript (ops/meeting-info-inference/SPEC.md).
// Suggestions only: never part of `metadata` until the owner saves them.
export const transcriptSuggestionItemSchema = z.object({
  key: z.enum(["organization", "place", "meeting_format", "attendee"]), value: utf16(200, 1), name: utf16(100, 1).optional(),
  segment_ids: z.array(idSchema).min(1).max(3), quote: utf16(300, 1),
}).strict();
export const metadataSuggestionsSchema = z.object({
  status: z.enum(["ok", "off", "skipped", "failed"]), reason: z.string().regex(/^[a-z0-9_]{1,60}$/).optional(),
  model_id: utf16(200).optional(), created_at: z.number().int().min(0), truncated: z.boolean().optional(),
  items: z.array(transcriptSuggestionItemSchema).max(25),
}).strict();
export type TranscriptSuggestionItem = z.infer<typeof transcriptSuggestionItemSchema>;
export type MetadataSuggestions = z.infer<typeof metadataSuggestionsSchema> & { transcript_revision: number };
export interface Draft { revision: number; transcript_revision: number; content: string; model_id: string; generation_mode: string; missing_fields: string[]; template_version: string; }
// A phone call handed over by the orchestrator (/api/integration/v1/calls). Only the last four digits of the
// number live on the job: the job goes to the browser, the worker and the warehouse export. The full number
// stays in the integration_calls table.
export interface CallInfo {
  external_ref: string; direction: "in" | "out"; started_at: string; duration_sec: number;
  counterpart: { name: string; phone_tail4: string; crm_contact_id: string | null; crm_company_id: string | null };
  // First time the owner opened the job (detail, audio, TXT, revisions, cancel, retry ...). A revert then refuses.
  owner_viewed_at?: number;
}
export interface JobData extends z.infer<typeof createJobSchema> {
  kind?: "call"; call?: CallInfo;
  // Set when the owner saves an edit (metadata, transcript, minutes). An integration revert leaves such jobs alone.
  owner_edited_at?: number;
  stage: string; transcript_revision: number; segments: Segment[]; draft?: Draft; pending?: "transcribe" | "minutes";
  upload_complete: boolean; sha256?: string; duration?: number; part_checksums?: string[];
  error?: { code: string; message: string; retryable: boolean; stage: string }; speech_mode?: string; generation_mode?: string;
  speech_complete?: boolean; diarization_complete?: boolean; usage?: unknown; local_received?: boolean;
  unresolved_external?: {stage:string;input_revision:number;generation:number};
  metadata_suggestions?: MetadataSuggestions;
  // Not stored here: the API attaches the current lease's progress (lib/progress-store.ts) to job responses.
  progress?: StoredProgress;
  // State changes of the current generation, for elapsed time and speed history.
  timing?: JobTiming;
  profile_requests?: { request_id: string; person_id?: string; segment_ids: string[]; display_name: string; role: string; company: string; confirmed_single_speaker: boolean }[];
}
export interface Job extends JobData { id: string; state: State; revision: number; generation: number; created_at: number; updated_at: number; lease_id?: string; lease_until?: number; }
export type JobSummary=Pick<Job,"id"|"state"|"created_at"|"metadata"|"filename"|"output_mode"|"kind"|"call"|"progress"|"lease_until">;
// "이름 (끝 1234)" for call jobs; the full number is never on the job. Screens add the "통화" tag themselves.
export const callLabel=(j:{kind?:string;call?:CallInfo})=>j.kind==="call"&&j.call?`${j.call.counterpart.name||"이름 없음"}${j.call.counterpart.phone_tail4?` (끝 ${j.call.counterpart.phone_tail4})`:""}`:null;
export const stateLabels: Record<State, string> = {
  uploading: "업로드 중", waiting_worker: "PC 연결 대기", preparing: "음성 준비 중", transcribing: "전사 중", diarizing: "화자 분석 중", aligning: "시간 정렬 중", needs_setup: "설정 필요", needs_review: "검토 필요", transcript_ready: "녹취 검토 가능", generating: "회의록 작성 중", minutes_ready: "회의록 완성", failed: "실패", cancelled: "취소됨", unknown_outcome: "외부 응답 확인 필요", rate_limited: "사용 한도 대기",
};
export const missingMetadata = (m: Metadata, type: string) => ["organization", "datetime", "place", "meeting_format", "author", "attendees", ...(type === "weekly" ? ["week"] : [])].filter(k => !m[k as keyof Metadata]?.trim());
