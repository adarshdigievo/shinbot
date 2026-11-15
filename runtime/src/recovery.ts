import { setTimeout as sleep } from 'node:timers/promises';

const positive = (value: string | undefined, fallback: number) => {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : fallback;
};

// Pi persists the retry phase and wake time alongside the generation checkpoint. Delays double to 30 s,
// so the default outlasts about two hours of continuous failure; the run then settles with the last error.
export const durableRetry = {enabled:true,maxRetries:positive(process.env.SHINBOT_MODEL_RETRIES,240),baseDelayMs:1000,maxAgentDelayMs:30000};
// How long a model request may stay silent before it counts as a lost connection.
export const modelIdleMs = positive(process.env.SHINBOT_MODEL_IDLE_MS,60000);

export class TemporaryConnectionError extends Error {}

// An unreachable service is waited for indefinitely. One that keeps answering with an error is failing:
// stop after about ten minutes of it.
export async function withConnectionRecovery<T>(
  operation: () => Promise<T>, signal?: AbortSignal,
  waiting: (value:boolean) => Promise<void> = async () => {}, rejections = 25,
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
      if (error instanceof TemporaryConnectionError && --rejections < 0) {
        await waiting(false);
        throw new Error('The ShinBot service kept returning errors. This step was not confirmed; check it in ShinBot before repeating it.');
      }
      await waiting(true);
      await sleep(delay, undefined, {signal});
      delay = Math.min(delay * 2, 30000);
    }
  }
}
