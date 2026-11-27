import { spawnSync } from "node:child_process";
import { cpSync, existsSync } from "node:fs";
const result=spawnSync(process.execPath,["node_modules/next/dist/bin/next","build","--webpack"],{stdio:"inherit",env:{...process.env,MEETING_RUNTIME:"oci",NEXT_TELEMETRY_DISABLED:"1"}});
if(result.status!==0)process.exit(result.status??1);
for(const [source,target] of [["public",".next/standalone/public"],[".next/static",".next/standalone/.next/static"]])if(existsSync(source))cpSync(source,target,{recursive:true});
console.log("OCI standalone ready: .next/standalone (Node 24, loopback HOSTNAME/PORT required)");
