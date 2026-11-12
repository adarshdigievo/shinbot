import test from 'node:test';
import assert from 'node:assert/strict';
import {submissionIds} from '../../src/shinbot/web/submissions.js';

test('lost acknowledgement and page reload reuse one submission without retaining message text', async () => {
  const values=new Map();
  const storage={getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
  const body={taskId:null,botId:'default',content:'private message'};
  const first=await submissionIds(storage)(body);
  const retried=await submissionIds(storage)(body);
  assert.equal(first.id,retried.id);
  assert.ok(!JSON.stringify([...values]).includes('private message'));
  const other=await submissionIds(storage)({...body,botId:'other'});
  assert.notEqual(other.id,first.id);
  retried.acknowledge();
  assert.notEqual((await submissionIds(storage)(body)).id,first.id);
});
