export function promptSuggestions({textarea, hint, request}) {
  const send = textarea.form.querySelector('[type=submit]');
  const placeholder = textarea.placeholder;
  let key = '', candidate = '', dismissed = false, requested = false, timer;
  let context, disposed = false;
  let previousDraft, previousWidth;
  function fitDraft() {
    const width = textarea.clientWidth;
    if (!width || (previousDraft === textarea.value && previousWidth === width)) return;
    previousDraft = textarea.value; previousWidth = width;
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(220, textarea.scrollHeight) + 'px';
    if (textarea.scrollHeight <= textarea.clientHeight) textarea.scrollTop = 0;
  }
  const resize = new ResizeObserver(fitDraft);
  resize.observe(textarea);
  const available = () => !disposed && !dismissed && !!candidate && !textarea.value;
  function sync() {
    hint.hidden = !available();
    textarea.placeholder = available() ? candidate : placeholder;
    if (available()) textarea.setAttribute('aria-describedby', hint.id);
    else textarea.removeAttribute('aria-describedby');
    send.disabled = !textarea.value.trim() || textarea.form.dataset.sending === 'true';
    const steer = textarea.form.querySelector('#steer-draft');
    if (steer) steer.disabled = send.disabled;
    fitDraft();
  }
  function load() {
    if (requested || dismissed || textarea.value.trim() || !context?.eligible) return;
    requested = true;
    const version = key;
    timer = setTimeout(async () => {
      if (disposed || !textarea.isConnected || version !== key || textarea.value.trim()) { requested = false; return; }
      try {
        const result = await request(context.payload);
        if (disposed || !textarea.isConnected || version !== key || dismissed) return;
        if (typeof result.suggestion === 'string' && result.suggestion.trim()) candidate = result.suggestion.trim();
        sync();
      } catch { /* Optional suggestions never interrupt composing. */ }
    }, 400);
  }
  function input() { sync(); load(); }
  textarea.addEventListener('input', input);
  const accept = () => {
    if (!available()) return false;
    textarea.value = candidate;
    textarea.setSelectionRange(candidate.length, candidate.length);
    textarea.dispatchEvent(new Event('input', {bubbles:true}));
    textarea.focus();
    return true;
  };
  return {
    accept,
    dismiss() { if (!available()) return false; dismissed = true; sync(); return true; },
    sync,
    update({task, bot, selection}) {
      const last = task?.messages?.at(-1);
      const next = JSON.stringify([task?.id, bot.id, last?.id, last?.finished, task?.status, selection.provider, selection.model]);
      if (key !== next) {
        clearTimeout(timer); key = next; dismissed = false; requested = false;
        const eligible = !task || task.status === 'completed';
        candidate = eligible ? (task ? 'Check the result against my original request and flag anything unfinished.' : bot.profile?.starters?.[0] || 'Help me choose a useful first task.') : '';
        context = {eligible, payload:{bot_id:bot.id,task_id:task?.id || null,provider:selection.provider,model:selection.model}};
      }
      sync(); load();
    },
    dispose() { disposed = true; clearTimeout(timer); resize.disconnect(); textarea.removeEventListener('input', input); },
  };
}
