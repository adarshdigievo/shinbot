import {projectTabs, bindProjectTabs} from './project-tabs.js';

export function inboxView({api,state,$,escape,icon,dialog,field,bind,action,toast,navigate,pageScope,date,connectGitHub}) {
  let view='attention', query='', generation=0, lastAuto=0, renderOptions={};
  const statusLabel=state=>({open:'Open',closed:'Closed',merged:'Merged',unknown:'Status unavailable'}[state]||'Status unavailable');
  const path=id=>'/api/github-inbox/items/'+encodeURIComponent(id);
  async function render({showCoding,isActive=()=>true}={}) {
    renderOptions={showCoding,isActive};
    const version=++generation,current=()=>pageScope('projects')()&&isActive()&&generation===version;
    const data=await api('/api/github-inbox?view='+view);
    if(!current())return;
    $('#page').className='page projects-page';
    $('#page').innerHTML=`<div class="page-head"><div><h1>Projects</h1><p class="subhead">What needs your attention across the repositories you follow.</p></div><div class="row"><button id="github-settings">${icon('settings')} ${data.connection.connected?'Manage sources':'Connect GitHub'}</button><button id="sync-inbox" ${!data.connection.connected?'disabled':''}>${icon('refresh-cw')} Refresh</button></div></div>
      ${projectTabs('inbox')}
      <section id="project-view-panel" role="tabpanel" aria-labelledby="show-github-inbox">
      <div class="inbox-status" role="status">${data.synced_at?'Synced '+escape(date(data.synced_at)):'No GitHub sync yet'}${data.login?' · @'+escape(data.login):''}<span id="inbox-syncing"></span></div>
      ${data.error?`<p class="notice">${escape(data.error)}</p>`:''}${data.warnings?.length?`<details class="notice"><summary>Some activity may be missing (${data.warnings.length})</summary><ul>${data.warnings.map(w=>`<li>${escape(w)}</li>`).join('')}</ul></details>`:''}
      <div class="inbox-layout ${!data.connection.connected?'inbox-unconnected':''}"><aside class="projects-rail" aria-label="Inbox filters">${[['attention','Needs attention'],['new','New activity'],['review','Review requested'],['assigned','Assigned to me'],['involved','Involved / subscribed'],['all','All open / activity'],['snoozed','Snoozed']].map(([id,label])=>`<button class="project-entry ${view===id?'selected':''}" data-inbox-view="${id}" ${view===id?'aria-current="true"':''}><span>${label}</span><small>${data.counts[id]}</small></button>`).join('')}<h2>Following</h2>${[...data.connection.repositories,...data.connection.organizations.map(x=>'All repositories in '+x)].map(x=>`<p class="hint">${escape(x)}</p>`).join('')||'<p class="hint">Choose repositories or organizations to follow.</p>'}<p class="hint">Priority uses GitHub review requests, assignments and mentions. Subscriptions appear when GitHub reports activity.</p></aside>
      <section class="project-main"><label class="inbox-search" ${!data.connection.connected?'hidden':''}>Filter this view<input type="search" id="inbox-search" placeholder="Title, repository, label…" value="${escape(query)}"></label><div id="inbox-list"></div></section></div></section>`;
    bindProjectTabs({active:'inbox',select:()=>{generation++;return showCoding?.();},$,action,toast});
    action($('#github-settings'),()=>configure(data.connection,()=>render({showCoding,isActive})));
    document.querySelectorAll('[data-inbox-view]').forEach(b=>action(b,()=>{view=b.dataset.inboxView;return render({showCoding,isActive});}));
    const refresh=async()=>{const el=$('#inbox-syncing');if(el)el.textContent=' · Refreshing…';try{await api('/api/github-inbox/sync','POST',{});if(current())await render({showCoding,isActive});}finally{if(current()&&$('#inbox-syncing'))$('#inbox-syncing').textContent='';}};
    action($('#sync-inbox'),refresh);
    function list(){
      const items=data.items.filter(x=>[x.title,x.repo,...x.labels].join(' ').toLowerCase().includes(query.toLowerCase()));
      $('#inbox-list').innerHTML=items.map(x=>`<article class="inbox-item"><div class="row between"><span class="hint">${escape(x.repo)} #${x.number} · ${x.kind==='pr'?'Pull request':'Issue'} · ${escape(statusLabel(x.state))}${x.stale?' · Last known activity':''}</span>${x.new?'<span class="pill">New</span>':''}</div><button class="inbox-title" data-issue="${escape(x.id)}">${escape(x.title)}</button><div class="row inbox-reasons">${x.reason_labels.map(r=>`<span class="pill">${escape(r)}</span>`).join('')}${x.labels.map(l=>`<small>${escape(l)}</small>`).join('')}</div><div class="row between"><small class="muted">Updated ${escape(x.updated_at?date(x.updated_at):'recently')}</small><div class="row"><button class="small-button" data-ask="${escape(x.id)}">Ask a bot</button><button class="small-button" data-remind="${escape(x.id)}">Remind me</button><button class="small-button quiet" data-snooze="${escape(x.id)}">${view==='snoozed'?'Unsnooze':'Snooze 1 day'}</button></div></div></article>`).join('')||`<div class="project-empty">${icon('circle-check')}<h2>${data.connection.connected?'Nothing in this view':'Connect GitHub to start'}</h2><p>${data.connection.connected?'Try All open / activity, change your filter, or refresh to check for updates.':'Follow the repositories and organizations you care about. Review requests, assignments and issue activity will appear here.'}</p>${!data.connection.connected?'<button id="first-github" class="primary">Connect GitHub</button>':''}</div>`;
      action($('#first-github'),()=>configure(data.connection,()=>render({showCoding,isActive})));
      document.querySelectorAll('[data-issue]').forEach(b=>action(b,()=>detail(b.dataset.issue,()=>render({showCoding,isActive}))));
      document.querySelectorAll('[data-ask]').forEach(b=>action(b,()=>work(data.items.find(x=>x.id===b.dataset.ask),false)));
      document.querySelectorAll('[data-remind]').forEach(b=>action(b,()=>work(data.items.find(x=>x.id===b.dataset.remind),true)));
      document.querySelectorAll('[data-snooze]').forEach(b=>action(b,async()=>{await api(path(b.dataset.snooze)+'/state','PUT',{snoozed_until:view==='snoozed'?0:Date.now()/1000+86400,seen:false});await render({showCoding,isActive});}));
    }
    $('#inbox-search').oninput=e=>{query=e.target.value;list();};list();
    if(data.connection.connected&&(data.connection.repositories.length||data.connection.organizations.length)&&Date.now()-lastAuto>300000&&(!data.synced_at||Date.now()/1000-data.synced_at>300)){
      lastAuto=Date.now();try{await refresh();}catch(e){if(current())toast(e.message,true);}
    }
  }
  function configure(connection,done){
    return connectGitHub({target:'inbox',repositories:connection.repositories,organizations:connection.organizations,onDone:async()=>{lastAuto=0;await done();},disconnect:connection.connected?()=>api('/api/github-inbox/connection','PUT',{repositories:[],organizations:[],token:''}):undefined});
  }
  async function detail(id,done){
    const item=await api(path(id));
    if(state.view!=='projects')return;
    const d=dialog(item.repo+' #'+item.number,`<h3>${escape(item.title)}</h3><p><a href="${escape(item.url)}" target="_blank" rel="noopener noreferrer">Open on GitHub</a> · ${escape(statusLabel(item.state))}</p><div class="issue-body">${escape(item.body||'No description.')}</div><div class="row"><button id="issue-ask" class="primary">Ask a bot</button><button id="issue-remind">Remind me</button><button id="issue-seen">Mark seen locally</button></div><h3>Linked work</h3>${item.links.map(x=>`<button class="small-button" data-linked="${escape(x.target_id)}" data-kind="${escape(x.kind)}">${x.kind==='task'?'Open bot conversation':'View reminder in Jobs'}</button>`).join('')||'<p class="muted">No bot work or reminders yet.</p>'}<h3>Conversation</h3>${item.conversation.map(c=>`<article class="list-item"><strong>${escape(c.author)}</strong><div class="issue-body">${escape(c.body)}</div></article>`).join('')||'<p class="muted">No comments.</p>'}${item.more_comments?'<p class="notice">Showing the first 50 comments. Open GitHub for the full conversation.</p>':''}`);
    d.addEventListener('close',()=>{if(state.view==='projects')done().catch(error=>toast(error.message,true));},{once:true});
    action(d.querySelector('#issue-ask'),()=>work(item,false));action(d.querySelector('#issue-remind'),()=>work(item,true));
    action(d.querySelector('#issue-seen'),async()=>{await api(path(id)+'/state','PUT',{seen:true});d.close();});
    d.querySelectorAll('[data-linked]').forEach(b=>action(b,async()=>{d.close();if(b.dataset.kind==='task'){const task=await api('/api/tasks/'+b.dataset.linked);state.task=task.id;state.bot=task.bot_id;await navigate('chat');}else await navigate('jobs');}));
  }
  function work(item,reminder){
    let id=crypto.randomUUID(),submitted=null;
    const d=dialog(reminder?'Remind me about this issue':'Ask a bot about this issue',`<p>${escape(item.repo)} #${item.number}: ${escape(item.title)}</p><form><label>Bot<select name="bot_id">${state.bots.map(b=>`<option value="${escape(b.id)}" ${b.id===state.bot?'selected':''}>${escape(b.name)}</option>`).join('')}</select></label><label>${reminder?'What should the bot follow up on?':'What should the bot check?'}<textarea name="prompt" required maxlength="6000" rows="4">${reminder?'Check for updates on this issue and remind me what still needs attention.':''}</textarea></label>${reminder?field('When (your local time)','when','','datetime-local','required'):''}<p class="hint">${reminder?'Runs once while ShinBot is running. Manage or pause it in Jobs.':'Starts a ShinBot conversation with the issue description attached. Findings stay in chat unless you ask to post them to GitHub.'}</p><div class="form-footer"><button type="submit" class="primary">${reminder?'Set reminder':'Start conversation'}</button></div></form>`,{guard:true});
    bind(d.querySelector('form'),async values=>{if(submitted&&submitted!==JSON.stringify(values))throw new Error('This request may already have started. Retry the original details or check linked work before submitting a different request.');const payload={...values,request_id:id};if(reminder)payload.when=new Date(values.when).toISOString();submitted=JSON.stringify(values);let result;try{result=await api(path(item.id)+'/work','POST',payload);}catch(error){if(error.requestStored===false){submitted=null;id=crypto.randomUUID();}throw error;}d.close();if(reminder){toast('Reminder saved in Jobs and Calendar.');}else{document.querySelectorAll('dialog').forEach(el=>el.close());state.task=result.id;state.bot=result.bot_id;await navigate('chat');}});
  }
  return {render,openItem:id=>detail(id,()=>render(renderOptions))};
}
