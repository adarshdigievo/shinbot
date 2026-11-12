// Apply appearance before CSS loads so a saved dark theme never flashes light.
(() => {
  const key = 'shinbot.appearance';
  const themes = {
    system: {name:'System', description:'Follow your device', mode:null, chrome:null},
    light: {name:'Light', description:'Clean whites and blue accents', mode:'light', chrome:'#f0f3f8'},
    dark: {name:'GitHub Dark', description:'Neutral charcoal and clear blues', mode:'dark', chrome:'#010409'},
    nord: {name:'Nord', description:'Cool slate and muted frost', mode:'dark', chrome:'#2e3440'},
    mocha: {name:'Catppuccin Mocha', description:'Soft pastels on deep purple', mode:'dark', chrome:'#181825'},
    latte: {name:'Catppuccin Latte', description:'Warm light with violet accents', mode:'light', chrome:'#e6e9ef'},
  };
  const valid = value => Object.hasOwn(themes, value) ? value : 'system';
  const system = matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { preference = valid(localStorage.getItem(key)); } catch {}

  function apply() {
    const palette = preference === 'system' ? (system.matches ? 'dark' : 'light') : preference;
    const theme = themes[palette].mode;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.palette = palette;
    document.documentElement.dataset.appearance = preference;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', themes[palette].chrome);
    document.querySelectorAll('[data-appearance-control]').forEach(control => {
      if (control.type === 'radio') control.checked = control.value === preference;
      else control.value = preference;
    });
    document.dispatchEvent(new CustomEvent('shinbot:appearance', {detail: {preference, theme}}));
  }

  window.ShinBotTheme = {
    themes,
    get preference() { return preference; },
    set(value) {
      preference = valid(value);
      let saved = true;
      try { localStorage.setItem(key, preference); } catch { saved = false; }
      apply();
      return saved;
    },
  };
  system.addEventListener('change', () => { if (preference === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      preference = valid(event.newValue);
      apply();
    }
  });
  apply();
})();
