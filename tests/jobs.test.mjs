import assert from 'node:assert/strict';
import test from 'node:test';
import {jobState} from '../src/shinbot/web/jobs.js';

test('one-time jobs offer rescheduling after completion or a missed time',()=>{
  const past={kind:'once',spec:'2020-01-01T09:00:00Z',enabled:0,next_run:1577869200};
  assert.deepEqual(jobState(past),{label:'Needs a new time',tone:'paused',reschedule:true});
  assert.equal(jobState({...past,next_run:null}).label,'Finished');
  assert.equal(jobState({...past,enabled:1,next_run:null}).reschedule,true);
  assert.equal(jobState({...past,last_error:'Connect a model'}).label,'Needs attention');
  assert.equal(jobState({...past,archived:1}).label,'Archived');
  assert.equal(jobState({kind:'interval',enabled:0}).reschedule,false);
});
