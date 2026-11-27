import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { existsSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { AccessDenied, createAccessVerifier, type AccessConfig } from "./runtime-access.ts";
import { SQLiteDatabase, FileBucket } from "./runtime-storage.ts";
import {readSpeechExecution} from "./processing.ts";
import { fromLoopbackOnly } from "./integration-gate.ts";

// MEETING_AUTH_MODE=local: single-owner login on the owner's own computer, instead of Cloudflare Access.
// Off unless set. Every request refuses (fails closed) unless the server binds 127.0.0.1, the public origin is
// http://127.0.0.1:<port> and no Access setting is present; scripts/local-run.sh also checks this before listening.
// A browser request is the owner only when all hold:
// - it is addressed to that exact origin's Host (a DNS-rebinding page or another name is refused),
// - it carries no proxy or tunnel headers (fromLoopbackOnly; a raw TCP forward adds none, so never forward the port),
// - it carries this port's owner session cookie.
// The session cookie is HMAC(MEETING_LOCAL_LOGIN_TOKEN) and never the secret itself; rotating the secret
// (scripts/local-setup.mjs --rotate-login) revokes every session. A browser gets it once through a one-time code
// that scripts/local-run.sh login writes (hashed, 10 minutes) into the database; the code is used up on first use.
// The cookie is HttpOnly and SameSite=Strict. Cookies are not separated by port, so any other web service the owner
// opens on 127.0.0.1 in the same browser can receive it: this mode is for a computer one person uses.
const LOCAL_COOKIE_PREFIX = "kms_owner_";
const LOGIN_CODE_SETTING = "local_login_code";
const MODE = process.env.MEETING_AUTH_MODE;
if (MODE !== undefined && MODE !== "" && MODE !== "local" && MODE !== "access") throw new Error("MEETING_AUTH_MODE must be local or access");
export function localMode(){return process.env.MEETING_AUTH_MODE==="local";}
function localOrigin(){
  if(process.env.HOSTNAME!=="127.0.0.1")throw new Error("Local auth mode requires HOSTNAME=127.0.0.1 (loopback bind)");
  if(process.env.MEETING_ACCESS_ISSUER||process.env.MEETING_ACCESS_AUD)throw new Error("Local auth mode cannot be combined with Cloudflare Access settings");
  const value=required("MEETING_PUBLIC_ORIGIN"),url=new URL(value);
  if(url.protocol!=="http:"||url.hostname!=="127.0.0.1"||!url.port||url.origin!==value||url.username||url.password)throw new Error("Local auth mode needs MEETING_PUBLIC_ORIGIN=http://127.0.0.1:<port>");
  return url;
}
function localSecret(){const value=required("MEETING_LOCAL_LOGIN_TOKEN");if(value.length<40)throw new Error("MEETING_LOCAL_LOGIN_TOKEN must contain at least 40 random characters");return value;}
function sessionCookieName(){return LOCAL_COOKIE_PREFIX+localOrigin().port;}
function sessionValue(){return createHmac("sha256",localSecret()).update("owner-session-v1").digest("base64url");}
function sameSecret(a:string,b:string){const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);}
function cookies(headers:Headers,name:string){const out:string[]=[];for(const part of (headers.get("cookie")??"").split(";")){const i=part.indexOf("=");if(i>0&&part.slice(0,i).trim()===name)out.push(part.slice(i+1).trim());}return out;}
function directLocal(headers:Headers){return headers.get("host")?.toLowerCase()===localOrigin().host&&fromLoopbackOnly(headers);}
function hasSession(headers:Headers){const expected=sessionValue();return cookies(headers,sessionCookieName()).some(v=>sameSecret(v,expected));}
function localIdentity(headers:Headers){
  const owner=required("MEETING_OWNER_ID");
  if(!directLocal(headers)||!hasSession(headers))return null;
  return {userId:owner,email:"owner@localhost",displayName:"Owner",fullName:null};
}
const LOCAL_PAGE_HEADERS={"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store","X-Frame-Options":"DENY","Content-Security-Policy":"default-src 'none'; frame-ancestors 'none'","Referrer-Policy":"no-referrer"};
function localPage(status:number,message:string,extraHead=""){return new Response(`<!doctype html><meta charset="utf-8">${extraHead}<title>로그인</title><p style="font:16px sans-serif;margin:3em">${message}</p>`,{status,headers:LOCAL_PAGE_HEADERS});}
function returnPath(value:string|null){const v=value??"/stt";return v.startsWith("/")&&!v.startsWith("//")&&!v.startsWith("/\\")&&!v.startsWith("/api")&&!/[\u0000-\u001f\u007f]/.test(v)?v:"/stt";}
function redirect(origin:URL,path:string,cookie?:string){const headers:Record<string,string>={Location:new URL(path,origin).href,"Cache-Control":"no-store","Referrer-Policy":"no-referrer"};if(cookie)headers["Set-Cookie"]=cookie;return new Response(null,{status:303,headers});}
// One-time code: stored as {"hash","expires"}; used up atomically by deleting exactly the row it matched.
function useLoginCode(code:string){
  const db=runtimeDatabase().connection,row=db.prepare("SELECT value FROM settings WHERE key=?").get(LOGIN_CODE_SETTING) as {value:string}|undefined;
  if(!row)return false;
  let stored:{hash?:string,expires?:number};try{stored=JSON.parse(row.value);}catch{return false;}
  const hash=createHash("sha256").update(code).digest("hex");
  if(typeof stored.hash!=="string"||typeof stored.expires!=="number"||stored.expires<Date.now()||!sameSecret(hash,stored.hash))return false;
  return Number(db.prepare("DELETE FROM settings WHERE key=? AND value=?").run(LOGIN_CODE_SETTING,row.value).changes)===1;
}
// GET /api/local-login: ?code= (one-time, from scripts/local-run.sh login) | ?logout=1 | ?check=1. Null outside local mode.
export function localLoginResponse(request:Request):Response|null{
  if(!localMode())return null;
  const origin=localOrigin(),url=new URL(request.url),back=returnPath(url.searchParams.get("return_to")),name=sessionCookieName();
  if(request.method!=="GET")return localPage(405,"GET만 받습니다.");
  if(!directLocal(request.headers))return localPage(403,`이 컴퓨터에서 ${origin.origin} 로 직접 연 요청만 로그인할 수 있습니다. 프록시나 포트 전달을 거친 요청은 받지 않습니다.`);
  if(url.searchParams.get("logout")==="1"){const page=localPage(200,"로그아웃했습니다. 다시 들어오려면 <code>scripts/local-run.sh login</code> 을 실행하세요.");page.headers.set("Set-Cookie",`${name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`);return page;}
  const code=url.searchParams.get("code");
  if(code){
    if(!useLoginCode(code))return localPage(403,"로그인 코드가 맞지 않거나, 이미 썼거나, 10분이 지났습니다. <code>scripts/local-run.sh login</code> 을 다시 실행하세요.");
    return redirect(origin,back,`${name}=${sessionValue()}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000`);
  }
  if(hasSession(request.headers))return redirect(origin,back);
  // A link from another site carries no Strict cookie: hop once within this origin, then ask for a code.
  if(url.searchParams.get("check")!=="1")return localPage(200,"확인 중입니다…",`<meta http-equiv="refresh" content="0;url=/api/local-login?check=1&amp;return_to=${encodeURIComponent(back)}">`);
  return localPage(401,"로그인이 필요합니다. 이 컴퓨터의 터미널에서 <code>scripts/local-run.sh login</code> 을 실행하고 나온 주소를 10분 안에 여세요.");
}
export const authMode = localMode() ? "local" : "access";
export const fixedOwner = true;
export function speechExecution(){return readSpeechExecution(process.env.MEETING_SPEECH_EXECUTION);}
function required(name: string) { const value=process.env[name];if(!value)throw new Error(`Missing ${name}`);return value; }
function config(): AccessConfig { return {issuer:required("MEETING_ACCESS_ISSUER"),audience:required("MEETING_ACCESS_AUD"),ownerEmail:required("MEETING_OWNER_EMAIL"),ownerId:required("MEETING_OWNER_ID"),ownerAliases:(process.env.MEETING_OWNER_EMAIL_ALIASES??"").split(",").map(x=>x.trim()).filter(Boolean)}; }
let verifier:ReturnType<typeof createAccessVerifier>|undefined, database:SQLiteDatabase|undefined, objects:FileBucket|undefined;
function directory(){const value=required("MEETING_DATA_DIR");if(!isAbsolute(value))throw new Error("MEETING_DATA_DIR must be absolute");return value;}
export function runtimeDatabase(){if(!database){const path=join(directory(),"meeting.sqlite");if(!existsSync(path))throw new Error("Run migrate:oci before starting");const candidate=new SQLiteDatabase(path);const row=candidate.connection.prepare("SELECT value FROM settings WHERE key='owner'").get() as {value:string}|undefined;if(row?.value!==required("MEETING_OWNER_ID")){candidate.close();throw new Error("Configured owner does not match migrated database");}database=candidate;}return database;}
export function runtimeBucket(){return objects??=new FileBucket(join(directory(),"objects"));}
export function publicOrigin(_request?:Request){if(localMode())return localOrigin().origin;const value=required("MEETING_PUBLIC_ORIGIN"),url=new URL(value);if(url.protocol!=="https:"||url.origin!==value||url.username||url.password)throw new Error("Invalid public origin");return value;}
export async function browserIdentity(headers: Headers){if(localMode())return localIdentity(headers);verifier??=createAccessVerifier(config());try{return await verifier(headers);}catch(e){if(e instanceof AccessDenied)return null;throw e;}}
export async function workerGateway(request:Request){const expected=required("MEETING_WORKER_GATEWAY_TOKEN"),actual=request.headers.get("oai-sites-authorization")??"";if(expected.length<40)throw new Error("Worker gateway token must contain at least 40 random characters");const a=Buffer.from(actual),b=Buffer.from(expected);return a.length===b.length&&timingSafeEqual(a,b);}
export function signInPath(returnTo:string){if(localMode())return `${localOrigin().origin}/api/local-login?return_to=${encodeURIComponent(returnTo)}`;return `/cdn-cgi/access/login?redirect_url=${encodeURIComponent(publicOrigin()+returnTo)}`;}
export function signOutPath(_returnTo:string){if(localMode())return "/api/local-login?logout=1";return "/cdn-cgi/access/logout";}
// Orchestrator door (/api/integration/v1). Unset means the door does not exist (every path 404).
export function integrationToken(){return process.env.INTEGRATION_TOKEN||null;}
// New calls accepted per Korean day through the door (default 200).
export function integrationDailyLimit(){const n=Number(process.env.INTEGRATION_DAILY_CALL_LIMIT);return Number.isInteger(n)&&n>0?n:200;}
// Phone push (VAPID key pair, unpadded base64url). Both unset means the feature is off: no button, no sends.
// The subject is the public origin, so no email address goes to the push services.
export function pushKeys(){const publicKey=process.env.MEETING_VAPID_PUBLIC_KEY,privateKey=process.env.MEETING_VAPID_PRIVATE_KEY;return publicKey&&privateKey?{publicKey,privateKey,subject:publicOrigin()}:null;}
