import {
  createAssistantMessageEventStream, isRetryableAssistantError, type AssistantMessage, type AssistantMessageEventStream,
  type Model, type Api, type Provider, type StreamOptions,
} from '@earendil-works/pi-ai';
import { modelIdleMs } from './recovery.js';

// Pi retries any error whose text contains 429, 500 or "timeout", which also matches request errors such
// as "400 max_tokens: 5000 > 4096". A leading client-error status is the provider's verdict on this
// request, so reword it until the retry policy lets it settle.
export function settledError(message: AssistantMessage): AssistantMessage {
  const status = /^(?:[\w .-]{0,60}\()?(4\d\d)(?=[\s:)])/.exec(message.errorMessage || '')?.[1];
  if (!status || !/^4(0[0-6]|1[01345]|22|31)$/.test(status) || !isRetryableAssistantError(message)) return message;
  for (const errorMessage of [message.errorMessage!.replace(/\d{3,}/g, digits => digits === status ? digits : '#'),
      `${status}: The provider rejected this request.`]) {
    if (!isRetryableAssistantError({...message, errorMessage})) return {...message, errorMessage};
  }
  return message;
}

// Provider SDK request deadlines do not always cover a stalled response body.
// Use Pi's public provider/stream API to turn idleness into a retryable error.
function connectedStream(
  model: Model<Api>, request: (signal:AbortSignal, touch:()=>void)=>AssistantMessageEventStream,
  signal: AbortSignal | undefined, timeout: number,
) {
  const output = createAssistantMessageEventStream();
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  let settled = false;
  let partial: AssistantMessage = {
    role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason:'error',
    usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},
  };
  const clean = () => {clearTimeout(timer);signal?.removeEventListener('abort',cancel);};
  const fail = (reason:'error'|'aborted', message:string) => {
    if(settled) return;
    settled=true;clean();
    output.push({type:'error',reason,error:{...structuredClone(partial),stopReason:reason,errorMessage:message}});
    controller.abort();
  };
  const cancel = () => fail('aborted','Request cancelled.');
  const touch = () => {
    clearTimeout(timer);
    if(!settled) timer=setTimeout(()=>fail('error','Connection timeout while waiting for model progress.'),timeout);
  };
  signal?.addEventListener('abort',cancel,{once:true});
  if(signal?.aborted) cancel();
  else {
    touch();
    void (async()=>{
      try {
        for await(const event of request(controller.signal,touch)) {
          if(settled) break;
          touch();
          if('partial' in event) partial=event.partial;
          if(event.type==='done'||event.type==='error') {settled=true;clean();}
          output.push(event.type==='error'&&event.reason==='error' ? {...event,error:settledError(event.error)} : event);
        }
        if(!settled) fail('error','Connection closed before the model response completed.');
      } catch(error) {
        fail('error',error instanceof Error ? error.message : 'Model request failed.');
      }
    })();
  }
  return output;
}

function progress<T extends StreamOptions>(options:T|undefined, signal:AbortSignal, touch:()=>void) {
  return Object.assign({},options,{signal,onProviderStreamEvent:(data:unknown,model:Model<Api>)=>{
    touch();return options?.onProviderStreamEvent?.(data,model);
  }});
}

export function withConnectionDeadline(provider:Provider, timeout=modelIdleMs):Provider {
  return {
    ...provider,
    stream:(model,context,options)=>connectedStream(model,
      (signal,touch)=>provider.stream(model,context,progress(options,signal,touch)),options?.signal,timeout),
    streamSimple:(model,context,options)=>connectedStream(model,
      (signal,touch)=>provider.streamSimple(model,context,{...options,...progress(options,signal,touch)}),options?.signal,timeout),
    ...(provider.fetchDeferred ? {fetchDeferred:(model,handle,options)=>connectedStream(model,
      signal=>provider.fetchDeferred!(model,handle,{...options,signal}),options?.signal,timeout)} : {}),
  };
}
