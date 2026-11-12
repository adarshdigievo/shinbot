// Notifications only. Requests and application data are never cached here.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const id = event.notification.data?.notificationId;
  if (typeof id !== 'string' || id.length > 256) return;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
    const existing = windows.find(client => {
      const url = new URL(client.url);
      return url.origin === self.location.origin && url.pathname === '/';
    });
    if (existing) {
      try { await existing.focus(); } catch { /* The browser may keep the current window in front. */ }
      existing.postMessage({type:'shinbot:notification-open', id});
    } else await self.clients.openWindow('/#notification=' + encodeURIComponent(id));
  })());
});
