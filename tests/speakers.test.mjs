import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { groupSpeakers, relabelSpeaker, assignSpeakerIdentity, speakerDiagnostics } from "../lib/speakers.ts";
const person=randomUUID();
const make=(label,start,name="")=>({segment_id:randomUUID(),start_ms:start,end_ms:start+900,text:`text-${start}`,local_speaker_id:label,speaker_name:name,confirmed_person_id:name?person:null,proposed_person_id:null,overlap:false,review_status:"confirmed"});
const invariant=s=>s.map(({segment_id,start_ms,end_ms,text,overlap})=>({segment_id,start_ms,end_ms,text,overlap}));
test("grouping counts the same voice label without changing source order or data",()=>{const ss=[make("B",1000),make("A",0,"검토한 이름"),make("A",2000,"검토한 이름")],before=structuredClone(ss);assert.deepEqual(groupSpeakers(ss).map(g=>({label:g.label,count:g.count,names:g.names})),[{label:"A",count:2,names:["검토한 이름"]},{label:"B",count:1,names:[]}]);assert.deepEqual(ss,before);});
test("bulk rename changes all matching labels while preserving IDs/times/text/identity",()=>{const ss=[make("A",0,"확인한 이름"),make("B",1000),make("A",2000)],before=structuredClone(ss),out=relabelSpeaker(ss,"A","고객 A");assert.deepEqual(out.map(s=>s.local_speaker_id),["고객 A","B","고객 A"]);assert.equal(out[0].confirmed_person_id,person);assert.deepEqual(invariant(out),invariant(ss));assert.deepEqual(ss,before);assert.equal(out[1],ss[1]);assert.throws(()=>relabelSpeaker(ss,"missing","X"));assert.throws(()=>relabelSpeaker(ss,"A"," "));});
test("bulk merge clears identity/review on both groups and never changes time or segment identity",()=>{const ss=[make("A",0,"사람 1"),make("B",1000,"사람 2"),make("C",2000)],out=relabelSpeaker(ss,"A","B");assert.equal(groupSpeakers(out).length,2);for(const s of out.slice(0,2)){assert.equal(s.local_speaker_id,"B");assert.equal(s.speaker_name,"");assert.equal(s.confirmed_person_id,null);assert.equal(s.review_status,"unreviewed");}assert.deepEqual(invariant(out),invariant(ss));assert.equal(out[2],ss[2]);});
test("explicit grouped identity applies only selected group and requires renewed review",()=>{const ss=[make("A",0),make("B",1000),make("A",2000)],out=assignSpeakerIdentity(ss,"A","김예시 직책 (회사)",person);for(const s of [out[0],out[2]]){assert.equal(s.speaker_name,"김예시 직책 (회사)");assert.equal(s.confirmed_person_id,person);assert.equal(s.proposed_person_id,null);assert.equal(s.review_status,"unreviewed");}assert.equal(out[1],ss[1]);assert.deepEqual(invariant(out),invariant(ss));const cleared=assignSpeakerIdentity(out,"A","",null);assert.equal(cleared[0].speaker_name,"");assert.equal(cleared[0].confirmed_person_id,null);assert.throws(()=>assignSpeakerIdentity(ss,"A","",person));});
test("speaker diagnostics use interval union instead of inflating overlapping time",()=>{const ss=[{...make("unknown",0),end_ms:1000},{...make("unknown",500),end_ms:1500},{...make("B",1600),end_ms:2000}];assert.deepEqual(speakerDiagnostics(ss),{labels:2,unknown_segments:2,unknown_ms:1500,covered_ms:1900,confirmed_identity_segments:0});});

test("naming table: same name on two labels merges them into one speaker with that name", async () => {
  const { applySpeakerNames, planSpeakerNames, groupSpeakers } = await import("../lib/speakers.ts");
  const ss = [make("S1", 0), make("S2", 1000), make("S3", 2000), make("S1", 3000), make("S4", 4000)];
  const names = { S1: "홍길동 프로", S2: "B상무", S3: " 홍길동  프로 ", S4: "" };
  assert.deepEqual(planSpeakerNames(ss, names).map(s => [s.kind, s.labels, s.name]), [["merge", ["S1", "S3"], "홍길동 프로"], ["name", ["S2"], "B상무"]]);
  const out = applySpeakerNames(ss, names, { "B상무": person });
  assert.deepEqual(groupSpeakers(out).map(g => [g.label, g.count, g.names]), [["S1", 3, ["홍길동 프로"]], ["S2", 1, ["B상무"]], ["S4", 1, []]]);
  assert.equal(out[1].confirmed_person_id, person);
  assert.equal(out[0].confirmed_person_id, null, "a typed name links to no registered person");
  assert.deepEqual(invariant(out), invariant(ss));
  assert.equal(out[4], ss[4], "an untouched label keeps its segments as they were");
});

test("naming table: unchanged names keep review state, clearing removes the name, unknown is left alone", async () => {
  const { applySpeakerNames, planSpeakerNames, attendeeNames } = await import("../lib/speakers.ts");
  const ss = [make("S1", 0, "B상무"), make("S2", 1000, "본부장"), make("unknown", 2000)];
  assert.deepEqual(planSpeakerNames(ss, {}), []);
  assert.deepEqual(planSpeakerNames(ss, { S1: "B상무" }), []);
  const out = applySpeakerNames(ss, { S2: "", unknown: "누구" });
  assert.equal(out[0], ss[0]);
  assert.equal(out[1].speaker_name, ""); assert.equal(out[1].confirmed_person_id, null);
  assert.equal(out[2], ss[2]);
  assert.deepEqual(attendeeNames("a 기업 B상무\na 기업 b 본부장, 가나소프트 홍길동 프로\n\n가나소프트 홍길동 프로"), ["a 기업 B상무", "a 기업 b 본부장", "가나소프트 홍길동 프로"]);
});

test("naming table: a label that already carries the chosen name absorbs a newly named one", async () => {
  const { applySpeakerNames, groupSpeakers } = await import("../lib/speakers.ts");
  const ss = [make("S1", 0, "B상무"), make("S2", 1000)];
  const out = applySpeakerNames(ss, { S2: "B상무" });
  assert.deepEqual(groupSpeakers(out).map(g => [g.label, g.count, g.names]), [["S1", 2, ["B상무"]]]);
});

test("naming table: a label fixed segment by segment is left alone unless edited, and stray spaces are no change", async () => {
  const { planSpeakerNames, applySpeakerNames } = await import("../lib/speakers.ts");
  const ss = [make("S1", 0, "A"), make("S1", 1000, "B"), make("S2", 2000, "B  상무")];
  assert.deepEqual(planSpeakerNames(ss, {}), []);
  assert.deepEqual(planSpeakerNames(ss, { S2: "B 상무" }), []);
  const out = applySpeakerNames(ss, { S2: "C" });
  assert.equal(out[0], ss[0]); assert.equal(out[1], ss[1]);
  assert.deepEqual(planSpeakerNames(ss, { S1: "A" }).map(s => s.kind), ["name"], "editing a mixed label unifies it");
});

test("naming table: merging into a confirmed person keeps that person", async () => {
  const { applySpeakerNames } = await import("../lib/speakers.ts");
  const ss = [make("S1", 0, "김예시"), make("S2", 1000)];
  const out = applySpeakerNames(ss, { S2: "김예시" });
  assert.deepEqual(out.map(s => [s.local_speaker_id, s.speaker_name, s.confirmed_person_id]), [["S1", "김예시", person], ["S1", "김예시", person]]);
});
