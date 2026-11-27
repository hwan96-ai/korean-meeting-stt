import test from "node:test";
import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import {audioAttachment} from "../lib/media.ts";
import {processingConfig,publicHeartbeat,readSpeechExecution,registeredLocation,speechClaimFilter,workerConnection} from "../lib/processing.ts";

test("server config disables PC pairing and automatic minutes without widening downloads",()=>{
 assert.equal(readSpeechExecution(undefined),"pc");assert.equal(readSpeechExecution("server"),"server");assert.throws(()=>readSpeechExecution("unknown"));
 assert.deepEqual(processingConfig("server"),{speech_execution:"server",pc_pairing_enabled:false,minutes_generation:"unconfigured",notion_export:"local_only",owner_scoped_downloads:true});
 assert.equal(processingConfig("pc").pc_pairing_enabled,true);assert.equal(processingConfig("pc").minutes_generation,"worker_config");
});
test("server connection requires current registered device, location and a fresh authenticated heartbeat",()=>{
 const now=1700000000000,device={token_hash:"new-server-hash",execution_location:"server"},heartbeat={last_seen:now-1000,execution_location:"server",device_token_hash:device.token_hash,capabilities:{speech:"local/test"}};
 assert.equal(workerConnection("server",heartbeat,device,now).connected,true);
 assert.equal(workerConnection("server",{...heartbeat,last_seen:now-45001},device,now).connected,false);
 assert.equal(workerConnection("server",{...heartbeat,last_seen:now+1},device,now).connected,false);
 assert.equal(workerConnection("server",{...heartbeat,execution_location:"pc"},device,now).connected,false);
 assert.equal(workerConnection("server",{...heartbeat,device_token_hash:"old-token"},device,now).connected,false);
 assert.equal(workerConnection("server",{...heartbeat,device_token_hash:undefined},device,now).connected,false);
 assert.equal(workerConnection("server",heartbeat,null,now).connected,false);
 assert.equal(workerConnection("server",null,device,now).state,"awaiting_heartbeat");
 assert.equal(workerConnection("server",{last_seen:now,capabilities:{execution_location:"server"}},device,now).connected,false);
});
test("legacy PC heartbeat compatibility does not make an old PC heartbeat a server heartbeat",()=>{
 const device={token_hash:"pc-token"},heartbeat={last_seen:10000,capabilities:{speech:"local/whisper"}};
 assert.equal(workerConnection("pc",heartbeat,device,10001).connected,true);
 assert.equal(workerConnection("pc",heartbeat,null,10001).connected,false);
 assert.equal(workerConnection("server",heartbeat,{token_hash:"server",execution_location:"server"},10001).connected,false);
 assert.equal(registeredLocation({token_hash:"server",role:"speech_server"}),"server");
});
test("public heartbeat never exposes the internal credential hash or unrelated stored fields",()=>{
 const raw={last_seen:100,execution_location:"server",device_token_hash:"private-hash",token:"never-return",capabilities:{speech:"local"}};
 assert.deepEqual(publicHeartbeat(raw),{last_seen:100,execution_location:"server",capabilities:{speech:"local"}});assert.equal(publicHeartbeat(null),null);
});
test("speech-only SQL skips minutes tasks but retains meeting-mode speech and legacy pending-less work",()=>{
 const db=new DatabaseSync(":memory:");try{
 db.exec("CREATE TABLE jobs(id TEXT PRIMARY KEY,data TEXT,created_at INTEGER)");
 const add=db.prepare("INSERT INTO jobs VALUES(?,?,?)");
 add.run("minutes-task",JSON.stringify({output_mode:"minutes",pending:"minutes"}),1);
 add.run("meeting-speech",JSON.stringify({output_mode:"minutes",pending:"transcribe"}),2);
 add.run("legacy-speech",JSON.stringify({meeting_type:"customer"}),3);
 add.run("stt-speech",JSON.stringify({output_mode:"transcript",pending:"transcribe"}),4);
 const rows=db.prepare(`SELECT id FROM jobs WHERE 1=1${speechClaimFilter(true)} ORDER BY created_at`).all().map(row=>row.id);
 assert.deepEqual(rows,["meeting-speech","legacy-speech","stt-speech"]);
 assert.equal(db.prepare(`SELECT COUNT(*) AS count FROM jobs WHERE 1=1${speechClaimFilter(false)}`).get().count,4);
 }finally{db.close();}
});
test("original attachment uses safe ASCII fallback and encoded Unicode with no path/header injection",()=>{
 const id="12345678-1234-4234-8234-123456789abc",value=audioAttachment('../../한글 "음성"\r\nInjected: bad\\clip.wav',id);
 assert.ok(value.startsWith(`attachment; filename="audio-${id}.wav"; filename*=UTF-8''`));
 assert.ok(!/[\r\n]/.test(value));assert.ok(!value.includes("../"));assert.ok(!value.includes("\\"));assert.ok(value.includes(encodeURIComponent("한글")));
 assert.ok(!value.includes('"음성"'));assert.doesNotThrow(()=>audioAttachment("broken\ud800.wav",id));
 assert.match(audioAttachment("unsafe.html",id),/\.bin"/);
});
