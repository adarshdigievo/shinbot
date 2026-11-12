import test from 'node:test';
import assert from 'node:assert/strict';
import {renderAttachment} from '../src/shinbot/web/attachments.js';
import {conversationTimeline} from '../src/shinbot/web/conversations.js';

const helpers={escape:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),icon:()=>'',messageTime:()=>''};
const file={id:'a'.repeat(32),action_id:'share',created:3,name:'report.png',size:1234,mime_type:'image/png',preview:true,url:`/api/tasks/chat/attachments/${'a'.repeat(32)}`};

test('only authenticated local image routes render previews; active documents download',()=>{
  for(const url of ['https://other.test/pixel','javascript:alert(1)','data:image/svg+xml,anything','//other.test/a'])assert.equal(renderAttachment({...file,url},helpers),'');
  assert.match(renderAttachment(file,helpers),/<img src="\/api\/tasks\/chat\/attachments\//);
  assert.doesNotMatch(renderAttachment({...file,mime_type:'image/svg+xml'},helpers),/<img/);
  const rendered=renderAttachment({...file,name:'<img src=x onerror="bad">'},helpers);
  assert.match(rendered,/&lt;img src=x onerror=&quot;bad/);
  assert.match(rendered,/\?download=true/);
});

test('attachments remain between their tool action and the saved answer after reload',()=>{
  const task={messages:[{id:'message',content:'Share a file',created:1,started:1,finished:4,answer:'Attached.',status:'completed'}],actions:[{id:'share',created:2,status:'completed'}],attachments:[file]};
  const kinds=value=>conversationTimeline(value).map(entry=>entry.kind);
  assert.deepEqual(kinds(task),['user','action','attachment','answer']);
  assert.deepEqual(kinds(JSON.parse(JSON.stringify(task))),kinds(task));
});
