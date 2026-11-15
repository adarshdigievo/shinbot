export function githubConnection({api,dialog,confirm,escape,field,bind,action,toast}) {
  return async function connect({target='inbox',version,repositories=[],organizations=[],onDone=()=>{},disconnect}={}) {
    const d=dialog('Connect GitHub','<div data-github-body><p role="status">Checking your connection…</p></div>');
    d.classList.add('github-dialog');
    const body=d.querySelector('[data-github-body]');
    let timer, pairId, revision, generation=0;
    const selected={repositories:new Set(repositories.map(x=>x.toLowerCase())),organizations:new Set(organizations.map(x=>x.toLowerCase()))};
    const limit=target==='plugin'?25:10;
    const count=()=>selected.repositories.size+selected.organizations.size;
    d.addEventListener('close',()=>{clearTimeout(timer);if(pairId)api('/api/github/pair/'+pairId,'DELETE').catch(()=>{});});
    async function start(){
      const account=await api('/api/github/account');if(!d.open)return;
      if(account.connected)return picker(account);
      signIn();
    }
    function signIn(){
      generation++;
      body.innerHTML=`<p>Sign in with GitHub CLI on your computer, then choose the repositories ShinBot can use.</p><button class="primary" data-cli>Connect with GitHub CLI</button><div data-handoff></div><p class="hint">Uses your existing <code>gh</code> account, or opens GitHub sign-in. A credential is stored encrypted on your ShinBot server. No credentials are added to chat.</p><details><summary>Use a personal access token instead</summary><form>${field('GitHub token','token','','password','required autocomplete="off"')}<p class="hint">Use a token limited to the repositories and permissions you need.</p><button type="submit">Connect token</button></form></details>`;
      action(body.querySelector('[data-cli]'),async()=>{
        if(pairId)await api('/api/github/pair/'+pairId,'DELETE');
        const pair=await api('/api/github/pair','POST',{});pairId=pair.id;
        if(!d.open){await api('/api/github/pair/'+pairId,'DELETE');return;}
        body.querySelector('[data-handoff]').innerHTML=`<div class="connection-progress"><p>Run this command in a terminal on your computer:</p><pre><code>${escape(pair.command)}</code></pre><button data-copy-command>Copy command</button><p class="hint">Need the CLI? <a href="https://cli.github.com" target="_blank" rel="noopener noreferrer">Install GitHub CLI</a>. This request expires in 10 minutes.</p><p role="status" data-pair-status>Waiting for GitHub sign-in…</p></div>`;
        action(body.querySelector('[data-copy-command]'),async()=>{try{await navigator.clipboard.writeText(pair.command);toast('Command copied.');}catch{toast('Select the command above and copy it.',true);}});
        async function poll(){
          if(!d.open)return;
          try {
            const result=await api('/api/github/pair/'+pair.id);if(!d.open||pairId!==pair.id)return;
            if(result.status==='complete'){pairId=null;await start();return;}
            if(!['pending','connecting'].includes(result.status)){body.querySelector('[data-pair-status]').textContent='This request '+(result.status==='failed'?'failed':'expired')+'. Click Connect with GitHub CLI to try again.';return;}
            timer=setTimeout(poll,1500);
          } catch(error){if(d.open){body.querySelector('[data-pair-status]').textContent=error.message;timer=setTimeout(poll,4000);}}
        }
        clearTimeout(timer);poll();
      });
      bind(body.querySelector('form'),async values=>{await api('/api/github/account','POST',values);body.querySelector('[name=token]').value='';if(pairId)await api('/api/github/pair/'+pairId,'DELETE');pairId=null;clearTimeout(timer);await start();});
    }
    async function picker(account){
      revision=account.revision;
      const ownGeneration=++generation;
      let kind='repositories', query='';
      const pages={repositories:0,organizations:0}, more={repositories:true,organizations:true}, items={repositories:[],organizations:[]};
      body.innerHTML=`<div class="row between"><p>Connected as <strong>@${escape(account.login)}</strong></p><button class="quiet" data-switch>Switch account</button></div><p>${target==='plugin'?'Choose repositories the GitHub plugin can read and act on. Writes still require approval.':'Choose the repositories to follow in your inbox. Following a whole organization includes all of its repositories.'}</p>${target==='inbox'?'<label>What to follow<select data-list-kind><option value="repositories">Individual repositories</option><option value="organizations">Whole organizations (all repositories)</option></select></label>':''}<label>Find a source<input type="search" data-repo-search placeholder="Filter the loaded list" autocomplete="off"></label><div class="row between"><span data-selection-count role="status"></span><button class="quiet" data-clear>Clear selection</button></div><div class="notice" data-organization-scope hidden><p data-organization-summary role="status"></p><button type="button" data-repositories-only>Follow selected repositories only</button></div><div class="repo-picker" data-repos aria-live="polite"></div><div class="row"><button data-load>Load repositories</button><button data-select-visible class="quiet">Select visible</button></div><details data-selected open><summary>Selected sources</summary><div data-selected-list></div></details><p class="hint">Only selected sources are connected. Up to ${limit} ${target==='plugin'?'repositories':'sources'}. Missing a repository? Load more or check its access in GitHub.</p><div class="form-footer setup-footer"><button class="primary" data-save>Save selection</button>${disconnect?'<button data-disconnect class="quiet danger">Disconnect this integration</button>':''}</div>`;
      const list=body.querySelector('[data-repos]'), load=body.querySelector('[data-load]');
      const current=()=>d.open&&generation===ownGeneration;
      function visible(){return items[kind].filter(x=>(x.name+' '+x.description).toLowerCase().includes(query.toLowerCase()));}
      function update(){
        if(!current())return;
        body.querySelector('[data-selection-count]').textContent=`${count()} of ${limit} selected`;
        body.querySelector('[data-save]').disabled=count()===0||count()>limit;
        body.querySelector('[data-organization-scope]').hidden=!selected.organizations.size;
        body.querySelector('[data-organization-summary]').textContent='Every repository in '+[...selected.organizations].join(', ')+' will appear in your inbox, including repositories you have not selected individually.';
        body.querySelector('[data-repositories-only]').hidden=!selected.repositories.size;
        list.innerHTML=visible().map(item=>`<label class="checkbox-row"><input type="checkbox" data-repo="${escape(item.name.toLowerCase())}" ${selected[kind].has(item.name.toLowerCase())?'checked':''}><span>${kind==='organizations'?'All repositories in '+escape(item.name):escape(item.name)}${kind==='organizations'?'<small>Includes every current and future repository in this organization.</small>':''}${item.private?' <small>Private repository</small>':''}${item.description?`<small>${escape(item.description)}</small>`:''}</span></label>`).join('')||'<p class="hint">'+(pages[kind]?'No matches in the loaded list.':'No sources loaded yet.')+'</p>';
        list.querySelectorAll('[data-repo]').forEach(input=>input.onchange=()=>{
          if(input.checked&&count()>=limit){input.checked=false;toast(`Choose up to ${limit} sources.`,true);return;}
          if(input.checked)selected[kind].add(input.dataset.repo);else selected[kind].delete(input.dataset.repo);
          update();
        });
        body.querySelector('[data-selected-list]').innerHTML=Object.entries(selected).flatMap(([type,values])=>[...values].map(name=>`<label class="checkbox-row"><input type="checkbox" checked data-remove-kind="${type}" data-remove-name="${escape(name)}"><span>${type==='organizations'?'All repositories in '+escape(name):escape(name)}</span></label>`)).join('')||'<p class="hint">No sources selected.</p>';
        body.querySelectorAll('[data-remove-name]').forEach(input=>input.onchange=()=>{selected[input.dataset.removeKind].delete(input.dataset.removeName);update();});
        load.hidden=!more[kind];load.textContent='Load '+(pages[kind]?'more ':'')+kind;
      }
      async function fetchPage(){
        const requestedKind=kind;
        list.setAttribute('aria-busy','true');
        try {
          const result=await api('/api/github/discover?'+new URLSearchParams({kind:requestedKind,page:pages[requestedKind]+1}));if(!current())return;
          if(result.revision!==revision)throw new Error('The GitHub account changed. Close this dialog and reconnect.');
          const known=new Set(items[requestedKind].map(x=>x.name));items[requestedKind].push(...result.items.filter(x=>!known.has(x.name)));
          pages[requestedKind]=result.page;more[requestedKind]=result.has_more;update();
        } finally {list.removeAttribute('aria-busy');}
      }
      action(load,fetchPage);
      action(body.querySelector('[data-select-visible]'),()=>{for(const item of visible()){if(count()>=limit)break;selected[kind].add(item.name.toLowerCase());}update();});
      action(body.querySelector('[data-clear]'),()=>{selected.repositories.clear();selected.organizations.clear();update();});
      action(body.querySelector('[data-repositories-only]'),()=>{selected.organizations.clear();update();});
      body.querySelector('[data-repo-search]').oninput=e=>{query=e.target.value;update();};
      const kinds=body.querySelector('[data-list-kind]');
      if(kinds)kinds.onchange=async()=>{kind=kinds.value;query='';body.querySelector('[data-repo-search]').value='';update();if(!pages[kind])try{await fetchPage();}catch(error){toast(error.message,true);}};
      action(body.querySelector('[data-switch]'),()=>{clearTimeout(timer);signIn();});
      action(body.querySelector('[data-save]'),async()=>{await api('/api/github/select','POST',{target,version,revision,repositories:[...selected.repositories],organizations:[...selected.organizations]});d.close();await onDone();});
      action(body.querySelector('[data-disconnect]'),async()=>{if(!await confirm({title:'Disconnect this integration?',body:target==='plugin'?'The GitHub plugin is disabled and its saved token and repository list are removed.':'ShinBot stops following these repositories and removes the inbox token.',confirm:'Disconnect',danger:true}))return;await disconnect();d.close();await onDone();});
      update();
      try{await fetchPage();}catch(error){if(current())toast(error.message,true);}
    }
    try{await start();}catch(error){if(d.open){body.innerHTML='<p>GitHub setup could not load.</p><button data-retry>Try again</button>';action(body.querySelector('[data-retry]'),start);toast(error.message,true);}}
  };
}
