export function updatesPanel(data, escape, date) {
  const native = data.installation === 'native';
  const available = data.status === 'available' && !native;
  const titles = {available:'An update is available', current:'You’re up to date', ahead:'Running a newer version', not_published:'No published release yet', publishing:'Release is being published', incompatible:'Python upgrade needed', unavailable:'Couldn’t check for updates', unchecked:'Check for updates'};
  return `<section id="updates-panel" data-settings-panel="updates" role="tabpanel" aria-labelledby="updates-tab" class="settings-section">
    <h2>Updates</h2><p class="muted">Keep ShinBot current. You choose when to install.</p>
    <div class="update-summary" role="status"><h3>${titles[data.status] || titles.unavailable}</h3><p>${escape(data.message)}</p>
    <dl class="update-versions"><div><dt>Installed</dt><dd>${escape(data.current_version)}</dd></div><div><dt>Latest stable</dt><dd>${escape(data.latest_version || 'Not verified')}</dd></div><div><dt>Last checked</dt><dd>${data.checked_at ? escape(date(data.checked_at)) : 'Not yet'}${data.stale ? ' · Check again for current information' : ''}</dd></div></dl></div>
    <div class="row"><button id="check-updates" type="button">Check now</button><a href="${escape(data.release_url)}" target="_blank" rel="noopener noreferrer">Release notes</a></div>
    <label class="checkbox-row space-top"><input id="automatic-updates" type="checkbox" ${data.automatic ? 'checked' : ''}>Check for updates daily</label><p class="hint">Checks contact GitHub and PyPI. They don’t send your chats or settings, or install anything.</p>
    ${available ? `<div class="update-install"><h3>Update on your server</h3><p>Run this in the terminal where you installed ShinBot. The updater saves a backup, checks the new services, and restores the previous release if startup fails.</p><div class="row"><code id="update-command">${escape(data.update_command)}</code><button id="copy-update-command" type="button">Copy command</button></div><p class="hint">Use the host terminal, not the bot’s Computer. Source installations should update their checkout and run <code>uv run --frozen shinbot update --build</code>.</p></div>` : ''}
    ${native ? '<p class="hint space-top">This Mac runs a native source installation. Stop it with <code>shinbot native stop</code>, back up its data directory, update the Python package and Pi runtime, then run <code>shinbot native start</code>. Your data stays in <code>~/.shinbot/native</code>. Keep the previous code and data copy for recovery.</p>' : '<p class="hint space-top">If an update fails, run <code>shinbot rollback</code> on the host. Backups and named volumes remain outside your Python environment.</p>'}
  </section>`;
}
