import assert from 'node:assert/strict';
import test from 'node:test';
import {projectsView} from '../src/shinbot/web/projects.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}

function harness(t, request = async () => undefined, inbox) {
  const projects = [{id: 'a', name: 'Repo A', path: 'a'}, {id: 'b', name: 'Repo B', path: 'b'}];
  const state = {view: 'projects', bot: 'default', bots: [{id: 'default', name: 'Default'}, {id: 'other', name: 'Other'}]};
  const elements = new Map([['#page', {innerHTML: ''}]]);
  const rails = projects.map(project => ({dataset: {project: project.id}}));
  const dialogs = [], calls = [];
  const previousDocument = globalThis.document;
  globalThis.document = {querySelectorAll: selector => selector === '[data-project]' ? rails : []};
  t.after(() => {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, {});
    return elements.get(selector);
  };
  const ui = projectsView({
    inbox,
    api: async (path, method = 'GET', body) => {
      calls.push({path, method, body});
      const result = await request(path, method, body);
      if (result !== undefined) return result;
      if (path === '/api/projects') return {projects};
      if (/^\/api\/projects\/[ab]\/workspaces$/.test(path)) return [];
      throw new Error('Unexpected request: ' + method + ' ' + path);
    },
    state,
    $: get,
    escape: String,
    icon: () => '',
    badge: String,
    field: () => '',
    dialog: (title, content) => {
      const form = {};
      const dialog = {title, content, form, open: true, querySelector: () => form, close() {this.open = false;}};
      dialogs.push(dialog);
      return dialog;
    },
    bind: (form, handler) => {form.submit = handler;},
    action: (element, handler) => {if (element) element.click = handler;},
    toast: () => {},
    navigate: () => {},
    pageScope: view => () => state.view === view,
  });
  return {ui, get, rails, dialogs, calls};
}

test('late repository response cannot change the latest selection or task destination', async t => {
  const requestedB = deferred(), responseB = deferred();
  const h = harness(t, async path => {
    if (path === '/api/projects/b/workspaces') {
      requestedB.resolve();
      return responseB.promise;
    }
  });
  await h.ui.render();
  const loadingB = h.rails[1].click();
  await requestedB.promise;
  await h.rails[0].click();
  responseB.resolve([]);
  await loadingB;

  assert.match(h.get('#page').innerHTML, /project-entry selected" data-project="a"/);
  assert.match(h.get('#page').innerHTML, /<h2>Repo A<\/h2>/);
  h.get('#new-project-task').click();
  assert.equal(h.dialogs.at(-1).title, 'New task in Repo A');
});

test('an unstored validation failure allows a corrected task with a new request ID', async t => {
  let submissions = 0;
  const h = harness(t, async (path, method) => {
    if (method !== 'POST') return;
    assert.equal(path, '/api/projects/a/tasks');
    if (++submissions === 1) {
      throw Object.assign(new Error('Enable Projects for this bot.'), {status: 400, requestStored: false});
    }
    return {id: 'task', workspace_id: 'workspace'};
  });
  await h.ui.render();
  h.get('#new-project-task').click();
  const dialog = h.dialogs.at(-1);
  const original = {title: 'Feature', bot_id: 'default', prompt: 'Implement a feature'};
  await assert.rejects(dialog.form.submit(original), /Enable Projects/);
  assert.equal(dialog.open, true);
  const corrected = {...original, bot_id: 'other'};
  await dialog.form.submit(corrected);

  const requests = h.calls.filter(call => call.method === 'POST');
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.bot_id, 'other');
  assert.notEqual(requests[0].body.request_id, requests[1].body.request_id);
  assert.equal(dialog.open, false);
});

for (const [label, metadata] of [
  ['persisted request failure', {status: 400, requestStored: true}],
  ['response loss', {}],
  ['HTTP failure without request state', {status: 500}],
]) {
  test(label + ' rejects edited retries and reuses the original request ID', async t => {
    let submissions = 0;
    const h = harness(t, async (_path, method) => {
      if (method !== 'POST') return;
      if (++submissions === 1) throw Object.assign(new Error('Request failed'), metadata);
      return {id: 'task', workspace_id: 'workspace'};
    });
    await h.ui.render();
    h.get('#new-project-task').click();
    const dialog = h.dialogs.at(-1);
    const original = {title: 'Feature', bot_id: 'default', prompt: 'Implement a feature'};
    await assert.rejects(dialog.form.submit(original), /Request failed/);
    await assert.rejects(dialog.form.submit({...original, prompt: 'Different feature'}), /previous request may have started/);
    assert.equal(submissions, 1);
    assert.equal(dialog.open, true);
    await dialog.form.submit({...original});

    const requests = h.calls.filter(call => call.method === 'POST');
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1].body, requests[0].body);
    assert.equal(dialog.open, false);
  });
}


test('Projects opens on folders and preserves the selected folder across both views', async t => {
  let inboxOptions, inboxCalls = 0;
  const h = harness(t, undefined, {render: async options => {inboxOptions=options; inboxCalls++;}});
  await h.ui.render();
  assert.equal(inboxCalls, 0);
  assert.match(h.get('#page').innerHTML, /id="coding-workspaces"[^>]+aria-selected="true"/);
  assert.match(h.get('#page').innerHTML, /id="show-github-inbox"[^>]+aria-selected="false"/);
  await h.rails[1].click();
  await h.get('#show-github-inbox').click();
  assert.equal(inboxCalls, 1);
  assert.equal(inboxOptions.isActive(), true);
  await inboxOptions.showCoding();
  assert.equal(inboxOptions.isActive(), false);
  assert.match(h.get('#page').innerHTML, /project-entry selected" data-project="b"/);
  h.get('#new-project-task').click();
  assert.equal(h.dialogs.at(-1).title, 'New task in Repo B');
});
