export function browserOwner(browser, state) {
  const task = state.tasks.find(task => task.id === browser?.lease?.owner);
  const bot = state.bots.find(bot => bot.id === task?.bot_id)
    || state.bots.find(bot => bot.browser_id === browser?.browser_id);
  return browser?.lease?.kind === 'human' ? (browser.control_allowed===false?'Browser in use on another device':'You have control')
    : browser?.lease?.kind === 'agent' ? `${bot?.name || 'A bot'} is ${browser.desktop ? 'using the computer' : 'browsing'}` : 'View only';
}

// A floating preview always stays fully on screen, even when the window shrinks.
export function clampPosition(position, size, viewport, margin = 8) {
  return {
    x: Math.max(margin, Math.min(position.x, viewport.width - size.width - margin)),
    y: Math.max(margin, Math.min(position.y, viewport.height - size.height - margin)),
  };
}

// While a lease is active, exactly one of the preview and the top-bar chip is shown.
// The Computer page shows the full browser, so neither appears there.
export function previewVisibility({active, view, minimized}) {
  const available = active && view !== 'computer';
  return {panel: available && !minimized, chip: available && minimized};
}

export function computerPreview({api, state, icon, navigate}) {
  let panel, screen, rfb, timer, generation = 0, connection = 0, connecting = false;
  let activity = null, owner = null, minimized = false, previousView = 'chat', error = '';
  let placement = 'dock', position = null, nextRetry = 0;
  let endDrag = null;
  let inspectorTarget = null, inspectorTask = null;
  const wide = matchMedia('(min-width: 761px)');
  const status = text => { if (panel) panel.querySelector('[data-preview-status]').textContent = text; };
  const inspecting = () => inspectorTarget?.isConnected && inspectorTask && activity
    && (activity.lease?.owner === inspectorTask.id || activity.browser_id === inspectorTask.browser_id);

  function disconnect() {
    connection++;
    connecting = false;
    const old = rfb;
    rfb = null;
    old?.disconnect();
    screen?.replaceChildren();
  }

  function stop() {
    endDrag?.();
    generation++;
    clearTimeout(timer);
    disconnect();
    panel?.remove();
    panel = null;
    activity = null;
    inspectorTarget = null;
    inspectorTask = null;
    owner = null;
    minimized = false;
    placement = 'dock';
    position = null;
    error = '';
    nextRetry = 0;
    window.removeEventListener('resize', place);
    wide.removeEventListener('change', home);
  }

  // Docked previews live in the shell's own flow: the sidebar on wide screens and
  // under the top bar on narrow ones, so they never cover headings or the composer.
  function home() {
    if (!panel) return;
    const inspected = inspecting();
    const floating = !inspected && placement === 'float';
    const target = inspected ? inspectorTarget : document.querySelector(floating ? '.layout' : wide.matches ? '#computer-dock' : '#computer-dock-mobile');
    if (target && panel.parentElement !== target) {
      const focused = panel.contains(document.activeElement) ? document.activeElement : null;
      target.append(panel);
      focused?.focus({preventScroll: true});
    }
    panel.classList.toggle('floating', floating);
    panel.classList.toggle('inspected', !!inspected);
    panel.querySelector('[data-preview-dock]').hidden = !floating;
    if (floating) place();
    else panel.removeAttribute('style');
  }

  function place() {
    if (!panel || inspecting() || placement !== 'float' || !position || panel.hidden) return;
    position = clampPosition(position, panel.getBoundingClientRect(), {width: innerWidth, height: innerHeight});
    Object.assign(panel.style, {left: position.x + 'px', top: position.y + 'px'});
  }

  function moveBy(dx, dy) {
    if (!panel || panel.hidden || inspecting()) return;
    if (placement !== 'float') {
      const bounds = panel.getBoundingClientRect();
      placement = 'float';
      position = {x: bounds.left, y: bounds.top};
      home();
    }
    position = {x: position.x + dx, y: position.y + dy};
    place();
  }

  function dock() {
    placement = 'dock';
    position = null;
    home();
    panel.querySelector('.preview-drag').focus();
  }

  function update() {
    if (!panel) return;
    const active = !!activity?.lease || !!inspecting();
    const shown = previewVisibility({active, view: state.view, minimized});
    panel.hidden = !shown.panel;
    const title = browserOwner(activity, state);
    panel.querySelector('[data-preview-owner]').textContent = title;
    const handle = panel.querySelector('.preview-drag');
    handle.title = inspecting() ? title : title + '. Drag to move. Arrow keys also move it.';
    handle.tabIndex = inspecting() ? -1 : 0;
    panel.setAttribute('aria-label', 'Browser preview. ' + title);
    const address = panel.querySelector('[data-preview-address]');
    address.textContent = activity?.url || '';
    address.title = activity?.url || '';
    const chip = document.querySelector('#header-computer');
    if (chip) {
      chip.hidden = !shown.chip;
      chip.title = title + '. Show the browser preview.';
    }
    if (panel.hidden) { endDrag?.(); disconnect(); }
    else if (!rfb && !connecting && Date.now() >= nextRetry) void connect();
    home();
  }

  async function connect() {
    if (!panel || panel.hidden || connecting) return;
    const id = ++connection;
    const current = () => id === connection && panel && !panel.hidden;
    connecting = true;
    status('Connecting…');
    try {
      const view = await api('/api/browser/view');
      if (!current()) return;
      screen.replaceChildren();
      if (activity.headless || activity.native_view) {
        if(view.empty||!view.screenshot){const message=document.createElement('p');message.className='muted';message.textContent='Browser ready';screen.append(message);status('Waiting for a website');return;}
        const image = document.createElement('img');
        image.alt = 'Current agent browser page';
        image.src = 'data:image/jpeg;base64,' + view.screenshot;
        screen.append(image);
        status('View only');
        return;
      }
      const {default: RFB} = await import('/static/vendor/novnc/core/rfb.js');
      if (!current()) return;
      const viewer = new RFB(screen, `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/browser/vnc?mode=view`, {credentials: {password: view.password}});
      rfb = viewer;
      viewer.scaleViewport = true;
      viewer.resizeSession = false;
      viewer.viewOnly = true;
      viewer.addEventListener('connect', () => { if (current()) { error = ''; status('Live · View only'); } });
      viewer.addEventListener('disconnect', () => {
        if (!current()) return;
        rfb = null;
        screen.replaceChildren();
        nextRetry = Date.now() + 3000;
        status('Reconnecting…');
      });
    } catch (failure) {
      if (current()) { error = failure.message; status('Browser unavailable. Retrying…'); nextRetry = Date.now() + 3000; }
    } finally {
      if (id === connection) connecting = false;
    }
  }

  async function poll(id) {
    try {
      const next = await api('/api/browser/activity');
      if (id !== generation) return;
      activity = next;
      document.dispatchEvent(new CustomEvent('shinbot:browser-activity', {detail:{activity:next}}));
      const nextOwner = next.lease ? next.lease.kind + ':' + next.lease.owner : null;
      if (nextOwner !== owner) { minimized = false; owner = nextOwner; nextRetry = 0; }
      error = '';
      update();
    } catch (failure) {
      if (id !== generation) return;
      error = failure.message;
      document.dispatchEvent(new CustomEvent('shinbot:browser-activity', {detail:{error}}));
      // Do not leave a frozen frame labelled as live when the service is unreachable.
      disconnect();
      status('Connection lost. Retrying…');
    } finally {
      if (id === generation) timer = setTimeout(() => poll(id), error ? 4000 : 1500);
    }
  }

  function mount() {
    stop();
    panel = document.createElement('aside');
    panel.id = 'computer-preview';
    panel.className = 'computer-preview';
    panel.setAttribute('aria-label', 'Browser preview');
    panel.hidden = true;
    panel.innerHTML = `<div class="preview-toolbar"><button type="button" class="preview-drag" aria-label="Move browser preview" title="Drag to move. Arrow keys also move it.">${icon('monitor')}<span data-preview-owner></span></button><button type="button" class="quiet icon-button" data-preview-dock aria-label="Dock browser preview" title="Dock" hidden>${icon('panel-left')}</button><button type="button" class="quiet icon-button" data-preview-toggle aria-label="Minimize browser preview" title="Minimize">${icon('chevron-down')}</button><button type="button" class="quiet icon-button" data-preview-expand aria-label="Expand browser" title="Open in Computer">${icon('arrow-up-right')}</button></div><div class="preview-screen" aria-label="Read-only browser stream"></div><div class="preview-footer"><span data-preview-status role="status">Connecting…</span><span data-preview-address></span></div>`;
    screen = panel.querySelector('.preview-screen');
    panel.querySelector('[data-preview-toggle]').onclick = () => {
      minimized = true;
      update();
      document.querySelector('#header-computer')?.focus();
    };
    panel.querySelector('[data-preview-expand]').onclick = expand;
    panel.querySelector('[data-preview-dock]').onclick = dock;
    const handle = panel.querySelector('.preview-drag');
    // Window listeners keep a drag alive while the preview moves out of its dock.
    handle.onpointerdown = event => {
      if (event.button !== 0 || inspecting()) return;
      endDrag?.();
      let last = {x: event.clientX, y: event.clientY}, moving = false;
      const move = next => {
        if (!moving && Math.hypot(next.clientX - event.clientX, next.clientY - event.clientY) < 4) return;
        moving = true;
        panel?.classList.add('dragging');
        moveBy(next.clientX - last.x, next.clientY - last.y);
        last = {x: next.clientX, y: next.clientY};
      };
      const end = () => {
        panel?.classList.remove('dragging');
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', end);
        window.removeEventListener('pointercancel', end);
        endDrag = null;
      };
      endDrag = end;
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', end);
      window.addEventListener('pointercancel', end);
    };
    handle.onkeydown = event => {
      const direction = {ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20]}[event.key];
      if (!direction) return;
      event.preventDefault();
      moveBy(...direction);
    };
    home();
    window.addEventListener('resize', place);
    wide.addEventListener('change', home);
    void poll(generation);
  }

  function expand() {
    previousView = state.view === 'computer' ? previousView : state.view;
    return navigate('computer');
  }

  function show() {
    minimized = false;
    nextRetry = 0;
    update();
    if (!activity?.lease || state.view === 'computer') return expand();
    panel.querySelector('[data-preview-expand]').focus();
  }

  function returnToChat() {
    minimized = false;
    return navigate(previousView === 'computer' ? 'chat' : previousView);
  }

  return {mount, stop, update, show, returnToChat,
    inspect(target, task) {
      const changed = target !== inspectorTarget || task?.id !== inspectorTask?.id || task?.browser_id !== inspectorTask?.browser_id;
      if(target && target!==inspectorTarget)minimized=false;
      inspectorTarget=target;inspectorTask=task;
      if(changed)update();
    },
  };
}
