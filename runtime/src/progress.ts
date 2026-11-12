import type { LiveState } from '@earendil-works/pi-durable';

export function liveProgress(live?: LiveState) {
  const message = live?.generation?.message;
  const text = message?.content.filter(part => part.type === 'text').map(part => part.text).join('') || '';
  const compacting = !!live?.compactions?.length;
  const tools = (live?.tools || []).map(({callId, name, status}) => ({callId, name, status}));
  const phase = compacting ? 'compacting' : live?.generation?.retry ? 'retrying'
    : tools.some(tool => tool.status === 'running') ? 'tool'
    : live?.generation ? (text ? 'writing' : 'thinking') : live?.run ? 'preparing' : 'idle';
  return {text, created:message?.timestamp ? message.timestamp / 1000 : undefined, compacting, phase, tools};
}
