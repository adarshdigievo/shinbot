import {projectTabs, bindProjectTabs} from './project-tabs.js';

export function projectsView({api, state, $, escape, icon, badge, dialog, confirm, field, bind, action, toast, navigate, pageScope, inbox}) {
  let selectedProject = null, selectedWorkspace = null, renderVersion = 0, mode = "coding";

  async function openChat(task) {
    state.task = task.id;
    state.bot = task.bot_id || state.bot;
    await navigate('chat');
  }

  async function render() {
    const version = ++renderVersion, scope = pageScope('projects');
    if (mode === "inbox") return inbox.render({showCoding: async () => {mode="coding"; await render();}, isActive: () => mode === 'inbox'});
    const current = () => scope() && version === renderVersion;
    const data = await api('/api/projects');
    if (!current()) return;
    if (!data.projects.some(p => p.id === selectedProject)) selectedProject = data.projects[0]?.id || null;
    const project = data.projects.find(p => p.id === selectedProject);
    const workspaces = project ? await api(`/api/projects/${project.id}/workspaces`) : [];
    if (!current()) return;
    $('#page').className = 'page projects-page';
    $('#page').innerHTML = `
      <div class="page-head"><div><h1>Projects</h1><p class="subhead">Give each task a place to work.</p></div>
        <button id="add-project">${icon('folder-plus')} Add repository</button></div>
      ${inbox ? projectTabs('coding') : ''}
      <section id="project-view-panel" ${inbox ? 'role="tabpanel" aria-labelledby="coding-workspaces"' : ''}>
      <div class="projects-layout">
        <aside class="projects-rail" aria-label="Repositories">
          <h2>Repositories</h2>
          ${data.projects.map(p => `<button class="project-entry ${p.id === selectedProject ? 'selected' : ''}" data-project="${escape(p.id)}" ${p.id === selectedProject ? 'aria-current="true"' : ''}>${icon('folder-git-2')}<span>${escape(p.name)}<small>${escape(p.path)}</small></span></button>`).join('') || '<p class="muted">Your repositories will appear here.</p>'}
          <p class="hint">Folders are on ShinBot’s Computer. Clone or copy a repository there first.</p>
        </aside>
        <section class="project-main" aria-label="Project tasks">
          ${project ? `<div class="project-heading"><div><h2>${escape(project.name)}</h2><p><code>${escape(project.path)}</code></p></div><button class="primary" id="new-project-task">${icon('plus')} New task</button></div>
            <div class="row between project-toolbar"><p class="muted">Each task has its own worktree and conversation. Bots can work on several tasks at once.</p><button class="quiet icon-button" id="refresh-project" aria-label="Refresh project">${icon('refresh-cw')}</button></div>
            <div class="project-tasks">${workspaces.map(w => `<article data-project-task="${escape(w.task_id||'')}" class="project-task ${w.id === selectedWorkspace ? 'selected' : ''}">
              <div class="project-task-body"><button class="project-task-title" data-workspace="${escape(w.id)}">${escape(w.display_title || w.title || w.prompt)}</button>
              <p>${escape(state.bots.find(b => b.id === w.bot_id)?.name || 'Bot')}${w.parent_task_id ? ' · Delegated task' : ''}</p>
              <small>${icon('git-branch')} ${escape(w.branch)}</small>${w.task_error ? `<p class="error-text">${escape(w.task_error)}</p>` : ''}</div>
              <div class="project-task-controls">${badge(w.state === 'ready' ? w.task_status : w.state)}
                ${w.task_id ? `<button class="small-button" data-project-chat="${escape(w.task_id)}">Open chat</button>` : ''}${w.state === 'ready' || w.state === 'archived' ? '' : ['archiving','abandoned'].includes(w.state) ? `<button class="small-button" data-retry-archive="${escape(w.id)}">${w.state==='abandoned'?'Archive unused worktree':'Retry archive'}</button>` : w.state === 'preparing' ? `<button class="small-button" data-retry-workspace="${escape(w.id)}">Retry setup</button>` : ''}
              </div></article>`).join('') || `<div class="project-empty">${icon('git-branch')}<h3>Start a task on ${escape(project.name)}</h3><p>Choose a bot and describe a change. Start a second task to work on another feature at the same time.</p><p class="hint">Your existing checkout stays in place. Uncommitted changes are not copied to new worktrees.</p></div>`}</div>
            <div id="workspace-inspector"></div>` : `<div class="project-empty">${icon('folder-git-2')}<h2>Choose a repository to begin</h2><p>Add an existing Git repository from the Computer workspace. Then start coding tasks, inspect their files and follow each bot’s progress.</p><button id="first-project" class="primary">Add repository</button></div>`}
        </section>
      </div></section>`;
    if (inbox) bindProjectTabs({active:'coding', select:async view=>{mode=view;await render();}, $, action, toast});
    action($('#add-project'), addRepository);
    action($('#first-project'), addRepository);
    action($('#refresh-project'), render);
    action($('#new-project-task'), () => taskForm(project));
    document.querySelectorAll('[data-project]').forEach(b => action(b, async () => {selectedProject = b.dataset.project; selectedWorkspace = null; await render();}));
    document.querySelectorAll('[data-workspace]').forEach(b => action(b, () => inspect(workspaces.find(w => w.id === b.dataset.workspace))));
    document.querySelectorAll('[data-project-chat]').forEach(b => action(b, () => openChat({id: b.dataset.projectChat, bot_id: workspaces.find(w => w.task_id === b.dataset.projectChat).bot_id})));
    document.querySelectorAll('[data-retry-workspace]').forEach(b => action(b, async () => {await api(`/api/project-workspaces/${b.dataset.retryWorkspace}/retry`, 'POST', {}); await render();}));
    document.querySelectorAll('[data-retry-archive]').forEach(b=>action(b,async()=>{await api(`/api/project-workspaces/${b.dataset.retryArchive}/archive`,'POST',{});await render();}));
    const selected = workspaces.find(w => w.id === selectedWorkspace);
    if (selected?.state === 'ready') await inspect(selected);
  }

  async function addRepository() {
    const d = dialog('Add a repository', `<p class="muted">Choose a Git repository on ShinBot’s Computer. These folders belong to the server, not the device you are browsing from.</p>
      <form class="project-folder-form">${field('Folder in Computer', 'path', '.', 'text', 'required')}<button type="submit">Open folder</button></form>
      <div id="project-folders" aria-live="polite"></div>`);
    const list = d.querySelector('#project-folders'), input = d.querySelector('[name=path]');
    let browseVersion = 0;
    async function browse(path) {
      const version = ++browseVersion;
      const result = await api('/api/projects/folders?path=' + encodeURIComponent(path));
      if (!d.isConnected || version !== browseVersion) return;
      input.value = result.path;
      list.innerHTML = `${result.path !== '.' ? '<button class="quiet" id="project-parent">' + icon('arrow-up') + ' Parent folder</button>' : ''}
        ${result.repository ? '<button id="select-repository" class="primary">Use this repository</button>' : ''}
        <div class="project-folder-list">${result.folders.map((f, i) => `<button data-folder="${i}">${icon(f.repository ? 'folder-git-2' : 'folder')}<span>${escape(f.name)}</span>${icon('chevron-right')}</button>`).join('') || '<p class="muted">No subfolders. Clone a repository in Computer, then open its folder here.</p>'}</div>`;
      action(d.querySelector('#project-parent'), () => browse(result.path.split('/').slice(0, -1).join('/') || '.'));
      action(d.querySelector('#select-repository'), async () => {const p = await api('/api/projects', 'POST', {path: result.path}); selectedProject = p.id; selectedWorkspace = null; d.close(); await render();});
      list.querySelectorAll('[data-folder]').forEach(b => action(b, () => browse(result.folders[Number(b.dataset.folder)].path)));
    }
    bind(d.querySelector('form'), values => browse(values.path));
    try {await browse('.');} catch (e) {toast(e.message, true);}
  }

  function taskForm(project) {
    let requestId = crypto.randomUUID();
    const d = dialog('New task in ' + project.name, `<form>${field('Task name', 'title', '', 'text', 'required maxlength="160" placeholder="Add account settings"')}
      <label>Bot<select name="bot_id">${state.bots.map(b => `<option value="${escape(b.id)}" ${b.id === state.bot ? 'selected' : ''}>${escape(b.name)}</option>`).join('')}</select></label>
      <label>What should the bot do?<textarea name="prompt" rows="5" required maxlength="20000" placeholder="Describe the change and how to check it."></textarea></label>
      <p class="hint">Creates a branch and worktree from the repository’s committed HEAD. The bot follows your current approval settings. You can start more tasks while this one runs.</p>
      <div class="form-footer"><button type="submit" class="primary">Create worktree and start</button></div></form>`, {guard: true});
    let submitted = null;
    bind(d.querySelector('form'), async values => {
      // A failed response keeps the same request and input, so retry cannot create a second task.
      if (submitted && JSON.stringify(values) !== JSON.stringify(submitted)) throw new Error('The previous request may have started. Check Projects before changing this task. Retry with the original details to recover it.');
      submitted = values;
      let task;
      try {task = await api(`/api/projects/${project.id}/tasks`, 'POST', {...values, request_id: requestId});}
      catch(error) {if(error.requestStored === false){submitted = null; requestId = crypto.randomUUID();}throw error;}
      selectedWorkspace = task.workspace_id;
      d.close();
      toast('Task started in its own worktree.');
      await render();
    });
  }

  async function inspect(workspace) {
    selectedWorkspace = workspace.id;
    const current = pageScope('projects');
    if (workspace.state !== 'ready') {toast(workspace.state === 'archived' ? 'This worktree is archived. Its branch and conversation are kept.' : 'Worktree setup has not finished. Use Retry setup to continue.'); return;}
    const details = await api(`/api/project-workspaces/${workspace.id}/changes`);
    if (!current() || selectedWorkspace !== workspace.id || !$('#workspace-inspector')) return;
    const container = $('#workspace-inspector');
    container.innerHTML = `<section class="workspace-inspector"><div class="row between"><h3>${escape(workspace.display_title || workspace.title || workspace.prompt)}</h3><button class="quiet icon-button" id="close-inspector" aria-label="Close worktree details">${icon('x')}</button></div>
      <p class="hint">Computer folder: <code>${escape(workspace.path)}</code></p>
      ${workspace.parent_task_id ? '<button id="parent-task" class="small-button">Open requesting conversation</button>' : ''}
      ${workspace.children.length ? `<div class="project-children"><h4>Delegated tasks</h4>${workspace.children.map(c => `<button data-child-task="${escape(c.id)}">${escape(c.title)} ${badge(c.status)}</button>`).join('')}</div>` : ''}
      <div class="workspace-tabs" role="tablist" aria-label="Worktree details"><button role="tab" id="changes-tab" aria-controls="changes-panel" aria-selected="true" tabindex="0">Changes</button><button role="tab" id="files-tab" aria-controls="files-panel" aria-selected="false" tabindex="-1">Files</button></div>
      <div id="changes-panel" role="tabpanel" aria-labelledby="changes-tab"><p class="hint">Compared with the starting commit. New untracked files appear in status; open them under Files.</p><pre class="project-code">${escape(details.status || 'Working tree clean.')}</pre>${details.patch ? `<pre class="project-code project-patch">${escape(details.patch)}</pre>` : '<p class="muted">No tracked changes yet.</p>'}</div>
      <div id="files-panel" role="tabpanel" aria-labelledby="files-tab" hidden><div id="worktree-files"></div></div>
      <div class="workspace-footer"><p class="hint">Archiving removes a clean worktree and keeps its branch and chat.</p><button id="archive-workspace" class="small-button">Archive worktree</button></div></section>`;
    action($('#close-inspector'), () => {selectedWorkspace = null; container.innerHTML = '';});
    action($('#parent-task'), async () => openChat(await api(`/api/tasks/${workspace.parent_task_id}`)));
    container.querySelectorAll('[data-child-task]').forEach(b => action(b, () => openChat(workspace.children.find(c => c.id === b.dataset.childTask))));
    let loadedFiles = false, filesVersion = 0;
    async function tab(name) {
      for (const id of ['changes', 'files']) {const button = $('#' + id + '-tab'); button.setAttribute('aria-selected', String(id === name)); button.tabIndex = id === name ? 0 : -1; $('#' + id + '-panel').hidden = id !== name;}
      if (name === 'files' && !loadedFiles) {await files('.'); loadedFiles = true;}
    }
    for (const name of ['changes', 'files']) {
      action($('#' + name + '-tab'), () => tab(name));
      $('#' + name + '-tab').addEventListener('keydown', async event => {if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return; event.preventDefault(); const target = event.key === 'Home' ? 'changes' : event.key === 'End' ? 'files' : name === 'files' ? 'changes' : 'files'; $('#' + target + '-tab').focus(); try {await tab(target);} catch(e) {toast(e.message, true);}});
    }
    async function files(path) {
      const version = ++filesVersion;
      const result = await api(`/api/project-workspaces/${workspace.id}/files?file=${encodeURIComponent(path)}`);
      if (!current() || selectedWorkspace !== workspace.id || version !== filesVersion || !$('#worktree-files')) return;
      const target = $('#worktree-files');
      target.innerHTML = `<p><code>${escape(path)}</code></p>${path !== '.' ? '<button id="files-parent" class="quiet">' + icon('arrow-up') + ' Parent folder</button>' : ''}<div class="project-folder-list">${result.files.filter(f => f.name !== '.git').map((f, i) => `<button data-worktree-file="${escape(f.name)}" data-directory="${f.directory}">${icon(f.directory ? 'folder' : 'file-text')}${escape(f.name)}</button>`).join('')}</div><pre id="worktree-file-content" class="project-code" hidden></pre>`;
      action($('#files-parent'), () => files(path.split('/').slice(0, -1).join('/') || '.'));
      target.querySelectorAll('[data-worktree-file]').forEach(b => action(b, async () => {
        const name = (path === '.' ? '' : path + '/') + b.dataset.worktreeFile;
        if (b.dataset.directory === 'true') return files(name);
        const result = await api(`/api/project-workspaces/${workspace.id}/read?file=${encodeURIComponent(name)}`);
        if (!current() || selectedWorkspace !== workspace.id || !$('#worktree-file-content')) return;
        $('#worktree-file-content').hidden = false; $('#worktree-file-content').textContent = result.text;
      }));
    }
    action($('#archive-workspace'), async () => {
      if (!await confirm({title: 'Archive this worktree?', body: 'The branch and conversation stay available. ShinBot will refuse to remove files with changes, including untracked or ignored files.', confirm: 'Archive worktree'})) return;
      await api(`/api/project-workspaces/${workspace.id}/archive`, 'POST', {}); selectedWorkspace = null; await render();
    });
  }
  function update(tasks){
    if(state.view!=='projects')return;
    document.querySelectorAll('[data-project-task]').forEach(el=>{
      const task=tasks.find(t=>t.id===el.dataset.projectTask),pill=el.querySelector('.pill');
      if(task&&pill&&!['archived','archiving','preparing'].includes(pill.dataset.status)&&pill.dataset.status!==task.status)pill.outerHTML=badge(task.status);
    });
  }
  async function openInbox(itemId) {
    mode='inbox';
    await navigate('projects');
    if (state.view==='projects' && mode==='inbox' && itemId) await inbox.openItem(itemId);
  }
  return {render,update,openInbox};
}
