// Who may use /api/integration/v1: only a caller on this server (the orchestrator), never the public tunnel.
//
// The web server listens on 127.0.0.1 only, and the Cloudflare tunnel (cloudflared) also connects from
// 127.0.0.1, so the socket address alone cannot tell them apart. Requests through the tunnel still differ:
// - Host stays the public name (the tunnel ingress has no httpHostHeader override), and the edge cannot be
//   asked to route a request for "127.0.0.1" to this tunnel.
// - Cloudflare's edge always adds CF-Ray, CF-Connecting-IP, CDN-Loop, X-Forwarded-For; a client cannot remove them.
// Next's own server always fills x-forwarded-host/port/proto/for when absent (next/dist/server/base-server.js,
// `??=` with the Host header, its listening port, "http" and the socket address). So these four headers are
// present on every request the route sees; "no proxy header at all" cannot be tested here. Instead each one must
// carry exactly the value Next itself would put there for a direct loopback call. Any other value, and any other
// proxy header (cf-*, cdn-loop, via, forwarded, x-real-ip, true-client-ip, other x-forwarded-*), means 404.
// Every check must pass; then the bearer token.
const LOOPBACK_HOST = /^(?:127\.0\.0\.1|localhost|\[::1\])(?::(\d{1,5}))?$/i;
const LOOPBACK_ADDRESS = /^(?:127\.0\.0\.1|::1|::ffff:127\.0\.0\.1)$/i;
const NEXT_FILLED = ["x-forwarded-host", "x-forwarded-port", "x-forwarded-proto", "x-forwarded-for"];
const EDGE_HEADERS = ["cdn-loop", "forwarded", "via", "x-real-ip", "true-client-ip"];

export function fromLoopbackOnly(headers: Headers): boolean {
  const host = headers.get("host"), hostMatch = host ? LOOPBACK_HOST.exec(host) : null;
  if (!host || !hostMatch) return false;
  const forwardedHost = headers.get("x-forwarded-host");
  if (forwardedHost !== null && forwardedHost.toLowerCase() !== host.toLowerCase()) return false;
  const forwardedPort = headers.get("x-forwarded-port");
  if (forwardedPort !== null && (!/^\d{1,5}$/.test(forwardedPort) || (hostMatch[1] !== undefined && forwardedPort !== hostMatch[1]))) return false;
  const forwardedProto = headers.get("x-forwarded-proto");
  if (forwardedProto !== null && forwardedProto !== "http") return false;
  const forwardedFor = headers.get("x-forwarded-for");
  if (forwardedFor !== null && !LOOPBACK_ADDRESS.test(forwardedFor)) return false;
  for (const [name] of headers) if (name.startsWith("cf-") || EDGE_HEADERS.includes(name) || (name.startsWith("x-forwarded-") && !NEXT_FILLED.includes(name))) return false;
  return true;
}

// Constant-time token check: both sides are hashed to 32 bytes first, so neither length nor content leaks
// through timing. The configured token must be long (32 random bytes encode to at least 43 characters).
export const MIN_TOKEN_LENGTH = 43;
export async function bearerMatches(expected: string, authorization: string | null): Promise<boolean> {
  const presented = /^Bearer ([^\s]+)$/.exec(authorization ?? "")?.[1] ?? "";
  const [a, b] = await Promise.all([expected, presented].map(async v => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(v)))));
  let diff = presented.length ? 0 : 1;
  for (let i = 0; i < 32; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
