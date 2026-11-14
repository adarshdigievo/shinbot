export function conversationOwner(task, bots) {
  const id = task.bot_id || 'default';
  return bots.find(bot => bot.id === id) || {id, name:id === 'default' ? 'ShinBot' : 'Unavailable bot', profile:{}};
}

export function recentConversations(tasks, botFilter = 'all') {
  return tasks.filter(task => botFilter === 'all' || (task.bot_id || 'default') === botFilter)
    .sort((a, b) => (b.updated || 0) - (a.updated || 0));
}

export function selectConversation(state, task) {
  state.task = task.id;
  state.bot = task.bot_id || 'default';
  if (state.chatFilter !== 'all' && state.chatFilter !== state.bot) state.chatFilter = 'all';
}

export function conversationSnapshots() {
  let requested = 0, applied = 0, previous = null;
  const active = task => ['running','queued','waiting'].includes(task?.status)
    ? task.messages?.find(message => ['running','dispatching'].includes(message.status)) : null;
  return {
    request: () => ++requested,
    accept(sequence, task) {
      if (sequence <= applied) return null;
      applied = sequence;
      const message = active(task), priorMessage = active(previous);
      // Keep the live reply until this turn has a saved answer or an explicit end state.
      if (message && !message.answer && !message.error && previous?.id === task.id
          && message.id === priorMessage?.id && !task.progress?.text && previous.progress?.text) {
        task = {...task, progress:{...task.progress, text:previous.progress.text, created:previous.progress.created}};
      }
      previous = task;
      return task;
    },
  };
}

export function queuedMessages(task) {
  return (task?.messages || []).filter(message => message.started == null && !message.answer && !message.error
    && (['queued','dispatching'].includes(message.status) || (message.status === 'running' && message.mode === 'steer')))
    .sort((a, b) => Number(b.mode === 'steer') - Number(a.mode === 'steer') || a.created - b.created);
}

export function conversationTimeline(task) {
  const queued = new Set(queuedMessages(task).map(message => message.id));
  let previousEnd = -Infinity;
  const messages = [...(task.messages || [])].filter(message => !queued.has(message.id)).sort((a, b) => a.created - b.created)
    .map(message => {
      const start = message.started ?? (message.mode === 'steer' ? message.created : Math.max(message.created, previousEnd));
      previousEnd = Math.max(previousEnd, message.finished ?? start);
      return {...message, start};
    }).sort((a, b) => a.start - b.start);
  const answerOwners = new Map(messages.filter(message => message.answer_id != null).map(message => [message.answer_id, message.id]));
  const entries = [];
  messages.forEach((message, index) => {
    const id = message.id || index;
    entries.push({kind:'user', key:`user-${id}`, at:message.start, order:1, message});
    // Legacy replies without completion records still belong before the next turn,
    // after the activity recorded for this one. Never change stored chat content.
    const next = messages.slice(index + 1).find(next => next.answer_id == null || next.answer_id !== message.answer_id);
    const at = Math.min(message.finished ?? Infinity, next?.start ?? Infinity);
    const order = next?.start === at || message.finished == null ? 0 : 3;
    if (message.answer && (message.answer_id == null || answerOwners.get(message.answer_id) === message.id)) entries.push({kind:'answer', key:`answer-${id}`, at, order, message});
    if (message.error) entries.push({kind:'error', key:`message-error-${id}`, at, order, message});
  });
  for (const action of task.actions || []) {
    entries.push({kind:'action', key:`action-${action.id}`, at:action.created, order:2, action});
  }
  for (const attachment of task.attachments || []) {
    entries.push({kind:'attachment', key:`attachment-${attachment.id}`, at:attachment.created, order:2.5, attachment});
  }
  if (task.progress?.text && messages.some(message => ['running','dispatching'].includes(message.status))) {
    entries.push({kind:'progress', key:'progress', at:task.progress.created ?? Infinity, order:3, text:task.progress.text});
  }
  return entries.sort((a, b) => a.at - b.at || a.order - b.order);
}

export function conversationRows({tasks, bots, filter, selected, escape, avatar, icon}) {
  return recentConversations(tasks, filter).map(task => {
    const bot = conversationOwner(task, bots);
    const status = {running:'Working', queued:'Queued', waiting:'Needs approval', failed:'Failed', uncertain:'Needs review'}[task.status];
    return `<button data-task="${escape(task.id)}" class="chat-history-item ${task.id === selected ? 'selected' : ''}" data-status="${escape(task.status || '')}" ${task.id === selected ? 'aria-current="true"' : ''} aria-label="${escape(task.title)} · ${escape(bot.name)}${status ? ` · ${escape(status)}` : ''}" title="${escape(task.title)} · ${escape(bot.name)}">${avatar('history-avatar', bot)}<span class="chat-history-copy"><span class="chat-history-title">${escape(task.title)}</span><small><span class="chat-history-bot">${escape(bot.name)}</span>${status ? `<span class="chat-history-status"><span class="status-separator"> · </span>${escape(status)}</span>` : ''}</small></span>${['running','queued'].includes(task.status) ? icon('loader-circle') : ''}</button>`;
  }).join('') || '<p class="sidebar-empty">No chats for this bot yet. Start a new chat to begin.</p>';
}
