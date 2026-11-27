// One-command local setup for a single owner on this Linux computer (also WSL2): web app in
// MEETING_AUTH_MODE=local plus the speech worker in server mode, both on 127.0.0.1 only.
//
//   node scripts/local-setup.mjs [--dir ~/.korean-meeting-stt] [--port 3187] [--rotate-login]
//   scripts/local-run.sh web      # in one terminal
//   scripts/local-run.sh worker   # in another (first run downloads models)
//   scripts/local-run.sh login    # prints the one-time owner login address for your browser
//
// Creates <dir>/env (web settings and the owner login token, 0600), <dir>/credentials/worker.json (worker
// settings, 0600), the SQLite database with the owner fixed, and the worker registration (token SHA-256 only).
// Running it again keeps existing tokens, owner and database, and only fills in what is missing; --rotate-login
// replaces the login secret, which signs every browser out. No secret is printed.
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmdirSync, writeFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const option = (name, fallback) => { const i = args.indexOf(name); return i >= 0 && args[i + 1] ? args[i + 1] : fallback; };
const defaultDir = join(homedir(), ".korean-meeting-stt");
const dir = resolve(option("--dir", defaultDir));
const port = Number(option("--port", "3187"));
const fail = message => { console.error(message); process.exit(1); };
if (process.platform !== "linux") fail("Linux (or WSL2) only: the speech worker needs a Linux user and private folders");
if (process.getuid() === 0) fail("Run as your normal user, not root (the worker refuses root)");
if (!Number.isInteger(port) || port < 1024 || port > 65535) fail("--port must be 1024-65535");
// Values go into a KEY=VALUE file; keep the path plain so no reader can misparse it.
if (!/^\/[A-Za-z0-9._\/-]+$/.test(dir)) fail("--dir must be an absolute path of letters, digits, '.', '_', '-' and '/' only");
const repo = fileURLToPath(new URL("..", import.meta.url));
// Never inside the repository: public/ is copied into the web build, and other folders could be committed.
if (dir === resolve(repo) || dir.startsWith(resolve(repo) + "/")) fail("--dir must be outside the repository");
// A folder is ours only if it carries the marker this script writes first; never take over another app's folder.
const MARKER = ".kms-setup";
if (existsSync(dir) && readdirSync(dir).length && !existsSync(join(dir, MARKER))) fail(`${dir} exists and is not a setup folder made by this script; choose an empty or new --dir`);
const rotate = args.includes("--rotate-login");
const token = () => randomBytes(48).toString("base64url"); // 64 URL-safe characters
const privateDir = path => { mkdirSync(path, { recursive: true, mode: 0o700 }); chmodSync(path, 0o700); return path; };
// Write next to the target, then rename: an interrupted run never leaves a half-written settings file.
const privateFile = (path, text) => { const tmp = `${path}.tmp-${process.pid}`; writeFileSync(tmp, text, { mode: 0o600 }); chmodSync(tmp, 0o600); renameSync(tmp, path); };
const readEnv = path => Object.fromEntries(readFileSync(path, "utf8").split("\n").map(l => l.replace(/\r$/, "")).filter(l => /^[A-Z_][A-Z0-9_]*=/.test(l)).map(l => { const i = l.indexOf("="); return [l.slice(0, i), l.slice(i + 1)]; }));

