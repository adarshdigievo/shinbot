import {renderAttachment} from './attachments.js?v=20261101-desktop-1';
import {browserSettings, browserAvailability} from './browser-settings.js?v=20261101-desktop-1';
import {chatSearch} from './chat-search.js?v=20261028-workspace-2';
import {chatInspector} from './chat-inspector.js?v=20261028-workspace-2';
import {actionDescription, currentActivity} from './activity.js?v=20261101-desktop-1';
import {chatControls, effortName, thinkingLevels} from './chat-controls.js?v=20261027-effort-2';
import {nativeBrowserView} from './browser-view.js?v=20261027-background-1';
import {displayZone, formatTime, messageTimestamp} from './time.js?v=20261101-desktop-1';
import {computerPreview, browserOwner} from './computer.js?v=20261028-workspace-2';
import {updatesPanel} from './updates.js?v=20261027-native-1';
import {projectsView} from './projects.js';
import {inboxView} from './inbox.js';
import {calendarView} from './calendar.js';
import {notifications} from './notifications.js?v=20261028-native-1';
import {submissionIds} from './submissions.js';
import {githubConnection} from './github-connect.js';
import {pluginsView} from './plugins.js?v=20261027-extensions-1';
import {pluginHost} from './plugin-host.js?v=20261027-extensions-1';
import {jobsView} from './jobs.js';
import {bindComposerKeys} from './composer.js?v=20261028-queue-1';
import {promptSuggestions} from './prompt-suggestions.js?v=20261028-native-1';
import {conversationOwner, conversationRows, selectConversation, conversationTimeline, conversationSnapshots, queuedMessages} from './conversations.js?v=20261101-desktop-1';
let submissionStorage;try{submissionStorage=sessionStorage;}catch{}
const pendingSubmission=submissionIds(submissionStorage);
const homeHelpKey='shinbot.home-help-dismissed';
let homeHelpDismissed=false;try{homeHelpDismissed=localStorage.getItem(homeHelpKey)==='true';}catch{}
const root = document.querySelector('#app');
const setupToken=new URLSearchParams(location.hash.slice(1)).get('setup')||'';
if(setupToken)history.replaceState(null,'',location.pathname);
const state = {view:'home',bot:'default',chatFilter:'all',bots:[],groups:[],colors:[],task:null,tasks:[],settings:null,providers:[],stream:null,interval:null,rfb:null,selectedNote:null,pendingTemplate:null,navigation:0,activeTask:null,checkingSession:false,settingsTab:"workspace",drafts:{},chatChoices:{},notebookTab:"notes"};
const escape = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = selector => document.querySelector(selector);
const badge = status => `<span class="pill ${escape(status)}">${escape(status)}</span>`;
const date = value => formatTime(value,state.settings?.timezone);
function messageTime(value){const stamp=messageTimestamp(value,state.settings?.timezone);return stamp?`<time class="message-time" datetime="${stamp.iso}" title="${escape(stamp.title)}">${escape(stamp.label)}</time>`:'';}
function markdown(text) {return window.markdownit && window.DOMPurify ? window.DOMPurify.sanitize(window.markdownit({html:false,linkify:false,typographer:false}).render(text || '')) : escape(text);}
function toast(text, isError=false) {const modal=[...document.querySelectorAll('dialog[open]')].at(-1);if(modal){let feedback=modal.querySelector('.dialog-feedback');if(!feedback){feedback=document.createElement('p');feedback.className='dialog-feedback';modal.append(feedback);}feedback.classList.toggle('error-text',isError);feedback.setAttribute('role',isError?'alert':'status');feedback.textContent=text;return;}$('#toast').textContent=text;$('#toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').style.display='none',5000);}
async function api(path, method='GET', body) {
  const response=await fetch(path,{method,cache:'no-store',headers:{'Content-Type':'application/json','X-Shinbot-CSRF':'1'},...(body!==undefined?{body:JSON.stringify(body)}:{})});
  const data=await response.json().catch(()=>({detail:`ShinBot could not complete this request (HTTP ${response.status}). Check Settings for service status, then try again.`}));
  if(!response.ok) {if(response.status===401 && !path.startsWith('/auth')){state.stream?.close();await boot();}const error=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail || data.error || 'Request failed.'));error.status=response.status;error.upstreamStatus=data.upstream_status;error.requestStored=data.request_stored;throw error;}
  return data;
}
function bind(form, fn) {form.addEventListener('submit',async event=>{event.preventDefault();const feedback=form.closest('dialog')?.querySelector('.dialog-feedback');if(feedback)feedback.textContent='';const buttons=[...form.querySelectorAll('[type=submit]')];buttons.forEach(button=>button.disabled=true);if(form.id==='composer')form.dataset.sending='true';try{await fn(Object.fromEntries(new FormData(form)),event);}catch(error){toast(error.message,true);}finally{buttons.forEach(button=>button.disabled=false);if(form.id==='composer'){delete form.dataset.sending;if(form.isConnected)state.promptSuggestions?.sync();}}});}
function action(element, fn) {element?.addEventListener('click',async()=>{element.disabled=true;try{await fn();}catch(error){toast(error.message,true);}finally{element.disabled=false;}});}
function dialog(title, content) {const d=document.createElement('dialog');const titleId='dialog-title-'+crypto.randomUUID();d.setAttribute('aria-labelledby',titleId);d.innerHTML=`<div class="row between"><h2 id="${titleId}">${escape(title)}</h2><button type="button" class="quiet" aria-label="Close dialog"><span class="icon icon-x" aria-hidden="true"></span></button></div>${content}`;document.body.append(d);d.querySelector('button').onclick=()=>d.close();d.addEventListener('close',()=>d.remove());d.showModal();const first=d.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,select');if(first&&!first.closest('details:not([open])'))first.focus();return d;}
function field(label,name,value='',type='text',extra=''){return `<label>${escape(label)}<input name="${name}" type="${type}" value="${escape(value)}" ${extra}></label>`;}
function timezoneField(value){const zones=Intl.supportedValuesOf?.('timeZone')||['UTC'];return `<label>Timezone<input name="timezone" value="${escape(value)}" list="timezone-options" required autocomplete="off"></label><datalist id="timezone-options"><option value="UTC">${zones.map(zone=>`<option value="${escape(zone)}">`).join('')}</datalist>`;}


const icon = name => `<span class="icon icon-${name}" aria-hidden="true"></span>`;
const botColor = bot => state.colors.includes(bot?.profile?.color) ? bot.profile.color : 'blue';
const avatar = (classes='',bot=null) => `<span class="mascot ${classes}${bot?' bot-identity bot-color-'+botColor(bot):''}"${bot?' data-bot-color="'+botColor(bot)+'"':''} aria-hidden="true"><img data-brand src="/static/brand/shinbot-icon.png?v=20261028" alt="" width="256" height="256"></span>`;
const appearanceModes = Object.entries(window.ShinBotTheme.themes).map(([id,t])=>[id,id==='system'?'monitor':t.mode==='dark'?'moon':'sun',t.name,t.description]);
function appearancePicker(){return `<label class="appearance-picker">${icon('sun')}<span class="sr-only">Appearance</span><select aria-label="Appearance" data-appearance-control>${appearanceModes.map(([value,,label])=>`<option value="${value}" ${window.ShinBotTheme.preference===value?'selected':''}>${label}</option>`).join('')}</select></label>`;}
function appearancePanel(){return `<section class="settings-section" id="appearance-panel" data-settings-panel="appearance" role="tabpanel" aria-labelledby="appearance-tab"><h2>Appearance</h2><p class="muted">Choose how ShinBot looks on this device. System follows your device’s light or dark setting.</p><fieldset class="appearance-options"><legend class="sr-only">Color theme</legend>${appearanceModes.map(([value,name,label,description])=>`<label class="appearance-option"><input type="radio" name="appearance" value="${value}" data-appearance-control ${window.ShinBotTheme.preference===value?'checked':''}><span class="theme-swatches swatches-${value}" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span>${label}<small>${description}</small></span></label>`).join('')}</fieldset><p class="hint" data-appearance-status role="status">Changes apply immediately and are saved in this browser.</p><label class="checkbox-row"><input type="checkbox" data-home-help ${homeHelpDismissed?'':'checked'}>Show getting-started tips on Home</label></section>`;}
document.addEventListener('change',event=>{if(!event.target.matches('[data-appearance-control]'))return;const saved=window.ShinBotTheme.set(event.target.value);document.querySelectorAll('[data-appearance-status]').forEach(el=>el.textContent=saved?'Appearance saved in this browser.':'Appearance changed for this visit. Browser storage is unavailable, so it may reset when you reload.');if(!saved&&!$('[data-appearance-status]'))toast('Appearance changed for this visit. Browser storage is unavailable.');});
function setHomeHelpDismissed(hidden){
  homeHelpDismissed=hidden;
  let saved=true;try{localStorage.setItem(homeHelpKey,String(hidden));}catch{saved=false;}
  if(hidden){const banner=$('.home-help');const focused=banner?.contains(document.activeElement);banner?.remove();if(focused)($('#page [data-open-bot]')||$('#new-bot'))?.focus();}
  return saved;
}
document.addEventListener('change',event=>{if(!event.target.matches('[data-home-help]'))return;if(!setHomeHelpDismissed(!event.target.checked))toast('Changed for this visit. Your browser could not save this choice.');});
window.addEventListener('storage',event=>{if(event.key!==homeHelpKey&&event.key!==null)return;homeHelpDismissed=event.newValue==='true';const choice=$('[data-home-help]');if(choice)choice.checked=!homeHelpDismissed;if(state.view==='home'&&$('#page'))renderHome().catch(error=>toast(error.message));});
function closeConversationMenu(restoreFocus=false){const menu=$('#conversation-menu');if(!menu?.open)return;menu.open=false;if(restoreFocus)menu.querySelector('summary').focus();}
document.addEventListener('pointerdown',event=>{if(!event.target.closest('#conversation-menu'))closeConversationMenu();});
document.addEventListener('focusin',event=>{if(!event.target.closest('#conversation-menu'))closeConversationMenu();});
const activeBot = () => state.bots.find(bot=>bot.id===state.bot);
const botName = () => activeBot()?.name || (state.bot==='default'?'ShinBot':'Unavailable bot');
const draftKey = () => state.task || 'new:'+state.bot;
const pages = [['home','house','Home'],['chat','message-circle','Chat'],['computer','monitor','Computer'],['notebook','notebook-pen','Notebook'],['board','columns-3','Board'],['projects','folder-git-2','Projects'],['calendar','clock','Calendar'],['jobs','clock','Jobs'],['plugins','puzzle','Plugins'],['settings','settings','Settings']];
function visiblePages(){const core=pages.filter(([id])=>!['board','projects','calendar'].includes(id)||(state.installedPlugins||[]).some(p=>p.id===(id==='board'?'kanban':id)&&p.status==='active'));const index=core.findIndex(([id])=>id==='plugins');return [...core.slice(0,index),...extensionsUI.pages(),...core.slice(index)];}
async function refreshPluginNavigation(installed){
  state.installedPlugins=installed;
  await extensionsUI.sync();
  const nav=$('.nav');if(!nav)return;
  nav.innerHTML=visiblePages().map(([id,name,label])=>`<button data-view="${id}" class="${state.view===id?'active':''}" ${state.view===id?'aria-current="page"':''}>${icon(name)}<span>${escape(label)}</span></button>`).join('');
  nav.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>navigate(button.dataset.view));
}
const extensionsUI=pluginHost({api,state,navigate,toast,compose:async text=>{rememberDraft();state.task=null;await navigate('chat');const input=$('#prompt');if(input){input.value=String(text);input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();}}});
const computerUI=computerPreview({api,state,icon,navigate});
const connectGitHub=githubConnection({api,dialog,escape,field,bind,action,toast});
const integrationDeps={connectGitHub,api,state,$,escape,icon,badge,dialog,field,bind,action,toast,navigate,pageScope,date};
const inboxUI=inboxView(integrationDeps),calendarUI=calendarView(integrationDeps);
const projectUI=projectsView({...integrationDeps,inbox:inboxUI});
const pluginUI=pluginsView({api,$,state,escape,icon,dialog,action,toast,pageScope,refreshPluginNavigation,latestCatalogEntries,scanSummary,configurePlugin,reviewPlugin,configurePluginAccess,extensionAccessFields,bindExtensionAccess,extensionAccessValue,connectMCP,importSource,installSkill,addMarketplace,uploadPlugin,createPlugin});
const notificationUI=notifications({api,$,escape,icon,action,toast,date,open:async item=>{
  if(item.task_id)await openConversation(await api('/api/tasks/'+item.task_id));
  else if(item.kind==='github')await projectUI.openInbox(item.item_id);
  else await navigate(item.view);
}});
function rememberDraft(){const prompt=$('#prompt');if(prompt?.dataset.draftKey)state.drafts[prompt.dataset.draftKey]=prompt.value;}
function prepareComposer(){
  const prompt=$('#prompt');prompt.dataset.draftKey=draftKey();state.renderedDraftKey=draftKey();prompt.value=state.drafts[draftKey()]||'';prompt.oninput=rememberDraft;
  $('#composer').onsubmit=event=>event.preventDefault();$('#composer [type=submit]').disabled=true;
}
const sidebarContent = new WeakMap();
function updateSidebarList(container,content,key){
  if(sidebarContent.get(container)===content)return false;
  const focused=container.contains(document.activeElement)?document.activeElement.dataset[key]:null;
  container.innerHTML=content;sidebarContent.set(container,content);
  if(focused)[...container.querySelectorAll('button')].find(button=>button.dataset[key]===focused)?.focus({preventScroll:true});
  return true;
}
function setNavigation(open){
  $('.layout')?.classList.toggle('navigation-open',open);
  $('.sidebar')?.classList.toggle('open',open);
  $('.mobile-menu')?.setAttribute('aria-expanded',String(open));
  if($('.main'))$('.main').inert=open;
  if(open)$('#close-navigation')?.focus();
  else $('.mobile-menu')?.focus();
}
function renderBotList(){
  if(!$('#bot-list'))return;
  const content=state.bots.map(bot=>`<button data-sidebar-bot="${escape(bot.id)}" class="roster-item ${bot.id===state.bot?'selected':''}" ${bot.id===state.bot?'aria-current="true"':''}>${avatar('roster-avatar',bot)}<span>${escape(bot.name)}<small>${state.tasks.some(t=>t.bot_id===bot.id&&['running','queued','waiting'].includes(t.status))?'Task in progress':escape(bot.profile.description||'Personal assistant')}</small></span></button>`).join('');
  if(!updateSidebarList($('#bot-list'),content,'sidebarBot'))return;
  document.querySelectorAll('[data-sidebar-bot]').forEach(button=>button.onclick=async()=>{rememberDraft();state.bot=button.dataset.sidebarBot;state.chatFilter=state.bot;state.task=null;$('#active-bot').value=state.bot;renderTaskList();await navigate('chat');});
}
const openSearch=chatSearch({api,state,dialog,escape,icon,date,toast,open:async(task,match)=>{
  await openConversation(task);
  if(match.message_id){const entry=$(`[data-entry="${match.match||'user'}-${CSS.escape(match.message_id)}"]`);if(entry){entry.scrollIntoView({block:'center'});entry.classList.add('search-match');}}
}});
const inspectorUI=chatInspector({escape,icon,date,computer:computerUI,openCard:card=>editWorkCard(card),openComputer:()=>navigate('computer')});
function inspectorToggle(){return `<button type="button" class="quiet icon-button" data-toggle-inspector aria-label="Chat details" aria-controls="chat-inspector" aria-expanded="false" title="Show chat details">${icon('panel-right')}</button>`;}
function chatLayout(content){return `<div class="chat-workspace"><div class="chat-main">${content}</div>${inspectorUI.markup()}</div>`;}
document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k') {event.preventDefault();openSearch();}if(event.key==='Escape'&&!document.querySelector('dialog[open]')){if($('#conversation-menu')?.open){event.preventDefault();closeConversationMenu(true);}else if($('.sidebar.open'))setNavigation(false);}});
matchMedia('(min-width: 761px)').addEventListener('change',event=>{if(event.matches&&$('.sidebar.open'))setNavigation(false);});

