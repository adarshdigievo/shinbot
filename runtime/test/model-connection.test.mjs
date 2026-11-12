import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {createAssistantMessageEventStream,isRetryableAssistantError,normalizeContext} from '@earendil-works/pi-ai';
import {openAICompletionsApi} from '@earendil-works/pi-ai/api/openai-completions.lazy';
import {withConnectionDeadline} from '../dist/model-connection.js';

const model={id:'fixture',api:'openai-completions',provider:'fixture'};
const message=()=>({role:'assistant',api:model.api,provider:model.provider,model:model.id,
  timestamp:Date.now(),content:[],stopReason:'toolUse',
  usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});

test('a stalled partial tool call becomes a retryable connection error and aborts transport',async()=>{
  let signal;
  const source=createAssistantMessageEventStream();
  const partial=message();
  partial.content=[{type:'toolCall',id:'partial',name:'write',arguments:{text:'once'}}];
  const provider=withConnectionDeadline({streamSimple:(_m,_c,options)=>{
    signal=options.signal;source.push({type:'start',partial});return source;
  }},20);
  const stream=provider.streamSimple(model,{});
  const events=[];
  for await(const event of stream) events.push(event);
  const result=await stream.result();
  assert.equal(result.stopReason,'error');
  assert.equal(isRetryableAssistantError(result),true);
  assert.equal(signal.aborted,true);
  partial.stopReason='aborted';
  source.push({type:'done',reason:'toolUse',message:partial});
  assert.equal(result.stopReason,'error');
  assert.deepEqual(events.map(event=>event.type),['start','error']);
});

test('provider progress keeps a long stream alive and normal completion clears the deadline',async()=>{
  let transport;
  let observations=0;
  const source=createAssistantMessageEventStream();
  const partial=message();
  const provider=withConnectionDeadline({streamSimple:(_m,_c,options)=>{
    transport=options;source.push({type:'start',partial});return source;
  }},100);
  const stream=provider.streamSimple(model,{}, {onProviderStreamEvent:()=>observations++});
  const heartbeat=setInterval(()=>transport.onProviderStreamEvent({},model),20);
  try {
    await new Promise(resolve=>setTimeout(resolve,230));
    source.push({type:'done',reason:'toolUse',message:partial});
    assert.equal((await stream.result()).stopReason,'toolUse');
    assert.ok(observations>0);
    assert.equal(transport.signal.aborted,false);
  } finally {clearInterval(heartbeat);}
});

test('explicit cancellation is aborted, never a retryable network failure',async()=>{
  const source=createAssistantMessageEventStream();
  const controller=new AbortController();
  const provider=withConnectionDeadline({streamSimple:()=>source},1000);
  const stream=provider.streamSimple(model,{}, {signal:controller.signal});
  controller.abort();
  const result=await stream.result();
  assert.equal(result.stopReason,'aborted');
  assert.equal(isRetryableAssistantError(result),false);
  source.end(result);
});

test('the released OpenAI adapter needs a stream deadline after HTTP headers arrive',async()=>{
  const server=createServer((request,response)=>{
    request.resume();
    response.writeHead(200,{'content-type':'text/event-stream'});
    response.write('data: '+JSON.stringify({id:'fixture',object:'chat.completion.chunk',created:1,model:'fixture',
      choices:[{index:0,delta:{role:'assistant',content:'partial'},finish_reason:null}]})+'\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const controller=new AbortController();
  try {
    const api=openAICompletionsApi();
    const local={...model,baseUrl:`http://127.0.0.1:${server.address().port}/v1`,reasoning:false,
      input:['text'],contextWindow:4096,maxTokens:1024,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
    const context=normalizeContext({messages:[{role:'user',content:'hello',timestamp:1}]});
    let observed;
    const progress=new Promise(resolve=>{observed=resolve;});
    const original=api.streamSimple(local,context,{apiKey:'local',timeoutMs:100,maxRetries:0,
      signal:controller.signal,onProviderStreamEvent:()=>observed()});
    await progress;
    assert.equal(await Promise.race([original.result().then(()=>'settled'),
      new Promise(resolve=>setTimeout(()=>resolve('stalled'),300))]),'stalled');
    controller.abort();
    await original.result();
    const guarded=withConnectionDeadline(api,100).streamSimple(local,context,{apiKey:'local',timeoutMs:100,maxRetries:0});
    const result=await guarded.result();
    assert.match(result.errorMessage,/Connection timeout/);
    assert.equal(result.stopReason,'error');
    assert.equal(isRetryableAssistantError(result),true);
  } finally {
    controller.abort();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
  }
});
