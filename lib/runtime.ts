// Sites runtime. The OCI production build aliases this module to runtime-node.ts.
import { env } from "cloudflare:workers";
import type { Bucket, Database, Identity } from "./runtime-types";
import type { SpeechExecution } from "./processing";
export const authMode = "sites";
export const fixedOwner = false;
export function speechExecution():SpeechExecution{return "pc";}
export function runtimeDatabase(): Database { return env.DB as unknown as Database; }
export function runtimeBucket(): Bucket { return env.BUCKET as unknown as Bucket; }
export function publicOrigin(request: Request): string { return new URL(request.url).origin; }
export async function browserIdentity(headers: Headers): Promise<Identity | null> {
  const userId = headers.get("oai-authenticated-user-id"), email = headers.get("oai-authenticated-user-email");
  if (!userId || !email) return null;
  let fullName: string | null = null;
  if (headers.get("oai-authenticated-user-full-name-encoding") === "percent-encoded-utf-8") {
    try { fullName = decodeURIComponent(headers.get("oai-authenticated-user-full-name") ?? "") || null; } catch { /* Ignore invalid optional display name. */ }
  }
  return { userId, email, displayName: fullName ?? email, fullName };
}
export async function workerGateway(_request: Request): Promise<boolean> { return true; }
export function signInPath(returnTo: string) { return `/signin-with-chatgpt?return_to=${encodeURIComponent(returnTo)}`; }
export function signOutPath(returnTo: string) { return `/signout-with-chatgpt?return_to=${encodeURIComponent(returnTo)}`; }
// The Sites build has no orchestrator door.
export function integrationToken():string|null{return null;}
export function integrationDailyLimit():number{return 200;}
// The Sites build sends no phone push.
export function pushKeys():{publicKey:string;privateKey:string;subject:string}|null{return null;}
