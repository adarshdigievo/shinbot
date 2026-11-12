import test from 'node:test';
import assert from 'node:assert/strict';
import {browserPoint} from '../src/shinbot/web/browser-view.js';

test('scaled browser input accounts for image letterboxing', () => {
  const rect={left:10,top:20,width:400,height:400}, size={width:1280,height:800};
  assert.deepEqual(browserPoint(rect,size,210,220),{x:640,y:400});
  assert.equal(browserPoint(rect,size,210,40),null);
  assert.equal(browserPoint(rect,size,410,220),null);
  assert.equal(browserPoint({...rect,width:0},size,10,20),null);
});
