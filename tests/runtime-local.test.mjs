import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// MEETING_AUTH_MODE=local: loopback-only single-owner login (lib/runtime-node.ts, scripts/local-login.mjs).
const runtime = await import("../lib/runtime-node.ts");
const repo = fileURLToPath(new URL("..", import.meta.url));
const KEYS = ["MEETING_AUTH_MODE", "HOSTNAME", "MEETING_PUBLIC_ORIGIN", "MEETING_OWNER_ID", "MEETING_LOCAL_LOGIN_TOKEN", "MEETING_ACCESS_ISSUER", "MEETING_ACCESS_AUD", "MEETING_OWNER_EMAIL", "MEETING_DATA_DIR"];
function withEnv(values, run) {
  const saved = Object.fromEntries(KEYS.map(k => [k, process.env[k]]));
  for (const k of KEYS) delete process.env[k];
  Object.assign(process.env, values);
  return Promise.resolve().then(run).finally(() => { for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });
}
const SECRET = "s".repeat(20) + "0123456789abcdefghijklmnopqrstuvwxyz";
const SESSION = createHmac("sha256", SECRET).update("owner-session-v1").digest("base64url");
const COOKIE = `kms_owner_3187=${SESSION}`;
const DATA = await mkdtemp(join(tmpdir(), "kms-local-"));
const LOCAL = { MEETING_AUTH_MODE: "local", HOSTNAME: "127.0.0.1", MEETING_PUBLIC_ORIGIN: "http://127.0.0.1:3187", MEETING_OWNER_ID: "owner-1", MEETING_LOCAL_LOGIN_TOKEN: SECRET, MEETING_DATA_DIR: DATA };
const migrated = spawnSync(process.execPath, ["--experimental-strip-types", join(repo, "scripts/migrate-oci.mjs")], { env: { ...process.env, ...LOCAL }, encoding: "utf8" });
assert.equal(migrated.status, 0, migrated.stderr);
const newCode = () => { const r = spawnSync(process.execPath, [join(repo, "scripts/local-login.mjs")], { env: { ...process.env, ...LOCAL }, encoding: "utf8" }); assert.equal(r.status, 0, r.stderr); return new URL(r.stdout.trim()).searchParams.get("code"); };
// What Next's own server adds to a direct loopback request (see lib/integration-gate.ts).
const direct = (host = "127.0.0.1:3187", extra = {}, cookie = COOKIE) =>
  new Headers({ host, "x-forwarded-host": host, "x-forwarded-port": host.split(":")[1] ?? "3187", "x-forwarded-proto": "http", "x-forwarded-for": "127.0.0.1", ...(cookie ? { cookie } : {}), ...extra });
const login = (query, headers = direct("127.0.0.1:3187", {}, "")) =>
  runtime.localLoginResponse(new Request(`http://127.0.0.1:3187/api/local-login${query}`, { headers }));

test("local mode: a direct request with this port's session cookie is the owner", () => withEnv(LOCAL, async () => {
  assert.equal((await runtime.browserIdentity(direct())).userId, "owner-1");
  assert.equal((await runtime.browserIdentity(direct("127.0.0.1:3187", {}, `kms_owner_3187=junk; ${COOKIE}`))).userId, "owner-1", "a planted duplicate cannot lock the owner out");
  assert.equal(runtime.publicOrigin(), "http://127.0.0.1:3187");
  assert.equal(runtime.signInPath("/stt"), "http://127.0.0.1:3187/api/local-login?return_to=%2Fstt");
  assert.equal(runtime.signOutPath("/"), "/api/local-login?logout=1");
}));

test("local mode: no cookie, the raw secret, another port's cookie, other hosts, proxies and rebinding are never the owner", () => withEnv(LOCAL, async () => {
  const cases = [
    direct("127.0.0.1:3187", {}, ""),                            // another OS user or program without a session
    direct("127.0.0.1:3187", {}, `kms_owner_3187=${SECRET}`),    // the login secret itself is not a session
    direct("127.0.0.1:3187", {}, `kms_owner_4187=${SESSION}`),   // another port's cookie name
    direct("localhost:3187"),                                   // loopback, but not the pinned origin (CSRF would fail)
    direct("evil.example:3187"),                                // DNS rebinding keeps the attacker's Host
    direct("127.0.0.1:3187", { "cf-connecting-ip": "203.0.113.9" }),
    direct("127.0.0.1:3187", { "x-forwarded-for": "203.0.113.9" }),
    direct("127.0.0.1:3187", { "x-forwarded-host": "meetings.example.com" }),
    direct("127.0.0.1:3187", { via: "1.1 proxy" }),
    new Headers({}),
  ];
  for (const headers of cases) assert.equal(await runtime.browserIdentity(headers), null, JSON.stringify([...headers]));
}));

