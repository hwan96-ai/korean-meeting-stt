import type { Segment } from "./contracts";

export interface SpeakerGroup {
  label: string;
  count: number;
  first_ms: number;
  names: string[];
  reviewed: number;
  overlaps: number;
}

export function unionDuration(segments: readonly Pick<Segment,"start_ms"|"end_ms">[]): number {
  const intervals=segments.map(s=>({start:s.start_ms,end:s.end_ms})).sort((a,b)=>a.start-b.start);
  let total=0,start=-1,end=-1;
  for(const next of intervals){if(start<0){start=next.start;end=next.end;}else if(next.start<=end){end=Math.max(end,next.end);}else{total+=end-start;start=next.start;end=next.end;}}
  return total+(start>=0?end-start:0);
}

export function speakerDiagnostics(segments: readonly Segment[]) {
  const unknown=segments.filter(s=>s.local_speaker_id==="unknown");
  return {labels:new Set(segments.map(s=>s.local_speaker_id)).size,unknown_segments:unknown.length,
    unknown_ms:unionDuration(unknown),covered_ms:unionDuration(segments),
    confirmed_identity_segments:segments.filter(s=>!!s.confirmed_person_id).length};
}

export function groupSpeakers(segments: readonly Segment[]): SpeakerGroup[] {
  const groups = new Map<string, SpeakerGroup>();
  for (const s of segments) {
    let group = groups.get(s.local_speaker_id);
    if (!group) {
      group = { label: s.local_speaker_id, count: 0, first_ms: s.start_ms, names: [], reviewed: 0, overlaps: 0 };
      groups.set(s.local_speaker_id, group);
    }
    group.count++;
    group.first_ms = Math.min(group.first_ms, s.start_ms);
    if (s.speaker_name && !group.names.includes(s.speaker_name)) group.names.push(s.speaker_name);
    if (s.review_status !== "unreviewed") group.reviewed++;
    if (s.overlap) group.overlaps++;
  }
  return [...groups.values()].sort((a, b) => a.first_ms - b.first_ms || a.label.localeCompare(b.label));
}

function requireSource(segments: readonly Segment[], source: string) {
  if (!segments.some(s => s.local_speaker_id === source)) throw Error("변경할 화자 라벨을 선택하세요.");
}

// A rename preserves identity. Merging distinct voice groups clears identity/review
// decisions on both groups so it never silently assigns a registered person.
export function relabelSpeaker(segments: readonly Segment[], source: string, target: string): Segment[] {
  requireSource(segments, source);
  const next = target.trim();
  if (!next || next.length > 100) throw Error("화자 라벨은 1~100자로 입력하세요.");
  if (next === source) throw Error("현재와 다른 화자 라벨을 입력하세요.");
  const merging = segments.some(s => s.local_speaker_id === next);
  return segments.map(s => {
    if (s.local_speaker_id !== source && !(merging && s.local_speaker_id === next)) return s;
    return {
      ...s,
      local_speaker_id: next,
      ...(merging ? { confirmed_person_id: null, proposed_person_id: null, speaker_name: "", review_status: "unreviewed" as const } : {}),
    };
  });
}

export function assignSpeakerIdentity(segments: readonly Segment[], source: string, displayName: string, personId: string | null): Segment[] {
  requireSource(segments, source);
  const name = displayName.trim();
  if (name.length > 200) throw Error("화자 이름은 200자 이내로 입력하세요.");
  if (personId && !name) throw Error("등록 화자를 적용할 때 이름이 필요합니다.");
  return segments.map(s => s.local_speaker_id === source ? {
    ...s, speaker_name: name, confirmed_person_id: personId,
    proposed_person_id: null, review_status: "unreviewed" as const,
  } : s);
}

// One table for naming speakers: every voice label gets a name, and labels given the same name are merged into
// one speaker. Only labels whose name actually changes (or that merge) are touched, so their review state stays.
export interface SpeakerNameStep { labels: string[]; name: string; kind: "merge" | "name" | "clear" }
const tidy = (name: string) => name.replace(/\s+/g, " ").trim();
export const currentSpeakerName = (group: Pick<SpeakerGroup, "names">) => group.names.length === 1 ? group.names[0] : "";

export function planSpeakerNames(segments: readonly Segment[], names: Readonly<Record<string, string>>): SpeakerNameStep[] {
  const groups = groupSpeakers(segments).filter(g => g.label !== "unknown");
  const buckets = new Map<string, { name: string; labels: string[]; changed: boolean }>();
  const steps: SpeakerNameStep[] = [];
  for (const g of groups) {
    const edited = Object.hasOwn(names, g.label);
    // A label whose segments carry different names was fixed segment by segment; leave it unless edited here.
    if (!edited && g.names.length > 1) continue;
    const before = tidy(currentSpeakerName(g)), wanted = edited ? tidy(names[g.label]) : before;
    const changed = edited && (wanted !== before || g.names.length > 1);
    if (!wanted) { if (changed) steps.push({ labels: [g.label], name: "", kind: "clear" }); continue; }
    if (wanted.length > 200) throw Error("화자 이름은 200자 이내로 입력하세요.");
    const bucket = buckets.get(wanted) ?? { name: wanted, labels: [], changed: false };
    bucket.labels.push(g.label); bucket.changed ||= changed; buckets.set(wanted, bucket);
  }
  for (const b of buckets.values()) {
    if (b.labels.length > 1) steps.push({ labels: b.labels, name: b.name, kind: "merge" });
    else if (b.changed) steps.push({ labels: b.labels, name: b.name, kind: "name" });
  }
  return steps;
}

// personIds maps a chosen name to a registered person. Otherwise a label that already had this name keeps the
// person it was confirmed as, so merging into it does not drop that link; a newly typed name links to nobody.
export function applySpeakerNames(segments: readonly Segment[], names: Readonly<Record<string, string>>, personIds: Readonly<Record<string, string>> = {}): Segment[] {
  let next = [...segments];
  for (const step of planSpeakerNames(segments, names)) {
    const [target, ...rest] = step.labels;
    const kept = segments.find(s => step.labels.includes(s.local_speaker_id) && tidy(s.speaker_name) === step.name && s.confirmed_person_id)?.confirmed_person_id ?? null;
    for (const label of rest) next = relabelSpeaker(next, label, target);
    next = assignSpeakerIdentity(next, target, step.name, step.name ? personIds[step.name] ?? kept : null);
  }
  return next;
}

// Attendees from the meeting info: one per line (commas also split), duplicates dropped.
export function attendeeNames(attendees: string): string[] {
  return [...new Set(attendees.split(/[\n,;]/).map(tidy).filter(Boolean))];
}
