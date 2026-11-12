export function chatSearch({api, state, dialog, escape, icon, date, open, toast}) {
  return function showSearch() {
    if (document.querySelector('dialog[open]')) return;
    const d = dialog('Search chats', `<label class="search-field">${icon('search')}<input id="chat-search-query" type="search" placeholder="Search titles and messages" maxlength="200" autocomplete="off" aria-label="Search chats"></label><div class="chat-search-filter"><label for="chat-search-bot">Bot</label><select id="chat-search-bot"><option value="">All bots</option>${state.bots.map(bot => `<option value="${escape(bot.id)}">${escape(bot.name)}</option>`).join('')}</select></div><p id="chat-search-status" class="hint" role="status"></p><div id="chat-search-results" class="search-results chat-search-results"></div><button id="chat-search-more" class="quiet small-button" hidden>Load more</button>`);
    d.classList.add('search-dialog');
    const input = d.querySelector('input'), filter = d.querySelector('select');
    const results = d.querySelector('#chat-search-results'), status = d.querySelector('#chat-search-status'), more = d.querySelector('#chat-search-more');
    let version = 0, timer, items = [], busy = false;
    function highlight(text, query) {
      if (!query) return escape(text);
      const index = String(text).toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
      if (index < 0) return escape(text);
      return escape(text.slice(0, index)) + '<mark>' + escape(text.slice(index, index + query.length)) + '</mark>' + escape(text.slice(index + query.length));
    }
    async function search(append = false) {
      const request = ++version, query = input.value.trim(), bot = filter.value;
      busy = true; more.disabled = true; status.textContent = 'Searching…';
      if (!append) { items = []; results.replaceChildren(); more.hidden = true; }
      const offset = items.length;
      try {
        const data = await api('/api/tasks/search?' + new URLSearchParams({q:query, ...(bot ? {bot_id:bot} : {}), offset:String(offset)}));
        if (!d.isConnected || request !== version) return;
        const seen = new Set(items.map(item => item.id));
        items.push(...data.items.filter(item => !seen.has(item.id)));
        const html = items.map((item, index) => `<button type="button" data-chat-result="${index}">${icon('message-circle')}<span><strong>${highlight(item.title, query)}</strong><small>${escape(item.bot_name || 'Archived bot')} · ${escape(date(item.updated))}</small>${item.snippet ? `<span class="search-excerpt">${highlight(item.snippet, query)}</span>` : ''}</span>${icon('arrow-up-right')}</button>`).join('');
        results.innerHTML = html || '<p class="empty">No matching chats. Try another word or choose All bots.</p>';
        results.querySelectorAll('[data-chat-result]').forEach(button => button.onclick = async () => {
          const item = items[Number(button.dataset.chatResult)];
          button.disabled = true;
          try { const task = await api('/api/tasks/' + encodeURIComponent(item.id)); d.close(); await open(task, item); }
          catch (error) { if (d.isConnected) {status.textContent = error.message;button.disabled=false;} else toast(error.message,true); }
        });
        more.hidden = !data.has_more;
        more.textContent = 'Load more';
        status.textContent = query ? `${items.length}${data.has_more ? '+' : ''} matching ${items.length === 1 ? 'chat' : 'chats'}` : 'Recent chats. Search to find older conversations.';
      } catch (error) { if (request === version && d.isConnected) {status.textContent = 'Could not search chats. ' + error.message;more.textContent='Retry';more.hidden=false;} }
      finally { if (request === version) {busy=false;more.disabled=false;} }
    }
    function changed() { ++version;busy=true;results.replaceChildren();more.hidden=true;status.textContent='Searching…';clearTimeout(timer);timer=setTimeout(() => search(), 200); }
    input.addEventListener('input', changed);
    filter.onchange = () => {clearTimeout(timer);search();};
    more.onclick = () => {if (!busy) search(more.textContent !== 'Retry');more.textContent='Load more';};
    input.onkeydown = event => {
      if (event.key === 'Escape') {event.preventDefault();event.stopPropagation();d.close();}
      if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('button')?.focus(); }
      if (event.key === 'Enter') {event.preventDefault(); if (!busy) results.querySelector('button')?.click();}
    };
    d.addEventListener('close', () => {++version;clearTimeout(timer);});
    input.focus(); search();
  };
}
