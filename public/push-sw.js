// Phone push for finished meeting-notes jobs. Only shows notices: no fetch handler and no caching,
// so the app itself never runs from this worker and Cloudflare Access still guards every page.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  event.waitUntil(self.registration.showNotification(typeof data.title === "string" ? data.title : "회의록", {
    body: typeof data.body === "string" ? data.body : "작업 상태가 바뀌었습니다.",
    tag: typeof data.tag === "string" ? data.tag : undefined,
    // A newer notice for the same job replaces the old one and still rings (renotify needs a tag).
    renotify: typeof data.tag === "string",
    icon: "/favicon.svg",
    data: { url: typeof data.url === "string" && data.url.startsWith("/") ? data.url : "/" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    const open = list.find(c => new URL(c.url).origin === self.location.origin);
    return open ? open.focus() : self.clients.openWindow(url);
  }));
});
