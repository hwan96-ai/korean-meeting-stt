// Web Push sending: VAPID sign-in (RFC 8292) and aes128gcm payload encryption (RFC 8291).
// WebCrypto only, so the OCI Node build and the Sites build share it and tests can run it under node.
const subtle = globalThis.crypto.subtle;
const text = (value: string) => new TextEncoder().encode(value);
const RECORD_SIZE = 4096;
// One record: payload + delimiter + 16-byte tag must fit, and push services cap the body near 4 KB.
export const MAX_PAYLOAD_BYTES = 3000;

export function b64uEncode(bytes: Uint8Array) {
  let s = ""; for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export function b64uDecode(value: string) {
  if (!/^[A-Za-z0-9_-]*$/.test(value)) throw new Error("not base64url");
  const s = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4));
  return Uint8Array.from(s, c => c.charCodeAt(0));
}
function concat(...parts: Uint8Array[]) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}
const buf = (bytes: Uint8Array) => bytes as unknown as BufferSource;
async function hkdf(salt: Uint8Array, ikm: Uint8Array, info: Uint8Array, length: number) {
  const key = await subtle.importKey("raw", buf(ikm), "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: buf(salt), info: buf(info) }, key, length * 8));
}
// A P-256 public key in uncompressed form (65 bytes, 0x04 || x || y).
export function isP256Public(bytes: Uint8Array) { return bytes.length === 65 && bytes[0] === 4; }
function jwkOf(publicKey: Uint8Array, d?: string): JsonWebKey {
  return { kty: "EC", crv: "P-256", x: b64uEncode(publicKey.slice(1, 33)), y: b64uEncode(publicKey.slice(33, 65)), ...(d ? { d } : {}) };
}
export async function importEcdhPair(publicKey: string, privateKey: string): Promise<CryptoKeyPair> {
  const raw = b64uDecode(publicKey);
  return { publicKey: await subtle.importKey("raw", buf(raw), { name: "ECDH", namedCurve: "P-256" }, true, []), privateKey: await subtle.importKey("jwk", jwkOf(raw, privateKey), { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]) };
}

export type PushTarget = { endpoint: string; p256dh: string; auth: string };
// RFC 8291 section 3: one aes128gcm record keyed from an ECDH secret with the browser's key and its auth secret.
// `fixed` only exists so the RFC worked example can be checked; real sends use a fresh key pair and salt.
export async function encryptPayload(payload: Uint8Array, target: Pick<PushTarget, "p256dh" | "auth">, fixed?: { salt: Uint8Array; serverKeys: CryptoKeyPair }) {
  if (payload.length > MAX_PAYLOAD_BYTES) throw new Error("payload too large");
  const uaPublic = b64uDecode(target.p256dh), authSecret = b64uDecode(target.auth);
  if (!isP256Public(uaPublic) || authSecret.length !== 16) throw new Error("bad subscription keys");
  const serverKeys = fixed?.serverKeys ?? await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]) as CryptoKeyPair;
  const salt = fixed?.salt ?? crypto.getRandomValues(new Uint8Array(16));
  const asPublic = new Uint8Array(await subtle.exportKey("raw", serverKeys.publicKey));
  const uaKey = await subtle.importKey("raw", buf(uaPublic), { name: "ECDH", namedCurve: "P-256" }, false, []);
  const ecdhSecret = new Uint8Array(await subtle.deriveBits({ name: "ECDH", public: uaKey }, serverKeys.privateKey, 256));
  const ikm = await hkdf(authSecret, ecdhSecret, concat(text("WebPush: info\0"), uaPublic, asPublic), 32);
  const cek = await hkdf(salt, ikm, text("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, text("Content-Encoding: nonce\0"), 12);
  const aes = await subtle.importKey("raw", buf(cek), "AES-GCM", false, ["encrypt"]);
  // 0x02 marks the last (and only) record; no padding.
  const sealed = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv: buf(nonce) }, aes, buf(concat(payload, Uint8Array.of(2)))));
  const header = new Uint8Array(21); header.set(salt, 0); new DataView(header.buffer).setUint32(16, RECORD_SIZE); header[20] = asPublic.length;
  return concat(header, asPublic, sealed);
}

export type VapidKeys = { publicKey: string; privateKey: string; subject: string };
// RFC 8292: a short-lived ES256 token for the push service's origin. WebCrypto signs in the raw r||s form JWS uses.
export async function vapidAuthorization(endpoint: string, keys: VapidKeys, now = Date.now()) {
  const key = await subtle.importKey("jwk", jwkOf(b64uDecode(keys.publicKey), keys.privateKey), { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const part = (value: object) => b64uEncode(text(JSON.stringify(value)));
  const unsigned = `${part({ typ: "JWT", alg: "ES256" })}.${part({ aud: new URL(endpoint).origin, exp: Math.floor(now / 1000) + 12 * 3600, sub: keys.subject })}`;
  const signature = new Uint8Array(await subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, buf(text(unsigned))));
  return `vapid t=${unsigned}.${b64uEncode(signature)}, k=${keys.publicKey}`;
}

// The server POSTs to whatever endpoint a browser hands it, so only the browser vendors' push services pass.
const PUSH_HOSTS = ["fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com"];
export function allowedEndpoint(endpoint: string) {
  let url: URL; try { url = new URL(endpoint); } catch { return false; }
  if (url.protocol !== "https:" || url.port || url.username || url.password || endpoint.length > 1000) return false;
  return PUSH_HOSTS.some(host => url.hostname === host || url.hostname.endsWith(`.${host}`));
}

export type Fetch = (input: string, init: RequestInit) => Promise<Response>;
// Returns the push service's HTTP status (201 sent, 404/410 the subscription is gone).
export async function sendPush(target: PushTarget, payload: string, keys: VapidKeys, fetcher: Fetch = fetch, now = Date.now()) {
  if (!allowedEndpoint(target.endpoint)) throw new Error("endpoint not allowed");
  const body = await encryptPayload(text(payload), target);
  const response = await fetcher(target.endpoint, { method: "POST", redirect: "error", signal: AbortSignal.timeout(10000), body: buf(body) as BodyInit,
    headers: { Authorization: await vapidAuthorization(target.endpoint, keys, now), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", TTL: "86400", Urgency: "high" } });
  await response.body?.cancel();
  return response.status;
}
