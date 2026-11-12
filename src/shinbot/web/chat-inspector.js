import {actionDescription, currentActivity} from './activity.js?v=20261028-workspace-2';

export function chatInspector({escape, icon, date, computer, openCard, openComputer}) {
  let task = null, panel = null, open = false, browser = null, error = '';
  const narrow = matchMedia('(max-width: 1100px)');
  const markup = () => `<button class="inspector-backdrop" type="button" aria-label="Close chat details" hidden></button><aside id="chat-inspector" class="chat-inspector" aria-label="Chat details" hidden><header><h2>Chat details</h2><button class="quiet icon-button" type="button" data-close-inspector aria-label="Close chat details">${icon('x')}</button></header><div class="inspector-scroll"><section><h3>Current activity</h3><p data-inspector-current></p></section><section><div class="row between"><h3>Browser</h3><button type="button" class="quiet small-button" data-inspector-computer>Open Computer ${icon('arrow-up-right')}</button></div><p class="hint" data-inspector-browser></p><div id="chat-browser-dock"></div></section><section><h3>Board cards</h3><div data-inspector-cards></div></section><section><h3>Operations</h3><div data-inspector-log></div></section></div></aside>`;
  function layout() {
    if (!panel?.isConnected) return;
    const workspace = panel.closest('.chat-workspace');
    workspace.classList.toggle('inspector-open', open);
    panel.hidden = !open;
    workspace.querySelector('.inspector-backdrop').hidden = !open || !narrow.matches;
    workspace.querySelector('.chat-main').inert = open && narrow.matches;
    const toggle = workspace.querySelector('[data-toggle-inspector]');
    toggle.setAttribute('aria-expanded', String(open));
    toggle.title = open ? 'Hide chat details' : 'Show chat details';
    computer.inspect(open ? panel.querySelector('#chat-browser-dock') : null, task);
  }
  function close(focus = true) {
    open = false; layout();
    if (focus) panel?.closest('.chat-workspace')?.querySelector('[data-toggle-inspector]')?.focus();
  }
  function browserSummary() {
    if (!panel?.isConnected) return;
    const text = error ? 'Browser connection lost. It will reconnect automatically.'
      : !task ? 'Start a conversation to follow its browser.'
      : !browser ? 'Checking browser activity…'
      : browser.lease?.kind === 'agent' && browser.lease.owner !== task.id ? 'The browser is being used by another chat.'
      : browser.lease?.kind === 'human' ? 'The browser is under manual control.'
      : browser.lease?.owner === task.id ? 'This chat is using the browser. The preview is view only.'
      : 'No browser action is running in this chat.';
    panel.querySelector('[data-inspector-browser]').textContent = text;
  }
  function update(next) {
    task = next;
    if (!panel?.isConnected) return;
    const activity = currentActivity(task);
    panel.querySelector('[data-inspector-current]').textContent = activity?.label || ({completed:'Reply ready',failed:'Task failed',cancelled:'Task stopped',uncertain:'Needs your review'})[task?.status] || 'No active task';
    const cards = panel.querySelector('[data-inspector-cards]');
    const cardMarkup = task?.board_cards?.map(card => `<button class="inspector-card" data-inspector-card="${escape(card.id)}"><strong>${escape(card.title)}</strong><small>${escape(({backlog:'Backlog',ready:'Ready',running:'In progress',review:'Review',done:'Done',blocked:'Blocked'})[card.status] || card.status)}</small></button>`).join('') || '<p class="hint">No board cards linked to this chat.</p>';
    if (cards.innerHTML !== cardMarkup) {cards.innerHTML=cardMarkup;cards.querySelectorAll('[data-inspector-card]').forEach(button => button.onclick=()=>openCard(task.board_cards.find(card=>card.id===button.dataset.inspectorCard)));}
    const log = panel.querySelector('[data-inspector-log]');
    const entries = [
      ...(task?.actions || []).map(action => ({id:'action-'+action.id,created:action.created,action})),
      ...(task?.events || []).filter(event=>['failed','warning','error','learning'].includes(event.kind)).map(event=>({id:'event-'+event.id,created:event.created,event})),
    ].sort((a,b)=>b.created-a.created).slice(0,100);
    const html = entries.map(entry=>{
      if(entry.event)return `<div class="inspector-event"><strong>${escape(entry.event.title)}</strong><small>${escape(date(entry.created))}</small></div>`;
      const action=entry.action, description=actionDescription(action);
      return `<details data-operation="${escape(action.id)}"><summary><span>${escape(description.label)}<small>${escape(description.state)} · ${escape(date(action.created))}</small></span>${icon('chevron-down')}</summary>${description.detail?`<p class="operation-detail">${escape(description.detail)}</p>`:''}<pre>${escape(String(action.error || action.result || action.args || '').slice(0,6000))}</pre></details>`;
    }).join('') || '<p class="hint">Tool calls and important updates will appear here.</p>';
    if (log.dataset.content !== html) {
      const expanded = new Set([...log.querySelectorAll('details[open]')].map(el=>el.dataset.operation));
      log.innerHTML=html;log.dataset.content=html;
      log.querySelectorAll('details').forEach(el=>{el.open=expanded.has(el.dataset.operation);});
    }
    browserSummary();layout();
  }
  document.addEventListener('shinbot:browser-activity', event => {browser=event.detail.activity || browser;error=event.detail.error || '';browserSummary();});
  narrow.addEventListener('change', () => {layout();if(open)panel?.querySelector('[data-close-inspector]')?.focus();});
  document.addEventListener('keydown', event => {
    if (!open || !panel?.isConnected || document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') {event.preventDefault();close();}
    if(event.key==='Tab' && narrow.matches){const controls=[...panel.querySelectorAll('button,summary,a,input,select')].filter(el=>!el.disabled&&el.getClientRects().length);const first=controls[0],last=controls.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
  });
  return {
    markup, update,
    unmount() {computer.inspect(null,null);panel=null;task=null;},
    mount() {
      panel=document.querySelector('#chat-inspector');if(!panel)return;
      const workspace=panel.closest('.chat-workspace');
      workspace.querySelector('[data-toggle-inspector]').onclick=()=>{open=!open;layout();if(open)panel.querySelector('[data-close-inspector]').focus();};
      panel.querySelector('[data-close-inspector]').onclick=()=>close();
      workspace.querySelector('.inspector-backdrop').onclick=()=>close();
      panel.querySelector('[data-inspector-computer]').onclick=()=>openComputer();
      update(null);
    },
  };
}
