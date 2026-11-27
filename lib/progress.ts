// Processing progress: what the worker reports on its lease heartbeat, the state timeline the web records,
// and the estimates the screen shows. Everything here is pure so node tests cover it without a server.
import type { Job, State } from "./contracts";

export const PROGRESS_STAGES = ["preparing", "diarizing", "transcribing", "aligning", "meta_inference"] as const;
export type ProgressStage = typeof PROGRESS_STAGES[number];
export interface ProgressReport { stage: ProgressStage; done: number; total: number }
export interface StoredProgress extends ProgressReport {
  lease_id: string; reported_at: number; changed_at: number;
  // First count change seen in this lease and stage. Units finished after it were computed now, not read back from
  // a checkpoint, so only they set the speed. A resumed job skips its finished blocks in seconds.
  base?: { done: number; at: number };
}
export interface TimingEvent { state: State | "resumed"; at: number }
export interface JobTiming { generation: number; events: TimingEvent[] }

export const ACTIVE_STATES: readonly State[] = ["preparing", "transcribing", "diarizing", "aligning", "generating"];
export const FINISHED_STATES: readonly State[] = ["transcript_ready", "minutes_ready", "needs_review"];
const MAX_EVENTS = 40;
export const isActive = (state: State) => ACTIVE_STATES.includes(state);

// Heartbeat: keep the speed base while the same lease keeps counting the same stage.
// Stored in its own settings row (lib/progress-store.ts), never in jobs.data.
export function nextProgress(previous: StoredProgress | undefined, report: ProgressReport, leaseId: string, now: number): StoredProgress {
  const fresh = { ...report, lease_id: leaseId, reported_at: now, changed_at: now };
  if (!previous || previous.lease_id !== leaseId || previous.stage !== report.stage || previous.total !== report.total) return fresh;
  if (report.done === previous.done) return { ...previous, reported_at: now };
  if (report.done < previous.done) return fresh;
  return { ...fresh, base: previous.base ?? { done: report.done, at: now } };
}

function keep(events: TimingEvent[]) { return events.slice(-MAX_EVENTS); }

// Every state change of one generation, oldest first. A new generation (reprocess, retry) starts a new timeline.
export function timingAfter(job: Pick<Job, "state" | "generation" | "timing">, state: State, generation: number, now: number): JobTiming | undefined {
  if (generation !== job.generation) return { generation, events: [{ state, at: now }] };
  if (state === job.state) return job.timing;
  const events = job.timing?.generation === generation ? job.timing.events : [];
  return { generation, events: keep([...events, { state, at: now }]) };
}

// Claim: a job whose lease ran out while it was running is picked up again; mark that as a resume.
export function timingAfterClaim(job: Pick<Job, "state" | "generation" | "timing">, state: State, now: number): JobTiming {
  const events = job.timing?.generation === job.generation ? job.timing.events : [];
  const resumed: TimingEvent[] = isActive(job.state) ? [{ state: "resumed", at: now }] : [];
  return { generation: job.generation, events: keep([...events, ...resumed, { state, at: now }]) };
}

// History: seconds spent in each speech step by one finished job.
export type SpeechStep = "preparing" | "diarizing" | "transcribing" | "aligning";
const SPEECH_STEPS: readonly SpeechStep[] = ["preparing", "diarizing", "transcribing", "aligning"];
// v2: only first runs. v1 (release-20261125-pg01) also kept retried jobs, whose cached steps take ~0 s.
export interface SpeedSample { v: 2; mode: string; duration: number; seconds: Partial<Record<SpeechStep, number>> }
export function sampleOf(job: Pick<Job, "state" | "timing" | "duration" | "speech_mode">): SpeedSample | null {
  const events = job.timing?.events ?? [];
  if (!FINISHED_STATES.includes(job.state) || !job.duration || !job.speech_mode) return null;
  // A retry, reprocess or resume reuses checkpoints, so its step times say nothing about a fresh job.
  if (job.timing?.generation !== 1 || events.some(e => e.state === "resumed")) return null;
  const last = events[events.length - 1];
  if (!last || !FINISHED_STATES.includes(last.state as State) || !events.some(e => e.state === "preparing")) return null;
  const seconds: Partial<Record<SpeechStep, number>> = {};
  for (let i = 0; i + 1 < events.length; i++) {
    const step = events[i].state as SpeechStep;
    if (SPEECH_STEPS.includes(step)) seconds[step] = (seconds[step] ?? 0) + Math.max(0, events[i + 1].at - events[i].at) / 1000;
  }
  return { v: 2, mode: job.speech_mode, duration: job.duration, seconds };
}

