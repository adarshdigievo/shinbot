const inFlight = new Set(['pending', 'approved', 'executing', 'retrying']);
const verbs = {
  desktop:{observe:'Looking at the desktop',launch:'Opening an application',click:'Clicking on the desktop',type:'Typing on the desktop',press:'Using the keyboard',scroll:'Scrolling',screenshot:'Sharing a desktop screenshot'},
  browser:{goto:'Opening a webpage',observe:'Reading the webpage',click:'Clicking on the page',fill:'Filling a field',press:'Using the keyboard',back:'Going back',close:'Closing the browser',tabs:'Checking browser tabs',screenshot:'Sharing a browser screenshot'},
  notes:{search:'Searching notes',save:'Saving a note',delete:'Deleting a note'},
  memory:{search:'Checking saved preferences',remember:'Saving a preference',forget:'Removing a saved preference'},
  plugins:{search:'Finding a plugin',read_skill:'Reading a skill',stage:'Preparing a plugin',install:'Installing a plugin',activate:'Activating a plugin',learn:'Preparing a reusable skill'},
};
export function actionDescription(action) {
  let args = {};
  try { args = typeof action.args === 'string' ? JSON.parse(action.args) : action.args || {}; } catch {}
  const tool = String(action.tool || 'a tool');
  const name = tool.startsWith('plugin:') ? tool.split(':').slice(3).join(':') : tool;
  let label = verbs[tool]?.[args.operation] || ({run_command:'Running a command',list_files:'Listing files',read_file:'Reading a file',write_file:'Writing a file',attach_file:'Attaching a file',shinbot_help:'Reading ShinBot help',status:'Checking workspace status',delegate:'Delegating work',delegate_task:'Delegating work',list_projects:'Listing projects',card_history:'Reading card history',updates:'Checking for updates'})[name];
  if (!label) label = 'Using ' + name.replaceAll('_', ' ');
  let detail = '';
  if (action.tool === 'browser' && args.url) {try {const url=new URL(args.url);detail=url.hostname+url.pathname;} catch {}}
  else if (['list_files','read_file','write_file','attach_file'].includes(action.tool)) detail=String(args.path || '');
  else if (action.tool === 'run_command') detail=String(args.command || '').split('\n')[0];
  const state = {pending:'Needs approval',approved:'Ready',executing:'Running',retrying:'Waiting to retry',completed:'Completed',failed:'Failed',denied:'Declined',uncertain:'Needs review',cancelled:'Cancelled'}[action.status] || action.status;
  return {label, detail:detail.slice(0,180), state, active:inFlight.has(action.status)};
}
export function currentActivity(task) {
  if (!['running','queued','waiting'].includes(task?.status)) return null;
  if (task.wait_reason) return {label:task.wait_reason,icon:'clock',waiting:true};
  const action = [...(task.actions || [])].reverse().find(action => inFlight.has(action.status));
  if (action) {
    const description=actionDescription(action), waiting=['pending','retrying'].includes(action.status);
    return {...description, label:action.status==='pending'?'Waiting for your approval':action.status==='retrying'?'Waiting to retry the action':description.label, detail:action.status==='pending'?description.label:description.detail,icon:waiting?'clock':'loader-circle',waiting};
  }
  if (task.progress?.compacting) return {label:'Summarizing earlier messages',icon:'loader-circle'};
  if (task.status==='waiting') return {label:'Waiting for your approval',icon:'clock',waiting:true};
  if (task.status==='queued') return {label:'Waiting to start',icon:'clock',waiting:true};
  const tool = task.progress?.tools?.find(tool => tool.status === 'running');
  if (tool) return {...actionDescription({tool:tool.name}),icon:'loader-circle'};
  if (task.progress?.phase==='retrying') return {label:'Waiting for the model connection',icon:'clock',waiting:true};
  return {label:task.progress?.text?'Writing a reply':task.progress?.phase==='thinking'?'Thinking through your request':'Preparing the next step',icon:'loader-circle'};
}
