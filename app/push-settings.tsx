"use client";
// Settings card: phone push for finished or stopped jobs, per device and opt-in.
import { useState } from "react";
import { Bell, BellOff, Send } from "lucide-react";
import { usePush } from "@/lib/push-client";

const STATUS_TEXT = { loading: "확인 중", unsupported: "이 브라우저는 지원하지 않음", unavailable: "서버에 설정되지 않음", blocked: "브라우저에서 알림이 차단됨", off: "꺼짐", on: "켜짐" } as const;

export function PushSettings() {
  const push = usePush({ refresh: true });
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (task: () => Promise<void>) => { setBusy(true); setResult(null); try { await task(); } finally { setBusy(false); } };
  return <section className="card settings-card">
    <h2><Bell size={22} />휴대폰 알림 <span className={`badge ${push.status === "on" ? "green" : ""}`}>{STATUS_TEXT[push.status]}</span></h2>
    <p>녹취가 준비되거나 검토, 설정, 확인이 필요해 작업이 멈추면 이 기기로 알림을 보냅니다. 이 페이지를 닫아도 옵니다. 알림에는 회의 이름과 상태만 담고 녹취 내용은 넣지 않습니다. 통화 녹취는 &apos;통화 녹취&apos;, STT 작업은 &apos;STT 녹취&apos;로만 표시하고 이름, 번호, 파일 이름은 넣지 않습니다.</p>
    {push.status === "unsupported" && <p>안드로이드 Chrome이나 삼성 인터넷에서 이 사이트를 열어 켜세요. 아이폰은 홈 화면에 추가한 앱에서만 받을 수 있습니다.</p>}
    {push.status === "blocked" && <p>브라우저의 사이트 설정에서 이 사이트의 알림을 허용한 뒤 다시 여세요.</p>}
    <div className="button-row">
      {push.status === "off" && <button className="button primary" disabled={busy} onClick={() => run(push.enable)}><Bell size={16} />이 기기에서 알림 켜기</button>}
      {push.status === "on" && <>
        <button className="button secondary" disabled={busy} onClick={() => run(async () => { const r = await push.test(); setResult(r === "sent" ? "시험 알림을 보냈습니다. 몇 초 안에 오지 않으면 휴대폰의 알림 설정을 확인하세요." : r === "gone" ? "알림 구독이 만료되었습니다. 다시 켜세요." : "시험 알림을 보내지 못했습니다."); })}><Send size={16} />시험 알림 보내기</button>
        <button className="button secondary" disabled={busy} onClick={() => run(push.disable)}><BellOff size={16} />이 기기 알림 끄기</button>
      </>}
    </div>
    {(result || push.error) && <p role="status">{push.error ?? result}</p>}
  </section>;
}
