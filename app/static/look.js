/* HEARTH shared look: the fleet-wide theme and palettes from the hub (/api/theme), KAGARI's shared
   lines, and the hand-off to RyuCore ("ask RyuCore about this"). Loaded before each page's script. */
'use strict';

const LOOK = (() => {
  const ls = {
    get(k) { try { return localStorage.getItem('hearth.' + k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem('hearth.' + k, v); } catch { /* private mode */ } },
  };
  const ROLES = ['a1', 'a2', 'a3'], PLAIN = ['bg', 'bg2', 'text', 'dim', 'faint'];
  const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)).join(', ');
  const L = { palettes: [], kagari: null, theme: null, rev: null, ryu: undefined, _subs: [] };
  try {
    const c = JSON.parse(ls.get('look') || 'null');
    if (c && Array.isArray(c.palettes)) Object.assign(L, { palettes: c.palettes, kagari: c.kagari, theme: c.theme, rev: c.rev });
  } catch { /* stale cache */ }
  const save = () => ls.set('look', JSON.stringify({ palettes: L.palettes, kagari: L.kagari, theme: L.theme, rev: L.rev }));

  L.get = id => L.palettes.find(p => p.id === id) || null;

  /** Applies a theme to <html>: data-theme for the CSS, plus the hub's colours as inline variables. */
  L.paint = id => {
    const root = document.documentElement, st = root.style, p = L.get(id);
    if (id === 'hearth') delete root.dataset.theme; else root.dataset.theme = id;
    const vars = [...ROLES.flatMap(r => [`--${r}`, `--${r}-rgb`]), '--s-rgb', '--panel', ...PLAIN.map(k => `--${k}`)];
    if (!p) { vars.forEach(v => st.removeProperty(v)); return; }
    for (const r of ROLES) { st.setProperty(`--${r}`, p[r]); st.setProperty(`--${r}-rgb`, rgb(p[r])); }
    st.setProperty('--s-rgb', rgb(p.surface));
    st.setProperty('--panel', `rgba(${rgb(p.panel)}, 0.9)`);
    for (const k of PLAIN) st.setProperty(`--${k}`, p[k]);
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', p.bg);
  };

  /** Theme list for menus: [{id, name, jp, cols}], built-ins first. */
  L.merge = themes => {
    for (const p of L.palettes) {
      let t = themes.find(x => x.id === p.id);
      if (!t) { t = { id: p.id, name: p.name, jp: p.jp || '' }; themes.push(t); }
      t.cols = [p.a1, p.a2, p.a3];
    }
    return themes;
  };

  /** Fetches the hub's look; calls the onChange subscribers when the fleet theme or the pack changed. */
  L.sync = async () => {
    try {
      const r = await fetch('api/theme', { cache: 'no-store' });
      if (!r.ok) return false;
      const j = await r.json();
      const changed = j.rev !== L.rev, themeChanged = j.theme !== L.theme;
      Object.assign(L, { palettes: j.palettes || [], kagari: j.kagari || null, theme: j.theme, rev: j.rev });
      save();
      if (changed) L._subs.forEach(f => { try { f(L.theme, themeChanged); } catch (e) { console.error(e); } });
      return changed;
    } catch { return false; }
  };
  /** Cheap check from a websocket/poll payload {id, rev}. */
  L.check = ref => { if (ref && ref.rev && ref.rev !== L.rev) L.sync(); };
  L.onChange = fn => L._subs.push(fn);

  /** Makes a theme the fleet's theme (every page, RyuCore, KuroDeck and neosh follow it). */
  L.push = async id => {
    L.theme = id;
    try {
      const r = await fetch('api/theme', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Hearth': '1' }, body: JSON.stringify({ theme: id }) });
      if (r.ok) { L.rev = (await r.json()).rev; save(); }
    } catch { /* hub unreachable: the choice still applies to this browser */ }
  };

  /** The theme to start with: ?theme= first, then the fleet theme, then this browser's last pick. */
  L.initial = () => new URLSearchParams(location.search).get('theme') || L.theme || ls.get('theme') || 'hearth';

  /** KAGARI's shared greeting for this hour: [kao, en, jp], or null when the hub sent none. */
  L.greeting = (where = 'network') => {
    const g = L.kagari?.greetings; if (!g?.length) return null;
    const d = new Date(), h = d.getHours(), x = g.find(y => h < y.until) || g[g.length - 1];
    const hm = `${String(h).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return [x.kao, x.en.replaceAll('{time}', hm).replaceAll('{where}', where), x.jp];
  };
  L.idle = () => (L.kagari?.idle || []).map(x => ({ kao: x.kao, en: x.en, jp: x.jp }));

  /* ── RyuCore hand-off ── */
  L.ryucore = async () => {
    if (L.ryu !== undefined) return L.ryu;
    try { const r = await fetch('api/ryucore', { cache: 'no-store' }); L.ryu = r.ok ? ((await r.json()).url || null) : null; }
    catch { L.ryu = null; }
    return L.ryu;
  };
  /** Opens RyuCore with `prompt` ready in a new agent session (nothing is sent until you press TRANSMIT). */
  L.ask = async prompt => {
    const url = await L.ryucore();
    if (!url) return false;
    window.open(`${url.replace(/\/+$/, '')}/#ask=${encodeURIComponent(prompt.slice(0, 6000))}`, '_blank', 'noopener');
    return true;
  };
  L.when = ms => new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });

  L.paint(L.initial());
  return L;
})();
