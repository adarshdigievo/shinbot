import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {generateKeyPair,SignJWT,exportJWK,createLocalJWKSet} from 'jose';
import {validateIdentity, CallbackRelay, loopbackRedirect, chatgptAuth} from '../dist/chatgpt-auth.js';
import {FileCredentials} from '../dist/credentials.js';
const {privateKey,publicKey}=await generateKeyPair('RS256');
const keys=createLocalJWKSet({keys:[await exportJWK(publicKey)]});
const sign=(claims={})=>new SignJWT({sub:'owner',nonce:'attempt',...claims}).setProtectedHeader({alg:'RS256'}).setIssuer('https://auth.openai.com').setAudience('client').setIssuedAt().setExpirationTime('2m').sign(privateKey);
test('identity is signature and account bound',async()=>{
 const token=await sign();assert.equal((await validateIdentity(token,'client','attempt','owner',keys)).subject,'owner');
 await assert.rejects(validateIdentity(token,'other','attempt','owner',keys));
 await assert.rejects(validateIdentity(token,'client','other','owner',keys));
 await assert.rejects(validateIdentity(token,'client','attempt','other',keys));
 const altered=token.slice(0,token.lastIndexOf('.')+1)+'AAAA';
 await assert.rejects(validateIdentity(altered,'client','attempt','owner',keys));
 const expired=await new SignJWT({sub:'owner',nonce:'attempt'}).setProtectedHeader({alg:'RS256'}).setIssuer('https://auth.openai.com').setAudience('client').setIssuedAt().setExpirationTime(1).sign(privateKey);
 await assert.rejects(validateIdentity(expired,'client','attempt','owner',keys));
});

test('gateway callbacks are single use, state-bound, and keep the exact redirect URI',async()=>{
 const relay=new CallbackRelay(),abort=new AbortController();
 const result=relay.wait('expected','http://127.0.0.1:8765/auth/callback',abort.signal);
 for(const callback of [
   'http://127.0.0.1:8765/auth/callback?state=wrong&code=test',
   'http://127.0.0.1:8766/auth/callback?state=expected&code=test',
   'http://localhost:8765/auth/callback?state=expected&code=test',
   'http://127.0.0.1:8765/auth/callback?state=expected&state=expected&code=test',
   'http://127.0.0.1:8765/auth/callback?state=expected&code=one&code=two',
 ])assert.throws(()=>relay.accept(callback));
 const good='http://127.0.0.1:8765/auth/callback?state=expected&code=test';
 relay.accept(good);assert.equal((await result).searchParams.get('code'),'test');
 assert.throws(()=>relay.accept(good));
 for(const url of ['https://server.example/auth/callback','http://localhost:8765/auth/callback','http://127.0.0.1:8765/other'])assert.throws(()=>loopbackRedirect(url));
});

test('relayed sign-in never asks for a pasted callback and cancellation releases its state',async()=>{
 const relay=new CallbackRelay();let authorize;
 const auth=chatgptAuth({read:async()=>undefined},()=> 'urn:uuid:test',{callbacks:relay,redirect:()=> 'http://127.0.0.1:8765/auth/callback'});
 await assert.rejects(auth.login({signal:new AbortController().signal,notify:event=>{
   authorize=new URL(event.url);
   assert.equal(authorize.searchParams.get('redirect_uri'),'http://127.0.0.1:8765/auth/callback');
   relay.accept('http://127.0.0.1:8765/auth/callback?error=access_denied&state='+authorize.searchParams.get('state'));
 },prompt:async()=>{assert.fail('No manual prompt in gateway flow');}}),/access_denied/);
 const abort=new AbortController();
 const waiting=relay.wait('cancelled','http://127.0.0.1:8765/auth/callback',abort.signal);
 abort.abort();await assert.rejects(waiting,/cancelled/);
 assert.throws(()=>relay.accept('http://127.0.0.1:8765/auth/callback?state=cancelled&code=test'));
});
test('concurrent credential rotation cannot lose another provider',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'shinbot-credentials-test-'));
 try{const file=path.join(dir,'credentials.json');const store=new FileCredentials(file);
 await Promise.all(Array.from({length:25},(_,i)=>store.modify('provider-'+i,async()=>({type:'api_key',key:'test-'+i}))));
 assert.equal((await store.list()).length,25);assert.equal((await fs.stat(file)).mode&0o777,0o600);
 await store.delete('provider-4');assert.equal(await store.read('provider-4'),undefined);
 assert.equal((await new FileCredentials(file).read('provider-5')).key,'test-5');
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('sign-in sends URN host identity and shows safe provider error codes',async()=>{
 const {chatgptAuth,oauthFailure}=await import('../dist/chatgpt-auth.js');
 let authorize;
 const auth=chatgptAuth({read:async()=>undefined},()=> 'urn:uuid:00000000-0000-4000-8000-000000000000');
 await assert.rejects(auth.login({signal:new AbortController().signal,notify:event=>{authorize=new URL(event.url);},prompt:async()=>{
 assert.equal(authorize.searchParams.get('ext_agent_host_id'),'urn:uuid:00000000-0000-4000-8000-000000000000');
 assert.equal(authorize.searchParams.get('client_id'),'dynamic_agent_client');
 const callback=new URL(authorize.searchParams.get('redirect_uri'));callback.searchParams.set('state',authorize.searchParams.get('state'));callback.searchParams.set('error','access_denied');return callback.toString();
 }}),/access_denied/);
 assert.match(oauthFailure('refresh_token_reused'),/Stop the duplicate/);
 assert.match(oauthFailure('invalid_client'),/installation identity/);
 assert.doesNotMatch(oauthFailure('secret token \"leak\"'),/leak/);
});
