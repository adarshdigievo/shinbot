import test from 'node:test';
import assert from 'node:assert/strict';
import { withConnectionRecovery, TemporaryConnectionError, durableRetry } from '../dist/recovery.js';
import { retryDelayMs, isRetryableAssistantError } from '@earendil-works/pi-ai/utils/retry';

test('a lost internal response retries the same closure and can be cancelled while offline', async () => {
  let calls=0;
  const states=[];
  assert.equal(await withConnectionRecovery(async()=>{
    if(++calls===1) throw new TypeError('fetch failed');
    return 'original receipt';
  },undefined,async state=>{states.push(state);}), 'original receipt');
  assert.deepEqual(states,[true,false]);
  const abort = new AbortController();
  await assert.rejects(withConnectionRecovery(async()=>{throw new TemporaryConnectionError();},abort.signal,async()=>{abort.abort();}));
});

test('permanent errors are not retried and durable backoff stays bounded after a long outage', async () => {
  let calls=0;
  await assert.rejects(withConnectionRecovery(async()=>{calls++;throw new Error('Unauthorized');}),/Unauthorized/);
  assert.equal(calls,1);
  assert.equal(retryDelayMs(durableRetry,10000),30000);
  assert.equal(isRetryableAssistantError({stopReason:'error',errorMessage:'401 invalid_api_key'}),false);
  assert.equal(isRetryableAssistantError({stopReason:'error',errorMessage:'insufficient_quota billing'}),false);
  assert.equal(isRetryableAssistantError({stopReason:'error',errorMessage:'Connection error.'}),true);
});
