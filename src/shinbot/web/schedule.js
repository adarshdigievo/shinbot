import {formatTime} from './time.js';
const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

export function schedulePreset(job){
  if(!job)return {mode:'weekdays',time:'09:00',weekday:'1',amount:1,unit:3600};
  if(job.kind==='interval')return {mode:'interval',amount:Number(job.spec)%(86400)===0?Number(job.spec)/86400:Number(job.spec)%3600===0?Number(job.spec)/3600:Number(job.spec)/60,unit:Number(job.spec)%86400===0?86400:Number(job.spec)%3600===0?3600:60};
  if(job.kind==='once')return {mode:'once'};
  const match=/^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|[0-6])$/.exec(job.spec);
  if(match)return {mode:match[3]==='*'?'daily':match[3]==='1-5'?'weekdays':'weekly',time:match[2].padStart(2,'0')+':'+match[1].padStart(2,'0'),weekday:match[3]};
  return {mode:'custom'};
}

export function scheduleInput(values){
  const timezone=values.timezone;
  if(values.mode==='once')return {kind:'once',local_time:values.once,timezone};
  if(values.mode==='interval')return {kind:'interval',spec:String(Number(values.amount)*Number(values.unit)),timezone};
  if(values.mode==='custom')return {kind:'cron',spec:values.custom,timezone};
  const [hour,minute]=(values.time||'').split(':');
  return {kind:'cron',spec:`${Number(minute)} ${Number(hour)} * * ${values.mode==='daily'?'*':values.mode==='weekdays'?'1-5':values.weekday}`,timezone};
}

export function scheduleLabel(job){
  const preset=schedulePreset(job);
  if(preset.mode==='once')return 'One time';
  if(preset.mode==='custom')return 'Custom schedule: '+job.spec;
  if(preset.mode==='interval')return `Every ${preset.amount} ${preset.unit===86400?'day':preset.unit===3600?'hour':'minute'}${preset.amount===1?'':'s'}`;
  return `${preset.mode==='daily'?'Every day':preset.mode==='weekdays'?'Weekdays':'Every '+days[Number(preset.weekday)]} at ${preset.time}`;
}

export function scheduleForm({api,state,dialog,escape,field,bind,action,toast,date,done}){
  return function open(job,{resume=false}={}){
    const preset=schedulePreset(job),zone=job?.timezone||state.settings.timezone;
    const parts=job?.kind==='once'&&!resume?new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(job.spec)).replace(' ','T'):'';
    const d=dialog(job?'Edit job':'Create a job',`<form><div class="form-grid">${field('Name','name',job?.name||'','text','required maxlength="200" placeholder="Morning briefing"')}<label>Bot<select name="bot_id">${state.bots.map(bot=>`<option value="${escape(bot.id)}" ${bot.id===(job?.bot_id||state.bot)?'selected':''}>${escape(bot.name)}</option>`).join('')}</select></label><label class="wide">What should the bot do?<textarea name="prompt" required maxlength="20000" placeholder="Summarize the latest updates and save a short note.">${escape(job?.prompt||'')}</textarea></label><label>Repeat<select name="mode">${[['weekdays','Every weekday'],['daily','Every day'],['weekly','Every week'],['interval','At an interval'],['once','One time'],['custom','Custom cron']].map(([id,label])=>`<option value="${id}" ${preset.mode===id?'selected':''}>${label}</option>`).join('')}</select></label>${field('Timezone','timezone',zone,'text','required list="schedule-timezones"')}<datalist id="schedule-timezones"><option value="UTC">${(Intl.supportedValuesOf?.('timeZone')||[]).map(z=>`<option value="${escape(z)}">`).join('')}</datalist><div data-schedule="daily weekdays weekly">${field('Time','time',preset.time||'09:00','time','required')}</div><label data-schedule="weekly">Day<select name="weekday">${days.map((day,i)=>`<option value="${i}" ${String(i)===(preset.weekday||'1')?'selected':''}>${day}</option>`).join('')}</select></label><div class="wide" data-schedule="once">${field('Date and time','once',parts,'datetime-local','required')}</div><div data-schedule="interval">${field('Every','amount',preset.amount||1,'number','min="1" step="any" required')}</div><label data-schedule="interval">Unit<select name="unit">${[[60,'Minutes'],[3600,'Hours'],[86400,'Days']].map(([value,label])=>`<option value="${value}" ${Number(value)===Number(preset.unit||3600)?'selected':''}>${label}</option>`).join('')}</select></label><div class="wide" data-schedule="custom">${field('Cron expression','custom',job?.kind==='cron'?job.spec:'0 9 * * 1-5','text','required')}<p class="hint">Minute, hour, day, month, weekday. For example, 0 9 * * 1-5 means weekdays at 09:00.</p></div></div><p data-preview class="connection-progress" role="status">Checking the next run…</p><p class="hint">Runs while ShinBot is online. After downtime, it runs at most one missed job. New jobs ask before commands or website changes.</p><div class="form-footer"><button class="primary" type="submit">Save job</button></div></form>`);
    const form=d.querySelector('form');let timer,generation=0;
    function fields(){
      for(const group of form.querySelectorAll('[data-schedule]')){group.hidden=!group.dataset.schedule.split(' ').includes(form.elements.mode.value);group.querySelectorAll('input,select').forEach(input=>input.disabled=group.hidden);}
    }
    async function preview(){
      const current=++generation,values=Object.fromEntries(new FormData(form));
      try{const result=await api('/api/jobs/preview','POST',scheduleInput(values));if(d.open&&generation===current){d.querySelector('[data-preview]').textContent='Next run: '+formatTime(result.next_run,result.timezone)+' ('+result.timezone+')'+(result.note?' '+result.note:'');d.querySelector('[data-preview]').classList.remove('error-text');}}
      catch(error){if(d.open&&generation===current){d.querySelector('[data-preview]').textContent=error.message;d.querySelector('[data-preview]').classList.add('error-text');}}
    }
    form.addEventListener('input',()=>{fields();clearTimeout(timer);generation++;d.querySelector('[data-preview]').textContent='Checking the next run…';timer=setTimeout(preview,300);});
    d.addEventListener('close',()=>{clearTimeout(timer);generation++;});fields();preview();
    bind(form,async values=>{const schedule=await api('/api/jobs/preview','POST',scheduleInput(values));await api('/api/jobs','POST',{name:values.name,prompt:values.prompt,bot_id:values.bot_id,kind:schedule.kind,spec:schedule.spec,timezone:schedule.timezone,...(job?{id:job.id,grants:JSON.parse(job.grants),...(resume?{enabled:true}:{})}:{grants:{}})});d.close();await done();toast(job&&!job.enabled&&!resume?'Job saved and paused. Resume it when ready.':'Job saved. Next run: '+formatTime(schedule.next_run,schedule.timezone));});
  };
}
