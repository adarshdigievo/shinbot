import assert from 'node:assert/strict';
import test from 'node:test';
import {currentActivity, actionDescription} from '../src/shinbot/web/activity.js';

test('live activity distinguishes execution, approval, retry, writing and completion', () => {
  const task={status:'running',actions:[{tool:'browser',args:'{"operation":"goto","url":"https://example.com/search?token=secret"}',status:'executing'}]};
  assert.equal(currentActivity(task).label,'Opening a webpage');
  assert.equal(currentActivity(task).detail,'example.com/search');
  task.actions[0].status='pending';
  assert.equal(currentActivity(task).label,'Waiting for your approval');
  task.actions[0].status='retrying';
  assert.equal(currentActivity(task).waiting,true);
  task.actions[0].status='completed';task.progress={text:'Draft reply'};
  assert.equal(currentActivity(task).label,'Writing a reply');
  task.status='completed';assert.equal(currentActivity(task),null);
});

test('compaction, connection loss and model preparation never invent completed work', () => {
  assert.equal(currentActivity({status:'running',progress:{compacting:true}}).label,'Summarizing earlier messages');
  assert.equal(currentActivity({status:'running',wait_reason:'Waiting for network'}).label,'Waiting for network');
  assert.equal(currentActivity({status:'running',progress:{phase:'thinking'}}).label,'Thinking through your request');
  assert.equal(currentActivity({status:'running',progress:{tools:[{name:'read_file',status:'running'}]}}).label,'Reading a file');
  assert.equal(actionDescription({tool:'custom_tool',args:'invalid'}).label,'Using custom tool');
  assert.equal(actionDescription({tool:'plugin:kanban:1.0.0:card_history'}).label,'Reading card history');
  assert.equal(actionDescription({tool:'plugin:custom:1.0.0:search_records'}).label,'Using search records');
});
