"use client";
// Browser side of the phone push: service worker, subscription and the owner API (/api/push).
import { useCallback, useEffect, useState } from "react";

export type PushStatus = "loading" | "unsupported" | "unavailable" | "blocked" | "off" | "on";
const WORKER = "/push-sw.js";

async function call<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(`/api/push${path}`, { method, headers: { "Content-Type": "application/json", "X-Meeting-Client": "1" }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
  const d = await r.json() as { error?: { message?: string } };
  if (!r.ok) throw new Error(d.error?.message ?? "요청에 실패했습니다.");
  return d as T;
}
const supported = () => typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
function keyBytes(value: string) {
  const s = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4));
  return Uint8Array.from(s, c => c.charCodeAt(0));
}
const sameKey = (a: ArrayBuffer | null | undefined, b: Uint8Array) => !!a && a.byteLength === b.length && new Uint8Array(a).every((x, i) => x === b[i]);
async function existing() { const reg = await navigator.serviceWorker.getRegistration("/"); return { reg, sub: await reg?.pushManager.getSubscription() ?? null }; }
async function register(sub: PushSubscription) {
  const json = sub.toJSON();
  await call("/subscribe", "POST", { endpoint: json.endpoint, keys: { p256dh: json.keys?.p256dh, auth: json.keys?.auth } });
}

// This device's push state. `enable` asks for permission, so call it from a tap. `refresh` (the settings page only)
// re-sends a live subscription to the server, in case it dropped this device after a push service said "gone".
export function usePush({ refresh = false } = {}) {
  const [status, setStatus] = useState<PushStatus>("loading");
  const [publicKey, setPublicKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      if (!supported()) return setStatus("unsupported");
      const info = await call<{ available: boolean; public_key: string | null }>("");
      if (!live) return;
      if (!info.available || !info.public_key) return setStatus("unavailable");
      setPublicKey(info.public_key);
      if (Notification.permission === "denied") return setStatus("blocked");
      const { sub } = await existing();
      // A subscription made with an older server key would never receive anything: treat it as off.
      if (sub && Notification.permission === "granted" && sameKey(sub.options.applicationServerKey, keyBytes(info.public_key))) {
        if (refresh) await register(sub).catch(() => undefined);
        if (live) setStatus("on");
      } else if (live) setStatus("off");
    })().catch(() => { if (live) setStatus("unavailable"); });
    return () => { live = false; };
  }, [refresh]);

  const enable = useCallback(async () => {
    if (!publicKey) return;
    setError(null);
    try {
      if (await Notification.requestPermission() !== "granted") { setStatus(Notification.permission === "denied" ? "blocked" : "off"); return; }
      const reg = await navigator.serviceWorker.register(WORKER, { scope: "/" });
      await navigator.serviceWorker.ready;
      const old = await reg.pushManager.getSubscription();
      if (old && !sameKey(old.options.applicationServerKey, keyBytes(publicKey))) await old.unsubscribe();
      const sub = await reg.pushManager.getSubscription() ?? await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
      await register(sub);
      setStatus("on");
    } catch (e) { setError(e instanceof Error ? e.message : "알림을 켜지 못했습니다."); }
  }, [publicKey]);

  const disable = useCallback(async () => {
    setError(null);
    try {
      const { sub } = await existing();
      if (sub) { await call("/unsubscribe", "POST", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      setStatus("off");
    } catch (e) { setError(e instanceof Error ? e.message : "알림을 끄지 못했습니다."); }
  }, []);

  const test = useCallback(async () => {
    setError(null);
    const { sub } = await existing();
    if (!sub) { setStatus("off"); return "gone" as const; }
    try {
      const r = await call<{ result: "sent" | "gone" | "failed" }>("/test", "POST", { endpoint: sub.endpoint });
      if (r.result === "gone") setStatus("off");
      return r.result;
    } catch (e) { setError(e instanceof Error ? e.message : "시험 알림을 보내지 못했습니다."); return "failed" as const; }
  }, []);

  return { status, error, enable, disable, test };
}