async function boot() {
  notificationUI.stop();computerUI.stop();extensionsUI.stop();clearInterval(state.pluginInterval);
  clearInterval(state.updateInterval);
  state.navigation++;document.querySelectorAll('dialog[open]').forEach(modal=>modal.close());
  clearInterval(state.interval);state.rfb?.disconnect();state.stream?.close();
  const auth=await api('/auth/status');
  if(!auth.authenticated){state.drafts={};state.renderedDraftKey=null;document.title='ShinBot';if(auth.claimed)renderLogin(true);else await renderWelcome();return;}
  state.settings=await api('/api/settings');state.installedPlugins=(await api('/api/plugins')).installed;
  const roster=await api('/api/bots');state.bots=roster.bots;state.groups=roster.groups;state.colors=roster.colors;
  if(!state.bots.some(b=>b.id===state.bot))state.bot='default';
  root.innerHTML=`<div class="layout"><aside class="sidebar" id="navigation-panel" aria-label="Workspace navigation"><div class="sidebar-brand"><a class="brand" href="#" id="brand-home">${avatar()}<span>ShinBot</span></a><button id="close-navigation" class="quiet icon-button" aria-label="Close navigation">${icon('x')}</button></div><button id="search-workspace" class="search-trigger">${icon('search')}<span>Search chats</span><kbd>${navigator.platform.includes('Mac')?'⌘':'Ctrl'} K</kbd></button><button id="new-task" class="new-chat">${icon('square-pen')}<span>New chat</span></button><div class="sidebar-scroll"><nav class="nav" aria-label="Main navigation">${visiblePages().map(([id,name,label])=>`<button data-view="${id}">${icon(name)}<span>${escape(label)}</span></button>`).join('')}</nav><div class="bot-roster-section"><details class="bot-roster" ${state.rosterOpen===false?'':'open'}><summary>${icon('chevron-right')}<span>Bot profiles</span></summary><div id="bot-list" class="bot-list"></div></details><button id="sidebar-add-bot" class="quiet icon-button" aria-label="Add bot profile" title="Add bot profile">${icon('plus')}</button></div><select id="active-bot" aria-label="Active bot" hidden>${state.bots.map(b=>`<option value="${escape(b.id)}">${escape(b.name)}</option>`).join('')}</select><div class="sidebar-label"><label for="chat-bot-filter">Recent chats</label><select id="chat-bot-filter" class="chat-bot-filter" aria-label="Filter recent chats by bot"></select></div><div class="task-list" id="task-list"></div></div><div id="computer-dock" class="computer-dock"></div><button id="update-notice" class="update-notice" hidden></button><div data-plugin-slot="sidebar-footer"></div><div class="sidebar-footer">${icon('server')}<span>On your server<small id="connection">Connecting…</small></span><span class="version">0.1</span></div></aside><button class="navigation-backdrop" aria-label="Close navigation" tabindex="-1"></button><main class="main"><header class="topbar"><div class="row"><button class="mobile-menu quiet icon-button" aria-label="Open navigation" aria-controls="navigation-panel" aria-expanded="false">${icon('panel-left')}</button><span class="trail"><span id="workspace-name">${escape(state.settings.name)}</span>${icon('chevron-right')}<strong id="crumb">Home</strong></span></div><div class="top-actions"><div data-plugin-slot="topbar"></div><button id="header-computer" class="live-chip" type="button" aria-label="Show browser preview" aria-controls="computer-preview" hidden><span class="live-dot" aria-hidden="true"></span>${icon('monitor')}<span>Show browser</span></button>${notificationUI.markup()}<button id="header-profile" class="profile-button" aria-label="Edit active bot">${avatar('header-avatar',activeBot())}<span id="header-bot-name">${escape(botName())}</span>${icon('chevron-down')}</button></div></header><div id="computer-dock-mobile" class="computer-dock-mobile"></div><div id="page" class="page"></div></main></div>`;
  notificationUI.mount();
  $('#active-bot').value=state.bot;$('#active-bot').onchange=()=>{rememberDraft();state.bot=$('#active-bot').value;state.task=null;renderTaskList();navigate(state.view);};
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>navigate(button.dataset.view));
  $('.mobile-menu').onclick=()=>setNavigation(true);$('#close-navigation').onclick=()=>setNavigation(false);$('.navigation-backdrop').onclick=()=>setNavigation(false);
  $('#navigation-panel').onkeydown=event=>{if(event.key!=='Tab'||!$('.sidebar.open'))return;const items=[...$('#navigation-panel').querySelectorAll('button,a,input,select')].filter(el=>el.offsetParent!==null&&!el.closest('.sr-only'));const first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}};
  $('.bot-roster').ontoggle=event=>{state.rosterOpen=event.target.open;};
  $('#brand-home').onclick=event=>{event.preventDefault();navigate('home');};$('#search-workspace').onclick=openSearch;$('#sidebar-add-bot').onclick=()=>editBot();$('#header-computer').onclick=()=>computerUI.show();$('#header-profile').onclick=()=>editBot(activeBot());workspaceTitle();
  $('#new-task').onclick=()=>{rememberDraft();if(state.chatFilter!=='all')state.bot=state.chatFilter;state.task=null;navigate('chat');};
  state.tasks=await api('/api/tasks');renderTaskList();computerUI.mount();await notificationUI.refresh().catch(()=>{});
  state.stream=new EventSource('/api/events');
  state.stream.addEventListener('snapshot',event=>{const data=JSON.parse(event.data);state.tasks=data.tasks;if(state.view==='chat'&&state.activeTask?.id===state.task&&!state.tasks.some(t=>t.id===state.task)){const t=state.activeTask;state.tasks.push({id:t.id,bot_id:t.bot_id,title:t.title,status:t.status,updated:t.updated});}renderTaskList();projectUI.update(data.tasks);notificationUI.update(data.notifications);document.dispatchEvent(new CustomEvent('shinbot:snapshot',{detail:data}));});
  state.stream.onopen=()=>{if($('#connection'))$('#connection').textContent='Connected';};
  state.stream.onerror=async()=>{if($('#connection'))$('#connection').textContent='Reconnecting…';if(state.stream?.readyState===EventSource.CLOSED&&!state.checkingSession){state.checkingSession=true;try{const auth=await api('/auth/status');if(!auth.authenticated)await boot();}catch{}finally{state.checkingSession=false;}}};
  const calendarResult=new URLSearchParams(location.search).get('calendar');
  if(['connected','failed'].includes(calendarResult)){state.view='calendar';history.replaceState(null,'',location.pathname);}
  await navigate(visiblePages().some(([id])=>id===state.view)?state.view:'home');
  if(calendarResult==='failed')toast('Calendar sign-in did not finish. Sign in to ShinBot, then try again from Calendar connection settings.',true);
  async function updateNotice(){try{const data=await api('/api/updates');const notice=$('#update-notice');if(!notice)return;notice.hidden=data.status!=='available';notice.textContent='ShinBot '+data.latest_version+' available';notice.onclick=()=>{state.settingsTab='updates';navigate('settings');};}catch{}}
  await updateNotice();state.updateInterval=setInterval(updateNotice,60000);
  state.pluginInterval=setInterval(async()=>{try{const data=await api('/api/plugins');await refreshPluginNavigation(data.installed);if(!visiblePages().some(([id])=>id===state.view))await navigate('home');}catch{}},15000);
}
function renderLogin(claimed) {
  root.innerHTML=`<div class="login-shell"><section class="login-story"><div class="brand">${avatar()}<span>ShinBot</span></div><div><h1>Continue with ShinBot</h1><p>Pick up a conversation, check a scheduled job, or open your notes.</p></div><footer><small>Your server. Your choice of model.</small></footer></section><section class="login-panel"><div class="login-appearance">${appearancePicker()}</div><form id="login"><h2>${claimed?'Welcome back.':'Set up ShinBot'}</h2><p class="muted">${claimed?'Sign in to continue with ShinBot.':'Set a password to protect your bots and saved data.'}</p>${claimed?'':field('Installation token','bootstrap','','password','required autocomplete="off"')}${field(claimed?'Password':'Choose a password','password','','password','required minlength="12" maxlength="256" autocomplete="'+(claimed?'current-password':'new-password')+'"')}<button class="primary" type="submit">${claimed?'Sign in':'Set up ShinBot'}</button><p class="hint">${claimed?'Use the password you set for this installation.':'Run shinbot setup-token on the host to get your installation token. Use at least 12 characters for your password.'}</p><p id="login-error" class="error-text" role="alert"></p></form></section></div>`;
  bind($('#login'),async data=>{try{await api('/auth/login','POST',data);await boot();}catch(error){$('#login-error').textContent=error.message;}});
}
async function openConversation(task) {
  rememberDraft();selectConversation(state,task);await navigate('chat');
}
function syncBotContext() {
  if($('#active-bot'))$('#active-bot').value=state.bot;
  if($('#header-bot-name'))$('#header-bot-name').textContent=botName();
  $('#header-profile')?.setAttribute('aria-label','Edit '+botName()+' profile');
  const headerAvatar=$('#header-profile .mascot');
  if(headerAvatar&&headerAvatar.dataset.botColor!==botColor(activeBot()))headerAvatar.outerHTML=avatar('header-avatar',activeBot());
  const owner=$('#conversation-owner');
  if(owner){
    const group=state.groups.find(group=>group.id===activeBot()?.group_id);
    const content=`${avatar('owner-avatar',activeBot())}<span>Chat with <strong>${escape(botName())}</strong>${group?`<span class="owner-group"> · ${escape(group.name)}</span>`:''}</span>`;
    if(owner.innerHTML!==content)owner.innerHTML=content;
  }
  const prompt=$('#prompt');
  if(prompt){if($('#prompt-suggestion')?.hidden!==false)prompt.placeholder='Message '+botName()+'…';prompt.setAttribute('aria-label','Message '+botName());$('#composer label[for="prompt"]').textContent='Message '+botName();}
  const newChat=$('#new-task'),newBot=state.chatFilter==='all'?botName():state.bots.find(bot=>bot.id===state.chatFilter)?.name||botName();if(newChat){newChat.title='New chat with '+newBot;newChat.setAttribute('aria-label','New chat with '+newBot);}
}
function renderTaskList() {
  if(!$('#task-list'))return;renderBotList();
  const filter=$('#chat-bot-filter');
  const options='<option value="all">All bots</option>'+state.bots.map(bot=>`<option value="${escape(bot.id)}">${escape(bot.name)}</option>`).join('');
  if(filter.innerHTML!==options)filter.innerHTML=options;
  if(state.chatFilter!=='all'&&!state.bots.some(bot=>bot.id===state.chatFilter))state.chatFilter='all';
  filter.value=state.chatFilter;
  filter.onchange=()=>{state.chatFilter=filter.value;renderTaskList();syncBotContext();};
  const content=state.tasks.length?conversationRows({tasks:state.tasks,bots:state.bots,filter:state.chatFilter,selected:state.task,escape,avatar,icon}):'<p class="sidebar-empty">Your conversations appear here.</p>';
  if(!updateSidebarList($('#task-list'),content,'task'))return;
  document.querySelectorAll('[data-task]').forEach(button=>button.onclick=()=>openConversation(state.tasks.find(task=>task.id===button.dataset.task)));
}
function pageScope(view){const navigation=state.navigation;return()=>state.navigation===navigation&&state.view===view&&!!$('#page');}
async function navigate(view) {
  rememberDraft();notificationUI.close();inspectorUI.unmount();extensionsUI.clearPage();const navigation=++state.navigation;
  clearInterval(state.interval);state.rfb?.disconnect();state.rfb=null;state.view=view;computerUI.update();
  if(view==='chat'&&state.task&&state.chatFilter!=='all'&&state.chatFilter!==state.bot)state.chatFilter='all';
  if($('.sidebar.open'))setNavigation(false);syncBotContext();renderTaskList();
  document.querySelectorAll('[data-view]').forEach(button=>{const active=button.dataset.view===view;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
  await refreshPluginNavigation(state.installedPlugins||[]);if(state.navigation!==navigation)return;
  $('#crumb').textContent=visiblePages().find(([id])=>id===view)?.[2]||'Plugin';
  document.dispatchEvent(new CustomEvent('shinbot:navigate',{detail:{view,botId:state.bot,taskId:state.task}}));
  const render={home:renderHome,chat:renderChat,computer:renderComputer,notebook:renderNotebook,board:renderBoard,projects:projectUI.render,calendar:calendarUI.render,jobs:renderJobs,plugins:renderPlugins,settings:renderSettings}[view];
  try{if(view.startsWith('plugin/'))await extensionsUI.render(view,$('#page'));else await render();}catch(error){if(state.navigation!==navigation||!$('#page'))return;$('#page').className='page';$('#page').innerHTML=`<div class="state-panel" role="alert"><h1>Could not load this page</h1><p>${escape(error.message)}</p><button id="retry">Try again</button></div>`;action($('#retry'),()=>navigate(view));}
}
function workspaceTitle(){document.title=(state.settings?.name||'ShinBot')+' · ShinBot';if($('#workspace-name'))$('#workspace-name').textContent=state.settings.name;}
function selectedModel(){if(state.task&&state.activeTask?.id===state.task)return state.activeTask;const profile=state.bots.find(b=>b.id===state.bot)?.profile;return {provider:profile?.provider||state.settings.provider,model:profile?.model||state.settings.model,thinking_level:state.settings.thinking_level||'off',approval_mode:state.settings.approval_mode||'auto',...(state.chatChoices[state.bot]||{})};}
function providerReady(providers,selection){return providers.some(p=>p.id===selection.provider&&p.configured)&&!!selection.model;}
function composer() {return `<form id="composer" class="composer"><label class="sr-only" for="prompt">Message ${escape(botName())}</label><div class="composer-input"><textarea id="prompt" name="content" placeholder="Message ${escape(botName())}…" required maxlength="20000" aria-label="Message ${escape(botName())}"></textarea><span id="prompt-suggestion" class="prompt-suggestion" hidden><kbd>Tab</kbd> to complete</span></div><div class="composer-footer"><div id="chat-controls" class="chat-controls"></div><div data-plugin-slot="composer-actions"></div><div class="row"><button class="primary send-button" type="submit" aria-label="Send" title="Send message">${icon('send-horizontal')}<span class="send-label" hidden>Queue</span></button><button id="steer-draft" class="quiet steer-draft" type="submit" title="Redirect the current task at its next step" hidden>Steer</button></div></div></form>`;}
async function renderChat() {
  const scope=pageScope('chat'),renderedTask=state.task,current=()=>scope()&&state.task===renderedTask;if(!current())return;
  inspectorUI.unmount();state.promptSuggestions?.dispose();state.promptSuggestions=null;
  state.chatSnapshots=conversationSnapshots();
  $('#page').className='page chat-page';
  if(!state.task){
    const profile=activeBot()?.profile;
    $('#page').innerHTML=chatLayout(`<div class="chat-start-toolbar">${inspectorToggle()}</div><div class="chat-start"><div class="welcome">${avatar('chat-emblem',activeBot())}<h1>What can I help with?</h1><p class="subhead">${escape(profile?.description||'Research a question, make a plan, or take care of a task.')}</p>${composer()}<p class="chat-footnote">Conversations with ${escape(botName())} are saved on your server.</p></div></div>`);
    inspectorUI.mount();prepareComposer();
  }else{
    $('#page').innerHTML=chatLayout(`<div class="chat-heading"><div class="chat-heading-copy"><h1 id="task-title">Loading conversation…</h1><div id="conversation-owner" class="conversation-owner"></div></div><div class="row">${inspectorToggle()}<span id="context-status" class="context-status" role="status" hidden>Managing conversation context…</span><button id="cancel-task" class="small-button danger">${icon('square')}Stop</button><details id="conversation-menu" class="conversation-menu"><summary aria-label="Conversation options" title="Conversation options">${icon('ellipsis')}</summary><div class="conversation-actions"><p>Older messages are summarized automatically when context fills up.</p><button id="compact" class="quiet" type="button" aria-describedby="compact-help">${icon('list-collapse')}Compact now</button><small id="compact-help">Summarize earlier messages to free up model context.</small></div></details></div></div><div class="conversation-scroll" id="conversation-scroll"><div id="messages" class="messages" aria-busy="false"></div><div id="chat-status" class="sr-only" role="status" aria-live="polite"></div></div><div class="composer-dock"><section id="message-queue" class="message-queue" aria-label="Queued messages" hidden></section>${composer()}<p class="chat-footnote">Review important details and actions before approving.</p></div>`);
    inspectorUI.mount();prepareComposer();
    action($('#cancel-task'),async()=>{await api(`/api/tasks/${state.task}/cancel`,'POST',{});await refreshTask();});
    action($('#compact'),async()=>{const taskId=state.task;closeConversationMenu(true);await api(`/api/tasks/${taskId}/compact`,'POST',{});toast('Summarizing earlier messages. Your saved conversation stays available.');if(state.task===taskId)await refreshTask();});
    await refreshTask();if(!current())return;state.interval=setInterval(()=>refreshTask().catch(error=>toast(error.message,true)),1500);
  }
  if(!current())return;
  $('#composer [type=submit]').disabled=false;
  const controlTask=state.task,controlBot=state.bot;
  state.chatControl=chatControls({root:$('#chat-controls'),api,selection:selectedModel,escape,icon,toast,
    settings:()=>{state.settingsTab='connections';return navigate('settings');},
    save:async choices=>{
      if(controlTask){const updated=await api(`/api/tasks/${controlTask}/options`,'PATCH',choices);if(!current())return;state.activeTask=updated;await refreshTask();}
      else{state.chatChoices[controlBot]={...state.chatChoices[controlBot],...choices};updatePromptSuggestions();}
    }});
  bind($('#composer'),async (data,event)=>{
    const content=data.content.trim();if(!content)return;
    if(state.chatControl?.saving){toast('Wait for your chat settings to save.');return;}
    const key=$('#prompt').dataset.draftKey,botId=state.bot,taskId=state.task,selection=state.chatControl?.selection()||selectedModel();
    const stillHere=()=>state.view==='chat'&&state.task===taskId&&state.bot===botId&&$('#prompt')?.dataset.draftKey===key;
    const providers=await api('/api/providers');
    if(!providerReady(providers,selection)){toast((selection.provider?'Connect '+selection.provider:'Choose a model')+' in Settings before sending. Your message is still here.');return;}
    const mode=event.submitter?.id==='steer-draft'?'steer':'queue';
    const submission=await pendingSubmission({taskId,botId,content,...(mode==='steer'?{mode}: {})});
    if(!taskId){
      const task=await api('/api/tasks','POST',{content,request_id:submission.id,bot_id:botId,options:{provider:selection.provider,model:selection.model,thinking_level:selection.thinking_level,approval_mode:selection.approval_mode}});submission.acknowledge();delete state.chatChoices[botId];
      const unchanged=(state.drafts[key]||'').trim()===content;
      if(stillHere()){
        if(!unchanged)state.drafts[task.id]=state.drafts[key];
        delete state.drafts[key];state.task=task.id;await renderChat();
      }else{if(unchanged)delete state.drafts[key];toast('Conversation started. You can open it from recent chats.');}
    }else{
      await api(`/api/tasks/${taskId}/messages`,'POST',{content,request_id:submission.id,bot_id:botId,mode});submission.acknowledge();
      const unchanged=(state.drafts[key]||'').trim()===content;
      if(unchanged)delete state.drafts[key];
      if(stillHere()){if(unchanged&&$('#prompt').value.trim()===content)$('#prompt').value='';await refreshTask();}
      else toast('Message sent. You can return to the conversation from recent chats.');
    }
    renderTaskList();
  });
  state.promptSuggestions=promptSuggestions({textarea:$('#prompt'),hint:$('#prompt-suggestion'),request:payload=>api('/api/prompt-suggestion','POST',payload)});
  bindComposerKeys($('#prompt'),{acceptSuggestion:()=>state.promptSuggestions?.accept(),dismissSuggestion:()=>state.promptSuggestions?.dismiss()});
  updatePromptSuggestions();if(state.task)renderMessageQueue(state.activeTask);
}
function updatePromptSuggestions(){state.promptSuggestions?.update({task:state.task?state.activeTask:null,bot:activeBot()||{id:state.bot,profile:{}},selection:selectedModel()});}
function renderMessageQueue(task) {
  const busy = ['running','waiting','queued'].includes(task?.status);
  const active = busy && task.messages.some(message=>['running','dispatching'].includes(message.status));
  if($('#steer-draft'))$('#steer-draft').hidden=!active;
  const send=$('#composer .send-button');
  if(send){
    send.classList.toggle('is-queue',busy);
    send.setAttribute('aria-label',busy?'Queue':'Send');
    send.title=busy?'Queue after the current reply':'Send message';
    send.querySelector('.send-label').hidden=!busy;
  }
  const queue=$('#message-queue');if(!queue||!task)return;
  const pending=queuedMessages(task);
  queue.hidden=!pending.length;
  const html=`<div class="queue-heading"><strong>${pending.length} queued</strong><span>Processed one at a time</span></div><ol>${pending.map(message=>`<li><p>${escape(message.content)}</p>${message.mode==='steer'?'<span class="queue-steering">Steering at next step</span>':message.status==='queued'&&active?`<button type="button" class="quiet small-button" data-steer-message="${escape(message.id)}" title="Use this message to redirect the current task">Steer</button>`:'<span class="muted">Up next</span>'}</li>`).join('')}</ol>`;
  if(queue.dataset.content===html)return;
  queue.innerHTML=html;queue.dataset.content=html;
  queue.querySelectorAll('[data-steer-message]').forEach(button=>action(button,async()=>{await api(`/api/tasks/${task.id}/messages/${button.dataset.steerMessage}/steer`,'POST',{});if(state.task===task.id)await refreshTask();}));
}
async function refreshTask() {
  const taskId=state.task,snapshots=state.chatSnapshots,current=pageScope('chat');
  if(!taskId||!snapshots||!current())return;
  const sequence=snapshots.request(),response=await api(`/api/tasks/${taskId}`);
  if(!current()||taskId!==state.task||snapshots!==state.chatSnapshots||!$('#messages'))return;
  const task=snapshots.accept(sequence,response);if(!task)return;
  if(document.visibilityState==='visible'&&document.hasFocus())notificationUI.readTask(task.id).catch(()=>{});
  const activity=currentActivity(task);state.activeTask=task;inspectorUI.update(task);updatePromptSuggestions();$('#cancel-task').hidden=!['running','queued','waiting'].includes(task.status);const compacting=!!task.progress?.compacting;$('#compact').disabled=compacting||['running','queued','waiting'].includes(task.status);$('#context-status').hidden=!compacting;$('#compact-help').textContent=compacting?'Context is being summarized.':$('#compact').disabled?'Available after this task finishes. Context is managed automatically.':'Summarize earlier messages to free up model context.';$('#task-title').textContent=task.title;const working=['running','queued','waiting'].includes(task.status);$('#messages').setAttribute('aria-busy',String(working));const statusText=activity?.label||task.wait_reason||{running:'ShinBot is working.',queued:'Your task is queued.',waiting:'ShinBot needs your approval.',completed:'Reply ready.',failed:'The task could not finish.',cancelled:'Task stopped.',uncertain:'This task needs your review.'}[task.status]||task.status;if($('#chat-status').textContent!==statusText)$('#chat-status').textContent=statusText;renderMessageQueue(task);
  const ownerId=conversationOwner(task,state.bots).id;
  if(ownerId!==state.bot)selectConversation(state,task);
  const summary={id:task.id,bot_id:ownerId,title:task.title,status:task.status,updated:task.updated},index=state.tasks.findIndex(item=>item.id===task.id);
  if(index<0)state.tasks.unshift(summary);else state.tasks[index]={...state.tasks[index],...summary};
  renderTaskList();syncBotContext();state.chatControl?.update();
  let content=(task.workspace_path?`<div class="notice" data-entry="workspace"><strong>Project worktree</strong><p><code>${escape(task.workspace_path)}</code></p><small>Commands and file tools start here. Return to Projects to inspect files and changes.</small></div>`:'');
  content+=conversationTimeline(task).map(entry=>{
    const key=escape(entry.key),message=entry.message;
    if(entry.kind==='user')return `<div class="message user" data-entry="${key}"><div class="byline">${message.request_id?.startsWith("delegation-result:")?"Delegated result":"You"}${messageTime(message.created)}</div>${escape(message.content).replaceAll('\n','<br>')}</div>`;
    if(entry.kind==='answer'||entry.kind==='progress')return `<div class="message assistant" data-entry="${key}"><div class="byline assistant-byline">${avatar("reply-avatar",activeBot())}${escape(botName())}${entry.kind==='progress'?' · writing':''}${messageTime(entry.kind==='answer'?message.finished:entry.at)}</div>${markdown(entry.kind==='answer'?message.answer:entry.text)}</div>`;
    if(entry.kind==='attachment')return renderAttachment(entry.attachment,{escape,icon,messageTime});
    if(entry.kind==='error')return `<div class="notice" data-entry="${key}">${escape(message.error)}</div>`;
    const item=entry.action;
    if(item.status==='pending')return `<div class="approval" data-entry="action-${escape(item.id)}"><h3>${icon('shield-check')}Allow this action?</h3><p>ShinBot wants to <strong>${escape(item.tool==='run_command'?'run a command':item.tool==='browser'?'use the browser':'use '+item.tool)}</strong>.</p>${item.approval?.reason?`<p class="hint">${escape(item.approval.reason)}</p>`:''}<pre>${escape(JSON.stringify(JSON.parse(item.args),null,2))}</pre><div class="row"><button class="primary" data-approve="${escape(item.id)}">Allow once</button>${item.approval?.once_only?'':`<button data-approve="${escape(item.id)}" data-scope="chat">Allow for this chat</button>`}${task.parent_task_id||item.approval?.once_only?'':`<button data-approve="${escape(item.id)}" data-scope="always">Always allow</button>`}<button data-deny="${escape(item.id)}">Decline</button><small>Expires ${date(item.expires)}</small></div><p class="hint">${item.approval?.once_only?'This action needs its own approval.':item.tool==='run_command'?'Saved approvals allow this tool. Auto still asks before destructive or unclear commands.':'Saved approvals apply to this tool or operation. Auto still checks the live browser target.'}${task.parent_task_id?' This is a delegated task, so the approval applies only to this chat.':''}${item.approval?.once_only?'':' Manage saved approvals in Settings → Permissions.'}</p></div>`;
    if(item.status==='uncertain')return `<div class="approval" data-entry="action-${escape(item.id)}"><h3>This action needs a check.</h3><p>${escape(item.error)}</p><pre>${escape(item.tool)} ${escape(item.args)}</pre><div class="row"><button data-resolve="${escape(item.id)}" data-outcome="completed">I verified it completed</button><button data-resolve="${escape(item.id)}" data-outcome="not_completed">I verified it did not happen</button></div></div>`;
    const description=actionDescription(item);
    return `<details class="tool-item" data-entry="action-${escape(item.id)}"><summary>${icon("chevron-right")}<span class="tool-copy">${escape(description.label)}${description.detail?`<small>${escape(description.detail)}</small>`:''}</span>${badge(item.status)}</summary><pre>${escape(item.error||item.result||item.args).slice(0,7000)}</pre></details>`;
  }).join('');
  if(activity)content+=`<div class="working ${activity.waiting?'waiting':''}" data-entry="status">${icon(activity.icon)}<span><strong>${escape(activity.label)}</strong>${activity.detail?`<small>${escape(activity.detail)}</small>`:''}</span></div>`;
  else content+=`<div class="row between muted" data-entry="status"><small>${escape(task.tokens.toLocaleString())} tokens · ${escape(task.provider)} / ${escape(task.model)}</small>${badge(task.status)}</div>`;
  if(task.error)content+=`<p class="error-text" data-entry="task-error">${escape(task.error)}</p>`;
  if($('#messages').dataset.content!==content){
    const scroller=$('#conversation-scroll'),atBottom=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<100;const messages=$('#messages'),fragment=document.createElement('template');fragment.innerHTML=content;const existing=new Map([...messages.children].map(node=>[node.dataset.entry,node]));const retained=new Set();[...fragment.content.children].forEach((next,index)=>{const previous=existing.get(next.dataset.entry);if(previous?.tagName==='DETAILS'&&previous.open&&next.tagName==='DETAILS')next.open=true;let node=next;const comparable=previous?.cloneNode(true);comparable?.querySelectorAll('[data-bound]').forEach(element=>element.removeAttribute('data-bound'));if(comparable?.isEqualNode(next))node=previous;else if(previous)previous.replaceWith(next);if(messages.children[index]!==node)messages.insertBefore(node,messages.children[index]||null);retained.add(node);});[...messages.children].forEach(node=>{if(!retained.has(node))node.remove();});messages.dataset.content=content;if(atBottom)scroller.scrollTop=scroller.scrollHeight;
    document.querySelectorAll('[data-approve],[data-deny]').forEach(button=>{if(button.dataset.bound)return;button.dataset.bound='1';action(button,async()=>{await api(`/api/actions/${button.dataset.approve||button.dataset.deny}/decision`,'POST',{approved:!!button.dataset.approve,scope:button.dataset.scope||'once'});await refreshTask();});});
    document.querySelectorAll('[data-resolve]').forEach(button=>{if(button.dataset.bound)return;button.dataset.bound='1';action(button,async()=>{await api(`/api/actions/${button.dataset.resolve}/resolve`,'POST',{outcome:button.dataset.outcome});await refreshTask();});});
  }
}
async function renderComputer() {
  const current=pageScope('computer');if(!current())return;
  $('#page').className='page';
  let browser;
  try{
    browser=await api('/api/browser/activity');if(!current())return;
    // Only an idle browser follows the selected bot. A leased one keeps its profile.
    if(!browser.lease){
      try{await api('/api/browser/select','POST',{bot_id:state.bot});}
      catch(error){if(error.upstreamStatus!==409)throw error;}
      if(!current())return;browser=await api('/api/browser/activity');if(!current())return;
    }
  }catch(error){
    if(!current())return;
    $('#page').innerHTML=`<div class="page-head"><div><h1>Computer</h1><p class="subhead">Watch your bot browse, or take control to complete a step yourself.</p></div></div><div class="state-panel" role="alert">${icon('monitor')}<h2>Browser unavailable</h2><p>${escape(error.message)}</p><p class="hint">The browser runs as its own service next to ShinBot. When it is running again, try again here. Chats and saved work are not affected.</p><button id="retry-browser">Try again</button></div>`;
    action($('#retry-browser'),()=>renderComputer());
    return;
  }
  $('#page').innerHTML=`<div class="page-head"><div><h1>Computer</h1><p class="subhead"><span id="active-browser-profile" class="profile-caption"></span> ${browser.desktop ? "Watch the desktop, or take control to work alongside your bot." : "Watch your bot browse, or take control to complete a step yourself."}</p></div><div class="row"><button id="minimize-computer" class="quiet">${icon('panel-left')} Picture in picture</button><button id="handback">Hand back</button><button id="takeover" class="primary">Take control</button></div></div><div class="browser-shell"><div class="browser-bar">${browser.desktop ? `<div class="row" id="desktop-apps"><button data-desktop-app="browser">${icon('globe')} Browser</button><button data-desktop-app="files">${icon('folder')} Files</button><button data-desktop-app="terminal">${icon('terminal')} Terminal</button></div>` : ''}<span id="browser-mode" class="pill">View only</span><div id="browser-address" class="browser-address"></div><form id="browser-navigation" class="browser-navigation" hidden><input id="browser-url" type="url" required aria-label="Website address" placeholder="https://example.com" autocomplete="off"><button type="submit" class="quiet">Go</button></form><button id="refresh-view" class="quiet icon-button" aria-label="Reconnect browser" title="Reconnect">${icon('refresh-cw')}</button></div><div id="browser-screen" class="browser-screen"><p class="muted">Connecting to your browser…</p></div></div><p class="browser-hint" id="browser-hint">Watching leaves your bot in control. Take control pauses browser actions until you hand it back.</p><p class="hint" id="browser-profile-id"></p>`;
  let connection=0,mode='view',lastPoll=0;
  const nativeScreen=browser.native_view?nativeBrowserView({screen:$('#browser-screen'),api,state:()=>browser,current,refresh:()=>{lastPoll=0;},toast}):null;
  const hasControl=()=>browser.lease?.kind==='human'&&browser.control_allowed!==false;
  function updateDetails(){
    $('#active-browser-profile').textContent=browserOwner(browser,state)+'.';
    $('#browser-profile-id').textContent='Browser profile: '+browser.browser_id;
    $('#browser-address').textContent=browser.desktop && browser.idle ? 'Linux desktop · /workspace' : browser.url;document.querySelectorAll('[data-desktop-app]').forEach(button=>button.disabled=!hasControl());
    $('#handback').hidden=!hasControl();
    $('#takeover').hidden=hasControl();
    const navigating=browser.native_view&&hasControl();
    $('#browser-navigation').hidden=!navigating;$('#browser-address').hidden=navigating;
    if(document.activeElement!==$('#browser-url'))$('#browser-url').value=browser.url==='about:blank'?'':browser.url;
    $('#minimize-computer').hidden=!browser.lease;
  }
  async function connect(nextMode='view'){
    const id=++connection,valid=()=>current()&&id===connection;
    if(!valid())return;
    mode=nextMode;state.rfb?.disconnect();state.rfb=null;
    const view=await api('/api/browser/view');if(!valid())return;
    const screen=$('#browser-screen');
    if(nativeScreen){nativeScreen.render(view);$('#browser-mode').textContent=hasControl()?'You have control':'View only';$('#browser-hint').textContent=hasControl()?'Click the page to type, paste or scroll. Hand back when you are finished.':'The browser runs in the background. Take control to interact here.';return;}
    screen.innerHTML='';
    if(browser.headless){screen.innerHTML=`<img alt="Current browser page" src="data:image/jpeg;base64,${view.screenshot}">`;return;}
    const {default:RFB}=await import('/static/vendor/novnc/core/rfb.js');if(!valid())return;
    const rfb=new RFB(screen,`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/browser/vnc?mode=${mode}`,{credentials:{password:view.password}});
    state.rfb=rfb;rfb.scaleViewport=true;rfb.resizeSession=false;rfb.viewOnly=mode!=='control';
    rfb.addEventListener('connect',()=>{if(valid()&&state.rfb===rfb)$('#browser-hint').textContent=mode==='control'?(browser.desktop ? 'You control this computer. Bot actions will resume when you hand it back.' : 'You control this browser. ShinBot is waiting for you to hand it back.'):'Connected. Watching leaves your bot in control.';});
    rfb.addEventListener('disconnect',()=>{if(valid()&&state.rfb===rfb){state.rfb=null;$('#browser-hint').textContent='Browser connection closed. Reconnect to continue viewing.';}});
    $('#browser-mode').textContent=mode==='control'?'You have control':'View only';
  }
  document.querySelectorAll('[data-desktop-app]').forEach(button=>action(button,()=>api('/api/desktop/open','POST',{application:button.dataset.desktopApp})));
  action($('#takeover'),async()=>{const button=$('#takeover');button.textContent='Taking control…';try{await api('/api/browser/takeover','POST',{});if(!current())return;browser=await api('/api/browser/activity');if(!current())return;updateDetails();await connect('control');}finally{button.textContent='Take control';}});
  action($('#handback'),async()=>{await api('/api/browser/release','POST',{});if(!current())return;browser=await api('/api/browser/activity');if(!current())return;updateDetails();await connect();});
  $('#browser-navigation').onsubmit=async event=>{event.preventDefault();if(!nativeScreen)return;const button=event.currentTarget.querySelector('button');button.disabled=true;try{await nativeScreen.navigate($('#browser-url').value);}finally{button.disabled=false;lastPoll=0;}};
  action($('#refresh-view'),()=>connect());
  action($('#minimize-computer'),()=>computerUI.returnToChat());
  updateDetails();try{await connect();}catch{if(current())$('#browser-hint').textContent='Preview unavailable. Retrying…';}if(!current())return;
  let polling=false;
  state.interval=setInterval(async()=>{
    if(polling||!current()||Date.now()-lastPoll<(hasControl()?350:1500))return;polling=true;lastPoll=Date.now();
    try{const next=await api('/api/browser/activity');if(!current())return;browser=next;updateDetails();if(mode==='control'&&browser.lease?.kind!=='human')await connect();else if(browser.headless||browser.native_view)await connect();}
    catch{if(current())$('#browser-hint').textContent='Browser status unavailable. Retrying…';}
    finally{polling=false;}
  },250);
}
async function renderNotebook() {
  const current=pageScope('notebook');if(!current())return;
  $('#page').className='page';
  const [notes,memories]=await Promise.all([api('/api/notes'),api('/api/memories?bot_id='+encodeURIComponent(state.bot))]);if(!current())return;
  $('#page').innerHTML=`<div class="page-head"><div><h1>Notebook</h1><p class="subhead">All bots share these notes. Saved memories belong to the selected bot.</p></div><button id="new-note" class="primary"><span class="icon icon-plus" aria-hidden="true"></span> New note</button></div><div class="segmented" role="tablist" aria-label="Notebook sections"><button role="tab" id="notes-tab" data-notebook-tab="notes" aria-controls="notes-panel">${icon("notebook-pen")} Notes <span>${notes.length}</span></button><button role="tab" id="memory-tab" data-notebook-tab="memory" aria-controls="memory-panel">${icon("brain")} Memory <span>${memories.length}</span></button></div><div class="note-layout" id="notes-panel" role="tabpanel" aria-labelledby="notes-tab"><div class="note-index">${notes.map(n=>`<button data-note="${escape(n.id)}">${icon("file-text")}<span>${escape(n.title)}<small>${date(n.updated)}</small></span></button>`).join('')||'<p class="muted">Your notes will appear here.</p>'}</div><div id="note-editor" class="card note-editor"></div></div><div class="card space-top memory-panel" id="memory-panel" role="tabpanel" aria-labelledby="memory-tab"><div class="row between"><h2>What ${escape(botName())} remembers</h2><button id="add-memory" class="small-button">Remember something</button></div><p class="muted"><small>Forgetting removes an active memory. Original conversations and older backups can still contain it.</small></p>${memories.map(m=>`<div class="memory-item row between"><span>${escape(m.text)}</span><button data-forget="${escape(m.id)}" class="quiet danger">Forget</button></div>`).join('')||'<p class="muted">No saved memories yet.</p>'}</div>`;
  bindTabs('[data-notebook-tab]',tab=>{state.notebookTab=tab;$('#notes-panel').hidden=tab!=='notes';$('#memory-panel').hidden=tab!=='memory';$('#new-note').hidden=tab!=='notes';},state.notebookTab,'notebookTab');
  const edit=note=>{
    document.querySelectorAll('[data-note]').forEach(button=>button.classList.toggle('selected',button.dataset.note===note?.id));
    $('#note-editor').innerHTML=`<form id="note-form">${field('Title','title',note?.title||'','','required maxlength="200"')}<label for="note-body">Note</label><textarea id="note-body" name="body">${escape(note?.body||'')}</textarea><div class="form-footer">${note?'<button id="delete-note" class="danger quiet" type="button">Delete</button>':''}<button class="primary" type="submit">Save note</button></div></form>`;
    bind($('#note-form'),async data=>{const saved=await api('/api/notes','POST',{...data,...(note?{id:note.id,revision:note.revision}:{})});state.selectedNote=saved.id;toast('Note saved.');await renderNotebook();});
    action($('#delete-note'),async()=>{await api(`/api/notes/${note.id}`,'DELETE');state.selectedNote=null;await renderNotebook();});
  };
  edit(notes.find(n=>n.id===state.selectedNote)||notes[0]);
  document.querySelectorAll('[data-note]').forEach(button=>button.onclick=()=>{state.selectedNote=button.dataset.note;edit(notes.find(n=>n.id===state.selectedNote));});
  $('#new-note').onclick=()=>{state.selectedNote=null;edit(null);$('#note-form input').focus();};
  document.querySelectorAll('[data-forget]').forEach(button=>action(button,async()=>{await api(`/api/memories/${button.dataset.forget}?bot_id=${encodeURIComponent(state.bot)}`,'DELETE');await renderNotebook();}));
  $('#add-memory').onclick=()=>{const d=dialog('Remember something',`<form><label>A useful fact or preference<textarea name="text" required maxlength="4000"></textarea></label><div class="form-footer"><button class="primary" type="submit">Remember</button></div></form>`);bind(d.querySelector('form'),async data=>{await api('/api/memories','POST',{...data,bot_id:state.bot});d.close();await renderNotebook();});};
}
const boardColumns = [['backlog','Backlog'],['ready','Ready'],['running','In progress'],['review','Review'],['blocked','Blocked'],['done','Done']];
async function renderBoard() {
  const current=pageScope('board');if(!current())return;
  const cards=await api('/api/board');if(!current())return;
  $('#page').innerHTML=`<div class="page-head"><div><h1>Board</h1><p class="subhead">Give a bot a goal. Follow its progress and review the result.</p></div><div class="row"><button id="refresh-board" class="quiet icon-button" aria-label="Refresh board">${icon('refresh-cw')}</button><button id="new-card" class="primary">${icon('plus')} New card</button></div></div><p class="board-guidance">Move a card to Ready to start work. Bots use your workspace permissions and stop at the card’s run limit. Token budgets are optional.</p><div class="work-board" aria-label="Work board">${boardColumns.map(([id,label])=>{const items=cards.filter(c=>c.status===id);return `<section class="board-column" data-column="${id}" aria-label="${label}"><h2>${label}<span class="board-count">${items.length}</span></h2><div class="board-stack">${items.map(card=>{const bot=state.bots.find(b=>b.id===card.bot_id);return `<article class="work-card" draggable="${card.status!=='running'}" data-card="${escape(card.id)}"><button class="work-card-title" data-open-card="${escape(card.id)}">${escape(card.title)}</button><p>${escape(card.waiting_for?'Waiting for '+card.waiting_for.title+' ('+card.waiting_for.status+').':card.summary||card.goal)}</p><div class="work-card-meta">${avatar('roster-avatar',bot)}<span>${escape(bot?.name||'Archived bot')}</span></div><small>${card.runs} / ${card.max_runs} runs · ${Number(card.tokens).toLocaleString()}${card.max_tokens?' / '+Number(card.max_tokens).toLocaleString():''} tokens${card.max_tokens?'':' · No token limit'}</small><div class="row work-card-actions">${card.status==='running'?`<button class="small-button" data-move-card="${escape(card.id)}" data-destination="blocked">Pause</button>`:card.status==='ready'?`<button class="small-button quiet" data-move-card="${escape(card.id)}" data-destination="backlog">Return to backlog</button>`:card.status==='review'?`<button class="small-button" data-open-card="${escape(card.id)}">Review evidence</button>`:card.status!=='done'?`<button class="small-button" data-move-card="${escape(card.id)}" data-destination="ready">${card.runs?'Resume':'Start'}</button>`:''}${card.task_id?`<button class="small-button quiet" data-card-chat="${escape(card.id)}">Open chat</button>`:''}</div></article>`;}).join('')||'<p class="board-empty">No cards</p>'}</div></section>`;}).join('')}</div>`;
  const find=id=>cards.find(c=>c.id===id);
  async function move(card,status){await api(`/api/board/${card.id}/move`,'POST',{status,revision:card.revision});await renderBoard();}
  action($('#new-card'),()=>editWorkCard());action($('#refresh-board'),()=>renderBoard());
  document.querySelectorAll('[data-open-card]').forEach(b=>action(b,()=>editWorkCard(find(b.dataset.openCard))));
  document.querySelectorAll('[data-move-card]').forEach(b=>action(b,()=>move(find(b.dataset.moveCard),b.dataset.destination)));
  document.querySelectorAll('[data-card-chat]').forEach(b=>action(b,()=>{const c=find(b.dataset.cardChat);state.task=c.task_id;state.bot=c.bot_id;return navigate('chat');}));
  document.querySelectorAll('.work-card[draggable="true"]').forEach(card=>{card.addEventListener('dragstart',event=>{event.dataTransfer.setData('text/shinbot-card',card.dataset.card);event.dataTransfer.effectAllowed='move';});});
  document.querySelectorAll('[data-column]').forEach(column=>{column.addEventListener('dragover',event=>{if(column.dataset.column!=='running')event.preventDefault();});column.addEventListener('drop',async event=>{event.preventDefault();const card=find(event.dataTransfer.getData('text/shinbot-card'));if(!card||card.status===column.dataset.column)return;try{await move(card,column.dataset.column);}catch(error){toast(error.message);}});});
  clearInterval(state.interval);state.interval=setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]')&&!$('#page').contains(document.activeElement))renderBoard().catch(error=>toast(error.message));},10000);
}
async function editWorkCard(card=null){
  const running=card&&['ready','running'].includes(card.status);
  const d=dialog(card?'Work card':'New work card',`<form class="work-card-form">${field('Title','title',card?.title||'','text','required maxlength="200"'+(running?' disabled':''))}<label>Goal and acceptance checks<textarea name="goal" rows="5" required maxlength="12000" ${running?'disabled':''} placeholder="What should be done, and how will you know it worked?">${escape(card?.goal||'')}</textarea></label><div class="form-grid"><label>Bot<select name="bot_id" ${running?'disabled':''}>${state.bots.map(bot=>`<option value="${escape(bot.id)}" ${bot.id===(card?.bot_id||state.bot)?'selected':''}>${escape(bot.name)}</option>`).join('')}</select></label>${field('Maximum runs','max_runs',card?.max_runs||8,'number','required min="1" max="100"'+(running?' disabled':''))}${field('Total token budget (optional)','max_tokens',card?.max_tokens||'','number','min="1000" max="10000000" placeholder="No limit"'+(running?' disabled':''))}</div><label class="checkbox-row"><input type="checkbox" name="auto_continue" ${card?.auto_continue===0?'':'checked'} ${running?'disabled':''}>Continue when the bot reports a concrete next step</label><p class="hint">Leave the token budget blank for no limit. Workspace caps still apply. A saved card stays in Backlog until you start it; completed work waits for your review.</p>${running?'<p class="hint">Pause this card before changing its goal or limits.</p>':'<div class="form-footer"><button class="primary" type="submit">Save card</button></div>'}</form>${card?`<section class="card-evidence"><h3>Latest result</h3><p>${escape(card.summary||'No runs yet.')}</p>${card.evidence?`<h4>Evidence</h4><div class="markdown">${markdown(card.evidence)}</div>`:''}<label>Move card<select id="card-state">${boardColumns.filter(([id])=>id!=='running'||card.status==='running').map(([id,label])=>`<option value="${id}" ${id===card.status?'selected':''}>${label}</option>`).join('')}</select></label><button id="move-work-card">Move card</button><details id="card-history"><summary>Run history</summary><div id="card-runs"></div></details></section>`:''}`);
  if(!running)bind(d.querySelector('form'),async data=>{await api('/api/board','POST',{...data,max_runs:Number(data.max_runs),max_tokens:Number(data.max_tokens),auto_continue:d.querySelector('[name=auto_continue]').checked,...(card?{id:card.id,revision:card.revision}:{})});d.close();await renderBoard();});
  else d.querySelector('form').onsubmit=event=>event.preventDefault();
  if(card){action(d.querySelector('#move-work-card'),async()=>{await api(`/api/board/${card.id}/move`,'POST',{status:d.querySelector('#card-state').value,revision:card.revision});d.close();await renderBoard();});d.querySelector('#card-history').addEventListener('toggle',async event=>{if(!event.target.open)return;try{const runs=await api(`/api/board/${card.id}/runs`);d.querySelector('#card-runs').innerHTML=runs.map(run=>`<div class="list-item"><strong>${escape(run.outcome||'In progress')}</strong><p>${escape(run.summary||'The bot is working on this run.')}</p><small>${escape(new Date(run.created*1000).toLocaleString())}</small></div>`).join('')||'<p>No runs yet.</p>';}catch(error){toast(error.message);}});}
}

const jobsUI=jobsView({...integrationDeps,bind,openChat:async id=>openConversation(await api('/api/tasks/'+id))});
async function renderJobs(){return jobsUI.render();}

function latestCatalogEntries(entries){
  const versions=[...entries].sort((a,b)=>{
    const [aCore,aPre]=a.version.split('-',2),[bCore,bPre]=b.version.split('-',2);
    return bCore.localeCompare(aCore,undefined,{numeric:true}) || Number(!bPre)-Number(!aPre) || (bPre||'').localeCompare(aPre||'',undefined,{numeric:true});
  });
  return entries.filter(entry=>versions.find(candidate=>candidate.id===entry.id)===entry);
}
async function renderPlugins(){return pluginUI.render();}
function addMarketplace(){const d=dialog('Add a marketplace',`<form>${field('GitHub repository','source','','text','required placeholder="owner/plugin-repository"')}${field('Branch, tag or commit','ref','HEAD')}<p class="muted"><small>The repository must contain a shinbot-marketplace.json catalog. Adding a marketplace does not execute its plugins.</small></p><div class="form-footer"><button class="primary" type="submit">Add marketplace</button></div></form>`);bind(d.querySelector('form'),async values=>{await api('/api/marketplaces','POST',values);d.close();await renderPlugins();});}
function createPlugin(){
  const d=dialog('Create a plugin',`<form><p>Describe what you want your bot to do. It can build a plugin, test it, and bring the files and permissions back for your review.</p><label>What should the plugin do?<textarea name="goal" rows="5" required maxlength="6000" placeholder="Connect to my team's service and summarize new requests."></textarea></label><p class="hint">Keep passwords and API keys out of this description. The plugin can ask for connection settings during setup.</p><div class="form-footer"><button class="primary" type="submit">Continue in chat</button></div></form>`);
  bind(d.querySelector('form'),async values=>{rememberDraft();state.task=null;state.drafts['new:'+state.bot]='Create a ShinBot plugin for this task: '+values.goal+'\n\nInspect ShinBot’s plugin creation guide first. Reuse an existing plugin if it fits. Build and test the package, explain its permissions, and ask me before activation. Keep credentials in declared connection settings.';d.close();await navigate('chat');$('#prompt')?.focus();});
}
function uploadPlugin(){const d=dialog('Install a local plugin',`<form><label>Plugin files<input name="files" type="file" multiple required></label><p class="muted"><small>Select shinbot-plugin.json, LICENSE and the files listed in the manifest. Use the CLI for packages with subdirectories. ShinBot checks the package before you choose its access and enable it.</small></p><div class="form-footer"><button class="primary" type="submit">Check plugin</button></div></form>`);bind(d.querySelector('form'),async()=>{const files={};for(const file of d.querySelector('input').files)files[file.name]=await file.text();const plugin=await api('/api/plugins/stage','POST',{files});d.close();await renderPlugins();await pluginUI.finish(plugin.id,plugin.version);});}
function extensionAccessFields(access={mode:'bots',bots:[state.bot]}) {
  return `<fieldset class="extension-access"><legend>Available to</legend><label class="checkbox-row"><input type="radio" name="access_mode" value="shared" ${access.mode==='shared'?'checked':''}>All bots</label><label class="checkbox-row"><input type="radio" name="access_mode" value="bots" ${access.mode==='bots'?'checked':''}>Selected bots</label><div class="extension-bots">${state.bots.map(bot=>`<label class="checkbox-row"><input type="checkbox" name="access_bot" value="${escape(bot.id)}" ${access.bots.includes(bot.id)?'checked':''}>${escape(bot.name)}</label>`).join('')}</div></fieldset>`;
}
function extensionAccessValue(form) {
  const mode=form.querySelector('[name=access_mode]:checked').value;
  const bots=mode==='shared'?[]:[...form.querySelectorAll('[name=access_bot]:checked')].map(x=>x.value);
  if(mode==='bots'&&!bots.length)throw new Error('Select at least one bot, or choose All bots.');
  return {mode,bots};
}
function bindExtensionAccess(form) {
  const update=()=>{const shared=form.querySelector('[name=access_mode]:checked').value==='shared';form.querySelector('.extension-bots').hidden=shared;};
  form.querySelectorAll('[name=access_mode]').forEach(input=>input.onchange=update);update();
}
function configurePluginAccess(plugin) {
  const d=dialog('Bot access for '+plugin.name,`<form><p class="muted">New chats include enabled extensions. Removing access also blocks calls from existing chats. Bots share one Computer workspace; this setting controls which tools and instructions ShinBot offers each bot.</p>${extensionAccessFields(plugin.access)}<div class="form-footer"><button type="submit" class="primary">Save access</button></div></form>`);
  const form=d.querySelector('form');bindExtensionAccess(form);
  bind(form,async()=>{await api(`/api/plugins/${plugin.id}/access`,'PUT',extensionAccessValue(form));d.close();await renderPlugins();});
}
function connectMCP() {
  const d=dialog('Connect an MCP server',`<form><p class="muted">Connect a remote endpoint or run a server in the Computer workspace. ShinBot reads its tool list before you activate it. The scan covers connection settings and tool descriptions. It cannot inspect a remote server or code downloaded by a command. Review the server and its dependencies before running it.</p><div class="form-grid">${field('Name','name','','text','required')}${field('Connection ID','id','','text','required pattern="[a-z][a-z0-9-]{0,23}" placeholder="my-server"')}${field('Version','version','1.0.0','text','required')}<label>Transport<select name="transport" aria-label="Transport"><option value="stdio">Local command (stdio)</option><option value="http">Streamable HTTP</option><option value="sse">SSE</option></select></label></div><div data-mcp-stdio>${field('Command','command','npx','text','required')}<label for="mcp-arguments">Arguments (JSON array)</label><textarea id="mcp-arguments" name="args" rows="2" placeholder='["-y", "package-name@1.0.0"]'>[]</textarea>${field('Environment variables (JSON object)','env','','password','autocomplete="off" placeholder="Optional"')}</div><div data-mcp-remote hidden>${field('Server URL','url','','url','placeholder="https://example.com/mcp"')}${field('Headers (JSON object)','headers','','password','autocomplete="off" placeholder="Optional"')}</div><p class="hint">Put tokens in environment variables or headers, not in command arguments or the URL. Credentials are saved privately on this server.</p>${extensionAccessFields()}<div class="form-footer"><button type="submit" class="primary">Connect and inspect tools</button></div></form>`);
  const form=d.querySelector('form');bindExtensionAccess(form);
  form.elements.transport.onchange=()=>{const local=form.elements.transport.value==='stdio';form.querySelector('[data-mcp-stdio]').hidden=!local;form.querySelector('[data-mcp-remote]').hidden=local;form.elements.command.required=local;form.elements.url.required=!local;d.querySelector('.dialog-feedback')?.remove();};
  bind(form,async values=>{let args=[],env={},headers={};try{if(values.transport==='stdio'){args=JSON.parse(values.args||'[]');env=JSON.parse(values.env||'{}');}else{headers=JSON.parse(values.headers||'{}');}}catch{throw new Error('Use valid JSON for arguments, environment variables and headers.');}const server=values.transport==='stdio'?{transport:'stdio',command:values.command,args,env}:{transport:values.transport,url:values.url,headers};await api('/api/plugins/mcp/connect','POST',{id:values.id,name:values.name,version:values.version,server,access:extensionAccessValue(form)});d.close();toast('Connected. Review the discovered tools and activate this version.');await renderPlugins();});
}
function scanSummary(report){
  if(!report)return '<p class="notice">Security scan required before enabling it. Open Review files to run it.</p>';
  const titles={passed:'Scan completed',blocked:'Activation blocked',error:'Scan unavailable'};
  return `<div class="security-report ${report.status==='passed'?'':'notice'}"><strong>${titles[report.status]||'Security scan'}</strong><p class="hint">${escape(report.scanner)} ${escape(report.version)} · ${date(report.scanned_at)}</p><p>${escape(report.message)}</p>${report.findings?.length?`<details><summary>${report.findings.length} scan finding${report.findings.length===1?'':'s'}</summary>${report.findings.map(f=>`<div class="list-item"><strong>${escape(f.severity)} · ${escape(f.title)}</strong><p>${escape(f.description)}</p><small>${escape(f.file_path||'')}${f.line_number?':'+escape(f.line_number):''}</small>${f.remediation?`<p class="hint">${escape(f.remediation)}</p>`:''}</div>`).join('')}</details>`:''}</div>`;
}
async function reviewPlugin(id,version) {
  const result=await api(`/api/plugins/${id}/${version}/files`);
  const d=dialog('Review '+result.name,`${scanSummary(result.security)}<button type="button" id="rescan-plugin">Run scan again</button><p class="hint">${escape(version)} · ${escape(result.source||'Local package')}. Read the instructions and code before activating.</p><label>File<select id="plugin-file">${Object.keys(result.files).map(name=>`<option>${escape(name)}</option>`).join('')}</select></label><pre class="plugin-source"><code id="plugin-source"></code></pre>`);
  action(d.querySelector('#rescan-plugin'),async()=>{await api(`/api/plugins/${id}/${version}/scan`,'POST',{});d.close();await renderPlugins();await reviewPlugin(id,version);});
  const select=d.querySelector('#plugin-file'),code=d.querySelector('#plugin-source');
  select.onchange=()=>{code.textContent=result.files[select.value];};if(result.files['SKILL.md'])select.value='SKILL.md';select.onchange();
}
function importSource() {
  const d=dialog('Import a skill or extension',`<form><p class="muted">Paste a public GitHub link. ShinBot checks the source at a fixed revision and preserves its license. Skills and command instructions can be imported directly. Other extensions need an adapter.</p>${field('Source URL','url','','url','required placeholder="https://github.com/owner/repo"')}<div class="form-footer"><button class="primary" type="submit">Inspect source</button></div></form><div id="source-preview"></div>`);
  bind(d.querySelector('form'),async values=>{
    const result=await api('/api/plugins/source/inspect','POST',values);if(!d.open)return;
    d.querySelector('#source-preview').innerHTML=`<p class="hint">Revision ${escape(result.revision.slice(0,12))}. No source code has run.</p>${result.candidates.map((item,index)=>`<div class="list-item"><h3>${escape(item.path)}</h3><p class="hint">${escape(item.format)} · ${escape(item.license||item.error)}</p>${item.license?`<button data-import-candidate="${index}">Stage for review</button>`:''}</div>`).join('')||'<p>This extension needs an adapter. Share the link in chat and ask ShinBot to inspect, build and test one.</p>'}${extensionAccessFields()}${result.more_candidates?'<p class="hint">More results exist. Use a link to the specific directory.</p>':''}`;
    const preview=d.querySelector('#source-preview');bindExtensionAccess(preview);
    preview.querySelectorAll('[data-import-candidate]').forEach(button=>action(button,async()=>{const item=result.candidates[Number(button.dataset.importCandidate)];await api('/api/plugins/source/import','POST',{url:result.url,path:item.path,access:extensionAccessValue(preview)});d.close();await renderPlugins();toast('Imported. Open its setup to review and enable it.');}));
  });
}
function installSkill() {
  const licenses=['MIT','Apache-2.0','BSD-2-Clause','BSD-3-Clause','ISC','0BSD','Unlicense','CC0-1.0'];
  const d=dialog('Install skills',`<form><p class="muted">Paste a Skills CLI command. ShinBot installs a copy in its Computer workspace, keeps the skill files, and opens them for review.</p>${field('Skills command','command','','text','required placeholder="npx skills add owner/repo --skill skill-name"')}<label>Upstream license<select name="license" aria-label="Upstream license" required><option value="">Choose the license stated by the skill author</option>${licenses.map(l=>`<option>${l}</option>`).join('')}</select></label><p class="hint">Use a public repository or a path inside the Computer workspace. Only install skills whose license permits your intended use. A skill can contain scripts, so review it before enabling it.</p>${extensionAccessFields()}<div class="form-footer"><button type="submit" class="primary">Install and review</button></div></form>`);
  const form=d.querySelector('form');bindExtensionAccess(form);
  bind(form,async values=>{await api('/api/plugins/skills/install','POST',{command:values.command,license:values.license,access:extensionAccessValue(form)});d.close();toast('Skills installed. Open each skill’s setup to review and enable it.');await renderPlugins();});
}

async function configurePlugin(id,version,onSaved=()=>renderPlugins()){
  const path=`/api/plugins/${encodeURIComponent(id)}/${encodeURIComponent(version)}/configuration`;
  const {fields,name,origin}=await api(path);
  if(id==='github'&&origin==='adarshdigievo/shinbot-official-plugins')return connectGitHub({target:'plugin',version,repositories:(fields.find(f=>f.key==='SHINBOT_PLUGIN_GITHUB_REPOSITORIES')?.value||'').split(',').map(x=>x.trim()).filter(Boolean),onDone:onSaved,disconnect:async()=>{await api(`/api/plugins/${encodeURIComponent(id)}/disable`,'POST',{});await api(path,'PUT',{SHINBOT_PLUGIN_GITHUB_TOKEN:null,SHINBOT_PLUGIN_GITHUB_REPOSITORIES:null});toast('GitHub plugin disconnected and disabled.');}});
  const d=dialog('Configure '+name,`<p class="hint">Source: ${escape(origin)}</p><p class="muted">Credentials are encrypted on your server and never added to chat. A configured executable plugin receives its own credentials when it runs; use plugins you trust.</p><form>${id==='github'?'<p><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">Create a GitHub token</a> with access to the repositories you want to connect.</p>':''}${fields.map(f=>`${field(f.label,f.key,f.value,f.secret?'password':'text','autocomplete="off" maxlength="10000" '+(f.required&&!f.configured?'required':''))}<p class="hint">${escape(f.description)}${f.secret&&f.configured?' A value is saved. Leave blank to keep it.':''}</p>${f.secret&&f.configured?`<label class="checkbox-row"><input type="checkbox" data-clear-setting="${f.key}">Remove saved ${escape(f.label)}</label>`:''}`).join('')}<div class="form-footer"><button type="submit" class="primary">Save settings</button></div></form>`);
  bind(d.querySelector('form'),async values=>{for(const f of fields){if(f.secret&&!values[f.key])delete values[f.key];if(d.querySelector(`[data-clear-setting="${f.key}"]`)?.checked)values[f.key]=null;}await api(path,'PUT',values);d.querySelectorAll('input[type=password]').forEach(input=>input.value='');d.close();toast('Connection saved.');await onSaved();});
}
async function renderSecrets(){
  const panel=$('#secrets-content'),current=pageScope('settings');if(!panel||!current())return;
  panel.innerHTML='<p class="muted" role="status">Loading saved credentials…</p>';
  try{
    const [data,githubAccount]=await Promise.all([api('/api/secrets'),api('/api/github/account')]);
    if(!current()||!panel.isConnected)return;
    panel.innerHTML=`${githubAccount.connected?`<div class="list-item"><h3>GitHub sign-in</h3><p>@${escape(githubAccount.login)}</p><p class="hint">Used when setting up new GitHub connections. Existing inbox and plugin connections keep their own credentials; manage them below.</p><button id="forget-github" class="quiet danger">Forget saved sign-in</button></div>`:''}<div class="list-item"><h3>GitHub inbox</h3><p>${data.github?'Token saved':'Not connected'}</p><button id="secrets-projects">Manage in Projects</button></div><div class="list-item"><h3>Google Calendar</h3><p>${data.calendar?'Connected':'Not connected'}</p><button id="secrets-calendar">Manage in Calendar</button></div>${data.plugins.map(p=>`<div class="list-item"><h3>${escape(p.name)} ${escape(p.version)}</h3>${p.fields.map(f=>`<p>${escape(f.label)}: ${f.configured?'Saved':'Not set'}</p>`).join('')}<button data-secret-plugin="${escape(p.id)}" data-version="${escape(p.version)}">Set or remove credentials</button></div>`).join('')}<p class="hint">Executable plugins receive their configured secrets when they run. The vault protects files at rest; a server administrator or someone with the vault and its key can decrypt them. Keep backups private.</p>`;
    for(const [selector,page] of [['#secrets-projects','projects'],['#secrets-calendar','calendar']])action(panel.querySelector(selector),async()=>{if(!visiblePages().some(([id])=>id===page)){toast('Install and activate '+(page==='projects'?'Projects':'Calendar')+' in Plugins first.');return navigate('plugins');}await navigate(page);});
    action(panel.querySelector('#forget-github'),async()=>{await api('/api/github/account','DELETE');await renderSecrets();});
    panel.querySelectorAll('[data-secret-plugin]').forEach(b=>action(b,()=>configurePlugin(b.dataset.secretPlugin,b.dataset.version,renderSecrets)));
  }catch(error){
    if(!current()||!panel.isConnected)return;
    panel.innerHTML=`<p class="error-text" role="alert">${escape(error.message)}</p><button data-retry-secrets>Retry</button>`;
    action(panel.querySelector('[data-retry-secrets]'),renderSecrets);
  }
}
function subscriptionSignIn(providers){
  const available=providers.filter(provider=>provider.oauth);
  if(!available.length)return '';
  const legacy=provider=>/\blegacy\b/i.test(provider.name);
  const current=available.filter(provider=>!legacy(provider)).sort((a,b)=>Number(b.id==='openai')-Number(a.id==='openai'));
  const older=available.filter(legacy);
  const option=provider=>{
    const name=provider.id==='openai'?'ChatGPT':provider.name;
    return `<button type="button" class="subscription-provider" data-login="${escape(provider.id)}" aria-label="Sign in with ${escape(name)}"><span class="subscription-provider-name">${escape(name)}</span><span class="subscription-provider-action" aria-hidden="true">Sign in ${icon('arrow-up-right')}</span></button>`;
  };
  return `<section class="subscription-signin" aria-labelledby="subscription-heading"><h3 id="subscription-heading">Sign in with an account</h3><p class="subscription-intro">Choose a provider to connect your account.</p><div class="subscription-providers">${current.map(option).join('')}</div>${older.length?`<details class="subscription-legacy"><summary>Older sign-in options</summary><div class="subscription-providers">${older.map(option).join('')}</div></details>`:''}<p class="hint subscription-note">${escape(available.find(provider=>provider.id==='openai')?.oauthNote||'Account eligibility and usage limits depend on the provider.')}</p></section>`;
}
async function renderSettings() {
  const current=pageScope('settings');if(!current())return;
  $('#page').className='page settings-page';
  let providerError='';const [settings,status,providers]=await Promise.all([api('/api/settings'),api('/api/status'),api('/api/providers').catch(error=>{providerError=error.message;return []})]);if(!current())return;state.settings=settings;state.providers=providers;const approvalRules=await api('/api/approvals');if(!current())return;const modelProviders=providers.length?providers:[{id:settings.provider,name:settings.provider,models:[{id:settings.model,name:settings.model}]}];
  $('#page').innerHTML=`<div class="page-head"><div><h1>Settings</h1><p class="subhead">Choose a model, set usage limits, and decide which actions need your approval.</p></div><div class="row"><button id="logout">Sign out</button></div></div>${providerError?'<div class="notice" role="status"><p>Model connections are unavailable. You can still update workspace settings and sign out.</p><p>'+escape(providerError)+'</p><button id="retry-providers">Retry model connection</button></div>':''}${status.restore_hold?'<div class="notice">Restored work is held and schedules are paused. Stop any older installation before continuing. <button id="release-recovery">Release recovery hold</button></div>':''}${!status.runtime_ready?'<div class="notice">The agent runtime is reconnecting or waiting for recovery. '+escape(status.runtime_error||'')+'</div>':''}<div class="settings-layout"><div class="settings-tabs" role="tablist" aria-label="Settings sections"><button id="workspace-tab" role="tab" data-settings-tab="workspace" aria-controls="workspace-panel">${icon("settings")} Workspace</button><button id="appearance-tab" role="tab" data-settings-tab="appearance" aria-controls="appearance-panel">${icon("sun")} Appearance</button><button id="notifications-tab" role="tab" data-settings-tab="notifications" aria-controls="notifications-panel">${icon("bell")} Notifications</button><button id="connections-tab" role="tab" data-settings-tab="connections" aria-controls="connections-panel">${icon("plug")} Connections</button><button id="browser-tab" role="tab" data-settings-tab="browser" aria-controls="browser-panel">${icon("monitor")} Browser</button><button id="permissions-tab" role="tab" data-settings-tab="permissions" aria-controls="permissions-panel">${icon("shield-check")} Permissions</button><button id="updates-tab" role="tab" data-settings-tab="updates" aria-controls="updates-panel">${icon("refresh-cw")} Updates</button><button id="accounts-tab" role="tab" data-settings-tab="accounts" aria-controls="accounts-panel">${icon("user-round")} Accounts</button><button id="secrets-tab" role="tab" data-settings-tab="secrets" aria-controls="secrets-panel">${icon("key-round")} Secrets</button></div><div class="settings-content"><section class="settings-section" id="secrets-panel" data-settings-panel="secrets" role="tabpanel" aria-labelledby="secrets-tab"><h2>Secrets</h2><p class="muted">Manage credentials saved for integrations and plugins. Values stay encrypted on your server and are never added to chat.</p><div id="secrets-content"></div></section>${appearancePanel()}<section class="settings-section" id="notifications-panel" data-settings-panel="notifications" role="tabpanel" aria-labelledby="notifications-tab"><h2>Notifications</h2>${notificationUI.browserControls()}</section>${updatesPanel(status.updates,escape,date)}<form id="settings-form"><section class="settings-section" id="workspace-panel" data-settings-panel="workspace" role="tabpanel" aria-labelledby="workspace-tab"><h2>Workspace</h2><p class="muted">Choose the default model and thinking effort for new chats. Each chat can use different settings, and bots can have their own model. Token caps are off by default; leave them blank for no limit.</p><div class="form-grid">${field('Workspace name','name',settings.name,'text','required')}${timezoneField(settings.timezone)}<label>Default provider<select name="provider" id="provider"><option value="">Choose a provider</option>${modelProviders.filter(p=>p.id).map(p=>`<option value="${escape(p.id)}">${escape(p.name)}${p.configured?' · connected':''}</option>`).join('')}</select></label><label>Default model<select name="model" id="model"></select></label><div><label>Default thinking effort<select name="thinking_level" id="default-thinking-level" aria-describedby="default-thinking-help"></select></label><p class="hint" id="default-thinking-help"></p></div>${field('Token cap per message (optional)','task_tokens',settings.task_tokens||'','number','min="1000" placeholder="No limit"')}${field('Token cap per 24 hours (optional)','daily_tokens',settings.daily_tokens||'','number','min="1000" placeholder="No limit"')}${field('Concurrent bot runs','max_active_runs',settings.max_active_runs||4,'number','min="1" max="16" required')}</div></section>${browserSettings(settings,escape)}<section class="settings-section" id="permissions-panel" data-settings-panel="permissions" role="tabpanel" aria-labelledby="permissions-tab"><h2>Automatic approvals</h2><p class="muted">Give ShinBot the access you are comfortable with. These permissions apply to new tasks.</p><label class="checkbox-row"><input name="computer_auto" type="checkbox" ${settings.computer_auto?'checked':''}>Allow read-only commands and trusted plugin tools in Auto</label><label>Default approval mode<select name="approval_mode">${[["auto","Auto"],["ask","Ask"],["full","Full access"]].map(([id,label])=>`<option value="${id}" ${id===(settings.approval_mode||"auto")?"selected":""}>${label}</option>`).join('')}</select></label><p class="hint">Auto allows routine browsing and checks other actions. Ask requests permission before commands and browser actions. Full access runs actions without approval, including changes and deletions. You can choose a different mode in each chat.</p><label class="checkbox-row"><input name="learn_preferences" type="checkbox" ${settings.learn_preferences?'checked':''}>Remember preferences and corrections I share without asking each time</label><p class="muted"><small>Saved facts stay with the current bot. Review or forget them in Notebook → Memory.</small></p><label class="checkbox-row"><input name="learn_from_tasks" type="checkbox" ${settings.learn_from_tasks!==false?'checked':''}>Let bots propose reusable skills from work they have tested</label><p class="muted"><small>Commands have workspace and network access. Browser access can act through accounts you have signed into.</small></p><h3>Saved approvals</h3><p class="hint">Review permissions saved from chat. Each rule names the command or tool it allows. Delegated tasks need their own approvals. Remove a rule to ask again.</p>${approvalRules.map(rule=>`<div class="list-item row between"><div class="saved-approval"><span>${escape(rule.label)}</span><small>${rule.scope==='always'?'All chats':escape(rule.chat_title||'This chat')} · ${date(rule.created)}</small>${rule.args?`<details><summary>Original request</summary><pre>${escape(JSON.stringify(JSON.parse(rule.args),null,2))}</pre></details>`:''}</div><button type="button" class="quiet danger" data-revoke-approval="${escape(rule.id)}">Remove</button></div>`).join('')||'<p class="muted">No saved approvals. You can add one when a bot asks in chat.</p>'}</section><div class="form-footer" id="settings-save"><button class="primary" type="submit">Save settings</button></div></form><section id="connections-panel" data-settings-panel="connections" role="tabpanel" aria-labelledby="connections-tab"><div class="settings-section"><h2>Connect your model</h2><p class="muted">Choose a provider, sign in, and select the model your bots will use.</p><button id="choose-model" class="primary">Choose a provider and model</button><details class="api-key-alternative"><summary>Connect an API key directly</summary><form id="key-form"><label>Provider<select name="provider">${providers.map(p=>`<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('')}</select></label>${field('API key','key','','password','required autocomplete="off"')}<div class="form-footer"><button class="primary" type="submit">Save key</button></div></form></details>${subscriptionSignIn(providers)}</div><div class="settings-section"><h2>Local or compatible model</h2><form id="custom-form">${field('Provider ID','id','custom-local','text','pattern="custom-[a-z0-9-]+" required')}${field('Endpoint','baseUrl','http://host.docker.internal:11434/v1','url','required')}${field('Model name','model','','text','required')}${field('API key, if needed','key','','password','autocomplete="off"')}<div class="form-footer"><button type="submit">Add model</button></div></form></div></section><section class="settings-section" id="accounts-panel" data-settings-panel="accounts" role="tabpanel" aria-labelledby="accounts-tab"><h2>Connected accounts</h2>${providers.filter(p=>p.configured).map(p=>`<div class="row between list-item"><span>${escape(p.name)}</span><div class="row"><button class="small-button" data-use-provider="${escape(p.id)}">Use as default</button><button class="small-button danger" data-disconnect="${escape(p.id)}">Disconnect</button></div></div>`).join('')||(providerError?'<p class="muted">Saved connections will appear when the model service reconnects.</p>':'<p class="muted">No model credentials saved yet.</p>')}<div class="list-item"><button id="revoke" class="danger quiet">Sign out all devices</button></div></section></div></div>`;
  browserAvailability(api,escape,current);
  bindTabs("[data-settings-tab]",tab=>{state.settingsTab=tab;document.querySelectorAll("[data-settings-panel]").forEach(panel=>panel.hidden=panel.dataset.settingsPanel!==tab);$("#settings-save").hidden=!["workspace","permissions","browser"].includes(tab);if(tab==="secrets")renderSecrets();},state.settingsTab,"settingsTab");
  action($('#check-updates'),async()=>{await api('/api/updates/check','POST',{});await renderSettings();});
  $('#automatic-updates').onchange=async event=>{try{await api('/api/updates/settings','PUT',{automatic:event.target.checked});toast(event.target.checked?'Daily update checks enabled.':'Daily update checks disabled.');}catch(error){event.target.checked=!event.target.checked;toast(error.message,true);}};
  action($('#copy-update-command'),async()=>{try{await navigator.clipboard.writeText(status.updates.update_command);toast('Update command copied.');}catch{toast('Select and copy the command shown here.',true);}});
  $('#settings-form').addEventListener('invalid',event=>{const panel=event.target.closest('[data-settings-panel]');if(panel?.hidden)document.querySelector('[data-settings-tab="'+panel.dataset.settingsPanel+'"]').click();},true);
  $('#provider').value=settings.provider;
  let defaultEffort=settings.thinking_level||'off';
  function effortOptions(){
    const select=$('#default-thinking-level'),selection={provider:$('#provider').value,model:$('#model').value};
    const levels=providerError?[defaultEffort]:thinkingLevels(modelProviders,selection);
    if(!levels.includes(defaultEffort))defaultEffort=levels[0];
    select.innerHTML=levels.map(level=>`<option value="${escape(level)}">${escape(effortName(level))}</option>`).join('');
    select.value=defaultEffort;select.disabled=Boolean(providerError)||levels.length<2||!selection.model;
    $('#default-thinking-help').textContent=providerError?'Reconnect to change the default model and effort.':!selection.model?'Choose a model to see its thinking levels.':levels.length<2?'This model has no adjustable thinking effort.':'Higher effort gives the model more time to think.';
  }
  function modelOptions(){const provider=modelProviders.find(p=>p.id===$('#provider').value);$('#model').innerHTML='<option value="">Choose a model</option>'+(provider?.models||[]).map(m=>`<option value="${escape(m.id)}">${escape(m.name)}</option>`).join('');if(provider?.models.some(m=>m.id===settings.model))$('#model').value=settings.model;effortOptions();}
  $('#default-thinking-level').onchange=event=>{defaultEffort=event.target.value;};
  $('#model').onchange=effortOptions;
  modelOptions();$('#provider').onchange=modelOptions;if(providerError){$('#provider').disabled=true;$('#model').disabled=true;document.querySelectorAll('#key-form input,#key-form select,#key-form button,#custom-form input,#custom-form button').forEach(element=>element.disabled=true);}action($('#retry-providers'),()=>renderSettings());
  bind($('#settings-form'),async data=>{if(providerError){data.provider=settings.provider;data.model=settings.model;}for(const name of ['computer_auto','learn_from_tasks','learn_preferences','browser_close_on_finish'])data[name]=$(`[name=${name}]`).checked;data.browser_auto=settings.browser_auto;data.extensions_auto=settings.extensions_auto;data.thinking_level=defaultEffort;data.max_active_runs=Number(data.max_active_runs);data.task_tokens=Number(data.task_tokens);data.daily_tokens=Number(data.daily_tokens);state.settings=await api('/api/settings','PUT',data);workspaceTitle();toast('Settings saved.');await renderSettings();});
  bind($('#key-form'),async data=>{await api('/api/providers/key','POST',data);await renderSettings();await connectModel(data.provider);});
  bind($('#custom-form'),async data=>{await api('/api/providers/custom','POST',data);await renderSettings();await connectModel(data.id);});
  document.querySelectorAll('[data-revoke-approval]').forEach(button=>action(button,async()=>{await api('/api/approvals/'+button.dataset.revokeApproval,'DELETE');await renderSettings();}));
  action($('#choose-model'),()=>connectModel());
  notificationUI.refreshControls();
  action($('#logout'),async()=>{await api('/api/logout','POST',{});await boot();});
  action($('#revoke'),async()=>{await api('/api/sessions/revoke','POST',{});await boot();});
  document.querySelectorAll('[data-use-provider]').forEach(button=>action(button,()=>connectModel(button.dataset.useProvider)));
  document.querySelectorAll('[data-disconnect]').forEach(button=>action(button,async()=>{await api(`/api/providers/${button.dataset.disconnect}`,'DELETE');await renderSettings();}));
  if($('#release-recovery'))action($('#release-recovery'),async()=>{if(confirm('Is the old installation stopped? This enables new tasks; old tasks stay stopped and schedules stay paused.')){await api('/api/recovery/release','POST',{old_installation_stopped:true});await renderSettings();}});
  document.querySelectorAll('[data-login]').forEach(button=>action(button,()=>oauth(button.dataset.login)));
}
async function oauth(provider, onComplete=()=>connectModel(provider)) {
  const previous=await api('/api/login');
  if(previous.status==='pending'&&previous.provider!==provider){
    const choice=dialog('Another sign-in is open',`<p>A sign-in for ${escape(previous.provider||'another provider')} is still waiting.</p><div class="form-footer"><button data-keep>Keep previous sign-in</button><button data-replace class="primary">Cancel it and continue</button></div>`);
    action(choice.querySelector('[data-keep]'),()=>choice.close());action(choice.querySelector('[data-replace]'),async()=>{await api('/api/login/respond','POST',{id:previous.id,cancel:true});choice.close();await oauth(provider,onComplete);});return;
  }
  const started=previous.status==='pending'?previous:await api('/api/login','POST',{provider});
  const d=dialog(provider==='openai'?'Connect ChatGPT':'Connect your account','<div data-oauth-body><p role="status">Preparing sign-in…</p></div>');
  const body=d.querySelector('[data-oauth-body]');
  let timer, last='', loginStatus='pending';
  d.addEventListener('close',()=>{clearTimeout(timer);if(loginStatus==='pending')api('/api/login/respond','POST',{id:started.id,cancel:true}).catch(()=>{});});
  const link=(url,label)=>typeof url==='string'&&/^https:\/\//.test(url)?`<a class="sign-in-link" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(label||'Open sign-in page')} ${icon('arrow-up-right')}</a>`:'';
  async function poll(){
    clearTimeout(timer);
    try {
      const data=await api('/api/login');if(!d.open)return;
      if(data.id&&data.id!==started.id){loginStatus='expired';body.innerHTML='<p role="alert">This sign-in was replaced. Close this dialog and start again.</p>';return;}
      loginStatus=data.status;
      if(data.status==='complete'){d.close();toast('Account connected.');await onComplete();return;}
      const encoded=JSON.stringify(data);
      if(last!==encoded){
        last=encoded;
        const pending=data.status==='pending';
        const statusText=pending?(data.command?'Finish sign-in from the computer where your browser is running.':'Open the sign-in page below. This dialog will update automatically when you finish.'):{failed:'Sign-in could not finish.',expired:'This sign-in expired.',cancelled:'Sign-in was cancelled.',idle:'No sign-in is active.'}[data.status]||'Sign-in could not finish.';
        const events=(data.events||[]).map(event=>{const text=[event.message,event.userCode].filter(Boolean).map(escape).join('<br>');const links=[event.url?link(event.url,provider==='openai'?'Continue with ChatGPT':'Open sign-in page'):'',event.verificationUri?link(event.verificationUri,'Enter the code'):'',...(event.links||[]).map(item=>link(item.url,item.label))].filter(Boolean).join('<br>');return `<div class="oauth-event">${text?'<p>'+text+'</p>':''}${links}</div>`}).join('');
        const prompt=data.prompt?(data.prompt.type==='select'?`<label>${escape(data.prompt.message)}<select name="value">${(data.prompt.options||[]).map(option=>`<option value="${escape(option.id)}">${escape(option.label)}</option>`).join('')}</select></label>`:field(data.prompt.message,'value','',data.prompt.type==='text'||data.prompt.type==='manual_code'?'text':'password','required autocomplete="off"')):'';
        const answer=prompt?`<form data-oauth-answer>${prompt}<div class="form-footer"><button type="submit">Continue</button></div></form>`:'';
        body.innerHTML=`<p role="status">${escape(statusText)}</p>${data.error?`<p class="error-text" role="alert">${escape(data.error)}</p>`:''}${pending?events:''}${data.command&&pending?`<div class="connection-progress"><p>Run this command in a local terminal. It opens the sign-in page and returns the result automatically.</p><pre><code>${escape(data.command)}</code></pre><button data-copy>Copy command</button><p class="hint">Your ShinBot server is remote. The local helper receives the browser callback without copying redirect URLs.</p></div>`:''}${data.prompt?.type==='manual_code'?`<details><summary>Having trouble returning from sign-in?</summary>${answer}</details>`:answer}<div class="form-footer">${pending?'<button data-cancel>Cancel sign-in</button>':'<button class="primary" data-retry>Try again</button><button data-close>Close</button>'}</div>`;
        if(body.querySelector('[data-oauth-answer]'))bind(body.querySelector('[data-oauth-answer]'),async values=>{await api('/api/login/respond','POST',{id:started.id,value:values.value});await poll();});
        action(body.querySelector('[data-copy]'),async()=>{try{await navigator.clipboard.writeText(data.command);toast('Command copied.');}catch{toast('Select the command and copy it.',true);}});
        action(body.querySelector('[data-cancel]'),async()=>{await api('/api/login/respond','POST',{id:started.id,cancel:true});loginStatus='cancelled';d.close();});
        action(body.querySelector('[data-close]'),()=>d.close());
        action(body.querySelector('[data-retry]'),async()=>{d.close();await oauth(provider,onComplete);});
      }
      if(d.open&&loginStatus==='pending')timer=setTimeout(poll,1000);
    } catch(error){
      if(!d.open)return;
      body.innerHTML=`<p role="alert">${escape(error.message)}</p><p>Your sign-in may still be in progress.</p><button data-recheck>Check again</button><button data-close>Cancel sign-in</button>`;
      last='';action(body.querySelector('[data-recheck]'),poll);action(body.querySelector('[data-close]'),()=>d.close());
    }
  }
  await poll();
}
boot().catch(error=>{root.innerHTML=`<main class="page"><h1>ShinBot could not start.</h1><p>${escape(error.message)}</p></main>`;});

const templateKey=t=>t.catalogKey||t.id;
function templateCard(t,authenticated=true){return `<article class="card template-card"><h3>${escape(t.name)}</h3><p class="muted">${escape(t.description)}</p><small>${t.profiles.length} ${t.profiles.length===1?'profile':'profiles'} · ${escape(t.license)}</small><div class="form-footer"><button data-template="${escape(templateKey(t))}" class="small-button">${authenticated?'Preview template':'Explore this template'} <span class="icon icon-arrow-up-right" aria-hidden="true"></span></button></div></article>`;}
function downloadJson(name,data){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function renderWelcome(){
  const data=await api('/welcome');
  root.innerHTML=`<main class="first-home"><header class="first-head"><div class="brand">${avatar()}<span>ShinBot</span></div>${appearancePicker()}</header><div class="first-grid"><section class="setup-intro">${avatar('setup-mascot')}<h1>Welcome to ShinBot.</h1><p class="subhead">Your own assistant for research, plans, and the tasks you come back to.</p><ol class="setup-steps" aria-label="Setup progress"><li aria-current="step"><span class="step-marker">${icon('key-round')}</span><span>Create your workspace<small>Set a password to protect your bots and saved work.</small></span></li><li><span class="step-marker">${icon('plug')}</span><span>Connect a model<small>Use an API key, a supported sign-in, or a local model.</small></span></li><li><span class="step-marker">${icon('message-circle')}</span><span>Start a conversation<small>Bring a task, or let ShinBot help you choose one.</small></span></li></ol><p class="setup-host">${icon('server')} Runs on your server. You choose the model.</p></section><section class="card setup-card"><h2>Set up ShinBot</h2><p class="muted">Start with a password for this workspace.</p><form id="claim-home">${setupToken?'<p class="setup-verified">'+icon('circle-check')+' Installation token included</p>':`<label>Installation token<input name="bootstrap" type="password" required autocomplete="off" aria-describedby="token-help"></label><p class="hint field-hint" id="token-help">Run <code>shinbot setup-token</code> on your server, then paste the token here. This confirms the installation is yours.</p>`}<label for="setup-password">Choose a password</label><div class="password-field"><input id="setup-password" name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password" aria-describedby="password-help"><button type="button" id="show-password" class="quiet icon-button" aria-label="Show password" aria-pressed="false">${icon('eye')}</button></div><p class="hint field-hint" id="password-help">At least 12 characters. A few words work well.</p>${timezoneField(displayZone())}<p class="hint">Suggested from your browser. Change it anytime in Settings.</p><div id="pending-template" class="pending-template" hidden></div><button class="primary" type="submit">Create workspace ${icon('arrow-right')}</button><p id="claim-error" class="error-text" role="alert"></p><p class="setup-next">Next, connect your model. You can explore the workspace first.</p></form></section></div><section class="home-templates"><div class="section-heading"><h2>Start with something useful</h2><p class="muted">Optional bot templates. You can add or change these later.</p></div><div class="cards">${data.templates.map(t=>templateCard(t,false)).join('')}</div></section></main>`;
  $('#show-password').onclick=()=>{const input=$('#setup-password'),show=input.type==='password';input.type=show?'text':'password';$('#show-password').setAttribute('aria-label',show?'Hide password':'Show password');$('#show-password').setAttribute('aria-pressed',String(show));$('#show-password').innerHTML=icon(show?'eye-off':'eye');};
  updatePendingTemplate();
  bind($('#claim-home'),async values=>{try{await api('/auth/login','POST',{...values,...(setupToken?{bootstrap:setupToken}:{})});}catch(error){$('#claim-error').textContent=error.message;return;}let templateError='';if(state.pendingTemplate){try{const result=await api('/api/templates/install','POST',{...state.pendingTemplate,allow_shared_browser:false});state.bot=result.bot_ids[0];state.pendingTemplate=null;}catch(error){templateError='Your workspace is ready, but the template could not be added. Preview it below to try again. '+error.message;}}state.view='home';await boot();if(templateError)toast(templateError);});
  document.querySelectorAll('[data-template]').forEach(button=>action(button,()=>previewTemplate(data.templates.find(t=>templateKey(t)===button.dataset.template),false)));
}
function updatePendingTemplate(){
  const target=$('#pending-template');if(!target)return;target.hidden=!state.pendingTemplate;
  target.innerHTML=state.pendingTemplate?`<span>${icon('check')}<span>Starting with <strong>${escape(state.pendingTemplate.template.name)}</strong></span></span><button type="button" class="quiet icon-button" id="clear-template" aria-label="Remove selected template">${icon('x')}</button>`:'';
  if($('#clear-template'))$('#clear-template').onclick=()=>{state.pendingTemplate=null;updatePendingTemplate();$('#setup-password').focus();};
}
async function previewTemplate(t,authenticated=true){
  const requestsShared=t.profiles.some(p=>p.browser==='shared');
  const d=dialog(t.name,`<p>${escape(t.description)}</p>${t.profiles.map(p=>`<div class="list-item"><h3>${escape(p.name)}</h3><p>${escape(p.description||'')}</p><details><summary>Read profile instructions</summary><p class="profile-instructions">${escape(p.instructions)}</p></details><small>Browser sign-ins: ${p.browser==='private'?'Just this bot':p.browser==='group'?'This new group':('Imports with separate sign-ins'+(authenticated?'; allow shared access below if needed':''))}${p.provider?' · Model: '+escape(p.provider)+' / '+escape(p.model):' · Uses your default model'}</small></div>`).join('')}<p class="muted"><small>A template creates new profiles. It does not copy conversations, memories, logins or credentials, and does not enable routines or grant new permissions.</small></p>${requestsShared&&authenticated?'<label class="checkbox-row"><input id="template-share" type="checkbox">Allow these profiles to use the installation’s shared browser sign-ins</label>':''}<div class="form-footer"><button id="use-template" class="primary">${authenticated?'Add to my bots':'Start with this template'}</button></div>`);
  action($('#use-template'),async()=>{if(!authenticated){const {repository,catalogKey,...template}=t;state.pendingTemplate={template,request_id:crypto.randomUUID()};d.close();updatePendingTemplate();$('#claim-home input').focus();toast('Set up ShinBot to add '+t.name+'.');return;}const {repository,catalogKey,...template}=t;const result=await api('/api/templates/install','POST',{template,request_id:crypto.randomUUID(),allow_shared_browser:$('#template-share')?.checked===true});state.bot=result.bot_ids[0];state.task=null;d.close();await boot();toast('Bot profiles added. Open a chat to start.');});
}
async function renderHome(){
  const current=pageScope('home');if(!current())return;
  $('#page').className='page home-page';
  let providerError='';const [roster,markets,providers]=await Promise.all([api('/api/bots'),api('/api/templates'),api('/api/providers').catch(error=>{providerError=error.message;return [];})]);if(!current())return;state.bots=roster.bots;state.groups=roster.groups;state.colors=roster.colors;
  const connected=providerReady(providers,state.settings);
  const templates=markets.flatMap(m=>m.templates.map(t=>({...t,repository:m.source,catalogKey:m.id+':'+t.id+':'+t.version})));
  $('#page').innerHTML=`<div class="page-head"><div><h1>Your bots</h1><p class="subhead">Open a conversation or create a bot for a specific task.</p></div><button id="new-bot" class="primary"><span class="icon icon-plus" aria-hidden="true"></span> Create bot</button></div>${connected?(homeHelpDismissed?'':'<div class="home-help row between"><p>Not sure where to start? Work out a first task together.</p><div class="row"><button id="start-chat" class="primary">Start chatting <span class="icon icon-arrow-right" aria-hidden="true"></span></button><button id="setup-conversation" class="quiet">Help me get started <span class="icon icon-arrow-up-right" aria-hidden="true"></span></button></div><button id="dismiss-home-help" class="quiet icon-button home-help-dismiss" type="button" aria-label="Dismiss getting-started tips" title="Dismiss getting-started tips"><span class="icon icon-x" aria-hidden="true"></span></button></div>'):'<div class="card setup-card inline-setup"><div><h2>Connect your model.</h2><p>Use an API key, a supported subscription, or a local model.</p></div><button id="connect-first" class="primary">Choose a model <span class="icon icon-arrow-up-right" aria-hidden="true"></span></button></div>'}<div class="section-heading row between"><h2>Profiles</h2><button id="new-group" class="quiet"><span class="icon icon-plus" aria-hidden="true"></span> New group</button></div>${roster.groups.map(g=>{const bots=roster.bots.filter(b=>b.group_id===g.id);return `<section class="bot-group"><div class="row between"><h3>${escape(g.name)}</h3>${bots.length?`<button class="small-button quiet" data-export-group="${g.id}">Export group template</button>`:''}</div><div class="bot-rows">${bots.map(b=>`<article class="card bot-card">${avatar("bot-avatar",b)}<div class="bot-summary"><h3>${escape(b.name)}</h3><p class="muted">${escape(b.profile.description||"Your personal assistant for everyday work.")}</p><small>${icon("monitor")} ${b.profile.browser==='private'?'Just this bot':b.profile.browser==='group'?'This group':'Shared with all bots'}</small></div><div class="form-footer row"><button class="primary small-button" data-open-bot="${b.id}">Open chat ${icon("arrow-up-right")}</button><button class="small-button" data-edit-bot="${b.id}">Edit</button><button class="small-button quiet" data-export-bot="${b.id}" aria-label="Export ${escape(b.name)} template">Share template</button></div></article>`).join('')||'<p class="muted">Add a bot and choose this group in its profile.</p>'}</div></section>`;}).join('')}<div class="section-heading row between"><div><h2>Bot templates</h2></div><div class="row"><button id="import-template">Import JSON</button><button id="add-template-repo"><span class="icon icon-plus" aria-hidden="true"></span> Template repository</button></div></div><div class="cards">${templates.map(t=>templateCard(t)).join('')}</div><p class="muted"><small>Official templates: adarshdigievo/shinbot-official-templates. Share improvements through pull requests. Review profile text for private information before sharing.</small></p>`;
  action($('#connect-first'),()=>connectModel());if(providerError)toast('Model connection is unavailable. Your profiles are still available. '+providerError);
  action($('#dismiss-home-help'),()=>{if(!setHomeHelpDismissed(true))toast('Hidden for this visit. Your browser could not save this choice.');});
  action($('#start-chat'),()=>{state.task=null;return navigate('chat');});
  action($('#setup-conversation'),async()=>{state.bot='default';state.task=null;await navigate('chat');$('#prompt').value='Help me get started. Ask about my day, suggest a useful first task, and help me choose or create bot profiles from the available templates.';$('#composer').requestSubmit();});
  action($('#new-bot'),()=>editBot());
  action($('#new-group'),()=>{const d=dialog('New bot group',`<form id="group-form">${field('Group name','name','','text','required maxlength="100"')}<button class="primary" type="submit">Create group</button></form>`);bind($('#group-form'),async values=>{await api('/api/bot-groups','POST',values);d.close();await renderHome();});});
  document.querySelectorAll('[data-open-bot]').forEach(button=>action(button,async()=>{state.bot=button.dataset.openBot;state.chatFilter=state.bot;state.task=null;$('#active-bot').value=state.bot;renderTaskList();await navigate('chat');}));
  document.querySelectorAll('[data-edit-bot]').forEach(button=>action(button,()=>editBot(roster.bots.find(b=>b.id===button.dataset.editBot))));
  document.querySelectorAll('[data-export-bot]').forEach(button=>action(button,async()=>downloadJson('bot-template.json',await api(`/api/bots/${button.dataset.exportBot}/export`))));
  document.querySelectorAll('[data-export-group]').forEach(button=>action(button,async()=>downloadJson('bot-group-template.json',await api(`/api/bot-groups/${button.dataset.exportGroup}/export`))));
  document.querySelectorAll('[data-template]').forEach(button=>action(button,()=>previewTemplate(templates.find(t=>templateKey(t)===button.dataset.template))));
  action($('#add-template-repo'),()=>{const d=dialog('Add a template repository',`<form id="template-repo">${field('GitHub repository','source','adarshdigievo/shinbot-official-templates','text','required')}<button type="submit" class="primary">Load templates</button></form>`);bind($('#template-repo'),async values=>{await api('/api/templates/repositories','POST',values);d.close();await renderHome();});});
  action($('#import-template'),()=>{const input=document.createElement('input');input.type='file';input.accept='.json';input.onchange=async()=>{try{if(input.files[0].size>200000)throw new Error('Template exceeds 200 KB.');await previewTemplate(JSON.parse(await input.files[0].text()));}catch(error){toast('Could not read this template. Choose a ShinBot template JSON file.');}};input.click();});
}
function botColorPicker(bot){
  const selected=bot?botColor(bot):'';
  return `<fieldset class="bot-colors"><legend>Bot colour</legend><div class="color-options">${[['','Automatic'],...state.colors.map(color=>[color,color[0].toUpperCase()+color.slice(1)])].filter(([color])=>color||!bot).map(([color,label])=>`<label class="color-option${color?' bot-color-'+color:''}" title="${label}"><input class="sr-only" type="radio" name="color" value="${color}" ${color===selected?'checked':''}><span class="color-swatch" aria-hidden="true">${color?icon('check'):'A'}</span><span class="sr-only">${label}</span></label>`).join('')}</div><p class="hint" id="bot-color-label" aria-live="polite">${selected?selected[0].toUpperCase()+selected.slice(1):'Automatic · choose a colour for me'}</p></fieldset>`;
}
async function editBot(bot){
  const p=bot?.profile||{name:'',description:'',instructions:'',starters:[],browser:'private'};
  const d=dialog(bot?'Edit bot profile':'Create a bot',`<form id="bot-form">${botColorPicker(bot)}<div class="form-grid">${field('Bot name','name',p.name,'text','required maxlength="80"')}${field('Short description','description',p.description,'text','maxlength="500"')}<label>Group<select name="group_id">${state.groups.map(g=>`<option value="${g.id}" ${g.id===(bot?.group_id||'default')?'selected':''}>${escape(g.name)}</option>`).join('')}</select></label><label>Browser sign-ins<select name="browser">${[['private','Just this bot'],['group','Share with this group'],['shared','Share with all bots']].map(([id,label])=>`<option value="${id}" ${id===p.browser?'selected':''}>${label}</option>`).join('')}</select></label></div><label>Role and instructions<textarea name="instructions" rows="6" maxlength="12000">${escape(p.instructions)}</textarea></label><label>Starter prompts, one per line<textarea name="starters" rows="3">${escape((p.starters||[]).join('\n'))}</textarea></label><details><summary>Model override</summary><p class="muted">Leave both blank to use the installation model.</p>${field('Provider ID','provider',p.provider||'')}${field('Model ID','model',p.model||'')}</details><p class="muted"><small>Browser sharing controls cookies and sign-ins. Workspace files and notebook pages remain shared. Role and browser changes apply to new conversations. Colours update immediately after saving.</small></p><div class="form-footer row">${bot&&bot.id!=='default'?'<button id="archive-bot" type="button" class="danger quiet">Archive bot</button>':''}<button type="submit" class="primary">Save profile</button></div></form>`);
  d.querySelectorAll('[name=color]').forEach(input=>input.addEventListener('change',()=>{$('#bot-color-label').textContent=input.value?input.value[0].toUpperCase()+input.value.slice(1):'Automatic · choose a colour for me';}));
  bind($('#bot-form'),async values=>{const {group_id,...profile}=values;profile.color=profile.color||null;profile.starters=profile.starters.split('\n').map(s=>s.trim()).filter(Boolean);profile.provider=profile.provider||null;profile.model=profile.model||null;const saved=await api('/api/bots','POST',{...(bot?{id:bot.id}:{}),group_id,profile});state.bot=saved.id;d.close();await boot();});
  action($('#archive-bot'),async()=>{if(confirm('Archive this bot and pause its schedules? Its history remains in storage.')){await api(`/api/bots/${bot.id}`,'DELETE');state.bot='default';d.close();await boot();}});
}

