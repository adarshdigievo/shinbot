export function pluginsView({api,$,state,escape,icon,dialog,action,toast,pageScope,refreshPluginNavigation,
  latestCatalogEntries,scanSummary,configurePlugin,reviewPlugin,configurePluginAccess,
  extensionAccessFields,bindExtensionAccess,extensionAccessValue,connectMCP,importSource,installSkill,addMarketplace,uploadPlugin,createPlugin}) {
  let query='';
  const identity=p=>p.id+'@'+p.version;
  function status(p){
    if(p.kind==='channel')return ['Host setup','paused'];
    if(p.status==='active')return [p.missing_configuration?.length?'Needs setup':'Enabled',p.missing_configuration?.length?'paused':'active'];
    if(p.security?.status!=='passed')return ['Review needed','paused'];
    if(p.missing_configuration?.length)return ['Needs setup','paused'];
    return [{disabled:'Disabled',draining:'Previous version',staged:'Ready to enable'}[p.status]||'Ready to enable',''];
  }
  async function finish(id,version){
    const data=await api('/api/plugins'),p=data.installed.find(x=>x.id===id&&x.version===version);
    if(!p)throw new Error('This plugin is no longer installed. Refresh the list.');
    const missing=p.missing_configuration?.length, blocked=p.security?.status!=='passed', channel=p.kind==='channel';
    const d=dialog((p.status==='active'?'Manage ':'Set up ')+p.name,`
      <ol class="install-steps" aria-label="Installation progress" ${p.status==='active'?'hidden':''}><li ${blocked?'aria-current="step"':''}>Review access</li>${p.configuration?.length?`<li ${missing?'aria-current="step"':''}>Connect account</li>`:''}<li ${!blocked&&!missing?'aria-current="step"':''}>Enable</li></ol>
      <p>${escape(p.description)}</p><p class="hint">${escape(p.origin)} · ${escape(p.version)} · ${escape(p.license)}</p>
      <p>${escape(p.execution_access||'No executable tools')}</p>
      ${p.ui?'<p class="notice">This plugin can change ShinBot’s interface and access the signed-in app. Enable UI plugins only from authors you trust. Reload with <code>?extensions=off</code> to recover from a broken UI.</p>':''}
      ${p.hooks?.length?`<p>Runs automatically during chats: ${p.hooks.map(h=>escape(h.event.replaceAll('_',' '))).join(', ')}. Hooks run with Computer permissions and can read chat and tool content.</p>`:''}
      ${p.permissions?.length?`<p class="hint">Declared access: ${p.permissions.map(escape).join(', ')}</p>`:''}
      <details ${blocked?'open':''}><summary>${blocked?'Review scan results':`Scan completed${p.security?.findings?.length?' · '+p.security.findings.length+' finding'+(p.security.findings.length===1?'':'s')+' to review':''}`}</summary>${scanSummary(p.security)}<button data-files>Review files</button></details>
      ${channel?'<p class="connection-progress">Connect Telegram on the computer running ShinBot:<br><code>shinbot telegram-setup</code><br>Then send <code>/start</code> in your private Telegram chat.</p>':`<form>${extensionAccessFields(p.access)}${missing?`<p class="notice">Complete the connection settings to enable this plugin.</p>`:''}<div class="form-footer setup-footer">${missing?'<button type="button" class="primary" data-configure>Connect account</button>':`<button type="button" class="primary" data-enable ${blocked?'disabled':''}>${p.status==='active'?'Save bot access':'Enable plugin'}</button>`}${p.configuration?.length&&!missing?'<button type="button" data-configure>Connection settings</button>':''}<button type="button" data-later>Done for now</button></div></form>`}`);
    action(d.querySelector('[data-files]'),()=>reviewPlugin(id,version));
    const form=d.querySelector('form');if(form)bindExtensionAccess(form);
    action(d.querySelector('[data-configure]'),async()=>{
      // Keep the chosen bot scope while account setup is open.
      await api(`/api/plugins/${id}/access`,'PUT',extensionAccessValue(form));
      await configurePlugin(id,version,async()=>{d.close();await render();await finish(id,version);});
    });
    action(d.querySelector('[data-enable]'),async()=>{
      await api(`/api/plugins/${id}/access`,'PUT',extensionAccessValue(form));
      if(p.status!=='active')await api(`/api/plugins/${id}/${version}/activate`,'POST',{});
      d.close();await render();toast(p.name+' is ready for new conversations.');
    });
    action(d.querySelector('[data-later]'),()=>d.close());
  }
  async function render(){
    const current=pageScope('plugins');if(!current())return;
    const data=await api('/api/plugins');
    await refreshPluginNavigation(data.installed);if(!current())return;$('#page').className='page plugins-page';
    $('#page').innerHTML=`<div class="page-head"><div><h1>Plugins</h1><p class="subhead">Add capabilities to your bots. Connect an account, review access, and enable when ready.</p></div><div class="row"><button id="create-plugin" class="primary">Create a plugin</button><details class="plugin-tools-menu"><summary>Add from another source</summary><div><button id="connect-mcp">Connect MCP server</button><button id="import-source">Import from a link</button><button id="install-skill">Install skills</button><button id="add-market">Add marketplace</button><button id="upload-plugin">Install local plugin</button></div></details></div></div><label class="plugin-search">Find a plugin<input type="search" id="plugin-search" placeholder="Search by name or capability" value="${escape(query)}"></label><section aria-labelledby="installed-title"><h2 id="installed-title">Your plugins <small>${data.installed.length}</small></h2><div class="plugin-list" id="installed-plugins"></div></section><section aria-labelledby="discover-title"><h2 id="discover-title">Discover plugins</h2><div id="plugin-catalog"></div></section>`;
    function draw(){
      const matches=p=>[p.name||p.id,p.description||''].join(' ').toLowerCase().includes(query.toLowerCase());
      $('#installed-plugins').innerHTML=data.installed.filter(matches).map(p=>{const [label,tone]=status(p);return `<article class="plugin-tile" data-plugin-card="${escape(identity(p))}"><div class="row between"><h3>${escape(p.name)}</h3><span class="pill ${tone}">${label}</span></div><p>${escape(p.description)}</p><p class="hint">${escape(p.version)} · ${p.access.mode==='shared'?'All bots':p.access.bots.length+' selected bot'+(p.access.bots.length===1?'':'s')}</p><div class="row plugin-controls">${p.status==='active'&&!p.missing_configuration?.length?`<button data-manage="${escape(identity(p))}">Manage</button>`:`<button class="primary" data-manage="${escape(identity(p))}">${p.kind==='channel'?'Set up on host':p.status==='disabled'?'Enable':'Finish setup'}</button>`}${p.configuration?.length?`<button class="quiet" data-configure="${escape(identity(p))}">Connection</button>`:''}${p.status==='active'&&p.kind!=='channel'?`<button class="quiet" data-disable="${escape(p.id)}">Disable</button>`:''}</div><details><summary>Details and permissions</summary><p class="hint">Source: ${escape(p.source||p.origin)}</p><p class="hint">${escape(p.license)} · ${escape(p.execution_access)}</p><div class="row"><button class="small-button" data-review="${escape(identity(p))}">Review files and scan</button><button class="small-button" data-plugin-access="${escape(p.id)}">Change bot access</button></div></details></article>`;}).join('')||'<p class="muted">'+(query?'No installed plugins match your search.':'No plugins yet. Choose one below to get started.')+'</p>';
      $('#plugin-catalog').innerHTML=data.marketplaces.map(m=>`<div class="row between"><p class="hint">${escape(m.source)}</p><button class="quiet small-button" data-refresh="${escape(m.source)}">Refresh catalog</button></div><div class="plugin-list">${latestCatalogEntries(m.plugins).filter(matches).map(p=>{
        const installed=data.installed.find(x=>x.id===p.id&&x.version===p.version&&x.marketplace===m.id);
        const other=data.installed.some(x=>x.id===p.id);
        return `<article class="plugin-tile"><div class="row between"><h3>${escape(p.name||p.id)}</h3><small>${escape(p.version)}</small></div><p>${escape(p.description||'')}</p><div class="row">${installed?`<span class="pill ${status(installed)[1]}">${status(installed)[0]}</span><button class="quiet" data-manage="${escape(identity(installed))}">Manage</button>`:`<button data-install="${escape(p.id)}" data-version="${escape(p.version)}" data-market="${escape(m.id)}">${other?'Review update':'Install'}</button>`}</div></article>`;
      }).join('')||'<p class="muted">No plugins match your search in this marketplace.</p>'}</div>`).join('');
      $('#page').querySelectorAll('[data-manage]').forEach(b=>action(b,()=>finish(...b.dataset.manage.split('@'))));
      $('#page').querySelectorAll('[data-configure]').forEach(b=>action(b,()=>configurePlugin(...b.dataset.configure.split('@'))));
      $('#page').querySelectorAll('[data-review]').forEach(b=>action(b,()=>reviewPlugin(...b.dataset.review.split('@'))));
      $('#page').querySelectorAll('[data-plugin-access]').forEach(b=>action(b,()=>configurePluginAccess(data.installed.find(p=>p.id===b.dataset.pluginAccess))));
      $('#page').querySelectorAll('[data-disable]').forEach(b=>action(b,async()=>{await api(`/api/plugins/${b.dataset.disable}/disable`,'POST',{});await render();toast('Plugin disabled.');}));
      $('#page').querySelectorAll('[data-refresh]').forEach(b=>action(b,async()=>{await api('/api/marketplaces','POST',{source:b.dataset.refresh});await render();}));
      $('#page').querySelectorAll('[data-install]').forEach(b=>action(b,async()=>{
        b.textContent='Checking plugin…';
        try{await api('/api/plugins/stage','POST',{marketplace:b.dataset.market,id:b.dataset.install,version:b.dataset.version});await render();await finish(b.dataset.install,b.dataset.version);}
        catch(error){b.textContent='Retry installation';throw error;}
      }));
    }
    $('#plugin-search').oninput=e=>{query=e.target.value;draw();};draw();
    action($('#create-plugin'),createPlugin);action($('#connect-mcp'),connectMCP);action($('#import-source'),importSource);action($('#install-skill'),installSkill);
    action($('#add-market'),addMarketplace);action($('#upload-plugin'),uploadPlugin);
  }
  return {render,finish};
}
