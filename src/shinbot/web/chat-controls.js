export const approvalModes = [
  ['auto','Auto','Browse and search freely. Ask before destructive or unclear actions.'],
  ['ask','Ask','Ask before commands and browser actions. Saved permissions still apply.'],
  ['full','Full access','Run actions without approval, including actions that change or delete data.'],
];
export const effortName = level => ({off:'Off',minimal:'Minimal',low:'Low',medium:'Medium',high:'High',xhigh:'Extra high',max:'Maximum'})[level] || level;
export function connectedModels(providers, query='') {
  const search=query.trim().toLocaleLowerCase();
  return providers.filter(p=>p.configured).flatMap(p=>(p.models||[]).map(m=>({...m,provider:p.id,providerName:p.name})))
    .filter(m=>`${m.name} ${m.id} ${m.providerName}`.toLocaleLowerCase().includes(search));
}
export function thinkingLevels(providers, selection) {
  return providers.find(p=>p.id===selection.provider)?.models.find(m=>m.id===selection.model)?.thinkingLevels || ['off'];
}

export function chatControls({root,api,selection,save,settings,escape,icon,toast,confirm}) {
  let providers=[],loading=true,saving=false;
  root.innerHTML=`<button type="button" id="composer-model" class="model-label" aria-label="Choose model" aria-describedby="composer-effort-summary" aria-haspopup="dialog" aria-expanded="false"><span class="composer-model-name"></span><span class="effort-meter" aria-hidden="true"></span>${icon('chevron-down')}</button><span id="composer-effort-summary" class="sr-only"></span><label class="composer-approval"><span class="sr-only">Approval mode</span>${icon('shield-check')}<select id="composer-approval" aria-label="Approval mode" aria-describedby="composer-approval-help">${approvalModes.map(([id,name])=>`<option value="${id}">${name}</option>`).join('')}</select>${icon('chevron-down')}</label><span id="composer-approval-help" class="sr-only"></span><div class="model-popover" popover id="chat-model-picker" role="dialog" aria-label="Choose model"><div class="model-search">${icon('search')}<input type="search" aria-label="Search models" placeholder="Find a model" autocomplete="off"></div><div class="model-options"></div><section class="model-effort" aria-labelledby="effort-heading"><div class="effort-heading"><span id="effort-heading">Thinking effort</span><span class="effort-value"></span></div><div class="effort-levels" role="radiogroup" aria-label="Thinking effort"></div><div class="effort-endpoints" aria-hidden="true"></div><p class="effort-help"></p></section><div class="model-picker-footer"><small class="model-picker-hint"></small><button type="button" class="quiet" data-model-settings>${icon('settings')}Settings</button></div></div>`;
  const button=root.querySelector('#composer-model'), approval=root.querySelector('#composer-approval');
  const panel=root.querySelector('#chat-model-picker'), search=panel.querySelector('input'), list=panel.querySelector('.model-options'), effort=panel.querySelector('.effort-levels');
  const current=()=>root.isConnected;
  const busy=()=>selection().messages?.some(m=>['queued','dispatching','running'].includes(m.status));
  function selectedChoice(){
    const selected=selection(),levels=thinkingLevels(providers,selected);
    return {...selected,thinking_level:loading?selected.thinking_level||'off':levels.includes(selected.thinking_level)?selected.thinking_level:levels[0]};
  }
  function update(){
    if(!current())return;
    const selected=selectedChoice(),levels=thinkingLevels(providers,selected),index=levels.indexOf(selected.thinking_level),locked=loading||saving||busy();
    const model=connectedModels(providers).find(m=>m.provider===selected.provider&&m.id===selected.model);
    button.querySelector('.composer-model-name').textContent=model?.name||selected.model||'Choose model';
    button.title=busy()?'Model changes are available after this reply.':`Choose model · Thinking effort: ${effortName(selected.thinking_level)}`;
    button.disabled=saving;
    button.querySelector('.effort-meter').innerHTML=levels.map((_,i)=>`<i class="effort-segment ${selected.thinking_level!=='off'&&i<=index?'filled':''}"></i>`).join('');
    root.querySelector('#composer-effort-summary').textContent=`Thinking effort: ${effortName(selected.thinking_level)}.`;
    const signature=JSON.stringify(levels);
    if(effort.dataset.levels!==signature){
      effort.innerHTML=levels.map(level=>`<label class="effort-step" title="${escape(effortName(level))}"><input class="sr-only" type="radio" name="thinking-effort" value="${escape(level)}" aria-label="${escape(effortName(level))}"><span class="effort-segment" aria-hidden="true"></span></label>`).join('');
      effort.dataset.levels=signature;
      effort.querySelectorAll('input').forEach(input=>input.onchange=async()=>{
        await change({thinking_level:input.value});
        if(current()&&panel.matches(':popover-open'))effort.querySelector('input:checked')?.focus();
      });
    }
    effort.querySelectorAll('input').forEach((input,i)=>{
      input.checked=input.value===selected.thinking_level;input.disabled=locked||levels.length<2;
      input.closest('label').classList.toggle('filled',selected.thinking_level!=='off'&&i<=index);
    });
    panel.querySelector('.effort-value').textContent=effortName(selected.thinking_level);
    panel.querySelector('.effort-endpoints').innerHTML=levels.length>1?`<span>${escape(effortName(levels[0]))}</span><span>${escape(effortName(levels.at(-1)))}</span>`:'';
    panel.querySelector('.effort-help').textContent=busy()?'Available after this reply.':levels.length<2?'This model has no adjustable thinking effort.':'Higher effort gives the model more time to think.';
    approval.value=approvalModes.some(([id])=>id===selected.approval_mode)?selected.approval_mode:'auto';approval.disabled=saving;
    approval.style.width=`calc(${approval.selectedOptions[0].text.length}ch + 54px)`;
    approval.title=approvalModes.find(([id])=>id===approval.value)[2];
    // Full access is marked for as long as it is on, and its meaning is available without hover.
    approval.closest('label').classList.toggle('is-full',approval.value==='full');
    root.querySelector('#composer-approval-help').textContent=approval.title;
    list.querySelectorAll('button').forEach(item=>item.disabled=locked);
    panel.querySelector('.model-picker-hint').textContent=busy()?'Wait for this reply to change models.':'Applies to this conversation.';
  }
  async function change(values){
    if(saving)return;
    saving=true;update();
    try{await save(values);}catch(error){toast(error.message,true);}finally{saving=false;update();}
  }
  function options(){
    const selected=selectedChoice(),models=connectedModels(providers,search.value);
    list.innerHTML=loading?'<p class="picker-empty">Loading models…</p>':models.map((model,index)=>`<button type="button" data-model-index="${index}" class="model-option" ${saving||busy()?'disabled':''} aria-pressed="${model.provider===selected.provider&&model.id===selected.model}"><span><strong>${escape(model.name)}</strong><small>${escape(model.providerName)}</small></span>${model.provider===selected.provider&&model.id===selected.model?icon('check'):''}</button>`).join('')||'<p class="picker-empty">No connected models found. Add a connection in Settings.</p>';
    list.querySelectorAll('[data-model-index]').forEach(item=>item.onclick=async()=>{
      const model=models[Number(item.dataset.modelIndex)],levels=model.thinkingLevels||['off'];
      await change({provider:model.provider,model:model.id,thinking_level:levels.includes(selected.thinking_level)?selected.thinking_level:levels[0]});
      if(!current())return;
      options();
      (effort.querySelector('input:checked:not(:disabled)')||search).focus();
    });
  }
  button.onclick=()=>{
    if(panel.matches(':popover-open')){panel.hidePopover();return;}
    options();const rect=button.getBoundingClientRect(),width=Math.min(350,innerWidth-24),above=rect.top>280;
    Object.assign(panel.style,{width:`${width}px`,left:`${Math.max(12,Math.min(rect.left,innerWidth-width-12))}px`,top:above?'auto':`${rect.bottom+8}px`,bottom:above?`${innerHeight-rect.top+8}px`:'auto',maxHeight:`${Math.max(160,Math.min(460,above?rect.top-24:innerHeight-rect.bottom-24))}px`});
    panel.showPopover();search.focus();
  };
  panel.addEventListener('toggle',()=>button.setAttribute('aria-expanded',String(panel.matches(':popover-open'))));
  panel.addEventListener('keydown',event=>{
    if(event.key==='Escape'){panel.hidePopover();button.focus();}
    // The picker sits inside the composer form. Enter here must choose a model, never send the draft.
    if(event.key==='Enter'&&event.target.matches('input')){event.preventDefault();if(event.target===search)list.querySelector('button:not(:disabled)')?.click();}
    if(['ArrowDown','ArrowUp'].includes(event.key)&&(event.target===search||event.target.closest('.model-option'))){
      const items=[search,...list.querySelectorAll('button:not(:disabled)')],index=items.indexOf(document.activeElement);
      event.preventDefault();items[(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
    }
  });
  search.oninput=options;
  approval.onchange=async()=>{
    const next=approval.value,previous=selectedChoice().approval_mode||'auto';
    if(next==='full'&&previous!=='full'&&confirm&&!await confirm({title:'Give this chat full access?',body:'The bot will run commands and browser actions without asking first, including ones that change or delete data. It stays on for this conversation until you change it.',confirm:'Use full access',danger:true})){if(current())update();return;}
    change({approval_mode:next});
  };
  panel.querySelector('[data-model-settings]').onclick=()=>{panel.hidePopover();settings();};
  update();
  api('/api/providers').then(data=>{if(!current())return;providers=data;loading=false;update();options();}).catch(error=>{if(!current())return;loading=false;update();list.innerHTML=`<p class="picker-empty">${escape(error.message)}</p>`;});
  return {update,selection:selectedChoice,get saving(){return saving;}};
}
