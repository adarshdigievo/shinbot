import { createHash, randomUUID } from 'node:crypto';
import { Type } from '@earendil-works/pi-ai';
import { defineExtension, defineTool, section, type ToolExecutionApi } from '@earendil-works/pi-durable';
import type { Context } from '@earendil-works/chord';
import { toLlmContent } from '@earendil-works/pi-mcp';

export type Bridge = {
  epoch: () => string;
  task: (conversationId: number) => Promise<string>;
  call: (path: string, payload: unknown, signal?: AbortSignal) => Promise<any>;
};
const s = Type.String();
const opt = (schema: any) => Type.Optional(schema);
const object = (properties: Record<string, any>) => Type.Object(properties, { additionalProperties: false });

export const specifications = [
  {name:'shinbot_help',description:'Read current ShinBot product help. Read the plugins topic before authoring a plugin; it includes the manifest schema and a complete executable MCP example. Use status for live configuration and bots list for current profiles.',parameters:object({topic:Type.Union(['overview','bots','plugins','approvals','storage','updates'].map(value=>Type.Literal(value)))})},
  {name:'updates',description:'Read the last release check or check official GitHub and PyPI for a new stable ShinBot version. Read-only; applying an update requires the owner to run the host CLI, not a Computer command.',parameters:object({operation:Type.Union([Type.Literal('status'),Type.Literal('check')])})},
  {name:"bots", description:"List bot profiles and templates. Create or edit profiles, export a bot or group, save a portable template to the local library, or install one after approval. Profiles define a name, role, starter prompts and browser sharing (private, group, shared). Does not copy credentials, conversations or memories.",parameters:object({operation:Type.Union(["list","templates","save","install","export","save_template"].map(value=>Type.Literal(value))),id:opt(s),group_id:opt(s),profile:opt(Type.Record(s,Type.Any())),template:opt(Type.Record(s,Type.Any()))})},
  { name: 'status', description: 'Read ShinBot identity, settings, permissions, jobs and available capabilities.', parameters: object({}) },
  { name: 'list_files', description: 'List files using a relative workspace path, such as plugins/example. Use . for the workspace root.', parameters: object({ path: opt(s) }) },
  { name: 'read_file', description: 'Read a UTF-8 file using a relative workspace path. Do not prefix /workspace or use parent traversal.', parameters: object({ path: s }) },
  { name: 'attach_file', description: 'Attach an existing Computer workspace file to this conversation. Use a relative path; optional name changes its displayed filename. Images appear inline and other files can be downloaded. Limit 10 MB. Create files with write_file or an approved command first. Attach only files meant for the owner, never credentials. The UI displays the attachment automatically; do not repeat it as a Markdown image. For a browser screenshot, use browser operation screenshot instead.', parameters: object({ path: s, name: opt(s) }) },
  { name: 'write_file', description: 'Write UTF-8 text using a relative workspace path such as plugins/example/server.py. Do not prefix /workspace. Does not change the running ShinBot application.', parameters: object({ path: s, content: s }) },
  { name: 'run_command', description: 'Run a shell command in the Computer workspace. Requires permission; commands have network and workspace access.', parameters: object({ command: s, timeout: opt(Type.Integer({ minimum: 1, maximum: 300 })) }) },
  { name: 'desktop', description: 'Use the optional Linux desktop. Check status browser.desktop first; when unavailable use browser and file tools. Observe returns a full screen image. Screenshot attaches it to chat. Launch opens files, terminal or browser. Click/type/press/scroll require a fresh desktop observation. Coordinates refer to the 1280x800 screen. Human takeover blocks these tools. Auto and Ask require approval for desktop input.', parameters: object({ operation: Type.Union(['observe','screenshot','launch','click','type','press','scroll'].map(value => Type.Literal(value))), application: opt(Type.Union(['files','terminal','browser'].map(value => Type.Literal(value)))), x: opt(Type.Integer()), y: opt(Type.Integer()), button: opt(s), text: opt(s), key: opt(s), direction: opt(s), amount: opt(Type.Integer()), name: opt(s) }) },
  { name: 'browser', description: 'Use the shared browser. Observe returns visible text and an image for the model only. Use screenshot to capture the current viewport and attach it visibly to this chat, optionally with name. Human takeover blocks agent access. Use current observed selectors; never act on a stale page. Close releases your browser and keeps saved sign-ins. Successful tasks close their browser automatically unless disabled in Settings.', parameters: object({ operation: Type.Union(['observe','goto','tabs','click','fill','press','back','close','screenshot'].map(value => Type.Literal(value))), url: opt(s), selector: opt(s), text: opt(s), key: opt(s), name: opt(s) }) },
  { name: 'notes', description: 'Search, save or delete visible notebook pages. Supply the current revision when editing.', parameters: object({ operation: Type.Union(['search','save','delete'].map(value => Type.Literal(value))), query: opt(s), id: opt(s), title: opt(s), body: opt(s), revision: opt(Type.Integer()) }) },
  { name: 'memory', description: 'Search explicitly saved personal facts; remember requested useful facts or forget a memory by ID.', parameters: object({ operation: Type.Union(['search','remember','forget'].map(value => Type.Literal(value))), query: opt(s), text: opt(s), id: opt(s) }) },
  { name: 'jobs', description: 'List, create or pause scheduled responsibilities. A job inherits this task\'s permissions.', parameters: object({ operation: Type.Union(['list','create','pause'].map(value => Type.Literal(value))), id: opt(s), job: opt(object({ name:s,prompt:s,kind:Type.Union(['cron','interval','once'].map(value => Type.Literal(value))),spec:s,timezone:s })) }) },
  { name: 'plugins', description: 'Discover marketplace plugins, read skills, stage a tested Computer directory with operation=stage,path=plugins/example or using files {"shinbot-plugin.json":"<manifest JSON>","LICENSE":"<license text>","server.py":"<full code>"}, install a catalog version or activate it. A package needs shinbot-plugin.json and LICENSE. Use kind skill with SKILL.md, mcp with a Python tool server, or extension for sidebar pages, themes, UI patches and lifecycle hooks. Read shinbot_help plugins for the UI SDK and hook contract. Connect MCP using server {transport:stdio,command,args} or {transport:http|sse,url}; enter secrets through Plugins UI. Install standard skills with command npx skills add SOURCE --skill NAME and the verified upstream license. Select access {mode:bots,bots:[BOT_ID]} or {mode:shared,bots:[]}. Inspect another agent’s public GitHub link with inspect_source {url}. Import a portable skill or command with import_source {url:pinned commit URL,path:candidate path}. Executable extensions need an adapter, dependency review and tests before staging. New capabilities load in new chats. Read skill references using path. Use learn {name,description,instructions,evidence:[action IDs]} to stage a tested reusable procedure for this bot; status lists evidence IDs. One proposal per task, three per bot per day. Omit secrets and task-specific personal facts. Additional packaged dependencies require a host-reviewed runner.', parameters: object({ operation: Type.Union(['search','read_skill','stage','install','activate','connect_mcp','install_skills','set_access','inspect_source','import_source','learn'].map(value => Type.Literal(value))), id: opt(s), version:opt(s), marketplace:opt(s), url:opt(s), path:opt(s), name:opt(s), command:opt(s), license:opt(s), server:opt(Type.Record(s,Type.Any())), access:opt(object({mode:Type.Union([Type.Literal('shared'),Type.Literal('bots')]),bots:Type.Array(s)})), description:opt(s), instructions:opt(s), evidence:opt(Type.Array(s)), files: opt(Type.Record(s,s)) }) },
];

