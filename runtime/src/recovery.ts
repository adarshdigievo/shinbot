import { setTimeout as sleep } from 'node:timers/promises';

// Pi persists the retry phase and wake time alongside the generation checkpoint.
export const durableRetry = {enabled:true,maxRetries:Number.MAX_SAFE_INTEGER,baseDelayMs:1000,maxAgentDelayMs:30000};

export class TemporaryConnectionError extends Error {}

export async function withConnectionRecovery<T>(
  operation: () => Promise<T>, signal?: AbortSignal,
  waiting: (value:boolean) => Promise<void> = async () => {},
) {
  let delay = 1000;
  for (;;) {
    signal?.throwIfAborted();
    try {
      const result = await operation();
      await waiting(false);
      return result;
    } catch (error) {
      signal?.throwIfAborted();
      if (!(error instanceof TemporaryConnectionError || error instanceof TypeError ||
          (error instanceof Error && ['TimeoutError','AbortError'].includes(error.name)))) throw error;
      await waiting(true);
      await sleep(delay, undefined, {signal});
      delay = Math.min(delay * 2, 30000);
    }
  }
}
