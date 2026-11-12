import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/shinbot/web/notification-worker.js', import.meta.url), 'utf8');
function worker(clients = []) {
  const handlers = {}, opened = [];
  const self = {
    location:{origin:'https://bot.example'},
    addEventListener:(name, fn) => { handlers[name] = fn; }, skipWaiting:() => {},
    clients:{matchAll:async () => clients, openWindow:async url => opened.push(url)},
  };
  vm.runInNewContext(source, {self, URL, encodeURIComponent});
  return {
    opened,
    async click(id) {
      let closed = false, work;
      handlers.notificationclick({notification:{data:{notificationId:id},close:() => {closed=true;}},waitUntil:promise => {work=promise;}});
      await work;
      assert.equal(closed, true);
    },
  };
}

test('Clicking an alert focuses an existing ShinBot tab and sends its activity id', async () => {
  const calls = [];
  const w = worker([
    {url:'https://elsewhere.example/',focus:() => assert.fail('Wrong origin')},
    {url:'https://bot.example/static/page.html',focus:() => assert.fail('Wrong page')},
    {url:'https://bot.example/',focus:async () => calls.push('focus'),postMessage:data => calls.push(data)},
  ]);
  await w.click('action:review');
  assert.equal(calls[0], 'focus');
  assert.equal(calls[1].type, 'shinbot:notification-open');
  assert.equal(calls[1].id, 'action:review');
  assert.deepEqual(w.opened, []);
});

test('Clicking after the app closes opens a safe local deep link', async () => {
  const w = worker();
  await w.click('event:17&other=value');
  assert.deepEqual(w.opened, ['/#notification=event%3A17%26other%3Dvalue']);
});

test('A window focus restriction still opens the activity in the existing tab', async () => {
  let destination;
  const w = worker([{url:'https://bot.example/',focus:async () => {throw new Error('Focus blocked');},postMessage:data => {destination=data.id;}}]);
  await w.click('event:18');
  assert.equal(destination, 'event:18');
});

test('Test alerts and malformed activity identifiers do not navigate', async () => {
  const w = worker();
  for (const id of [null, undefined, {}, 'x'.repeat(257)]) await w.click(id);
  assert.deepEqual(w.opened, []);
});