test("one-time login code: sets the session cookie once, then is used up; wrong, expired-format or proxied codes fail", () => withEnv(LOCAL, async () => {
  const code = newCode();
  const ok = login(`?code=${code}&return_to=%2Fstt`);
  assert.equal(ok.status, 303);
  assert.equal(ok.headers.get("location"), "http://127.0.0.1:3187/stt");
  assert.equal(ok.headers.get("set-cookie"), `kms_owner_3187=${SESSION}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000`);
  assert.equal(login(`?code=${code}`).status, 403, "a code works once");
  const second = newCode(), third = newCode();
  assert.equal(login(`?code=${second}`).status, 403, "a newer code replaces an unused one");
  assert.equal(login(`?code=${third}`, direct("127.0.0.1:3187", { via: "1.1 proxy" }, "")).status, 403);
  assert.equal(login(`?code=${third}`, direct("localhost:3187", {}, "")).status, 403);
  assert.equal(login(`?code=${third}&return_to=%2F%2Fevil.example`).headers.get("location"), "http://127.0.0.1:3187/stt");
  const unicode = newCode();
  assert.equal(login(`?code=${unicode}&return_to=%2F%ED%95%9C%0D%0Ax`).headers.get("location"), "http://127.0.0.1:3187/stt", "control characters fall back");
  const api = newCode();
  assert.equal(login(`?code=${api}&return_to=%2Fapi%2Fjobs`).headers.get("location"), "http://127.0.0.1:3187/stt", "never bounce into the API");
  const korean = newCode();
  assert.equal(login(`?code=${korean}&return_to=%2F%ED%95%9C`).headers.get("location"), "http://127.0.0.1:3187/%ED%95%9C");
}));

test("login page: a session goes straight through, a cross-site arrival hops once, logout clears the cookie", () => withEnv(LOCAL, async () => {
  assert.equal(login("?return_to=%2Fstt", direct()).headers.get("location"), "http://127.0.0.1:3187/stt");
  const hop = login("?return_to=%2Fstt");
  assert.equal(hop.status, 200);
  assert.match(await hop.text(), /http-equiv="refresh" content="0;url=\/api\/local-login\?check=1&amp;return_to=%2Fstt"/);
  const ask = login("?check=1&return_to=%2Fstt");
  assert.equal(ask.status, 401);
  for (const r of [hop, ask]) { assert.equal(r.headers.get("set-cookie"), null); assert.equal(r.headers.get("x-frame-options"), "DENY"); }
  const out = login("?logout=1", direct());
  assert.equal(out.headers.get("set-cookie"), "kms_owner_3187=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0");
}));

test("local mode refuses a non-loopback bind or origin, a short secret, or Access settings next to it", () => withEnv({}, async () => {
  const bad = [
    { ...LOCAL, HOSTNAME: "0.0.0.0" }, { ...LOCAL, HOSTNAME: "localhost" }, { ...LOCAL, HOSTNAME: undefined },
    { ...LOCAL, MEETING_PUBLIC_ORIGIN: "https://meetings.example.com" },
    { ...LOCAL, MEETING_PUBLIC_ORIGIN: "http://198.51.100.10:3187" },
    { ...LOCAL, MEETING_PUBLIC_ORIGIN: "http://localhost:3187" },
    { ...LOCAL, MEETING_PUBLIC_ORIGIN: "http://127.0.0.1:3187/path" },
    { ...LOCAL, MEETING_ACCESS_ISSUER: "https://team.cloudflareaccess.com" },
    { ...LOCAL, MEETING_ACCESS_AUD: "aud" },
    { ...LOCAL, MEETING_LOCAL_LOGIN_TOKEN: "short" }, { ...LOCAL, MEETING_LOCAL_LOGIN_TOKEN: undefined },
  ];
  for (const values of bad) {
    await withEnv(Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined)), async () => {
      await assert.rejects(runtime.browserIdentity(direct()), Error, JSON.stringify(values));
    });
  }
  const refused = spawnSync(process.execPath, [join(repo, "scripts/local-login.mjs")], { env: { ...process.env, ...LOCAL, MEETING_PUBLIC_ORIGIN: "http://198.51.100.10:3187" }, encoding: "utf8" });
  assert.notEqual(refused.status, 0, "no login address is printed for a non-loopback origin");
  assert.equal(refused.stdout, "");
}));

test("without MEETING_AUTH_MODE the Access path runs and local login does not exist", () => withEnv({
  HOSTNAME: "127.0.0.1", MEETING_PUBLIC_ORIGIN: "https://meetings.example.com", MEETING_OWNER_ID: "owner-1", MEETING_LOCAL_LOGIN_TOKEN: SECRET,
  MEETING_ACCESS_ISSUER: "https://example.cloudflareaccess.com", MEETING_ACCESS_AUD: "aud", MEETING_OWNER_EMAIL: "owner@example.com",
}, async () => {
  assert.equal(runtime.localMode(), false);
  assert.equal(await runtime.browserIdentity(direct()), null);
  assert.equal(login("?code=x"), null);
  assert.equal(runtime.publicOrigin(), "https://meetings.example.com");
  assert.match(runtime.signInPath("/"), /^\/cdn-cgi\/access\/login/);
  assert.equal(runtime.signOutPath("/"), "/cdn-cgi/access/logout");
}));

test("a mistyped mode stops the module from loading", () => {
  const r = spawnSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", "await import('./lib/runtime-node.ts')"],
    { cwd: repo, env: { ...process.env, MEETING_AUTH_MODE: "Local" }, encoding: "utf8" });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /MEETING_AUTH_MODE must be local or access/);
});
