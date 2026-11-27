import type { Identity } from "./runtime-types.ts";

// ownerAliases: extra Access login emails that sign in as the same single owner.
export interface AccessConfig { issuer: string; audience: string; ownerEmail: string; ownerId: string; ownerAliases?: string[]; }
type Jwk = JsonWebKey & { kid?: string };
export class AccessDenied extends Error {}
const deny = (): never => { throw new AccessDenied("Access authentication required"); };
function decodePart(value: string) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return deny();
  const raw = atob(value.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
function jsonPart(value: string): Record<string, unknown> {
  const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decodePart(value)));
  if (!data || typeof data !== "object" || Array.isArray(data)) return deny();
  return data;
}
export function validateAccessConfig(config: AccessConfig): void {
  const issuer = new URL(config.issuer);
  if (issuer.protocol !== "https:" || !/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(issuer.hostname) || issuer.origin !== config.issuer || !config.audience || !config.ownerId || [config.ownerEmail, ...(config.ownerAliases ?? [])].some(email => !/^[^\s@]+@[^\s@]+$/.test(email))) throw new Error("Invalid Access configuration");
}

// Bounded cache; unknown kid forces at most one refresh per 30 seconds. Never fetch a JWT-supplied URL.
export function createAccessVerifier(config: AccessConfig, fetcher: typeof fetch = fetch, clock = () => Date.now()) {
  validateAccessConfig(config);
  const allowedEmails = new Set([config.ownerEmail, ...(config.ownerAliases ?? [])].map(email => email.toLowerCase()));
  let keys: Jwk[] = [], expires = 0, refreshed = -Infinity, nextAttempt = -Infinity;
  let pending: Promise<void> | undefined;
  async function refresh() {
    if (pending) return pending;
    if (clock() < nextAttempt) throw new AccessDenied("Access keys unavailable");
    nextAttempt = clock() + 30_000;
    pending = (async () => {
      const response = await fetcher(`${config.issuer}/cdn-cgi/access/certs`, { redirect: "error", signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new AccessDenied("Access keys unavailable");
      const reader=response.body?.getReader();if(!reader)throw new AccessDenied("Access keys unavailable");let bytes=0;const chunks:Uint8Array[]=[];
      for(;;){const x=await reader.read();if(x.done)break;bytes+=x.value.length;if(bytes>262144){await reader.cancel();throw new AccessDenied("Invalid key response");}chunks.push(x.value);}
      const joined=new Uint8Array(bytes);let offset=0;for(const c of chunks){joined.set(c,offset);offset+=c.length;}
      const body = JSON.parse(new TextDecoder().decode(joined));
      if (!Array.isArray(body.keys) || body.keys.length > 30) throw new AccessDenied("Invalid key response");
      keys = body.keys.filter((k: Jwk) => k.kty === "RSA" && typeof k.kid === "string" && (!k.alg || k.alg === "RS256") && (!k.use || k.use === "sig"));
      refreshed = clock(); expires = refreshed + 5 * 60_000;
    })().finally(() => { pending = undefined; });
    return pending;
  }
  return async function verify(headers: Headers): Promise<Identity | null> {
    const token = headers.get("cf-access-jwt-assertion");
    if (!token) return null;
    try {
      if (token.length > 16384) return deny();
      const parts = token.split("."); if (parts.length !== 3) return deny();
      const header = jsonPart(parts[0]), claims = jsonPart(parts[1]);
      if (header.alg !== "RS256" || typeof header.kid !== "string" || header.crit !== undefined) return deny();
      const now = clock() / 1000;
      if (claims.iss !== config.issuer || !(Array.isArray(claims.aud) ? claims.aud : [claims.aud]).includes(config.audience) ||
        typeof claims.exp !== "number" || !Number.isFinite(claims.exp) || claims.exp <= now ||
        (claims.nbf !== undefined && (typeof claims.nbf !== "number" || !Number.isFinite(claims.nbf) || claims.nbf > now)) ||
        typeof claims.iat !== "number" || !Number.isFinite(claims.iat) || claims.iat > now + 30 ||
        typeof claims.sub !== "string" || !claims.sub || typeof claims.email !== "string" || !allowedEmails.has(claims.email.toLowerCase()) ||
        (claims.type !== undefined && claims.type !== "app")) return deny();
      if (clock() >= expires) await refresh();
      let jwk = keys.find(k => k.kid === header.kid);
      if (!jwk && clock() - refreshed >= 30_000) { await refresh(); jwk = keys.find(k => k.kid === header.kid); }
      if (!jwk) return deny();
      const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
      if (!await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, decodePart(parts[2]), new TextEncoder().encode(`${parts[0]}.${parts[1]}`))) return deny();
      return { userId: config.ownerId, email: config.ownerEmail, displayName: config.ownerEmail, fullName: null };
    } catch { return deny(); }
  };
}
