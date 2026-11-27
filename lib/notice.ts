// What a finished or stopped job says in a notice. Shared by the in-tab desktop notice and the phone push.
// A notice carries the meeting's short name and its state only: never transcript, minutes or call contact details.
import { jobMode, type Job, type State } from "./contracts.ts";

export const NOTICE_TEXT: Partial<Record<State, string>> = {
  transcript_ready: "녹취가 준비되었습니다.", minutes_ready: "회의록이 완성되었습니다.", needs_review: "녹취를 검토해 주세요.",
  failed: "처리하지 못했습니다.", needs_setup: "설정이 필요합니다.", unknown_outcome: "외부 응답 확인이 필요합니다.", rate_limited: "사용 한도로 기다리는 중입니다.",
};
export const NOTICE_STATES = Object.keys(NOTICE_TEXT) as State[];
// Same tag for the desktop notice and the push, so one device never stacks two notices for one job.
export const noticeTag = (id: string) => `job-${id}`;

type NoticeJob = Pick<Job, "id" | "kind" | "metadata" | "output_mode">;
// Lock-screen safe: call jobs show no name or number, and STT jobs no file name (a phone's call recording is often
// named after the other person or their number).
export function noticeName(job: NoticeJob) {
  const name = job.kind === "call" ? "통화 녹취" : jobMode(job) === "transcript" ? "STT 녹취" : job.metadata?.organization || "이름 없는 회의";
  return name.length > 40 ? `${name.slice(0, 39)}…` : name;
}
export type NoticePayload = { title: string; body: string; tag: string; url: string };
export function noticePayload(job: NoticeJob, state: State): NoticePayload | null {
  const text = NOTICE_TEXT[state];
  return text ? { title: "회의록", body: `${noticeName(job)}: ${text}`, tag: noticeTag(job.id), url: "/" } : null;
}
