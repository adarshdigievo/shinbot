import test from 'node:test';
import assert from 'node:assert/strict';
import { admissionError, submissionError } from '../dist/errors.js';

test('budget failures identify the exhausted limit without assuming a board card', () => {
  for (const [scope, expected] of [
    ['workspace_message', /workspace token budget per message/],
    ['workspace_daily', /past 24 hours/],
    ['saved_chat', /saved when this chat started/],
    ['card', /board card reached its total token budget/],
    ['saved_run', /run reached its saved token budget/],
  ]) {
    const detail = admissionError({budget_scope:scope,error:'PRIVATE-KEY'});
    const message = submissionError('admission_denied', detail);
    assert.match(message, expected);
    assert.doesNotMatch(message, /PRIVATE-KEY/);
    if (!['card','saved_run'].includes(scope)) assert.doesNotMatch(message, /card|board/i);
  }
  for (const reason of ['admission_denied','model_error']) {
    const message = submissionError(reason, 'The configured token budget has been reached. PRIVATE-KEY');
    assert.match(message, /configured token budget/);
    assert.doesNotMatch(message, /card|board|PRIVATE-KEY/i);
  }
  assert.doesNotMatch(submissionError('model_error', admissionError({budget_scope:'card'}) + ' PRIVATE-KEY'), /board|card|PRIVATE-KEY/i);
});

test('model failures give an action to take without returning provider diagnostics or credentials', () => {
  for (const [detail, hint] of [
    ['401 invalid_api_key: PRIVATE-KEY sent to https://provider.example?token=PRIVATE-KEY', /Reconnect.*Settings/],
    ['insufficient_quota account=PRIVATE-KEY', /provider account/],
    ['The configured token budget has been reached. PRIVATE-KEY', /token budget/],
    ['Unrecognized response with PRIVATE-KEY and private user data', /provider connection in Settings/],
  ]) {
    const message = submissionError('model_error', detail);
    assert.match(message, hint);
    assert.doesNotMatch(message, /PRIVATE-KEY|provider\.example|private user data|model_error/);
  }
  assert.match(submissionError('no_model'), /Choose a connected provider/);
  assert.match(submissionError('aborted'), /stopped/);
});

test('empty or truncated answers cannot be reported as completed work', async () => {
  const { answerError } = await import('../dist/errors.js');
  assert.match(answerError([{role:'assistant',stopReason:'length'}], 'Partial reply'), /response limit/);
  assert.match(answerError([{role:'assistant',stopReason:'stop'}], '  '), /without a reply/);
  assert.equal(answerError([{role:'assistant',stopReason:'stop'}], 'Verified result'), undefined);
});
