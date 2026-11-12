import assert from 'node:assert/strict';
import test from 'node:test';
import { pluginExtension } from '../dist/tools.js';

test('remote MCP aliases remain unique and preserve open input schemas', () => {
  const schema={type:'object',additionalProperties:{type:'string'}};
  const make=id=>pluginExtension({id,version:'1.0.0',kind:'mcp',server:{},tools:[{name:'search.tool',input_schema:schema}]},{}).tools[0];
  const a=make('github-enterprise-mcp-work'),b=make('github-enterprise-mcp-home');
  assert.notEqual(a.name,b.name);
  assert.ok(a.name.length<=64 && /^[a-zA-Z0-9_]+$/.test(a.name));
  assert.deepEqual(a.parameters,schema);
});

test('extension packages can combine UI contributions with runtime tools', () => {
  const plugin={id:'paint',version:'1.0.0',kind:'extension',ui:{pages:[{id:'canvas'}]},tools:[{name:'export_drawing',description:'Export drawing',input_schema:{type:'object',properties:{}}}]};
  const extension=pluginExtension(plugin,{});
  assert.equal(extension.tools[0].name,'plg__paint__export_drawing');
  assert.equal(extension.tools[0].parameters.additionalProperties,false);
  assert.equal(pluginExtension({...plugin,tools:[]},{}).tools.length,0);
});
