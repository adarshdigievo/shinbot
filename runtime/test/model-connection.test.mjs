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

test('a request the provider rejected is settled, while outages and rate limits stay retryable',async()=>{
  const {settledError}=await import('../dist/model-connection.js');
  const error=errorMessage=>({...message(),stopReason:'error',errorMessage});
  for(const text of [
    '400: {"message":"max_tokens: 5000 > 4096, which is the maximum allowed"}',
    '404 model gpt-4290 does not exist',
    'OpenAI API error (422): request timeout must be below 600',
    '401 Incorrect API key provided: sk-500abc',
    '413 payload of 150000 bytes hit the server error threshold',
  ]) {
    assert.equal(isRetryableAssistantError(error(text)),true,text);
    const settled=settledError(error(text));
    assert.equal(isRetryableAssistantError(settled),false,text);
    assert.equal(settled.errorMessage.slice(0,40).includes(/4\d\d/.exec(text)[0]),true);
  }
  for(const text of ['429 rate limit exceeded','408 request timeout','409 conflict, try your request again','499 client closed request, timeout',
    '500: upstream exploded','503 overloaded','Connection timeout while waiting for model progress.','fetch failed']) {
    assert.equal(settledError(error(text)).errorMessage,text);
    assert.equal(isRetryableAssistantError(settledError(error(text))),true,text);
  }
  // Applied to the terminal event of a wrapped provider, and nowhere else.
  const source=createAssistantMessageEventStream();
  const provider=withConnectionDeadline({streamSimple:()=>source},1000);
  const stream=provider.streamSimple(model,{});
  const failed=error('400 max_tokens: 5000 > 4096');
  source.push({type:'error',reason:'error',error:failed});
  const result=await stream.result();
  assert.equal(result.errorMessage,'400 max_tokens: # > #');
  assert.equal(isRetryableAssistantError(result),false);
});
