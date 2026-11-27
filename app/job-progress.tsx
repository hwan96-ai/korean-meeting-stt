"use client";
// Live progress card for the open job: steps, elapsed and remaining time, stalls, finish notice, API cost.
import { useEffect, useRef, useState } from "react";
import { Bell, BellOff, Clock3 } from "lucide-react";
import type { Job } from "@/lib/contracts";
import { formatClock, formatDuration, isActive, progressView, type SpeedSample } from "@/lib/progress";
import { NOTICE_TEXT, noticeTag } from "@/lib/notice";
import { usePush } from "@/lib/push-client";

type OpenJob = Job & { queue_ahead?: number };
const NOTIFY_KEY = "meeting-notify-jobs";

function notifyList(): string[] { try { return JSON.parse(localStorage.getItem(NOTIFY_KEY) ?? "[]"); } catch { return []; } }
function setNotify(id: string, on: boolean) { const next = notifyList().filter(x => x !== id); if (on) next.push(id); localStorage.setItem(NOTIFY_KEY, JSON.stringify(next.slice(-20))); }
// Desktop browsers show page notifications; phone browsers need a service worker, so the button stays hidden there.
const notificationsUsable = () => typeof window !== "undefined" && "Notification" in window && !/Android|iPhone|iPad/i.test(navigator.userAgent);

export function JobProgress({ job, title, session, apiOrder }: { job: OpenJob; title: string; session: { server_time?: number; progress_samples?: SpeedSample[] } | null; apiOrder: boolean }) {
  // Rendered with key={job.id}, so each job starts with fresh state.
  const [now, setNow] = useState(() => Date.now());
  const [notify, setNotifyState] = useState(() => notifyList().includes(job.id));
  const previous = useRef<Job["state"] | null>(null);
  // Server clock minus this clock, measured when a session arrives; elapsed times then match the server's stamps.
  const offset = useRef(0);
  const tracked = isActive(job.state) || job.state === "waiting_worker";
  // Phone push (settings card). The in-tab notice stays as it was: both use the same tag, so a device that gets
  // both shows one notice, and a device the server dropped still hears from the open tab.
  const push = usePush();

  useEffect(() => { if (session?.server_time) offset.current = session.server_time - Date.now(); }, [session]);
  useEffect(() => { if (!tracked) return; const t = setInterval(() => setNow(Date.now() + offset.current), 1000); return () => clearInterval(t); }, [tracked]);

  const view = progressView(job, { now, apiOrder, samples: session?.progress_samples ?? [], speechMode: job.speech_mode ?? (apiOrder ? "api/" : "local/"), queueAhead: job.queue_ahead ?? null });

  // Finish notice: only for a state change seen while this page is open, never for an old result.
  useEffect(() => {
    const before = previous.current;
    previous.current = job.state;
    if (!before || before === job.state) return;
    const wasRunning = isActive(before) || before === "waiting_worker";
    const text = NOTICE_TEXT[job.state];
    if (!wasRunning || !text || !notifyList().includes(job.id)) return;
    setNotify(job.id, false); setNotifyState(false);
    try { if (notificationsUsable() && Notification.permission === "granted") new Notification(title, { body: text, tag: noticeTag(job.id) }); } catch { /* the page still shows the new state */ }
  }, [job.id, job.state, title]);

  // Tab title: "(62%) 회의록" while it runs, so a background tab shows how far it got.
  useEffect(() => {
    if (!view) return;
    const original = document.title.replace(/^\([^)]*\) /, "");
    const mark = view.percent !== null ? `${view.percent}%` : view.unit ? `${view.unit.done}/${view.unit.total}` : view.stageLabel;
    document.title = `(${mark}) ${original}`;
    return () => { document.title = original; };
  });

  async function toggleNotify() {
    if (notify) { setNotify(job.id, false); setNotifyState(false); return; }
    if (Notification.permission !== "granted" && await Notification.requestPermission() !== "granted") return;
    setNotify(job.id, true); setNotifyState(true);
  }

  const cost = (job.usage as { speech?: { cost_upper_bound_usd?: number } } | undefined)?.speech?.cost_upper_bound_usd;
  if (!view) return typeof cost === "number" && job.speech_mode?.startsWith("api/") ? <p className="progress-cost">OpenAI 전사 비용 상한 ${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)} (이 작업 누적)</p> : null;

  const stagePercent = view.unit && view.unit.total > 0 ? Math.round(view.unit.done / view.unit.total * 100) : null;
  return <section className="card progress-card" aria-live="polite">
    <ol className="progress-steps">{view.steps.map(step => <li key={step.key} className={step.status}><span />{step.label}</li>)}</ol>
    <div className="progress-main">
      <div>
        <strong>{view.stageLabel}{view.unit && ` ${view.unit.done}/${view.unit.total} ${view.unit.noun}`}</strong>
        <p>
          <Clock3 size={13} /> 올린 지 {formatDuration(view.jobElapsedMs)}
          {view.stageElapsedMs !== null && (view.stageElapsedMs < 60000 ? " · 이 단계 막 시작" : ` · 이 단계 ${formatDuration(view.stageElapsedMs)}째`)}
          {view.queueAhead !== null && (view.queueAhead > 0 ? ` · 앞에 ${view.queueAhead}건이 있습니다` : " · 곧 시작합니다")}
        </p>
      </div>
      {!view.queueAhead && view.steps[0].status === "done" && <div className="progress-eta">
        {view.totalRemainingMs !== null && view.finishAt !== null ? <><strong>{view.totalRemainingMs < 60000 ? "곧 끝납니다" : `약 ${formatDuration(view.totalRemainingMs)} 남음`}</strong><p>{formatClock(view.finishAt)}쯤 완료 예상</p></>
          : view.stageRemainingMs !== null ? <><strong>이 단계 약 {formatDuration(view.stageRemainingMs)} 남음</strong><p>전체 예상은 끝난 작업 기록이 쌓이면 보입니다</p></>
          : <><strong>남은 시간 계산 중</strong><p>{view.unit ? "두 단위가 끝나면 계산합니다" : "이 단계의 기록이 쌓이면 계산합니다"}</p></>}
      </div>}
      {push.status === "off" && !notificationsUsable() && <button className="text-link" onClick={() => { void push.enable(); }} title="작업이 끝나거나 멈추면 이 기기로 알립니다. 페이지를 닫아도 옵니다"><Bell size={15} />휴대폰 알림 켜기</button>}
      {!notificationsUsable() && (push.error || push.status === "blocked") && <p className="progress-note" role="status">{push.error ?? "브라우저에서 이 사이트의 알림이 차단되어 있습니다. 사이트 설정에서 허용하세요."}</p>}
      {notificationsUsable() && <button className="text-link" onClick={() => { void toggleNotify(); }} title="이 탭이 열려 있는 동안 끝나면 알립니다">{notify ? <><BellOff size={15} />알림 끄기</> : <><Bell size={15} />끝나면 알림</>}</button>}
    </div>
    {(view.percent !== null || stagePercent !== null) && <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={view.percent ?? stagePercent ?? 0} aria-label={view.percent !== null ? "전체 진행률" : "이 단계 진행률"}><span style={{ width: `${view.percent ?? stagePercent}%` }} /></div>}
    {view.attention && <p className={`progress-note ${view.attention.kind}`} role="status">{view.attention.message}</p>}
    {!view.attention && view.resumedAt !== null && <p className="progress-note" role="status">{formatClock(view.resumedAt)}에 작업기가 다시 이어받았습니다. 이미 끝낸 부분은 그대로 씁니다.</p>}
  </section>;
}