const family = (mode: string | undefined) => (mode ?? "").split("/")[0];
function median(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b), middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}
// Expected seconds of each step for this much audio. Uses only finished jobs of the same speech mode (api or
// local); with none, there is no estimate rather than an invented one.
export function expectedSeconds(samples: SpeedSample[], mode: string, duration: number): Partial<Record<SpeechStep, number>> {
  const same = samples.filter(s => family(s.mode) === family(mode) && s.duration > 0);
  const result: Partial<Record<SpeechStep, number>> = {};
  for (const step of SPEECH_STEPS) {
    const values = same.filter(s => s.seconds[step] !== undefined);
    if (!values.length) continue;
    // Alignment and meeting-info inference take about the same time for any length; the others grow with the audio.
    result[step] = step === "aligning" ? median(values.map(s => s.seconds[step]!)) : median(values.map(s => s.seconds[step]! / s.duration)) * duration;
  }
  return result;
}

export interface ProgressStep { key: SpeechStep | "waiting" | "done"; label: string; status: "done" | "current" | "pending" }
export interface ProgressView {
  steps: ProgressStep[]; stageLabel: string;
  unit: { done: number; total: number; noun: string } | null;
  jobElapsedMs: number; stageElapsedMs: number | null;
  stageRemainingMs: number | null; totalRemainingMs: number | null; finishAt: number | null; percent: number | null;
  attention: { kind: "lease_expired" | "slow"; message: string } | null;
  resumedAt: number | null; queueAhead: number | null;
}
export interface ProgressContext { now: number; apiOrder: boolean; samples: SpeedSample[]; speechMode: string; queueAhead?: number | null }

const LABELS: Record<SpeechStep, string> = { preparing: "음성 준비", diarizing: "화자 분석", transcribing: "전사", aligning: "정렬과 정보 추정" };
const NOUN: Partial<Record<SpeechStep, string>> = { diarizing: "구간", transcribing: "조각" };
const SLOW_MIN_MS = 10 * 60 * 1000;

function stepOfProgress(stage: ProgressStage): SpeechStep { return stage === "meta_inference" ? "aligning" : stage; }

