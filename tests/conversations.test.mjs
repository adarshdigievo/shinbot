import test from 'node:test';
import assert from 'node:assert/strict';
import {conversationOwner, recentConversations, selectConversation, conversationRows, conversationTimeline, conversationSnapshots, queuedMessages} from '../src/shinbot/web/conversations.js';

const bots = [{id:'default',name:'ShinBot'},{id:'research',name:'Researcher'},{id:'writer',name:'Writer'}];
const tasks = [
  {id:'one',bot_id:'research',title:'Weekly report',updated:2},
  {id:'two',bot_id:'writer',title:'Weekly report',updated:3},
  {id:'legacy',title:'Old chat',updated:1},
];

test('all-bot history is recent first without changing the source collection', () => {
  assert.deepEqual(recentConversations(tasks).map(t=>t.id), ['two','one','legacy']);
  assert.deepEqual(tasks.map(t=>t.id), ['one','two','legacy']);
});

test('bot filters include only that bot and assign legacy chats to default', () => {
  assert.deepEqual(recentConversations(tasks,'research').map(t=>t.id), ['one']);
  assert.deepEqual(recentConversations(tasks,'default').map(t=>t.id), ['legacy']);
  assert.deepEqual(recentConversations(tasks,'absent'), []);
});

test('opening a chat selects its owner and reveals it outside the previous filter', () => {
  const state = {bot:'research',task:'one',chatFilter:'research'};
  selectConversation(state,tasks[1]);
  assert.deepEqual(state,{bot:'writer',task:'two',chatFilter:'all'});
  state.chatFilter='writer';
  selectConversation(state,tasks[1]);
  assert.equal(state.chatFilter,'writer');
  selectConversation(state,tasks[2]);
  assert.equal(state.bot,'default');
});

test('unknown owners never fall back to a different existing bot', () => {
  assert.equal(conversationOwner(tasks[2],bots).name,'ShinBot');
  const owner=conversationOwner({bot_id:'removed'},bots);
  assert.equal(owner.id,'removed');
  assert.equal(owner.name,'Unavailable bot');
});

test('duplicate titles remain distinguishable by bot, with current and working states', () => {
  const html=conversationRows({tasks:tasks.map(t=>({...t,status:'running'})),bots,filter:'all',selected:'two',escape:String,avatar:(_class,bot)=>`avatar:${bot.id}`,icon:()=>'<i></i>'});
  assert.match(html,/Weekly report · Writer/);
  assert.match(html,/Weekly report · Researcher/);
  assert.match(html,/data-task="two"[^>]*aria-current="true"/);
  assert.match(html,/avatar:writer/);
  assert.match(html,/Working/);
});

const order = task => conversationTimeline(task).map(entry => entry.key);

test('live reply stays visible through empty progress and connection gaps until it is saved', () => {
  const snapshots=conversationSnapshots();
  const running={id:'chat',status:'running',messages:[{id:'turn',created:1,status:'running'}]};
  snapshots.accept(snapshots.request(),{...running,progress:{text:'The complete reply.',created:2}});
  for (const progress of [{text:'',compacting:true},undefined,{text:''}]) {
    const incoming={...running,progress};
    const result=snapshots.accept(snapshots.request(),incoming);
    assert.equal(conversationTimeline(result).find(e=>e.kind==='progress').text,'The complete reply.');
    assert.equal(result.progress.created,2);
    assert.equal(result.progress.compacting,progress?.compacting);
    assert.equal(incoming.progress,progress);
  }
  const completed={...running,status:'completed',messages:[{...running.messages[0],status:'completed',answer:'The saved reply.',finished:3}]};
  assert.deepEqual(order(snapshots.accept(snapshots.request(),completed)),['user-turn','answer-turn']);
});

test('an older delayed refresh cannot replace the saved reply', () => {
  const snapshots=conversationSnapshots(),older=snapshots.request(),newer=snapshots.request();
  const completed={id:'chat',status:'completed',messages:[{id:'turn',created:1,finished:2,status:'completed',answer:'Done'}]};
  assert.equal(snapshots.accept(newer,completed),completed);
  assert.equal(snapshots.accept(older,{id:'chat',status:'running',messages:[]}),null);
});

test('retained text does not leak into another turn, another chat, or a stopped task', () => {
  const running={id:'chat',status:'running',messages:[{id:'turn',created:1,status:'running'}]};
  for (const next of [
    {...running,id:'other'},
    {...running,messages:[{id:'next-turn',created:3,status:'running'}]},
    {...running,messages:[{...running.messages[0],status:'failed',error:'Connection failed',finished:3}]},
    ...['cancelled','failed','paused','uncertain'].map(status=>({...running,status})),
  ]) {
    const snapshots=conversationSnapshots();
    snapshots.accept(snapshots.request(),{...running,progress:{text:'Old draft',created:2}});
    assert.equal(snapshots.accept(snapshots.request(),next).progress,undefined);
  }
});

