import { createHash, randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from 'jose';
import type { OAuthAuth, OAuthCredential, CredentialStore } from '@earendil-works/pi-ai';

const issuer = 'https://auth.openai.com';
const resource = 'https://api.openai.com/v1';
const tokenEndpoint = `${issuer}/api/accounts/oauth/token`;
const jwks = createRemoteJWKSet(new URL(`${issuer}/.well-known/jwks.json`));
const random = () => randomBytes(32).toString('base64url');
export function oauthFailure(code:unknown,status?:number) {
 const reasons:Record<string,string>={access_denied:'Sign-in was declined. Start again when you are ready.',invalid_grant:'This sign-in or refresh token is no longer valid. Reconnect ChatGPT.',refresh_token_reused:'This account token was used by another installation. Stop the duplicate and reconnect ChatGPT.',invalid_client:'This installation identity was rejected. Reconnect ChatGPT to register it again.',subscription_sharing_usage_limit_exceeded:'Your ChatGPT sharing allowance is exhausted. Check your plan or use another provider.'};
 const safe=typeof code==='string' && /^[a-z0-9_]{1,80}$/.test(code)?code:undefined;
 return safe && reasons[safe]?`${safe}: ${reasons[safe]}`:`ChatGPT sign-in failed${safe?' ('+safe+')':status?' (HTTP '+status+')':''}. Start a fresh attempt and check account eligibility.`;
}


export async function validateIdentity(idToken:string, clientId:string, nonce:string|undefined, subject:string|undefined, keys:JWTVerifyGetKey=jwks) {
  const {payload} = await jwtVerify(idToken,keys,{issuer,audience:clientId,algorithms:['RS256'],requiredClaims:['exp','iat','sub']});
  if(nonce!==undefined && payload.nonce!==nonce) throw new Error('Sign-in nonce did not match.');
  if(typeof payload.sub!=='string' || (subject!==undefined && payload.sub!==subject)) throw new Error('The selected account changed during sign-in.');
  return {subject:payload.sub,email:typeof payload.email==='string'?payload.email:undefined,issuer};
}

async function exchange(parameters:Record<string,string>, signal:AbortSignal) {
  const response=await fetch(tokenEndpoint,{method:'POST',body:new URLSearchParams(parameters),signal:AbortSignal.any([signal,AbortSignal.timeout(30000)])});
  if(!response.ok) {const data=await response.json().catch(()=>({})) as any;const code=typeof data.error==='string'?data.error:data.error?.code;throw new Error(oauthFailure(code,response.status));}
  const data=await response.json() as any;
  if(typeof data.access_token!=='string' || typeof data.refresh_token!=='string' || !Number.isFinite(data.expires_in) || data.expires_in<=0) throw new Error('Incomplete token response.');
  const scopes=typeof data.scope==='string'?data.scope.split(/\s+/):[];
  if(!scopes.includes('chatgpt.tokens.use.direct')) throw new Error('ChatGPT plan usage was not granted to this installation.');
  return {...data,scopes};
}

export function loopbackRedirect(value:string) {
  const url=new URL(value);
  if(url.protocol!=='http:' || url.hostname!=='127.0.0.1' || url.pathname!=='/auth/callback' || url.username || url.password || url.search || url.hash)
    throw new Error('ChatGPT needs an HTTP callback at 127.0.0.1/auth/callback.');
  return url.toString();
}

export class CallbackRelay {
  private pending=new Map<string,{redirect:string,receive:(url:URL)=>void}>();
  wait(state:string,redirect:string,signal:AbortSignal):Promise<URL> {
    redirect=loopbackRedirect(redirect);
    return new Promise((resolve,reject)=>{
      const abort=()=>{this.pending.delete(state);reject(new Error('Sign-in cancelled.'));};
      if(signal.aborted){abort();return;}
      signal.addEventListener('abort',abort,{once:true});
      this.pending.set(state,{redirect,receive:url=>{
        this.pending.delete(state);signal.removeEventListener('abort',abort);resolve(url);
      }});
    });
  }
  accept(value:string) {
    const url=new URL(value),state=url.searchParams.get('state') || '',pending=this.pending.get(state);
    if(!pending || url.searchParams.getAll('state').length!==1)throw new Error('This sign-in expired or does not match. Start again in ShinBot.');
    const expected=new URL(pending.redirect);
    if(url.origin!==expected.origin || url.pathname!==expected.pathname || url.username || url.password || url.hash)
      throw new Error('The callback address did not match this attempt.');
    for(const key of ['code','client_id','error'])if(url.searchParams.getAll(key).length>1)throw new Error('Invalid sign-in callback.');
    if(!url.searchParams.get('code')&&!url.searchParams.get('error'))throw new Error('Missing authorization code.');
    pending.receive(url);
  }
}

export function chatgptAuth(store:CredentialStore, getHostId:()=>string, relay?:{callbacks:CallbackRelay,redirect:()=>string|undefined}):OAuthAuth {
  return {
    name:'OpenAI (ChatGPT subscription)',loginLabel:'Continue with ChatGPT',isSubscription:true,
    async login(interaction) {
      const saved=await store.read('openai');
      const previous=saved?.type==='oauth' && typeof saved.clientId==='string'?saved:undefined;
      const state=random(),nonce=random(),verifier=random();
      const attemptAbort=new AbortController();
      const signal=AbortSignal.any([interaction.signal,attemptAbort.signal]);
      let receive!:(url:URL)=>void;
      let callback=new Promise<URL>(resolve=>{receive=resolve;});
      const relayed=relay?.redirect();
      const server=relayed?undefined:createServer((request,response)=>{
        const url=new URL(request.url||'/', 'http://127.0.0.1');
        if(url.pathname!=='/auth/callback' || url.searchParams.get('state')!==state){response.writeHead(400);response.end('Invalid sign-in callback.');return;}
        response.writeHead(200,{'content-type':'text/plain','cache-control':'no-store'});
        response.end('Return to ShinBot to check your connection.');receive(url);
      });
      let redirect:string;
      if(relayed){redirect=loopbackRedirect(relayed);callback=relay!.callbacks.wait(state,redirect,signal);}
      else {
        await new Promise<void>((resolve,reject)=>{server!.once('error',reject);server!.listen(0,'127.0.0.1',resolve);});
        const address=server!.address();
        if(!address || typeof address==='string') throw new Error('Could not start the sign-in callback.');
        redirect=`http://127.0.0.1:${address.port}/auth/callback`;
      }
      const authorize=new URL(`${issuer}/api/accounts/authorize`);
      const params:Record<string,string>={client_id:previous?.clientId as string || 'dynamic_agent_client',ext_agent_host_id:getHostId(),
        response_type:'code',redirect_uri:redirect,scope:'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
        resource,state,nonce,code_challenge_method:'S256',code_challenge:createHash('sha256').update(verifier).digest('base64url')};
      if(previous?.idToken) params.id_token_hint=String(previous.idToken); else if(!previous) params.agent_name_hint='ShinBot';
      for(const [key,value] of Object.entries(params))authorize.searchParams.set(key,value);
      void callback.catch(()=>{});
      try {
      interaction.notify({type:'auth_url',url:authorize.toString(),instructions:relayed?'Sign in in your browser. ShinBot will finish connecting automatically.':'Sign in in your browser, then return to ShinBot.'});
        const manual=relayed?undefined:interaction.prompt({type:'manual_code',message:'Paste the full callback URL from your browser',signal}).then(value=>{
          const url=new URL(value.trim());
          const expected=new URL(redirect);
          if(url.origin!==expected.origin || url.pathname!==expected.pathname)throw new Error('The callback address did not match this attempt.');
          return url;
        });
        const url=await (manual?Promise.race([callback,manual]):callback);
        if(url.searchParams.get('state')!==state)throw new Error('Sign-in state did not match.');
        if(url.searchParams.has('error'))throw new Error(oauthFailure(url.searchParams.get('error')));
        const clientId=url.searchParams.get('client_id') || previous?.clientId as string;
        if(!clientId || clientId==='dynamic_agent_client' || (previous && clientId!==previous.clientId))throw new Error('Invalid issued client identity.');
        const code=url.searchParams.get('code');if(!code)throw new Error('Missing authorization code.');
        const data=await exchange({grant_type:'authorization_code',client_id:clientId,code,code_verifier:verifier,redirect_uri:redirect,resource},signal);
        if(typeof data.id_token!=='string')throw new Error('Missing identity token.');
        const identity=await validateIdentity(data.id_token,clientId,nonce,previous?.subject as string|undefined);
        return {type:'oauth',access:data.access_token,refresh:data.refresh_token,expires:Date.now()+data.expires_in*1000-180000,
          clientId,idToken:data.id_token,subject:identity.subject,...(identity.email?{email:identity.email}:{}),issuer,scopes:data.scopes,hostId:getHostId()};
      } finally {attemptAbort.abort();server?.close();server?.closeAllConnections();}
    },
    async refresh(credential,signal) {
      if(typeof credential.clientId!=='string' || typeof credential.subject!=='string')throw new Error('Reconnect ChatGPT to validate this account.');
      const data=await exchange({grant_type:'refresh_token',client_id:credential.clientId,refresh_token:credential.refresh,resource},signal);
      if(data.id_token)await validateIdentity(data.id_token,credential.clientId,undefined,credential.subject);
      return {...credential,access:data.access_token,refresh:data.refresh_token,expires:Date.now()+data.expires_in*1000-180000,
        scopes:data.scopes,idToken:data.id_token || credential.idToken};
    },
    async toAuth(credential:OAuthCredential){return {apiKey:credential.access};},
  };
}
