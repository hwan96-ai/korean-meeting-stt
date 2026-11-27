// Phone push for finished or stopped jobs. Subscriptions are one settings row (no migration; rolling the code back
// just leaves the row unread). Database only: keys and the network call come in as a `PushSender`.
import type { Job, State } from "./contracts.ts";
import type { Database } from "./runtime-types.ts";
import { NOTICE_STATES, noticePayload } from "./notice.ts";
import type { PushTarget } from "./web-push.ts";

const SUBSCRIPTIONS = "push_subscriptions";
const MAX_DEVICES = 5;
// `server_key` is the VAPID public key the browser subscribed with: after a key rotation the old subscriptions can
// never receive anything, so delivery drops them instead of failing on them after every job.
export type PushSubscriptionRecord = PushTarget & { created_at: number; server_key?: string };
// Sends one encrypted payload and returns the push service's HTTP status.
export type PushSender = (target: PushTarget, payload: string) => Promise<number>;
export type JobNotice = { send: PushSender; publicKey: string; track?: (delivery: Promise<unknown>) => void };

export async function readSubscriptions(db: Database): Promise<PushSubscriptionRecord[]> {
  const row = await db.prepare("SELECT value FROM settings WHERE key=?").bind(SUBSCRIPTIONS).first<{ value: string }>();
  try { const list = row ? JSON.parse(row.value) : []; return Array.isArray(list) ? list : []; } catch { return []; }
}
async function writeSubscriptions(db: Database, list: PushSubscriptionRecord[]) {
  await db.prepare("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(SUBSCRIPTIONS, JSON.stringify(list)).run();
}
// Same endpoint replaces its old keys; the newest MAX_DEVICES stay.
export async function saveSubscription(db: Database, target: PushTarget, serverKey: string, now = Date.now()) {
  const list = [{ endpoint: target.endpoint, p256dh: target.p256dh, auth: target.auth, created_at: now, server_key: serverKey }, ...(await readSubscriptions(db)).filter(s => s.endpoint !== target.endpoint)].slice(0, MAX_DEVICES);
  await writeSubscriptions(db, list);
  return list.length;
}
// One statement, so a device saved at the same moment is never written back out.
export async function removeSubscription(db: Database, endpoint: string) {
  await db.prepare("UPDATE settings SET value=(SELECT json_group_array(json(value)) FROM json_each(settings.value) WHERE json_extract(value,'$.endpoint')<>?) WHERE key=? AND json_valid(value)").bind(endpoint, SUBSCRIPTIONS).run();
  return (await readSubscriptions(db)).length;
}

// Only the worker's own result moves a job into a finished or stopped state within its run (lease held, same
// generation). Owner saves, retries and resolve-outcome start a new generation, so they never notify.
export function noticeDue(before: Pick<Job, "state" | "generation">, state: State, generation: number, guardLease?: string) {
  return !!guardLease && generation === before.generation && state !== before.state && NOTICE_STATES.includes(state);
}
const sentKey = (id: string, generation: number, state: State) => `push_sent:${id}:${generation}:${state}`;
// One notice per job run and state, even if the same result arrives twice.
export async function claimNotice(db: Database, id: string, generation: number, state: State) {
  const r = await db.prepare("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)").bind(sentKey(id, generation, state), String(Date.now())).run();
  return r.meta.changes > 0;
}
export async function clearNotices(db: Database, id: string) {
  const prefix = `push_sent:${id}:`;
  await db.prepare("DELETE FROM settings WHERE substr(key,1,?)=?").bind(prefix.length, prefix).run();
}

// Sends to every subscribed device at once. A gone subscription (404/410, or made with an older server key) is
// dropped; any other failure is only counted. With no device nothing is recorded.
export async function deliverNotice(db: Database, job: Job, state: State, notice: Pick<JobNotice, "send" | "publicKey">) {
  const payload = noticePayload(job, state), all = await readSubscriptions(db);
  const stale = all.filter(s => s.server_key !== notice.publicKey), targets = all.filter(s => s.server_key === notice.publicKey);
  for (const s of stale) await removeSubscription(db, s.endpoint);
  if (!payload || !targets.length || !await claimNotice(db, job.id, job.generation, state)) return { sent: 0, failed: 0, removed: stale.length };
  const body = JSON.stringify(payload);
  const statuses = await Promise.all(targets.map(t => notice.send(t, body).catch(() => 0)));
  let sent = 0, failed = 0, removed = stale.length;
  for (const [n, status] of statuses.entries()) {
    if (status === 404 || status === 410) { await removeSubscription(db, targets[n].endpoint); removed++; }
    else if (status >= 200 && status < 300) sent++;
    else failed++;
  }
  return { sent, failed, removed };
}
