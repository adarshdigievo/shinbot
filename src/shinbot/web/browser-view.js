export function browserPoint(rect, size, clientX, clientY) {
  const scale = Math.min(rect.width / size.width, rect.height / size.height);
  const left = rect.left + (rect.width - size.width * scale) / 2;
  const top = rect.top + (rect.height - size.height * scale) / 2;
  const x = (clientX - left) / scale, y = (clientY - top) / scale;
  return scale > 0 && x >= 0 && y >= 0 && x < size.width && y < size.height ? {x, y} : null;
}

export function nativeBrowserView({screen, api, state, current, refresh, toast}) {
  let image, queue = Promise.resolve(), inputEpoch = 0, frameGeneration;
  const input = document.createElement('textarea');
  input.className = 'browser-input-capture';
  input.setAttribute('aria-label', 'Type in the remote browser');
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('autocapitalize', 'off');
  input.spellcheck = false;
  const controls = () => current() && state().lease?.kind === 'human' && state().control_allowed !== false;
  const point = event => image?.naturalWidth ? browserPoint(image.getBoundingClientRect(), {width:image.naturalWidth,height:image.naturalHeight}, event.clientX,event.clientY) : null;

  function send(event) {
    if (!controls()) return Promise.resolve();
    const epoch = inputEpoch, generation = frameGeneration ?? state().generation;
    queue = queue.then(async () => {
      if (!controls() || epoch !== inputEpoch || generation !== state().generation) return;
      try {
        await api('/api/browser/input', 'POST', {event, generation});
        if (current()) refresh();
      } catch (error) {
        inputEpoch++;
        if (current()) { toast(error.message, true); refresh(); }
      }
    });
    return queue;
  }

  function text() {
    if (!input.value) return;
    const value = input.value;
    input.value = '';
    void send({type:'text',text:value});
  }
  input.oninput = event => { if (!event.isComposing) text(); };
  input.oncompositionend = () => queueMicrotask(text);
  input.onpaste = event => {
    if (!controls()) return;
    event.preventDefault();
    void send({type:'text',text:event.clipboardData.getData('text/plain')});
  };
  input.onkeydown = event => {
    if (event.isComposing || !controls()) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') return;
    const special = ['Enter','Tab','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown'];
    if (!special.includes(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) return;
    if (['Control','Meta','Shift','Alt'].includes(event.key)) return;
    event.preventDefault();
    const modifiers = [event.ctrlKey?'Control':null,event.metaKey?'Meta':null,event.altKey?'Alt':null,event.shiftKey?'Shift':null].filter(Boolean);
    void send({type:'key',key:[...modifiers,event.key===' '?'Space':event.key].join('+')});
  };
  screen.onclick = event => {
    if (!controls() || event.target !== image) return;
    const position = point(event); if (!position) return;
    input.focus({preventScroll:true});
    void send({type:'click',...position,button:'left',count:Math.min(event.detail||1,3)});
  };
  screen.oncontextmenu = event => {
    if (!controls()) return;
    const position=point(event); if (!position) return;
    event.preventDefault(); input.focus({preventScroll:true});
    void send({type:'click',...position,button:'right'});
  };
  let wheel, wheelTimer;
  screen.addEventListener('wheel', event => {
    if (!controls()) return;
    const position=point(event); if (!position) return;
    event.preventDefault();
    const factor=event.deltaMode===1?20:event.deltaMode===2?800:1;
    wheel={type:'wheel',...position,dx:Math.max(-4000,Math.min(4000,(wheel?.dx||0)+event.deltaX*factor)),dy:Math.max(-4000,Math.min(4000,(wheel?.dy||0)+event.deltaY*factor))};
    clearTimeout(wheelTimer);
    wheelTimer=setTimeout(()=>{void send(wheel);wheel=null;},40);
  }, {passive:false});

  return {
    navigate: url => send({type:'navigate',url}),
    render(frame) {
      if (!current()) return;
      frameGeneration=frame.generation;
      screen.classList.toggle('browser-controlling',controls());
      input.disabled=!controls();
      if (frame.empty || !frame.screenshot) {
        image=null;
        const message=document.createElement('div');
        message.className='browser-empty';
        const title=document.createElement('h2'); title.textContent='Browser ready';
        const hint=document.createElement('p'); hint.textContent=controls()?'Enter a website address above to start browsing.':'Ask your bot to open a website, or choose Take control to browse here.';
        message.append(title,hint);screen.replaceChildren(message,input);return;
      }
      if (!image) {
        image=document.createElement('img');image.alt='Current browser page';image.draggable=false;
        screen.replaceChildren(image,input);
      }
      image.src='data:image/jpeg;base64,'+frame.screenshot;
    },
  };
}
