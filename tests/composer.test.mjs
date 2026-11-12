import test from 'node:test';
import assert from 'node:assert/strict';
import {bindComposerKeys} from '../src/shinbot/web/composer.js';

function composer() {
  const input = new EventTarget(), button = {disabled:false};
  let sends = 0;
  input.value = 'A message';
  input.form = {querySelector:()=>button, requestSubmit:()=>{sends++;}};
  bindComposerKeys(input);
  function key(properties={}) {
    const event = new Event('keydown', {cancelable:true});
    Object.assign(event, {key:'Enter'}, properties);
    input.dispatchEvent(event);
    return event;
  }
  return {input, button, key, sends:()=>sends};
}

test('Enter sends; Shift+Enter and Alt+Enter retain native editing', () => {
  const c = composer();
  assert.equal(c.key().defaultPrevented, true);
  assert.equal(c.sends(), 1);
  assert.equal(c.key({shiftKey:true}).defaultPrevented, false);
  assert.equal(c.key({altKey:true}).defaultPrevented, false);
  assert.equal(c.sends(), 1);
  c.key({metaKey:true});
  c.key({ctrlKey:true});
  assert.equal(c.sends(), 3);
});

test('IME confirmation never sends, including the Safari keyCode fallback', () => {
  const c = composer();
  c.input.dispatchEvent(new Event('compositionstart'));
  assert.equal(c.key().defaultPrevented, false);
  c.input.dispatchEvent(new Event('compositionend'));
  assert.equal(c.key({isComposing:true}).defaultPrevented, false);
  assert.equal(c.key({keyCode:229}).defaultPrevented, false);
  assert.equal(c.sends(), 0);
  c.key();
  assert.equal(c.sends(), 1);
});

test('Holding Enter, an in-flight send, and empty drafts do not submit', () => {
  const c = composer();
  c.key({repeat:true});
  c.button.disabled = true;
  c.key();
  c.button.disabled = false;
  c.input.value = ' \n ';
  c.key();
  assert.equal(c.sends(), 0);
});

test('Multiline text stays intact and is sent only on Enter', () => {
  const c = composer();
  c.input.value = 'First line\nSecond line';
  c.input.dispatchEvent(new Event('input'));
  c.key({key:'a'});
  assert.equal(c.sends(), 0);
  c.key();
  assert.equal(c.sends(), 1);
  assert.equal(c.input.value, 'First line\nSecond line');
});

test('Tab accepts a suggestion without submitting; Shift+Tab and IME retain native behavior', () => {
  const input = new EventTarget();
  let accepted = 0, dismissed = 0;
  bindComposerKeys(input, {acceptSuggestion:()=>{accepted++;return true;}, dismissSuggestion:()=>{dismissed++;return true;}});
  const key = properties => { const e = new Event('keydown', {cancelable:true}); Object.assign(e, properties); input.dispatchEvent(e); return e; };
  assert.equal(key({key:'Tab',shiftKey:true}).defaultPrevented, false);
  assert.equal(key({key:'Tab',isComposing:true}).defaultPrevented, false);
  assert.equal(key({key:'Tab',ctrlKey:true}).defaultPrevented, false);
  assert.equal(key({key:'Tab',repeat:true}).defaultPrevented, false);
  assert.equal(accepted, 0);
  assert.equal(key({key:'Tab'}).defaultPrevented, true);
  assert.equal(accepted, 1);
  assert.equal(key({key:'Escape'}).defaultPrevented, true);
  assert.equal(dismissed, 1);
});

test('Tab and Escape keep their normal behavior when no suggestion is available', () => {
  const input = new EventTarget();
  bindComposerKeys(input, {acceptSuggestion:()=>false, dismissSuggestion:()=>false});
  for (const name of ['Tab','Escape']) {
    const e = new Event('keydown', {cancelable:true}); Object.assign(e, {key:name}); input.dispatchEvent(e);
    assert.equal(e.defaultPrevented, false);
  }
});
