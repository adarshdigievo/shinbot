import assert from 'node:assert/strict';
import test from 'node:test';
import {browserOwner, clampPosition, previewVisibility} from '../src/shinbot/web/computer.js';

const state = {
  bot: 'selected',
  tasks: [{id: 'running', bot_id: 'research'}],
  bots: [{id: 'selected', name: 'Personal assistant', browser_id: 'shared'}, {id: 'research', name: 'Research partner', browser_id: 'shared'}],
};

test('browser ownership follows the running task even when profiles are shared and another bot is selected', () => {
  assert.equal(browserOwner({browser_id: 'shared', lease: {kind: 'agent', owner: 'running'}}, state), 'Research partner is browsing');
});

test('browser labels distinguish human control, an unknown agent and an idle view', () => {
  assert.equal(browserOwner({lease: {kind: 'human', owner: 'session'}}, state), 'You have control');
  assert.equal(browserOwner({browser_id: 'unlisted', lease: {kind: 'agent', owner: 'unlisted'}}, state), 'A bot is browsing');
  assert.equal(browserOwner({browser_id: 'shared', lease: null}, state), 'View only');
});

test('a moved preview stays fully inside the window, including after the window shrinks', () => {
  const size = {width: 320, height: 240}, viewport = {width: 1440, height: 960};
  assert.deepEqual(clampPosition({x: 400, y: 300}, size, viewport), {x: 400, y: 300});
  assert.deepEqual(clampPosition({x: -40, y: -10}, size, viewport), {x: 8, y: 8});
  assert.deepEqual(clampPosition({x: 1300, y: 900}, size, viewport), {x: 1112, y: 712});
  // A preview wider than a phone screen pins to the left margin rather than going off screen.
  assert.deepEqual(clampPosition({x: 100, y: 100}, {width: 400, height: 240}, {width: 390, height: 844}), {x: 8, y: 100});
});

test('the preview and its restore chip are never shown together, and neither appears on Computer', () => {
  assert.deepEqual(previewVisibility({active: true, view: 'chat', minimized: false}), {panel: true, chip: false});
  assert.deepEqual(previewVisibility({active: true, view: 'settings', minimized: true}), {panel: false, chip: true});
  assert.deepEqual(previewVisibility({active: true, view: 'computer', minimized: false}), {panel: false, chip: false});
  assert.deepEqual(previewVisibility({active: true, view: 'computer', minimized: true}), {panel: false, chip: false});
  assert.deepEqual(previewVisibility({active: false, view: 'chat', minimized: false}), {panel: false, chip: false});
  assert.deepEqual(previewVisibility({active: false, view: 'notebook', minimized: true}), {panel: false, chip: false});
});
