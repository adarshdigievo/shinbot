export function bindComposerKeys(textarea, {acceptSuggestion, dismissSuggestion} = {}) {
  let composing = false;
  textarea.addEventListener('compositionstart', () => { composing = true; });
  textarea.addEventListener('compositionend', () => { composing = false; });
  textarea.addEventListener('keydown', event => {
    if (!event.defaultPrevented && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey
        && !event.repeat && !composing && !event.isComposing && event.keyCode !== 229) {
      if (event.key === 'Tab' && acceptSuggestion?.()) { event.preventDefault(); return; }
      if (event.key === 'Escape' && dismissSuggestion?.()) { event.preventDefault(); return; }
    }
    if (event.defaultPrevented || event.key !== 'Enter' || event.shiftKey || event.altKey
        || composing || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    const form = textarea.form;
    if (event.repeat || !textarea.value.trim() || !form || form.querySelector('[type=submit]')?.disabled) return;
    form.requestSubmit();
  });
}
