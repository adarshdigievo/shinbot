import test from 'node:test';
import assert from 'node:assert/strict';
import {schedulePreset,scheduleInput,scheduleLabel} from '../src/shinbot/web/schedule.js';

test('editing common schedules preserves recurrence and custom cron',()=>{
  for(const spec of ['0 9 * * 1-5','15 18 * * *','30 8 * * 2']){
    const job={kind:'cron',spec};
    assert.equal(scheduleInput({...schedulePreset(job),timezone:'UTC'}).spec,spec);
  }
  assert.equal(schedulePreset({kind:'cron',spec:'0 9 1 * *'}).mode,'custom');
  assert.equal(scheduleInput({mode:'interval',amount:1.5,unit:60}).spec,'90');
  assert.equal(scheduleLabel({kind:'interval',spec:'3600'}),'Every 1 hour');
});
