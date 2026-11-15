import {scheduleForm,scheduleLabel} from './schedule.js?v=20261114-review-1';

export function jobState(job,now=Date.now()){
  if(job.archived)return {label:'Archived',tone:'paused',reschedule:false};
  const ended=job.kind==='once'&&(!job.next_run||new Date(job.spec).getTime()<=now);
  if(job.last_error)return {label:'Needs attention',tone:'failed',reschedule:ended};
  if(ended)return {label:job.next_run?'Needs a new time':'Finished',tone:'paused',reschedule:true};
  return {label:job.enabled?'Scheduled':'Paused',tone:job.enabled?'active':'paused',reschedule:false};
}

export function jobsView({api,state,$,dialog,confirm,badge,escape,icon,field,bind,action,toast,date,pageScope,openChat}){
  let archived=false;
  async function render(){
    const current=pageScope('jobs');if(!current())return;
    const jobs=await api('/api/jobs'+(archived?'?archived=true':''));if(!current())return;
    $('#page').className='page jobs-page';
    $('#page').innerHTML=`<div class="page-head"><div><h1>${archived?'Archived jobs':'Jobs'}</h1><p class="subhead">${archived?'Restore a job to change its schedule or run it again. Past conversations stay available.':'Run a task once or on a schedule. Jobs continue while ShinBot is running, even with this page closed.'}</p></div><div class="row"><button id="job-filter" class="quiet">${archived?'Back to jobs':'Archived jobs'}</button>${archived?'':'<button class="primary" id="new-job">'+icon('plus')+' New job</button>'}</div></div><div class="stack">${jobs.map(job=>{const status=jobState(job);return `<article class="job-row"><div class="job-content"><div class="row between"><h2>${escape(job.name)}</h2><span class="pill ${status.tone}">${status.label}</span></div><p>${escape(job.prompt)}</p><p class="muted"><small>${escape(scheduleLabel(job))} · ${escape(job.timezone)} · ${escape(state.bots.find(b=>b.id===job.bot_id)?.name||'Archived bot')}<br>${job.enabled&&job.next_run?'Next run: '+escape(date(job.next_run)):job.kind==='once'?'Scheduled for: '+escape(date(Date.parse(job.spec)/1000)):'Future runs are paused.'}</small></p>${job.last_error?`<p class="notice">${escape(job.last_error)}</p>`:''}<div class="row">${archived?`<button data-restore-job="${job.id}">Restore</button>`:`${status.reschedule?`<button data-reschedule-job="${job.id}">Choose a new time</button>`:`<button data-toggle-job="${job.id}" data-enabled="${job.enabled?0:1}">${job.enabled?'Pause':'Resume'}</button><button data-edit-job="${job.id}" class="quiet">Edit</button>`}<button data-archive-job="${job.id}" class="quiet">Archive</button>`}<button data-history="${job.id}" class="quiet">History</button></div></div></article>`;}).join('')||`<div class="state-panel">${icon('clock')}<h2>${archived?'No archived jobs':'No jobs scheduled'}</h2><p>${archived?'Jobs you archive will appear here.':'Ask a bot to schedule a task, or create one here.'}</p></div>`}</div>`;
    const form=scheduleForm({api,state,dialog,escape,field,bind,action,toast,date,done:render});
    action($('#new-job'),()=>form());
    action($('#job-filter'),()=>{archived=!archived;return render();});
    for(const button of $('#page').querySelectorAll('[data-toggle-job]'))action(button,async()=>{await api(`/api/jobs/${button.dataset.toggleJob}/toggle`,'POST',{enabled:button.dataset.enabled==='1'});await render();});
    for(const button of $('#page').querySelectorAll('[data-edit-job]'))action(button,()=>form(jobs.find(j=>j.id===button.dataset.editJob)));
    for(const button of $('#page').querySelectorAll('[data-reschedule-job]'))action(button,()=>form(jobs.find(j=>j.id===button.dataset.rescheduleJob),{resume:true}));
    for(const button of $('#page').querySelectorAll('[data-restore-job]'))action(button,async()=>{await api(`/api/jobs/${button.dataset.restoreJob}/restore`,'POST',{});await render();toast('Job restored and paused. Go back to Jobs to resume it.');});
    for(const button of $('#page').querySelectorAll('[data-archive-job]'))action(button,async()=>{
      const job=jobs.find(j=>j.id===button.dataset.archiveJob);
      if(!await confirm({title:'Archive '+job.name+'?',body:'This stops future runs and cancels queued work. Running chats continue. You can restore the job and still open its past results.',confirm:'Archive job'}))return;
      await api('/api/jobs/'+job.id,'DELETE');await render();toast('Job archived.');
    });
    for(const button of $('#page').querySelectorAll('[data-history]'))action(button,async()=>{
      const runs=await api(`/api/jobs/${button.dataset.history}/history`);
      const labels={admitted:'Queued',overlap:'Skipped: an earlier run was still active',missed:'Missed while offline',failed:'Could not start'};
      // A run that reached a chat shows that chat's state. Otherwise the pill takes the tone of what happened to the run.
      const tones={admitted:'queued',overlap:'paused',missed:'paused',failed:'failed'};
      const d=dialog('Run history',runs.map(run=>`<div class="list-item"><div class="row between"><strong>${escape(date(run.intended))}</strong>${run.task_status?badge(run.task_status):badge(tones[run.status]||'paused',labels[run.status]||run.status)}</div>${run.error||run.task_error?`<p class="error-text">${escape(run.error||run.task_error)}</p>`:''}${run.answer?`<p>${escape(run.answer.slice(0,280))}${run.answer.length>280?'…':''}</p>`:''}${run.task_id?`<button data-job-chat="${escape(run.task_id)}">${run.task_status==='waiting'?'Review approval':'Open result'}</button>`:''}</div>`).join('')||'<p class="muted">No runs yet. The first result will appear here after this job runs.</p>');
      for(const link of d.querySelectorAll('[data-job-chat]'))action(link,async()=>{await openChat(link.dataset.jobChat);d.close();});
    });
  }
  return {render};
}
