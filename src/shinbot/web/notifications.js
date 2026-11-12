import {browserNotifications} from './browser-notifications.js?v=20261028-native-1';

export function notifications({api, $, escape, icon, action, toast, date, open}) {
  let data = null, rendered = '', pending = new Set();
  const browser = browserNotifications({toast, open:async id => {
    await refresh();
    const item = data?.items.find(item => item.id === id);
    if (!item) { toast('This activity is no longer available. Check Notifications for the latest updates.'); return; }
    close(); await open(item); await read([item]);
  }});
  const markup = () => `<details id="notifications-menu" class="notifications-menu"><summary id="notifications-toggle" aria-label="Notifications" title="Notifications">${icon('bell')}<span id="notification-count" class="notification-count" hidden></span></summary><section class="notifications-panel" aria-label="Notifications"><div class="notifications-heading"><h2>Notifications</h2><button id="read-notifications" class="quiet small-button" disabled>Mark all read</button></div><p class="hint">Recent activity across your bots and followed repositories.</p><div id="notification-list" class="notification-list"></div>${browser.controls()}</section></details><span id="notification-announcement" class="sr-only" role="status" aria-live="polite"></span>`;
  function close(restoreFocus=false) {
    const menu=$('#notifications-menu');
    if (!menu?.open) return;
    menu.open=false;
    if (restoreFocus) $('#notifications-toggle').focus();
  }
  document.addEventListener('pointerdown', event => {if (!event.target.closest('#notifications-menu')) close();});
  document.addEventListener('focusin', event => {if (!event.target.closest('#notifications-menu')) close();});
  document.addEventListener('keydown', event => {if (event.key==='Escape' && $('#notifications-menu')?.open) {event.preventDefault();close(true);}});
  function render() {
    const list=$('#notification-list');
    if (!list) return;
    if (!data) {list.innerHTML='<p class="notification-empty">Loading activity…</p>';return;}
    const content=data.items.map(item=>`<button class="notification-item ${item.unread?'unread':''}" data-notification="${escape(item.id)}">${icon(item.kind==='approval'?'shield-check':item.kind==='github'?'folder-git-2':item.kind==='completed'?'circle-check':'bell')}<span class="notification-copy"><strong>${escape(item.title)}</strong><span>${escape(item.detail)}</span><small>${escape(date(item.created))}</small></span>${item.unread?'<span class="notification-dot"><span class="sr-only">Unread</span></span>':''}</button>`).join('') || `<div class="notification-empty">${icon('circle-check')}<h3>You’re all caught up</h3><p>Task results, approval requests and new GitHub activity will appear here.</p></div>`;
    if (content===rendered) return;
    const focused=list.contains(document.activeElement)?document.activeElement.dataset.notification:null;
    const scroll=list.scrollTop;
    list.innerHTML=content;rendered=content;list.scrollTop=scroll;
    list.querySelectorAll('[data-notification]').forEach(button=>{
      if (button.dataset.notification===focused) button.focus({preventScroll:true});
      action(button,async()=>{
        const item=data.items.find(item=>item.id===button.dataset.notification);
        if (!item) return;
        close();
        await open(item);
        await read([item]);
      });
    });
  }
  function update(snapshot) {
    if (!snapshot || !$('#notifications-toggle')) return;
    data=snapshot;
    browser.update(snapshot);
    const count=$('#notification-count'),label=data.unread?`Notifications, ${data.unread} unread`:'Notifications, no unread activity';
    count.hidden=!data.unread;count.textContent=data.unread>99?'99+':String(data.unread);
    $('#notifications-toggle').setAttribute('aria-label',label);
    $('#notifications-toggle').title=label;
    if ($('#notification-announcement').textContent!==label) $('#notification-announcement').textContent=label;
    $('#read-notifications').disabled=!data.unread;
    $('#read-notifications').textContent=data.has_more?'Mark shown as read':'Mark all read';
    if ($('#notifications-menu').open) render();
  }
  async function read(items) {
    const changes=items.filter(item=>item.unread && !pending.has(item.id+':'+item.revision));
    if (!changes.length) return;
    const keys=changes.map(item=>item.id+':'+item.revision);
    keys.forEach(key=>pending.add(key));
    try {update(await api('/api/notifications/read','POST',{items:changes.map(({id,revision})=>({id,revision}))}));}
    finally {keys.forEach(key=>pending.delete(key));}
  }
  async function refresh() {
    try {update(await api('/api/notifications'));}
    catch (error) {if ($('#notification-list')) {$('#notification-list').textContent='Could not load activity. Close and reopen Notifications to retry.';$('#notifications-toggle').setAttribute('aria-label','Notifications unavailable');}throw error;}
  }
  function mount() {
    data=null;rendered='';pending=new Set();
    browser.mount();
    $('#notifications-menu').ontoggle=()=>{if ($('#notifications-menu')?.open) {render();refresh().catch(error=>toast(error.message,true));}};
    $('#read-notifications').onclick=async()=>{
      const button=$('#read-notifications');button.disabled=true;
      try {await read(data?.items||[]);} catch(error) {toast(error.message,true);}
      finally {if (button.isConnected) button.disabled=!data?.unread;}
    };
  }
  return {markup,mount,update,refresh,close,stop:browser.stop,browserControls:browser.controls,refreshControls:browser.render,readTask:taskId=>read((data?.items||[]).filter(item=>item.task_id===taskId))};
}
