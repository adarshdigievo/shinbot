// A deliberate refusal of the caller's request. Everything else the runtime throws is reported as a 500.
export function rejected(message: string, statusCode = 400) {
  return Object.assign(new Error(message), {statusCode});
}

export function errorReply(error: any) {
  const code = Number(error?.statusCode);
  return {statusCode: Number.isInteger(code) && code >= 400 && code < 600 ? code : 500,
    body: {error: String(error?.message || 'The runtime could not complete this request.').slice(0, 500)}};
}

export function answerError(messages: readonly any[], answer: string): string | undefined {
  const last = messages.filter(message => message.role === 'assistant').at(-1);
  if (last?.stopReason === 'length') return 'The model reached its response limit before finishing. Ask it to continue from the last confirmed action, or use a model with a larger output limit.';
  if (!answer.trim()) return 'The model ended without a reply. Check the completed actions, then ask it to continue from there.';
  return undefined;
}

const budgetMessages = new Map([
  ['workspace_message', 'This message reached the workspace token budget per message. Raise or clear Token cap per message in Settings → Workspace, then ask the bot to continue.'],
  ['workspace_daily', 'The workspace reached its token budget for the past 24 hours. Wait for earlier usage to fall outside that window, or raise or clear Token cap per 24 hours in Settings → Workspace.'],
  ['card', 'This board card reached its total token budget. Raise or clear Total token budget in the card, then resume it from the Board.'],
  ['saved_chat', 'This message reached the token budget saved when this chat started. New chats use the current Token cap per message in Settings → Workspace; raise or clear that setting if needed before starting a new chat.'],
  ['saved_run', 'This run reached its saved token budget. Check Token cap per message in Settings → Workspace, then resume the card from the Board to use the current limit.'],
]);
const unknownBudget = 'A configured token budget stopped this task. Review your token limits before continuing.';

export function admissionError(result: {budget_scope?: unknown; error?: unknown}): string {
  return budgetMessages.get(String(result.budget_scope)) || (typeof result.error === 'string' ? result.error : 'Budget or task permission stopped this run.');
}

export function submissionError(reason: unknown, detail: unknown): string {
  if (reason === 'admission_denied' && [...budgetMessages.values()].includes(detail as string)) return detail as string;
  const hints = typeof detail === 'string' ? detail.toLowerCase() : '';
  if (/token budget|budget has been reached/.test(hints)) {
    return unknownBudget;
  }
  if (/invalid[_ -]?api[_ -]?key|incorrect api key|authentication|unauthorized|\b401\b/.test(hints)) {
    return 'The model provider rejected the API key or sign-in. Reconnect the selected provider in Settings and try again.';
  }
  if (/quota|insufficient[_ -]?credit|billing|payment required/.test(hints)) {
    return 'The model provider reported a usage or billing limit. Check your provider account or choose another model.';
  }
  if (/rate[_ -]?limit|too many requests|\b429\b/.test(hints)) {
    return 'The model provider is rate limiting requests. Wait briefly and try again, or choose another model.';
  }
  if (/model[_ -]?not[_ -]?found|unknown model|does not exist|unavailable provider or model/.test(hints)) {
    return 'This model is unavailable. Check the model in Settings or the bot profile, then start a new conversation.';
  }
  if (/recovery hold/.test(hints)) {
    return 'ShinBot is holding restored work. Review recovery in Settings before starting a new task.';
  }
  switch (reason) {
    case 'aborted': return 'This task was stopped before it could finish.';
    case 'no_model': return 'Choose a connected provider and model in Settings, then start a new conversation.';
    case 'stale': return 'This task can no longer continue from its saved state. Start a new conversation.';
    case 'model_error': return 'The model provider could not complete this request. Check the selected model and provider connection in Settings, then try again.';
    case 'faulted': return 'ShinBot could not finish this task. Check service status with shinbot doctor, then start a new conversation.';
    default: return 'ShinBot could not finish this task. Check Settings and try again.';
  }
}
