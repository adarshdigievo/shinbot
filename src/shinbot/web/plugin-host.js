export function availableUI(installed, bot, disabled = false) {
  return disabled ? [] : installed.filter(p => p.status === 'active' && p.ui && !p.missing_configuration?.length && (p.access?.mode !== 'bots' || p.access.bots.includes(bot)));
}

export function pluginHost({api, state, navigate, toast, compose}) {
  const disabled = new URLSearchParams(location.search).get('extensions') === 'off';
  let signature = '', generation = 0, cleanups = [], pageCleanup, pageGeneration = 0;
  const active = () => availableUI(state.installedPlugins || [], state.bot, disabled);
  const knownIcons=new Set();
  for(const sheet of document.styleSheets){try{for(const rule of sheet.cssRules){const name=rule.selectorText?.match(/^\.icon-([a-z0-9-]+)$/)?.[1];if(name)knownIcons.add(name);}}catch{}}
  const base = p => `/api/plugin-ui/${encodeURIComponent(state.bot)}/${p.id}/${p.version}`;
  const asset = (p, path) => `${base(p)}/assets/${path.split('/').map(encodeURIComponent).join('/')}`;
  function report(error) { toast('Plugin UI: '+error.message+'. Open Plugins to disable it, or reload with ?extensions=off.', true); }
  function dispose(list) { for (const fn of list.reverse()) { try { fn(); } catch (error) { report(error); } } }
  function scope(p) {
    const controller = new AbortController(), disposers = [() => controller.abort()];
    let closed=false;
    const botId = state.bot, prefix = base(p);
    const sdk = {
      version: 1, plugin: Object.freeze({id:p.id,version:p.version}), botId,
      get taskId() { return state.task; }, signal:controller.signal,
      api, toast, navigate, compose, asset: path => `${prefix}/assets/${path.split('/').map(encodeURIComponent).join('/')}`,
      storage: {
        get: async key => (await api(`${prefix}/state/${encodeURIComponent(key)}`)).value,
        set: (key,value) => api(`${prefix}/state/${encodeURIComponent(key)}`,'PUT',{value}),
        delete: key => api(`${prefix}/state/${encodeURIComponent(key)}`,'DELETE'),
      },
      on(event, handler) {
        const listener = e => { try { Promise.resolve(handler(e.detail)).catch(report); } catch (error) { report(error); } };
        document.addEventListener('shinbot:'+event,listener,{signal:controller.signal});
        return () => document.removeEventListener('shinbot:'+event,listener);
      },
      mount(slot, element) {
        if (!['composer-actions','sidebar-footer','topbar'].includes(slot)) throw new Error('Unknown UI slot');
        const place = () => { const target=document.querySelector(`[data-plugin-slot="${slot}"]`);if(target&&!target.contains(element))target.append(element); };
        const observer=new MutationObserver(place);observer.observe(document.querySelector('#app'),{childList:true,subtree:true});place();
        const remove=()=>{observer.disconnect();element.remove();};disposers.push(remove);return remove;
      },
    };
    return {sdk,add:fn=>{if(typeof fn==='function'){if(closed)dispose([fn]);else disposers.push(fn);}},dispose:()=>{if(closed)return;closed=true;dispose(disposers);}};
  }
  function clearPage() { pageGeneration++;document.querySelector('#page')?.removeAttribute('data-plugin');if(pageCleanup){dispose([pageCleanup]);pageCleanup=null;} }
  return {
    pages: () => active().flatMap(p=>p.ui.pages.map(page=>[`plugin/${p.id}/${page.id}`,knownIcons.has(page.icon)?page.icon:'puzzle',page.title])),
    clearPage,
    stop() { generation++;signature='';clearPage();dispose(cleanups);cleanups=[]; },
    async sync() {
      const plugins=active(), next=JSON.stringify([state.bot,plugins.map(p=>[p.id,p.version,p.digest])]);
      if(next===signature)return;
      signature=next;const run=++generation;dispose(cleanups);cleanups=[];
      for(const p of plugins) {
        const context=scope(p);cleanups.push(context.dispose);
        try {
          for(const path of p.ui.styles||[]) {
            const link=document.createElement('link');link.rel='stylesheet';link.href=asset(p,path);link.dataset.plugin=p.id;
            document.head.append(link);context.add(()=>link.remove());
          }
          for(const path of p.ui.patches||[]) {
            const module=await import(asset(p,path));if(run!==generation)return;
            if(typeof module.activate!=='function')throw new Error(p.name+' must export activate(sdk)');
            const cleanup=await module.activate(context.sdk);
            if(run!==generation){if(typeof cleanup==='function')cleanup();return;}
            context.add(cleanup);
          }
        } catch(error) { context.dispose();report(error); }
      }
    },
    async render(view, root) {
      const [,id,pageId]=view.split('/'), p=active().find(p=>p.id===id), page=p?.ui.pages.find(page=>page.id===pageId);
      if(!page)throw new Error('This plugin page is disabled or unavailable to this bot');
      const current=++pageGeneration, context=scope(p);pageCleanup=context.dispose;
      root.className='page plugin-page';root.replaceChildren();root.dataset.plugin=id;
      const title=document.createElement('h1');title.textContent=page.title;
      const surface=document.createElement('div');surface.className='plugin-surface';root.append(title,surface);
      const module=await import(asset(p,page.entrypoint));
      if(current!==pageGeneration)return;
      if(typeof module.mount!=='function')throw new Error(p.name+' must export mount(root, sdk)');
      const cleanup=await module.mount(surface,context.sdk);
      if(current!==pageGeneration){if(typeof cleanup==='function')cleanup();return;}
      context.add(cleanup);
    },
  };
}
