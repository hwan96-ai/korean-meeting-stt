"use client";
// Speaker naming as one table: listen to each voice label, pick who it is. Two labels given the same person
// become one speaker. Applying saves a new transcript version in one step.
import { useMemo, useState } from "react";
import { Play } from "lucide-react";
import type { Segment } from "@/lib/contracts";
import { applySpeakerNames, attendeeNames, currentSpeakerName, groupSpeakers, planSpeakerNames, unionDuration } from "@/lib/speakers";

type Profile = { person_id: string; display_name: string; role?: string; company?: string };
type Props = {
  segments: Segment[]; profiles: Profile[]; attendees: string; activeFilter: string | null; canSave: boolean; otherEdits: boolean;
  onApply: (segments: Segment[]) => void; onFilter: (label: string | null) => void; onPlay: (segment: Segment) => void;
};
const minutes = (ms: number) => ms < 60000 ? `${Math.max(1, Math.round(ms / 1000))}초` : `${Math.round(ms / 60000)}분`;
const profileName = (p: Profile) => `${p.display_name}${p.role ? " " + p.role : ""}${p.company ? " (" + p.company + ")" : ""}`;

export default function SpeakerReview({ segments, profiles, attendees, activeFilter, canSave, otherEdits, onApply, onFilter, onPlay }: Props) {
  // Only what the user typed here; everything else shows the name the transcript already has.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const groups = useMemo(() => groupSpeakers(segments).map(g => {
    const own = segments.filter(s => s.local_speaker_id === g.label);
    // The longest segment is the easiest one to recognise a voice by.
    const sample = own.reduce((a, b) => b.end_ms - b.start_ms > a.end_ms - a.start_ms ? b : a, own[0]);
    return { ...g, spoken_ms: unionDuration(own), sample };
  }), [segments]);
  const options = useMemo(() => {
    const ids: Record<string, string> = {};
    for (const p of profiles) ids[profileName(p)] = p.person_id;
    return { names: [...new Set([...attendeeNames(attendees), ...Object.keys(ids)])], ids };
  }, [attendees, profiles]);
  let steps: ReturnType<typeof planSpeakerNames> = [], planError = "";
  try { steps = planSpeakerNames(segments, edits); } catch (e) { planError = e instanceof Error ? e.message : "이름을 확인하세요."; }
  const value = (label: string) => edits[label] ?? currentSpeakerName(groups.find(g => g.label === label)!);
  const named = new Set(groups.filter(g => g.label !== "unknown" && value(g.label).trim()).map(g => value(g.label).replace(/\s+/g, " ").trim())).size;

  function apply() {
    try {
      if (!steps.length) return;
      const lines = steps.map(s => s.kind === "merge" ? `${s.labels.join(", ")} → ${s.name} (한 사람으로 합침)` : s.kind === "clear" ? `${s.labels[0]} → 이름 지움` : `${s.labels[0]} → ${s.name}`);
      const tail = canSave ? `녹취 새 버전으로 저장합니다. 구간의 시간과 텍스트는 그대로입니다.${otherEdits ? " 아직 저장하지 않은 다른 수정도 함께 저장됩니다." : ""}` : "편집기에 반영합니다. 위의 검토본 저장으로 저장하세요.";
      if (!window.confirm(`${lines.join("\n")}\n\n${tail}`)) return;
      onApply(applySpeakerNames(segments, edits, options.ids));
      setEdits({}); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "화자 이름 적용에 실패했습니다."); }
  }

  if (!segments.length) return null;
  return <section className="speaker-review" aria-labelledby="speaker-map-title">
    <div className="speaker-map-head">
      <div>
        <h3 id="speaker-map-title">화자 이름 정하기</h3>
        <p>▶로 목소리를 듣고 누구인지 고르세요. <strong>같은 사람을 두 줄에 고르면 한 사람으로 합쳐집니다.</strong></p>
      </div>
      <span className="speaker-map-count">목소리 {groups.filter(g => g.label !== "unknown").length}개 → 사람 {named}명</span>
    </div>
    {!options.names.length && <p className="speaker-map-hint">회의 정보에 참석자를 한 줄에 한 명씩 적어 두면 여기서 고를 수 있습니다. 직접 입력해도 됩니다.</p>}
    <datalist id="speaker-name-options">{options.names.map(n => <option key={n} value={n} />)}</datalist>
    <div className="speaker-map" role="table" aria-label="화자 이름 표">
      {groups.map(g => {
        const unknown = g.label === "unknown", merging = steps.find(s => s.kind === "merge" && s.labels.includes(g.label));
        return <div role="row" key={g.label} className={`speaker-map-row${activeFilter === g.label ? " selected" : ""}`}>
          <div role="cell"><button className="play-button" aria-label={`${g.label} 목소리 듣기`} onClick={() => onPlay(g.sample)}><Play size={13} /></button></div>
          <div role="cell" className="speaker-map-label">
            <strong>{g.label === "unknown" ? "화자 미확인" : g.label}</strong>
            <small>{g.count}개 구간 · 말한 시간 {minutes(g.spoken_ms)}{g.overlaps ? ` · 겹침 ${g.overlaps}` : ""}</small>
          </div>
          <div role="cell" className="speaker-map-input">
            {unknown ? <small>여러 사람이 섞여 있을 수 있어 아래 구간에서 하나씩 고치세요.</small>
              : <input aria-label={`${g.label}의 이름`} list="speaker-name-options" maxLength={200} placeholder="누구인지 고르거나 입력" value={value(g.label)} onChange={e => setEdits(x => ({ ...x, [g.label]: e.target.value }))} />}
            {merging && <small className="speaker-map-merge">{merging.labels.filter(l => l !== g.label).join(", ")} 줄과 한 사람으로 합칩니다</small>}
            {!unknown && g.names.length > 1 && !(g.label in edits) && <small className="speaker-map-merge">이름이 {g.names.length}개 섞여 있습니다. 하나로 정하세요.</small>}
          </div>
          <div role="cell"><button className="text-link" onClick={() => onFilter(activeFilter === g.label ? null : g.label)}>{activeFilter === g.label ? "전체 보기" : "구간 보기"}</button></div>
        </div>;
      })}
    </div>
    <div className="speaker-map-actions">
      <button className="button primary" disabled={!steps.length || !!planError} onClick={apply}>{canSave ? "이름 적용하고 저장" : "이름 적용"}{steps.length ? ` (${steps.length}건)` : ""}</button>
      {Object.keys(edits).length > 0 && <button className="text-link" onClick={() => { setEdits({}); setError(""); }}>되돌리기</button>}
      <small>자동 라벨은 실명이 아닙니다. 들어 보고 확인한 이름만 고르세요.</small>
    </div>
    {(error || planError) && <p role="alert" className="group-error">{error || planError}</p>}
  </section>;
}
