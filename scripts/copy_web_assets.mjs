import fs from 'node:fs/promises';
import path from 'node:path';
const root=path.resolve(import.meta.dirname,'..');
const vendor=path.join(root,'src/shinbot/web/vendor');
await fs.mkdir(vendor,{recursive:true});
for(const [source,target] of [
  ['markdown-it/dist/markdown-it.min.js','markdown-it.min.js'],
  ['markdown-it/LICENSE','markdown-it.LICENSE'],
  ['dompurify/dist/purify.min.js','purify.min.js'],
  ['dompurify/LICENSE','dompurify.LICENSE'],
]) await fs.copyFile(path.join(root,'runtime/node_modules',source),path.join(vendor,target));
await fs.cp(path.join(root,'runtime/node_modules/@novnc/novnc'),path.join(vendor,'novnc'),{recursive:true});
const icons=['pencil','folder','folder-plus','folder-git-2','git-branch','columns-3','bot','house','message-circle','monitor','notebook-pen','clock','puzzle','settings','menu','x','plus','arrow-up-right','arrow-right','refresh-cw','loader-circle','circle-check','chevron-right','chevron-down','search','square-pen','server','panel-left','panel-right','sliders-horizontal','arrow-up','send-horizontal','compass','list-collapse','square','brain','file-text','plug','shield-check','key-round','user-round','sun','moon','ellipsis','eye','eye-off','check','bell','globe','terminal'];
const lucide=path.join(root,'runtime/node_modules/lucide-static');
await fs.mkdir(path.join(vendor,'lucide'),{recursive:true});
for(const name of icons) await fs.copyFile(path.join(lucide,'icons',name+'.svg'),path.join(vendor,'lucide',name+'.svg'));
await fs.copyFile(path.join(lucide,'LICENSE'),path.join(vendor,'lucide','LICENSE'));
await fs.writeFile(path.join(vendor,'icons.css'),icons.map(name=>`.icon-${name}{mask-image:url('./lucide/${name}.svg')}`).join('\n')+'\n');
console.log('Copied unmodified browser assets and their license files.');