privateDir(dir);
const lock = join(dir, ".setup-lock");
try { mkdirSync(lock); } catch { fail(`another setup is running (or a stale ${lock} remains; remove it if not)`); }
writeFileSync(join(dir, MARKER), "created by scripts/local-setup.mjs\n", { mode: 0o600 });
try {
  const data = privateDir(join(dir, "data")), home = privateDir(join(dir, "worker")), credentials = privateDir(join(dir, "credentials"));
  privateDir(join(home, "workspace"));
  const envPath = join(dir, "env"), workerPath = join(credentials, "worker.json");

  const fresh = {
    MEETING_AUTH_MODE: "local", HOSTNAME: "127.0.0.1", PORT: String(port), NODE_ENV: "production",
    MEETING_PUBLIC_ORIGIN: `http://127.0.0.1:${port}`, MEETING_DATA_DIR: data, MEETING_OWNER_ID: randomUUID(),
    MEETING_WORKER_GATEWAY_TOKEN: token(), MEETING_LOCAL_LOGIN_TOKEN: token(), MEETING_SPEECH_EXECUTION: "server", NEXT_TELEMETRY_DISABLED: "1",
  };
  const saved = existsSync(envPath) ? readEnv(envPath) : null;
  if (saved && existsSync(join(data, "meeting.sqlite")) && !saved.MEETING_OWNER_ID) throw new Error(`${envPath} lost MEETING_OWNER_ID; restore it before rerunning`);
  // A lost gateway token is recovered from worker.json so the worker keeps authenticating.
  let savedWorker = null;
  if (existsSync(workerPath)) { try { savedWorker = JSON.parse(readFileSync(workerPath, "utf8")); } catch { throw new Error(`${workerPath} is not valid JSON; fix or move it (its content is not shown)`); } }
  if (saved && !saved.MEETING_WORKER_GATEWAY_TOKEN && savedWorker?.sites_dispatch_token) fresh.MEETING_WORKER_GATEWAY_TOKEN = savedWorker.sites_dispatch_token;
  const env = { ...fresh, ...(saved ?? {}) };
  if (saved && saved.PORT && saved.PORT !== String(port) && args.includes("--port")) console.warn(`keeping the saved port ${saved.PORT}; to move, use a new --dir (PORT, MEETING_PUBLIC_ORIGIN and worker.json base_url must change together)`);
  if (savedWorker && savedWorker.sites_dispatch_token !== env.MEETING_WORKER_GATEWAY_TOKEN) throw new Error(`${workerPath} and ${envPath} hold different gateway tokens; make them the same`);
  if (savedWorker && savedWorker.base_url !== env.MEETING_PUBLIC_ORIGIN) throw new Error(`${workerPath} base_url and MEETING_PUBLIC_ORIGIN in ${envPath} differ; make them the same`);
  const added = Object.keys(fresh).filter(k => !saved || !(k in saved));
  if (!saved) { privateFile(envPath, Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n") + "\n"); console.log(`wrote ${envPath}`); }
  else if (added.length || rotate) {
    // Keep every existing line, comments included; replace only a rotated secret and append missing keys.
    if (rotate) env.MEETING_LOCAL_LOGIN_TOKEN = token();
    const lines = readFileSync(envPath, "utf8").replace(/\n$/, "").split("\n")
      .map(l => rotate && l.replace(/\r$/, "").startsWith("MEETING_LOCAL_LOGIN_TOKEN=") ? `MEETING_LOCAL_LOGIN_TOKEN=${env.MEETING_LOCAL_LOGIN_TOKEN}` : l);
    for (const k of added) lines.push(`${k}=${env[k]}`);
    privateFile(envPath, lines.join("\n") + "\n");
    if (added.length) console.log(`added ${added.join(", ")} to ${envPath}`);
    if (rotate) console.log("rotated the login secret: restart the web app; every browser must log in again");
  } else console.log(`keeping existing settings in ${envPath}`);

  let deviceToken;
  if (savedWorker) {
    deviceToken = savedWorker.device_token;
    console.log(`keeping existing worker settings in ${workerPath}`);
  } else {
    deviceToken = token();
    privateFile(workerPath, JSON.stringify({
      base_url: env.MEETING_PUBLIC_ORIGIN, device_token: deviceToken, sites_dispatch_token: env.MEETING_WORKER_GATEWAY_TOKEN,
      whisper_model: "base", cpu_threads: 2, chunk_seconds: 600,
    }, null, 2) + "\n");
    console.log(`wrote ${workerPath} (local Whisper base on CPU; see docs/SPEECH.md to switch to the API)`);
  }

  const migrate = spawnSync(process.execPath, ["--experimental-strip-types", join(repo, "scripts/migrate-oci.mjs")],
    { cwd: repo, env: { ...process.env, MEETING_DATA_DIR: env.MEETING_DATA_DIR, MEETING_OWNER_ID: env.MEETING_OWNER_ID }, stdio: "inherit" });
  if (migrate.status !== 0) throw new Error("database migration failed (see above)");

  const db = new DatabaseSync(join(env.MEETING_DATA_DIR, "meeting.sqlite"));
  try {
    const registration = JSON.stringify({ owner: env.MEETING_OWNER_ID, token_hash: createHash("sha256").update(deviceToken).digest("hex"), execution_location: "server" });
    const current = db.prepare("SELECT value FROM settings WHERE key='device'").get();
    if (current?.value === registration) console.log("worker already registered");
    else {
      db.exec("BEGIN IMMEDIATE");
      db.prepare("INSERT INTO settings(key,value) VALUES('device',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(registration);
      db.prepare("DELETE FROM settings WHERE key IN ('heartbeat','pair')").run();
      db.exec("COMMIT");
      console.log("worker registered (token hash only)");
    }
  } finally { db.close(); }

  const prefix = dir === defaultDir ? "" : `KMS_DIR=${dir} `;
  console.log(`\nnext:\n  npm run build:oci                    # once, and after each update\n  ${prefix}scripts/local-run.sh web      # terminal 1\n  ${prefix}scripts/local-run.sh worker   # terminal 2\n  ${prefix}scripts/local-run.sh login    # open the printed address once in your browser`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  rmdirSync(lock);
}
