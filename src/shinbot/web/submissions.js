export function submissionIds(storage) {
  const pending = new Map();
  return async (payload) => {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(payload)));
    const key = 'shinbot.pending.' + [...new Uint8Array(bytes)].map(x=>x.toString(16).padStart(2,'0')).join('');
    let id = pending.get(key);
    try { id ||= storage?.getItem(key); } catch {}
    id ||= crypto.randomUUID();
    pending.set(key, id);
    // Store only a content hash and request ID, never the message text.
    try { storage?.setItem(key, id); } catch {}
    return {id, acknowledge() {pending.delete(key);try {storage?.removeItem(key);} catch {}}};
  };
}
