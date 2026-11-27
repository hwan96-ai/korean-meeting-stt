// Print a one-time owner login address for MEETING_AUTH_MODE=local (called by scripts/local-run.sh login).
// The code is random, valid for 10 minutes and used up on first use; only its SHA-256 is stored in the database.
// A new code replaces any unused one. Expects the env from <KMS_DIR>/env (MEETING_DATA_DIR, MEETING_PUBLIC_ORIGIN).
import { createHash, randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";

const origin = process.env.MEETING_PUBLIC_ORIGIN ?? "", data = process.env.MEETING_DATA_DIR ?? "";
const url = URL.canParse(origin) ? new URL(origin) : null;
if (!url || url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.origin !== origin) {
  console.error("MEETING_PUBLIC_ORIGIN must be http://127.0.0.1:<port>; refusing to print a login address for anything else");
  process.exit(1);
}
if (!data || !existsSync(join(data, "meeting.sqlite"))) { console.error("database missing: run node scripts/local-setup.mjs first"); process.exit(1); }
const code = randomBytes(32).toString("base64url");
const db = new DatabaseSync(join(data, "meeting.sqlite"));
try {
  db.exec("PRAGMA busy_timeout=5000");
  const value = JSON.stringify({ hash: createHash("sha256").update(code).digest("hex"), expires: Date.now() + 10 * 60 * 1000 });
  db.prepare("INSERT INTO settings(key,value) VALUES('local_login_code',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(value);
} finally { db.close(); }
console.log(`${origin}/api/local-login?code=${code}&return_to=/stt`);
