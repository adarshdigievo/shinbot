const views = [['coding', 'coding-workspaces', 'Workspaces'], ['inbox', 'show-github-inbox', 'GitHub inbox']];

export function projectTabs(active) {
  return `<div class="workspace-tabs project-view-tabs" role="tablist" aria-label="Project views">${views.map(([view, id, label]) => `<button id="${id}" type="button" role="tab" aria-controls="project-view-panel" aria-selected="${active === view}" tabindex="${active === view ? 0 : -1}">${label}</button>`).join('')}</div>`;
}

export function bindProjectTabs({active, select, $, action, toast}) {
  for (const [view, id] of views) {
    const button = $('#' + id);
    action(button, () => view === active ? undefined : select(view));
    button.onkeydown = async event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? views[0] : event.key === 'End' ? views[1] : views.find(([name]) => name !== active);
      try {
        if (next[0] !== active) await select(next[0]);
        $('#' + next[1])?.focus();
      } catch (error) {toast(error.message, true);}
    };
  }
}
