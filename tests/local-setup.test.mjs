import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// scripts/local-setup.mjs is Linux (and WSL2) only.
const linux = process.platform === "linux" && process.getuid() !== 0;
const script = fileURLToPath(new URL("../scripts/local-setup.mjs", import.meta.url));
const run = (...args) => spawnSync(process.execPath, [script, ...args], { encoding: "utf8" });
const mode = async path => (await stat(path)).mode & 0o777;
const env = async dir => Object.fromEntries((await readFile(join(dir, "env"), "utf8")).trim().split("\n").map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));

test("local setup: private files, hashed registration, nothing secret printed, rerun keeps tokens", { skip: !linux && "Linux only" }, async () => {
  const base = await mkdtemp(join(tmpdir(), "kms-setup-")), dir = join(base, "home");
  try {
    const first = run("--dir", dir, "--port", "4187");
    assert.equal(first.status, 0, first.stderr);
    const values = await env(dir), worker = JSON.parse(await readFile(join(dir, "credentials", "worker.json"), "utf8"));
    for (const secret of [values.MEETING_WORKER_GATEWAY_TOKEN, values.MEETING_LOCAL_LOGIN_TOKEN, worker.device_token]) {
      assert.match(secret, /^[A-Za-z0-9_-]{64}$/);
      assert.ok(!first.stdout.includes(secret) && !first.stderr.includes(secret));
    }
    assert.equal(values.MEETING_AUTH_MODE, "local");
    assert.equal(values.MEETING_PUBLIC_ORIGIN, "http://127.0.0.1:4187");
    assert.equal(worker.sites_dispatch_token, values.MEETING_WORKER_GATEWAY_TOKEN);
    for (const p of [dir, join(dir, "credentials"), join(dir, "data"), join(dir, "worker"), join(dir, "worker", "workspace")]) assert.equal(await mode(p), 0o700, p);
    for (const p of [join(dir, "env"), join(dir, "credentials", "worker.json"), join(dir, "data", "meeting.sqlite")]) assert.equal(await mode(p), 0o600, p);
    const db = new DatabaseSync(join(dir, "data", "meeting.sqlite"));
    try {
      const device = JSON.parse(db.prepare("SELECT value FROM settings WHERE key='device'").get().value);
      assert.deepEqual(device, { owner: values.MEETING_OWNER_ID, token_hash: createHash("sha256").update(worker.device_token).digest("hex"), execution_location: "server" });
      assert.equal(db.prepare("SELECT value FROM settings WHERE key='owner'").get().value, values.MEETING_OWNER_ID);
    } finally { db.close(); }
    const second = run("--dir", dir, "--port", "4188");
    assert.equal(second.status, 0, second.stderr);
    assert.deepEqual(await env(dir), values);
    assert.match(second.stdout + second.stderr, /keeping the saved port 4187/);
    assert.match(second.stdout, /worker already registered/);
    assert.ok(second.stdout.includes(`KMS_DIR=${dir} scripts/local-run.sh web`));
    const rotated = run("--dir", dir, "--rotate-login");
    assert.equal(rotated.status, 0, rotated.stderr);
    const after = await env(dir);
    assert.notEqual(after.MEETING_LOCAL_LOGIN_TOKEN, values.MEETING_LOCAL_LOGIN_TOKEN);
    assert.deepEqual({ ...after, MEETING_LOCAL_LOGIN_TOKEN: "" }, { ...values, MEETING_LOCAL_LOGIN_TOKEN: "" }, "only the login secret changes");
    assert.ok(!rotated.stdout.includes(after.MEETING_LOCAL_LOGIN_TOKEN));
  } finally { await rm(base, { recursive: true, force: true }); }
});

test("local setup refuses unsafe paths and folders it did not create", { skip: !linux && "Linux only" }, async () => {
  const base = await mkdtemp(join(tmpdir(), "kms-setup-"));
  try {
    const other = join(base, "other");
    await mkdir(other);
    await writeFile(join(other, "notes.txt"), "mine");
    const before = await mode(other);
    assert.notEqual(run("--dir", other).status, 0);
    assert.equal(await mode(other), before, "an unrelated folder keeps its permissions");
    assert.equal(await readFile(join(other, "notes.txt"), "utf8"), "mine");
    for (const bad of [join(base, "a b"), join(base, "x$(id)"), join(base, "x;y")]) assert.notEqual(run("--dir", bad, "--port", "4189").status, 0, bad);
    const foreign = join(base, "foreign");
    await mkdir(foreign);
    await writeFile(join(foreign, "env"), "lower=case\nexport X=1\n");
    assert.notEqual(run("--dir", foreign).status, 0, "an env file alone does not make a folder ours");
    assert.equal(await readFile(join(foreign, "env"), "utf8"), "lower=case\nexport X=1\n");
    assert.notEqual(run("--dir", join(fileURLToPath(new URL("..", import.meta.url)), "public", "kms")).status, 0, "never inside the repository");
  } finally { await rm(base, { recursive: true, force: true }); }
});
