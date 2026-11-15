import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {setTimeout as delay} from 'node:timers/promises';

const token='t'.repeat(40);
const listen=async handler=>{
  const server=createServer(handler);
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return server;
};
const stop=async server=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));};
const json=(response,status,body)=>{response.writeHead(status,{'content-type':'application/json'});response.end(JSON.stringify(body));};
const freePort=async()=>{const server=await listen(()=>{});const {port}=server.address();await stop(server);return port;};

// A gateway and a model provider the test controls, and the built runtime between them.
async function installation(t,env={}) {
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'shinbot-runtime-'));
  const state={admit:(_request,response)=>json(response,200,{allowed:true}),model:undefined,requests:0};
  const read=(request,done)=>{let body='';request.on('data',chunk=>{body+=chunk;});request.on('end',()=>done(body));};
  const control=await listen((request,response)=>read(request,()=>state.admit(request,response)));
  const model=await listen((request,response)=>read(request,body=>{state.requests++;state.model(request,response,body);}));
  const port=await freePort();
  let child;
  const start=async()=>{
    child=spawn(process.execPath,['--no-warnings',new URL('../dist/server.js',import.meta.url).pathname],{stdio:'ignore',env:{
      PATH:process.env.PATH,SHINBOT_SERVICE_TOKEN:token,SHINBOT_RUNTIME_DATA:directory,PORT:String(port),HOST:'127.0.0.1',
      SHINBOT_CONTROL_URL:`http://127.0.0.1:${control.address().port}`,SHINBOT_FIXTURE_URL:`http://127.0.0.1:${model.address().port}/v1`,...env}});
    for(let attempt=0;;attempt++) {
      try {if((await call('GET','/v1/health')).status===200) break;} catch {}
      assert.ok(attempt<100 && child.exitCode===null,'runtime did not start');
      await delay(100);
    }
    assert.equal((await call('POST','/v1/reconcile',{epoch:'one',plugins:[],tasks:[],held:false})).status,200);
  };
  const call=async(method,route,body)=>{
    const response=await fetch(`http://127.0.0.1:${port}${route}`,{method,headers:{authorization:`Bearer ${token}`,...(body?{'content-type':'application/json'}:{})},
      body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(5000)});
    return {status:response.status,body:await response.json()};
  };
  const submit=async(taskId,changes={})=>call('POST','/v1/submit',{taskId,content:'hello',requestId:taskId+'-1',provider:'custom-fixture',model:'fixture',
    extensions:['shinbot-core-v1'],instructions:'Reply briefly.',maxTokens:1000,...changes});
  const settle=async id=>{
    for(let attempt=0;attempt<150;attempt++) {
      const {body}=await call('GET',`/v1/submissions/${id}`);
      if(['done','unanswered'].includes(body.status)) return body;
      await delay(100);
    }
    assert.fail('submission did not settle');
  };
  t.after(async()=>{child?.kill('SIGKILL');await stop(control);await stop(model);await fs.rm(directory,{recursive:true,force:true});});
  await start();
  return {state,call,submit,settle,start,terminate:async()=>{child.kill('SIGTERM');const [code]=await once(child,'exit');return code;}};
}

const answer=(_request,response)=>{
  response.writeHead(200,{'content-type':'text/event-stream'});
  const chunk=(delta,finish_reason=null)=>response.write('data: '+JSON.stringify({id:'c',object:'chat.completion.chunk',created:1,model:'fixture',
    choices:[{index:0,delta,finish_reason}]})+'\n\n');
  chunk({role:'assistant',content:'All done.'});chunk({},'stop');response.end('data: [DONE]\n\n');
};

test('refusals are 4xx and only those: unknown things, bad input and conflicts', async t => {
  const {call,submit}=await installation(t);
  for(const [method,route,body,status] of [
    ['GET','/v1/submissions/999',undefined,404],
    ['GET','/v1/submissions/not-a-number',undefined,404],
    ['GET','/v1/tasks/unknown/progress',undefined,404],
    ['POST','/v1/tasks/unknown/abort',{},404],
    ['POST','/v1/submit',{},400],
    ['POST','/v1/reconcile',{epoch:'one'},400],
    ['POST','/v1/reconcile',{epoch:'two',plugins:[],tasks:[],held:false},409],
    ['POST','/v1/reconcile',{epoch:'one',plugins:[],tasks:[{extensions:['plugin:gone:1.0.0']}],held:false},409],
    ['POST','/v1/providers/key',{provider:'nowhere',key:'k'},400],
    ['POST','/v1/providers/custom',{id:'not-custom',model:'m',baseUrl:'http://x'},400],
    ['POST','/v1/login',{provider:'custom-fixture'},400],
    ['POST','/v1/login/callback',{url:'http://127.0.0.1/auth/callback?code=1'},409],
    ['POST','/v1/login/respond',{id:'none',value:'x'},409],
  ]) assert.equal((await call(method,route,body)).status,status,`${method} ${route}`);
  assert.equal((await submit('a',{model:'missing'})).status,400);
  assert.equal((await submit('b',{mode:'shout'})).status,400);
  assert.deepEqual(await submit('c',{extensions:['plugin:gone:1.0.0']}),{status:409,body:{error:'Missing extension plugin:gone:1.0.0'}});
});

test('a request the provider rejects settles at once; a failing or silent provider is retried a bounded number of times', async t => {
  const {state,submit,settle}=await installation(t,{SHINBOT_MODEL_RETRIES:'1',SHINBOT_MODEL_IDLE_MS:'300'});
  state.model=(_request,response)=>json(response,400,{error:{message:'max_tokens: 5000 > 4096, which is the maximum allowed'}});
  const rejected=await submit('rejected');
  assert.equal(rejected.status,200);
  const first=await settle(rejected.body.submissionId);
  assert.equal(first.status,'unanswered');
  assert.match(first.error,/model provider could not complete/);
  assert.equal(state.requests,1);
  state.requests=0;
  state.model=(_request,response)=>json(response,500,{error:{message:'upstream exploded'}});
  const failing=await submit('failing');
  const second=await settle(failing.body.submissionId);
  assert.equal(second.status,'unanswered');
  assert.equal(state.requests,2);
  state.requests=0;
  state.model=()=>{};  // Accepts the request and never answers: the configured idle deadline ends each attempt.
  const silent=await submit('silent');
  assert.equal((await settle(silent.body.submissionId)).status,'unanswered');
  assert.equal(state.requests,2);
});

test('shutdown is prompt, leaves running work pending, and the next start finishes it', async t => {
  const {state,submit,settle,start,terminate,call}=await installation(t);
  // The first model request fails, so the run is waiting to retry when the runtime is told to stop. A slow
  // prompt suggestion is in flight too: closing must not wait for it, nor treat the waiting run as held work.
  state.model=(_request,response,body)=>{
    if(body.includes('Suggest one short')) return;
    if(state.requests===1) return json(response,500,{error:{message:'upstream exploded'}});
    answer(_request,response);
  };
  const {body}=await submit('durable');
  while(!state.requests) await delay(20);
  const suggestion=call('POST','/v1/prompt-suggestion',{provider:'custom-fixture',model:'fixture',context:'recent chat'}).catch(()=>undefined);
  while(state.requests<2) await delay(20);
  const started=Date.now();
  assert.equal(await terminate(),0);
  assert.ok(Date.now()-started<5000,'shutdown waited for an optional request');
  await suggestion;
  await start();
  const result=await settle(body.submissionId);
  assert.equal(result.status,'done');
  assert.equal(result.answer,'All done.');
  assert.equal((await call('GET','/v1/tasks/durable/progress')).status,200);
});
