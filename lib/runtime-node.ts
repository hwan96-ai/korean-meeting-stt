import { timingSafeEqual } from "node:crypto";
import { existsSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { AccessDenied, createAccessVerifier, type AccessConfig } from "./runtime-access.ts";
import { SQLiteDatabase, FileBucket } from "./runtime-storage.ts";
import {readSpeechExecution} from "./processing.ts";

export const authMode = "access";
export const fixedOwner = true;
export function speechExecution(){return readSpeechExecution(process.env.MEETING_SPEECH_EXECUTION);}
function required(name: string) { const value=process.env[name];if(!value)throw new Error(`Missing ${name}`);return value; }
function config(): AccessConfig { return {issuer:required("MEETING_ACCESS_ISSUER"),audience:required("MEETING_ACCESS_AUD"),ownerEmail:required("MEETING_OWNER_EMAIL"),ownerId:required("MEETING_OWNER_ID"),ownerAliases:(process.env.MEETING_OWNER_EMAIL_ALIASES??"").split(",").map(x=>x.trim()).filter(Boolean)}; }
let verifier:ReturnType<typeof createAccessVerifier>|undefined, database:SQLiteDatabase|undefined, objects:FileBucket|undefined;
function directory(){const value=required("MEETING_DATA_DIR");if(!isAbsolute(value))throw new Error("MEETING_DATA_DIR must be absolute");return value;}
export function runtimeDatabase(){if(!database){const path=join(directory(),"meeting.sqlite");if(!existsSync(path))throw new Error("Run migrate:oci before starting");const candidate=new SQLiteDatabase(path);const row=candidate.connection.prepare("SELECT value FROM settings WHERE key='owner'").get() as {value:string}|undefined;if(row?.value!==required("MEETING_OWNER_ID")){candidate.close();throw new Error("Configured owner does not match migrated database");}database=candidate;}return database;}
export function runtimeBucket(){return objects??=new FileBucket(join(directory(),"objects"));}
export function publicOrigin(_request?:Request){const value=required("MEETING_PUBLIC_ORIGIN"),url=new URL(value);if(url.protocol!=="https:"||url.origin!==value||url.username||url.password)throw new Error("Invalid public origin");return value;}
export async function browserIdentity(headers: Headers){verifier??=createAccessVerifier(config());try{return await verifier(headers);}catch(e){if(e instanceof AccessDenied)return null;throw e;}}
export async function workerGateway(request:Request){const expected=required("MEETING_WORKER_GATEWAY_TOKEN"),actual=request.headers.get("oai-sites-authorization")??"";if(expected.length<40)throw new Error("Worker gateway token must contain at least 40 random characters");const a=Buffer.from(actual),b=Buffer.from(expected);return a.length===b.length&&timingSafeEqual(a,b);}
export function signInPath(returnTo:string){return `/cdn-cgi/access/login?redirect_url=${encodeURIComponent(publicOrigin()+returnTo)}`;}
export function signOutPath(_returnTo:string){return "/cdn-cgi/access/logout";}
// Orchestrator door (/api/integration/v1). Unset means the door does not exist (every path 404).
export function integrationToken(){return process.env.INTEGRATION_TOKEN||null;}
// New calls accepted per Korean day through the door (default 200).
export function integrationDailyLimit(){const n=Number(process.env.INTEGRATION_DAILY_CALL_LIMIT);return Number.isInteger(n)&&n>0?n:200;}
// Phone push (VAPID key pair, unpadded base64url). Both unset means the feature is off: no button, no sends.
// The subject is the public origin, so no email address goes to the push services.
export function pushKeys(){const publicKey=process.env.MEETING_VAPID_PUBLIC_KEY,privateKey=process.env.MEETING_VAPID_PRIVATE_KEY;return publicKey&&privateKey?{publicKey,privateKey,subject:publicOrigin()}:null;}