async function connectModel(providerId){
  const providers=(await api('/api/providers')).sort((a,b)=>Number(b.configured)-Number(a.configured)||(['openai','anthropic','google'].includes(b.id)?1:0)-(['openai','anthropic','google'].includes(a.id)?1:0));
  const d=dialog('Connect your model',`<p>Choose the model your bots will use. You can change this later in Settings.</p><form id="first-model"><label>Provider<select name="provider" id="first-provider">${providers.map(p=>`<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('')}</select></label><label>Model<select name="model" id="first-model-id"></select></label><div id="first-credentials"></div><div class="form-footer"><button class="primary" type="submit">Use this model <span class="icon icon-arrow-up-right" aria-hidden="true"></span></button></div></form><div class="list-item"><button id="advanced-model" class="quiet">Use a local or compatible model</button></div>`);
  const select=$('#first-provider');
  if(!providerId&&!state.settings.provider&&providers.some(p=>p.id==='openai'))select.value='openai';
  if(providers.some(p=>p.id===(providerId||state.settings.provider)))select.value=providerId||state.settings.provider;
  async function save(provider,model){state.settings=await api('/api/settings','PUT',{...state.settings,provider,model});d.close();if(state.view==='home')await renderHome();else if(state.view==='settings')await renderSettings();else if(state.view==='chat')await renderChat();toast('Model saved.');}
  function options(){
    const p=providers.find(p=>p.id===select.value);
    $('#first-model-id').innerHTML='<option value="">Choose a model</option>'+(p?.models||[]).map(m=>`<option value="${escape(m.id)}">${escape(m.name)}</option>`).join('');
    if(p?.models.some(m=>m.id===state.settings.model))$('#first-model-id').value=state.settings.model;
    $('#first-credentials').innerHTML=p?.configured?'<p class="muted">This provider is connected.</p>':`${p?.oauth?'<button id="first-oauth" class="primary" type="button">'+(p.id==='openai'?'Continue with ChatGPT':'Sign in with '+escape(p.name))+'</button><details class="api-key-alternative"><summary>Use an API key instead</summary>':''}${field('API key','key','','password','autocomplete="off"')}${p?.oauth?'</details>':''}`;
    action($('#first-oauth'),async()=>{const provider=select.value,model=$('#first-model-id').value;await oauth(provider,async()=>{if(model)await save(provider,model);else{d.close();await connectModel(provider);}});});
  }
  select.onchange=options;options();
  bind($('#first-model'),async values=>{const p=providers.find(p=>p.id===values.provider);if(!values.model)throw new Error('Choose a model first.');if(!p.configured){if(!values.key?.trim())throw new Error(p.oauth?'Enter your API key or use the subscription sign-in button.':'Enter your API key to connect this provider.');await api('/api/providers/key','POST',{provider:values.provider,key:values.key});}await save(values.provider,values.model);});
  action($('#advanced-model'),async()=>{d.close();state.settingsTab='connections';await navigate('settings');});
}

function bindTabs(selector,select,initial,key){
  const buttons=[...document.querySelectorAll(selector)];
  function activate(tab){buttons.forEach(button=>{const active=button.dataset[key]===tab;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});select(tab);}
  buttons.forEach((button,index)=>{button.onclick=()=>activate(button.dataset[key]);button.onkeydown=event=>{const offset={ArrowRight:1,ArrowDown:1,ArrowLeft:-1,ArrowUp:-1}[event.key];if(offset||event.key==='Home'||event.key==='End'){event.preventDefault();const next=buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+offset+buttons.length)%buttons.length];activate(next.dataset[key]);next.focus();}};});activate(initial);
}