function remoteTool(spec: {name: string; description: string; parameters: any}, remoteName: string, bridge: Bridge) {
  return defineTool({ ...spec, replay: 'safe', execute: async (args: any, api: ToolExecutionApi, context: Context) => {
    const identity = await api.memo('shinbot-action', { id: randomUUID(), epoch: bridge.epoch() }, context);
    let waitUntil: number | undefined;
    const taskId = await bridge.task(api.conversationId);
    const payload = { ...identity, task_id: taskId, tool: remoteName, args };
    let lastStatus = "";
    let delay = 500;
    while (true) {
      context.abortSignal?.throwIfAborted();
      const result = await bridge.call('/internal/actions', payload, context.abortSignal);
      if(result.status==='pending' && result.error==='Waiting for the shared browser.') waitUntil ??= await api.memo('shinbot-browser-wait-until', Date.now()+600000, context);
      if(result.status==='pending' && result.error==='Waiting for the shared browser.' && waitUntil !== undefined && Date.now()>waitUntil) return {content:[{type:'text' as const,text:'The browser is still in use. Ask the owner to hand back control or finish the other task, then try again.'}],isError:true};
      if (result.status === 'completed') {
        const content = Array.isArray(result.result?.content) ? toLlmContent(result.result) : [{ type: 'text' as const, text: JSON.stringify(result.result) }];
        return { content: content, isError: result.result?.isError === true };
      }
      if (!['pending','approved','executing','retrying'].includes(result.status)) {
        return { content: [{ type: 'text' as const, text: result.error || `Action ${result.status}. Do not repeat uncertain actions.` }], isError: true };
      }
      if (lastStatus !== result.status) {
        await api.details({ actionId: identity.id, status: result.status }, context);
        lastStatus = result.status; delay = 500;
      } else delay = Math.min(delay * 1.5, 5000);
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(context.abortSignal?.reason); };
        const timer = setTimeout(() => { context.abortSignal?.removeEventListener('abort', abort); resolve(); }, delay);
        context.abortSignal?.addEventListener('abort', abort, { once: true });
      });
    }
  }});
}

export function coreExtension(bridge: Bridge) {
  return defineExtension({ name: 'shinbot-core-v1', sections: [section('identity', () => 'Use the provided general-purpose tools and respect persisted permissions. Your name and role come from your bot profile.', {tag:false})],
    tools: specifications.map(spec => remoteTool(spec, spec.name, bridge)) });
}

export function pluginExtension(plugin: any, bridge: Bridge) {
  const name = `plugin:${plugin.id}:${plugin.version}`;
  const tools = ['mcp','builtin','extension'].includes(plugin.kind) ? plugin.tools.map((tool: any) => remoteTool({
    name: plugin.server ? `mcp__${plugin.id.replaceAll('-', '_').slice(0,20)}__${tool.name.replace(/[^a-zA-Z0-9_]/g,'_').slice(0,24)}_${createHash('sha256').update(plugin.id+'\0'+tool.name).digest('hex').slice(0,8)}` : `plg__${plugin.id.replaceAll('-', '_')}__${tool.name}`, description: tool.description || tool.name,
    parameters: Type.Unsafe(plugin.server ? tool.input_schema : { ...tool.input_schema, additionalProperties: false }),
  }, `${name}:${tool.name}`, bridge)) : [];
  return defineExtension({ name, tools });
}