export function progressView(job: Job, context: ProgressContext): ProgressView | null {
  const { now } = context;
  const waiting = job.state === "waiting_worker";
  if (!waiting && !(isActive(job.state) && job.state !== "generating")) return null;
  const order: SpeechStep[] = context.apiOrder ? ["preparing", "diarizing", "transcribing", "aligning"] : ["preparing", "transcribing", "diarizing", "aligning"];
  const current = waiting ? null : job.state as SpeechStep;
  const index = current ? order.indexOf(current) : -1;
  const steps: ProgressStep[] = [
    { key: "waiting", label: "대기", status: waiting ? "current" : "done" },
    ...order.map((key, i): ProgressStep => ({ key, label: LABELS[key], status: i < index ? "done" : i === index ? "current" : "pending" })),
    { key: "done", label: "완료", status: "pending" },
  ];
  const events = job.timing?.generation === job.generation ? job.timing.events : [];
  const stageStart = current ? [...events].reverse().find(e => e.state === current)?.at ?? null : null;
  const processingStart = events.find(e => e.state === "preparing")?.at ?? null;
  const resumed = [...events].reverse().find(e => e.state === "resumed");
  const view: ProgressView = {
    steps, stageLabel: current ? LABELS[current] : "작업기 대기", unit: null,
    jobElapsedMs: Math.max(0, now - job.created_at), stageElapsedMs: stageStart === null ? null : Math.max(0, now - stageStart),
    stageRemainingMs: null, totalRemainingMs: null, finishAt: null, percent: null, attention: null,
    resumedAt: resumed?.at ?? null, queueAhead: waiting ? context.queueAhead ?? null : null,
  };
  if (!current) return view;

  const progress = job.progress && job.progress.lease_id === job.lease_id && stepOfProgress(job.progress.stage) === current ? job.progress : null;
  if (progress?.stage === "meta_inference") view.stageLabel = "회의 정보 추정";
  if (progress && progress.total > 0 && NOUN[current]) view.unit = { done: progress.done, total: progress.total, noun: NOUN[current]! };
  const expected = job.duration ? expectedSeconds(context.samples, context.speechMode, job.duration) : {};
  const unitMs = progress?.base && progress.done > progress.base.done ? (progress.changed_at - progress.base.at) / (progress.done - progress.base.done) : null;

  if (unitMs !== null && progress) view.stageRemainingMs = Math.max(0, unitMs * (progress.total - progress.done) - (now - progress.changed_at));
  else if (expected[current] !== undefined && view.stageElapsedMs !== null) view.stageRemainingMs = Math.max(0, expected[current]! * 1000 - view.stageElapsedMs);
  const later = order.slice(index + 1);
  if (view.stageRemainingMs !== null && later.every(step => expected[step] !== undefined)) {
    view.totalRemainingMs = view.stageRemainingMs + later.reduce((sum, step) => sum + expected[step]! * 1000, 0);
    view.finishAt = now + view.totalRemainingMs;
    if (processingStart !== null) {
      const spent = Math.max(0, now - processingStart);
      view.percent = spent + view.totalRemainingMs > 0 ? Math.min(99, Math.round(spent / (spent + view.totalRemainingMs) * 100)) : null;
    }
  }

  if (job.lease_until && job.lease_until < now) {
    view.attention = { kind: "lease_expired", message: "작업기 응답이 끊겼습니다. 작업기가 다시 이어받으면 끝낸 부분부터 계속합니다." };
  } else if (progress && progress.total > 0 && progress.done < progress.total) {
    const still = now - progress.changed_at;
    if (still > Math.max(SLOW_MIN_MS, unitMs !== null ? unitMs * 4 : 0)) view.attention = { kind: "slow", message: `진행 숫자가 ${formatDuration(still)}째 그대로입니다. 작업기는 응답하고 있으니 조금 더 기다려 보세요.` };
  } else if (view.stageElapsedMs !== null && !view.unit) {
    const limit = Math.max(SLOW_MIN_MS, (expected[current] ?? 0) * 3000);
    if (view.stageElapsedMs > limit) view.attention = { kind: "slow", message: `${view.stageLabel} 단계가 평소보다 오래 걸리고 있습니다. 작업기는 응답하고 있습니다.` };
  }
  return view;
}

// Short label for the job list: "화자 분석 8/21".
export function progressShort(job: Pick<Job, "state" | "progress" | "lease_until">, now: number): string | null {
  if (!isActive(job.state) || job.state === "generating") return null;
  const progress = job.progress;
  if (!progress || stepOfProgress(progress.stage) !== job.state || progress.total <= 0) return null;
  if (job.lease_until && job.lease_until < now) return null;
  return `${progress.done}/${progress.total}`;
}

export function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 1) return "1분 미만";
  if (minutes < 60) return `${minutes}분`;
  return `${Math.floor(minutes / 60)}시간${minutes % 60 ? ` ${minutes % 60}분` : ""}`;
}
export function formatClock(at: number): string {
  return new Date(at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Seoul" });
}