test('a new generation replaces retained text without changing its recorded position', () => {
  const snapshots=conversationSnapshots();
  const task={id:'chat',status:'running',messages:[{id:'turn',created:1,status:'running'}],actions:[{id:'tool',created:3}]};
  snapshots.accept(snapshots.request(),{...task,progress:{text:'Checking',created:2}});
  const result=snapshots.accept(snapshots.request(),{...task,progress:{text:'Finished checking',created:4}});
  assert.equal(result.progress.text,'Finished checking');
  assert.deepEqual(order(result),['user-turn','action-tool','progress']);
});

test('activity stays before its reply in every turn, including legacy conversations', () => {
  const task = {
    messages:[{id:'one',created:1,answer:'First reply'},{id:'two',created:4,answer:'Second reply'}],
    actions:[{id:'first',created:2},{id:'second',created:5}],
  };
  assert.deepEqual(order(task),['user-one','action-first','answer-one','user-two','action-second','answer-two']);
  assert.deepEqual(order(structuredClone(task)),order(task));
  assert.deepEqual(task.actions.map(a=>a.id),['first','second']);
});

test('a queued follow-up appears after the preceding response, including legacy history', () => {
  const task = {
    messages:[{id:'one',created:1,finished:5,answer:'First reply'},{id:'two',created:3,finished:8,answer:'Second reply'}],
    actions:[{id:'second',created:7},{id:'first',created:2},{id:'more',created:4}],
  };
  assert.deepEqual(order(task),['user-one','action-first','action-more','answer-one','user-two','action-second','answer-two']);
});

test('pending prompts stay outside the transcript until each starts', () => {
  const task={messages:[{id:'one',created:1,started:2,status:'running'},{id:'two',created:3,status:'queued'},{id:'three',created:4,status:'queued'}],progress:{text:'Working',created:5}};
  assert.deepEqual(order(task),['user-one','progress']);
  assert.deepEqual(queuedMessages(task).map(m=>m.id),['two','three']);
  task.messages[0]={...task.messages[0],status:'completed',finished:6,answer:'First reply'};
  task.messages[1]={...task.messages[1],status:'running',started:7};
  delete task.progress;
  assert.deepEqual(order(task),['user-one','answer-one','user-two']);
  assert.deepEqual(queuedMessages(task).map(m=>m.id),['three']);
});

test('steering precedes queued follow-ups and a joint response appears once', () => {
  const task={messages:[{id:'one',created:1,started:1,status:'completed',answer_id:12,answer:'Updated plan',finished:8},{id:'two',created:2,status:'queued'},{id:'steer',created:3,started:5,mode:'steer',status:'completed',answer_id:12,answer:'Updated plan',finished:8}],actions:[{id:'tool',created:4}]};
  assert.deepEqual(order(task),['user-one','action-tool','user-steer','answer-steer']);
  task.messages[2]={...task.messages[2],started:null,status:'running',answer:null,answer_id:null};
  assert.deepEqual(queuedMessages(task).map(m=>m.id),['steer','two']);
});

test('streaming text keeps its position while subsequent actions arrive', () => {
  const task = {
    messages:[{id:'one',created:1,status:'running'}],
    progress:{text:'Checking the page',created:3},
    actions:[{id:'first',created:2}],
  };
  assert.deepEqual(order(task),['user-one','action-first','progress']);
  task.actions.push({id:'next',created:4,status:'pending'});
  assert.deepEqual(order(task),['user-one','action-first','progress','action-next']);
  task.messages[0]={...task.messages[0],status:'completed',finished:5,answer:'Done'};
  assert.deepEqual(order(task),['user-one','action-first','action-next','answer-one']);
});

test('errors and approval traces remain at their recorded positions with stable identities', () => {
  const task = {
    messages:[{id:'one',created:1,finished:3,error:'Connection failed'},{id:'two',created:4,status:'running'}],
    actions:[{id:'first',created:2,status:'failed'},{id:'approval',created:5,status:'pending'}],
  };
  const expected=['user-one','action-first','message-error-one','user-two','action-approval'];
  assert.deepEqual(order(task),expected);
  task.actions[1].status='completed';
  assert.deepEqual(order(task),expected);
});

test('timestamp ties retain input, activity, reply order without dropping entries', () => {
  assert.deepEqual(order({messages:[{id:'one',created:1,finished:1,answer:'Done'}],actions:[{id:'a',created:1},{id:'b',created:1}]}),['user-one','action-a','action-b','answer-one']);
});
