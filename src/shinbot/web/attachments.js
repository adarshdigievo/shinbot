export function renderAttachment(file, {escape, icon, messageTime}) {
  // Accept only this app's authenticated file routes, including for image previews.
  if (!/^\/api\/tasks\/[a-zA-Z0-9_-]+\/attachments\/[a-f0-9]{32}$/.test(file.url || '')) return '';
  const url=escape(file.url),name=escape(file.name),size=Number(file.size)||0;
  const label=size>=1000000?`${(size/1000000).toFixed(1)} MB`:size>=1000?`${Math.ceil(size/1000)} KB`:`${size} B`;
  const preview=file.preview && ['image/png','image/jpeg','image/gif','image/webp'].includes(file.mime_type);
  return `<figure class="chat-attachment ${preview?'image-attachment':''}" data-entry="attachment-${escape(file.id)}">
    ${preview?`<a class="attachment-preview" href="${url}" target="_blank" rel="noopener" aria-label="Open ${name}"><img src="${url}" alt="${name}" loading="lazy"></a>`:''}
    <figcaption>${icon('file-text')}<span class="attachment-info"><strong>${name}</strong><small>${label}${messageTime(file.created)}</small></span><a class="attachment-download" href="${url}?download=true" download="${name}">Download</a></figcaption>
  </figure>`;
}
