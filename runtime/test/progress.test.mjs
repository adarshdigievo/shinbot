import assert from 'node:assert/strict';
import test from 'node:test';
import {liveProgress} from '../dist/progress.js';

test('progress reports tool and generation phases without exposing hidden reasoning', () => {
  assert.equal(liveProgress().phase,'idle');
  const live={run:{},generation:{message:{timestamp:123000,content:[{type:'thinking',thinking:'private reasoning'},{type:'text',text:'Checking the page.'}]}}};
  const result=liveProgress(live);
  assert.equal(result.phase,'writing');assert.equal(result.text,'Checking the page.');assert.equal(result.created,123);
  assert.ok(!JSON.stringify(result).includes('private reasoning'));
  live.tools=[{callId:'one',name:'browser',status:'running',details:{secret:'not a UI field'}}];
  assert.equal(liveProgress(live).phase,'tool');
  assert.deepEqual(liveProgress(live).tools,[{callId:'one',name:'browser',status:'running'}]);
  live.compactions=[{}];assert.equal(liveProgress(live).phase,'compacting');
});
