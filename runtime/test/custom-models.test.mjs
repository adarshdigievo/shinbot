import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';
import { CustomModelStore } from '../dist/custom-models.js';

const model = id => ({ id, model: 'example', baseUrl: 'http://localhost:1234/v1' });

test('concurrent custom provider saves survive reopening without losing models', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'shinbot-custom-models-'));
  try {
    const filename = path.join(directory, 'custom-models.json');
    const store = new CustomModelStore(filename);
    await Promise.all(Array.from({ length: 25 }, (_, index) => store.upsert(model(`custom-${index}`))));
    const reopened = new CustomModelStore(filename);
    assert.equal((await reopened.list()).length, 25);
    await reopened.upsert({ ...model('custom-4'), model: 'updated' });
    const saved = await new CustomModelStore(filename).list();
    assert.equal(saved.length, 25);
    assert.equal(saved.find(item => item.id === 'custom-4').model, 'updated');
    assert.equal((await fs.stat(filename)).mode & 0o777, 0o600);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test('killing a custom provider writer preserves a readable catalog and permits the next save', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'shinbot-custom-crash-'));
  let child;
  try {
    const filename = path.join(directory, 'custom-models.json');
    await new CustomModelStore(filename).upsert(model('custom-existing'));
    const source = `
      import { CustomModelStore } from ${JSON.stringify(new URL('../dist/custom-models.js', import.meta.url).href)};
      const store = new CustomModelStore(${JSON.stringify(filename)});
      const item = {id: 'custom-interrupted', model: 'example', baseUrl: 'http://localhost:1234/v1',
        name: 'x'.repeat(8_000_000)};
      // Keep writing until the parent interrupts us. One write could finish
      // before a loaded parent gets scheduled to send SIGKILL.
      while (true) await store.upsert(item);
    `;
    child = spawn(process.execPath, ['--input-type=module', '-e', source], { stdio: 'ignore' });
    const completed = once(child, 'exit');
    const deadline = Date.now() + 10000;
    while (child.exitCode === null && child.signalCode === null && Date.now() < deadline) {
      try {
        await fs.stat(filename + '.tmp');
        child.kill('SIGKILL');
        break;
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      await delay(1);
    }
    if (Date.now() >= deadline) {
      child.kill('SIGKILL');
      throw new Error('The writer did not create a pending file within ten seconds.');
    }
    const [code, signal] = await completed;
    assert.equal(code, null);
    assert.equal(signal, 'SIGKILL');
    const restarted = new CustomModelStore(filename);
    assert.ok((await restarted.list()).some(item => item.id === 'custom-existing'));
    await restarted.upsert(model('custom-after-restart'));
    assert.ok((await new CustomModelStore(filename).list()).some(item => item.id === 'custom-after-restart'));
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    await fs.rm(directory, { recursive: true, force: true });
  }
});
