const preferenceKey = 'shinbot.browser-notifications';
const deliveredKey = 'shinbot.browser-notifications.delivered';
const focusKey = 'shinbot.browser-notifications.focus';
const lockName = 'shinbot.browser-notifications';

export function browserNotifications({open, toast}) {
  const tabId = crypto.randomUUID();
  let active = false, generation = 0, seen = null, heartbeat, registration;
  let enabled = false, busy = false, error = '', pendingOpen = new URLSearchParams(location.hash.slice(1)).get('notification');
  const delivered = new Set(), windows = new Set();
  function read(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
  }
  const supported = () => window.isSecureContext && 'Notification' in window;
  const allowed = () => active && enabled && supported() && Notification.permission === 'granted';
  function controls() {
    return '<div class="browser-notifications"><strong>Browser notifications</strong><p data-browser-notification-status role="status"></p><div class="row"><button type="button" class="small-button" data-browser-notifications-toggle>Enable notifications</button><button type="button" class="quiet small-button" data-browser-notifications-test hidden>Send test</button></div></div>';
  }
  function render() {
    let message = 'Get alerts for replies, approvals and other activity while ShinBot is open in the background.';
    if (!window.isSecureContext) message = 'Open ShinBot over HTTPS or on localhost to enable browser notifications.';
    else if (!('Notification' in window)) message = 'This browser does not support notifications. Activity is still available here.';
    else if (Notification.permission === 'denied') message = 'Notifications are blocked. Allow them in your browser’s site settings, then enable them here.';
    else if (enabled && Notification.permission === 'granted') message = 'On in this browser. Alerts appear while ShinBot is open in the background. Clicking one opens its activity.';
    document.querySelectorAll('[data-browser-notification-status]').forEach(el => { el.textContent = error || message; });
    document.querySelectorAll('[data-browser-notifications-toggle]').forEach(el => {
      el.textContent = busy ? 'Waiting for permission…' : enabled ? 'Turn off notifications' : 'Enable notifications';
      el.disabled = busy || (!enabled && (!supported() || Notification.permission === 'denied'));
    });
    document.querySelectorAll('[data-browser-notifications-test]').forEach(el => { el.hidden = !allowed(); el.disabled = busy; });
  }
  function trackFocus() {
    if (!active) return;
    if (document.visibilityState === 'visible' && document.hasFocus()) write(focusKey, {tabId, at: Date.now()});
    else if (read(focusKey, {}).tabId === tabId) write(focusKey, null);
    render();
  }
  const focused = () => {
    const focus = read(focusKey, {});
    return (document.visibilityState === 'visible' && document.hasFocus()) || Date.now() - focus.at < 15000;
  };
  async function worker() {
    if (!('serviceWorker' in navigator)) return null;
    if (!registration) registration = navigator.serviceWorker.register('/static/notification-worker.js').then(async reg => {
      if (reg.active) return reg;
      const installing = reg.installing || reg.waiting;
      if (!installing) throw new Error('The notification service could not start. Try again.');
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => { cleanup(); reject(new Error('The notification service did not start. Try again.')); }, 5000);
        const changed = () => {
          if (installing.state === 'activated') { cleanup(); resolve(); }
          else if (installing.state === 'redundant') { cleanup(); reject(new Error('The notification service could not start. Try again.')); }
        };
        const cleanup = () => { clearTimeout(timeout); installing.removeEventListener('statechange', changed); };
        installing.addEventListener('statechange', changed); changed();
      });
      return reg;
    }).catch(err => { registration = null; throw err; });
    return registration;
  }
  async function show(item, test = false) {
    const current = generation;
    const options = {
      body: item.detail, icon: '/static/brand/shinbot-icon.png?v=20261028',
      tag: test ? 'shinbot:test' : 'shinbot:' + item.id,
      data: {notificationId: test ? null : item.id},
    };
    let reg;
    try { reg = await worker(); } catch { /* Desktop browsers can also deliver without a service worker. */ }
    if (!allowed() || generation !== current) return;
    if (reg) await reg.showNotification(item.title, options);
    else {
      const notification = new Notification(item.title, options);
      windows.add(notification);
      notification.onclose = () => windows.delete(notification);
      notification.onclick = () => { notification.close(); window.focus(); if (!test && active) open(item.id).catch(err => toast(err.message, true)); };
    }
  }
  async function closeDelivered() {
    windows.forEach(notification => notification.close()); windows.clear();
    try {
      const reg = await navigator.serviceWorker?.getRegistration('/static/');
      if (reg) (await reg.getNotifications()).filter(n => n.tag.startsWith('shinbot:')).forEach(n => n.close());
    } catch { /* Closing old alerts should not block signing out. */ }
  }
  async function toggle() {
    if (busy) return;
    error = '';
    if (enabled) {
      enabled = false;
      if (!write(preferenceKey, false)) error = 'Notifications are off for this visit. Browser storage is unavailable, so this choice may reset.';
      await closeDelivered(); render(); return;
    }
    if (!supported()) { render(); return; }
    const current = generation;
    busy = true; render();
    try {
      // Request directly from the click; browsers require a user gesture.
      const permission = await Notification.requestPermission();
      if (!active || generation !== current) return;
      if (permission === 'granted') {
        enabled = true;
        if (!write(preferenceKey, true)) error = 'Notifications are on for this visit. Browser storage is unavailable, so this choice may reset.';
      } else if (permission === 'default') error = 'Permission was not granted. Choose Enable notifications to try again.';
    } catch { error = 'Could not enable notifications. Check your browser’s site permissions and try again.'; }
    finally { busy = false; render(); }
  }
  document.addEventListener('click', event => {
    if (!active) return;
    if (event.target.closest('[data-browser-notifications-toggle]')) toggle().catch(err => toast(err.message, true));
    if (event.target.closest('[data-browser-notifications-test]')) show({title:'ShinBot notifications are on', detail:'Replies and approval requests will appear here.'}, true).catch(() => { error = 'Could not show a notification. Check browser and system notification permissions.'; render(); });
  });
  window.addEventListener('storage', event => {
    if (event.key === preferenceKey || event.key === null) {
      enabled = read(preferenceKey, false) === true;
      if (!enabled) closeDelivered();
      render();
    }
  });
  window.addEventListener('focus', trackFocus);
  window.addEventListener('blur', trackFocus);
  document.addEventListener('visibilitychange', trackFocus);
  window.addEventListener('pagehide', () => { if (read(focusKey, {}).tabId === tabId) write(focusKey, null); });
  function destination() {
    if (!active || seen === null || !pendingOpen) return;
    const id = pendingOpen; pendingOpen = null;
    if (new URLSearchParams(location.hash.slice(1)).has('notification')) history.replaceState(null, '', location.pathname + location.search);
    open(id).catch(err => toast(err.message, true));
  }
  window.addEventListener('hashchange', () => { pendingOpen = new URLSearchParams(location.hash.slice(1)).get('notification'); destination(); });
  navigator.serviceWorker?.addEventListener('message', event => {
    if (event.source?.scriptURL !== new URL('/static/notification-worker.js', location.href).href
        || event.data?.type !== 'shinbot:notification-open' || typeof event.data.id !== 'string') return;
    pendingOpen = event.data.id; destination();
  });
  async function deliver(items, current) {
    if (!allowed() || current !== generation) return;
    const persisted = read(deliveredKey, []);
    const known = new Set([...(Array.isArray(persisted) ? persisted : []), ...delivered]);
    for (const item of items) {
      const key = item.id + ':' + item.revision;
      if (known.has(key)) continue;
      if (!allowed() || current !== generation) return;
      // Claim even foreground activity so another tab cannot alert for it later.
      if (!focused()) await show(item);
      known.add(key); delivered.add(key);
      write(deliveredKey, [...known].slice(-500));
      if (delivered.size > 500) delivered.delete(delivered.values().next().value);
    }
  }
  function update(snapshot) {
    if (!active) return;
    render();
    const fresh = seen === null ? [] : snapshot.items.filter(item => item.unread && !seen.has(item.id + ':' + item.revision));
    if (seen === null) seen = new Set();
    for (const item of snapshot.items) seen.add(item.id + ':' + item.revision);
    if (seen.size > 1000) seen = new Set([...seen].slice(-1000));
    destination();
    if (!fresh.length || !allowed()) return;
    const current = generation;
    const send = () => deliver(fresh, current);
    const work = navigator.locks ? navigator.locks.request(lockName, send) : send();
    work.catch(() => { error = 'A browser notification could not be shown. Your activity is still available here.'; render(); });
  }
  function stop() {
    active = false; generation++; seen = null; clearInterval(heartbeat);
    if (read(focusKey, {}).tabId === tabId) write(focusKey, null);
    closeDelivered();
  }
  return {
    controls, render, update, stop,
    mount() { active = true; generation++; seen = null; enabled = read(preferenceKey, false) === true; error = ''; clearInterval(heartbeat); trackFocus(); heartbeat = setInterval(trackFocus, 5000); render(); },
  };
}
