import test from 'node:test';
import assert from 'node:assert/strict';
import {connectedModels,thinkingLevels,approvalModes} from '../src/shinbot/web/chat-controls.js';

const providers=[
  {id:'one',name:'First',configured:true,models:[{id:'deep',name:'Deep model',thinkingLevels:['low','high']},{id:'quick',name:'Quick model',thinkingLevels:['off']}]},
  {id:'two',name:'Second',configured:false,models:[{id:'deep',name:'Unavailable'}]},
];
test('model search includes connected providers and preserves provider identity',()=>{
  assert.deepEqual(connectedModels(providers,'DEEP').map(m=>[m.provider,m.id]),[['one','deep']]);
  assert.equal(connectedModels(providers,'First').length,2);
  assert.equal(connectedModels(providers,'missing').length,0);
});
test('effort choices follow the selected model and approval modes explain their effect',()=>{
  assert.deepEqual(thinkingLevels(providers,{provider:'one',model:'deep'}),['low','high']);
  assert.deepEqual(thinkingLevels(providers,{provider:'one',model:'quick'}),['off']);
  assert.deepEqual(thinkingLevels([],{provider:'gone',model:'gone'}),['off']);
  assert.equal(approvalModes[0][0],'auto');
  assert.match(approvalModes.find(([id])=>id==='full')[2],/change or delete/);
});
