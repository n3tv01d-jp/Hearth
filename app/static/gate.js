/* GATE // 転送門 — service portal client */
'use strict';

let hits = {};
try { hits = JSON.parse(store.get('gate.hits') || '{}') || {}; } catch { hits = {}; }

const S = {
  services: [], machines: [], edit: false, hub: null, ok: false,
  q: '', by: store.get('gate.by') || 'group', showHidden: false, sel: null, waking: new Map(),
};

/* ───────── data ───────── */
const resolve = u => String(u || '').split('__HOST__').join(location.hostname);
const shortUrl = u => resolve(u).replace(/^https?:\/\//, '').replace(/\/$/, '');
const isUrl = s => /^https?:\/\//i.test(s || '');
const nodeName = id => S.machines.find(m => m.id === id)?.name || id;
const STATUS = { up: 'online', warn: 'degraded', crit: 'failing', off: 'stopped', lost: 'unreachable', none: '' };

let pollTimer = null, failures = 0;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const r = await fetch('api/gate');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    S.services = d.services; S.machines = d.machines; S.edit = d.edit; S.hub = d.hub; S.ok = true; failures = 0;
    render();
  } catch (e) {
    failures++; S.ok = false; renderLink();
    if (failures === 1) setText($('#footInfo'), `hub unreachable (${e.message})`);
  }
  if (!document.hidden) pollTimer = setTimeout(poll, failures ? Math.min(30000, 3000 * failures) : 5000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

/* ───────── search ───────── */
function score(s, q) {
  const hay = [s.name, s.group, s.ct, s.node && nodeName(s.node), shortUrl(s.url), s.image, s.desc].filter(Boolean).join(' ').toLowerCase();
  const name = s.name.toLowerCase();
  let sc = 0;
  if (name === q) sc = 1000;
  else if (name.startsWith(q)) sc = 800;
  else if (name.split(/[\s._-]+/).some(w => w.startsWith(q))) sc = 600;
  else if (hay.includes(q)) sc = 400;
  else { // subsequence: "jlf" finds "jellyfin"
    let i = 0; for (const ch of name) if (ch === q[i]) i++;
    if (i < q.length) return 0;
    sc = 200;
  }
  const h = hits[s.id];
  return sc + (h ? Math.min(150, h[0] * 10) : 0) + (s.pinned ? 30 : 0) - (s.status === 'off' || s.status === 'lost' ? 50 : 0);
}

/* ───────── render ───────── */
function visible() {
  return S.services.filter(s => S.showHidden || !s.hidden);
}
function groupsFor(list) {
  if (S.q) {
    const q = S.q.toLowerCase();
    const res = list.map(s => [score(s, q), s]).filter(x => x[0] > 0).sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name)).map(x => x[1]);
    return [{ key: 'results', title: 'RESULTS', items: res }];
  }
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
  if (S.by === 'az') return [{ key: 'az', title: 'ALL SERVICES', items: [...list].sort(byName) }];
  const m = new Map();
  for (const s of list) {
    const k = S.by === 'node'
      ? (s.node ? nodeName(s.node) : s.kind === 'beacon' ? 'BEACONS' : 'LINKS')
      : (s.group || (s.kind === 'container' ? 'STANDALONE' : 'LINKS'));
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(s);
  }
  const tail = new Set(['STANDALONE', 'BEACONS', 'LINKS']);
  return [...m.entries()]
    .sort((a, b) => (tail.has(a[0]) - tail.has(b[0])) || a[0].localeCompare(b[0]))
    .map(([k, items]) => ({ key: 'g:' + k, title: k.toUpperCase(), items: items.sort(byName) }));
}

function iconHtml(s) {
  if (s.icon && !isUrl(s.icon)) return `<span class="glyph emoji">${esc(s.icon)}</span>`;
  return `<img alt="" loading="lazy" decoding="async"><span class="glyph"></span>`;
}
function hue(str) { let h = 0; for (const c of str) h = (h * 31 + c.codePointAt(0)) % 360; return h; }
function tile(s) {
  const el = html(`<div class="gt">
    <a class="gt-a" target="_blank" rel="noopener noreferrer">
      <span class="gt-ico"></span>
      <span class="gt-txt"><b class="gt-name"></b><small class="gt-url"></small></span>
    </a>
    <span class="gt-st" aria-hidden="true"></span>
    <div class="gt-foot"></div>
    <button class="gt-more" type="button" aria-label="Edit">⋯</button>
  </div>`);
  return el;
}
function updTile(el, s) {
  const url = resolve(s.url);
  const a = el.querySelector('.gt-a');
  if (a.getAttribute('href') !== url) a.href = url;
  setText(el.querySelector('.gt-name'), s.name);
  setText(el.querySelector('.gt-url'), shortUrl(s.url));
  el.className = `gt st-${s.status}${s.hidden ? ' is-hidden' : ''}${s.pinned ? ' is-pinned' : ''}${S.sel === s.id ? ' sel' : ''}`;
  const tip = [s.name, url, s.kind === 'container' ? `${s.ct} on ${nodeName(s.node)} · ${s.detail}` : s.detail, s.image, s.desc].filter(Boolean);
  el.title = tip.join('\n');
  el.querySelector('.gt-st').title = STATUS[s.status] || '';
  el.querySelector('.gt-more').hidden = !S.edit;
  // icon: re-render only when its source changes
  const ico = el.querySelector('.gt-ico'), isrc = `${s.id}|${s.icon || ''}`;
  if (ico.dataset.src !== isrc) {
    ico.dataset.src = isrc;
    ico.innerHTML = iconHtml(s);
    const g = ico.querySelector('.glyph:not(.emoji)');
    if (g) {
      g.textContent = (s.name.match(/[\p{L}\p{N}]/u) || ['?'])[0].toUpperCase();
      ico.style.setProperty('--h', hue(s.name));
      const img = ico.querySelector('img');
      img.addEventListener('load', () => ico.classList.add('has-img'));
      img.addEventListener('error', () => img.remove());
      img.src = `api/gate/icon?id=${encodeURIComponent(s.id)}&v=${encodeURIComponent(s.icon || '')}`;
    }
  }
  const tags = [];
  if (S.by !== 'node' && s.node && S.machines.length > 1) tags.push(`<span class="chip">${esc(nodeName(s.node))}</span>`);
  if (s.kind === 'beacon') tags.push('<span class="chip">BEACON</span>');
  if (s.cert && s.cert.days != null) {
    const d = s.cert.days, cls = d <= 3 ? 'crit' : d <= 14 ? 'warn' : 'ok';
    const label = d < 0 ? 'CERT EXPIRED' : d <= 14 ? `CERT ${Math.max(0, Math.floor(d))}d` : '🔒';
    tags.push(`<span class="chip cert ${cls}" title="TLS certificate ${d < 0 ? 'expired' : 'valid until'} ${new Date(s.cert.until).toLocaleDateString()}${s.cert.self ? ' · self-signed' : ''}">${label}</span>`);
  }
  for (const x of s.extra || []) tags.push(`<a class="chip port" href="${esc(resolve(x.url))}" target="_blank" rel="noopener noreferrer" title="${esc(resolve(x.url))}">:${x.port}</a>`);
  if (s.hidden) tags.push('<span class="chip dim">HIDDEN</span>');
  const foot = el.querySelector('.gt-foot'), h = tags.join('');
  if (foot.dataset.h !== h) { foot.dataset.h = h; foot.innerHTML = h; }
}

function render() {
  renderLink();
  const list = visible();
  // favourites
  const fav = S.q ? [] : list.filter(s => s.pinned).sort((a, b) => a.name.localeCompare(b.name));
  $('#favPanel').hidden = !fav.length;
  setText($('#favMeta'), fav.length ? `${fav.length} pinned` : '');
  keyed($('#favTiles'), fav, s => s.id, tile, updTile);
  // groups
  const groups = groupsFor(list);
  keyed($('#groups'), groups, g => g.key,
    () => html('<div class="g-group"><div class="sub-h"><b></b><span></span></div><div class="g-tiles"></div></div>'),
    (el, g) => {
      setText(el.querySelector('.sub-h b'), g.title);
      const up = g.items.filter(s => s.status === 'up').length, live = g.items.filter(s => s.status !== 'none').length;
      setText(el.querySelector('.sub-h span'), live ? `${up}/${live} online` : `${g.items.length}`);
      el.hidden = !g.items.length;
      keyed(el.querySelector('.g-tiles'), g.items, s => s.id, tile, updTile);
    });
  const shown = groups.reduce((n, g) => n + g.items.length, 0);
  const empty = $('#gEmpty');
  empty.hidden = shown > 0;
  if (!shown) {
    empty.innerHTML = S.q
      ? `Nothing matches “${esc(S.q)}”. <button class="tgl sm" type="button" data-x="clear">CLEAR</button>`
      : `No services yet. GATE lists containers with published ports, HTTP beacons and bookmarks. Add a <code>hearth.gate.port</code> label to containers on host networking, or <button class="tgl sm" type="button" data-x="add">＋ ADD A LINK</button>`;
  }
  setText($('#svcTitle'), S.q ? 'SEARCH' : 'SERVICES');
  $('#groupBy').hidden = !!S.q;
  const c = { up: 0, warn: 0, crit: 0, off: 0, lost: 0 };
  for (const s of S.services) if (s.status in c) c[s.status]++;
  setText($('#svcMeta'), `${S.services.length} entries · ${S.machines.filter(m => m.kind === 'node').length} machines`);
  $('#gSum').innerHTML = [['up', 'ONLINE'], ['warn', 'DEGRADED'], ['crit', 'FAILING'], ['off', 'STOPPED'], ['lost', 'UNREACHABLE']]
    .filter(([k]) => c[k] || k === 'up').map(([k, l]) => `<span class="st-${k}"><i></i>${c[k]} ${l}</span>`).join('');
  $('#hiddenBtn').hidden = !S.services.some(s => s.hidden);
  if (S.sel && !document.querySelector(`.gt[data-key="${CSS.escape(S.sel)}"]`)) S.sel = null;
  if (S.q && !S.sel) select(firstTile()?.dataset.key, false);
  renderMachines();
  const certs = S.services.filter(s => s.cert?.days != null && s.cert.days <= 14);
  setText($('#footInfo'), `hub ${S.hub} · ${c.up} of ${S.services.filter(s => s.status !== 'none').length} services online` +
    (certs.length ? ` · ${certs.length} certificate${certs.length > 1 ? 's' : ''} expiring` : ''));
  $('#addBtn').hidden = !S.edit;
}

function renderLink() {
  const l = $('#link');
  l.classList.toggle('on', S.ok);
  setText(l.querySelector('span'), S.ok ? 'LIVE' : 'NO LINK');
}

function renderMachines() {
  const ms = S.machines;
  const wakeable = ms.some(m => m.mac && !m.local);
  $('#machPanel').hidden = !ms.length || !wakeable && ms.length < 2;
  const now = Date.now();
  for (const [id, t] of S.waking) if (now - t > 180000 || ms.find(m => m.id === id)?.online) S.waking.delete(id);
  keyed($('#machines'), ms, m => m.id,
    () => html(`<div class="gm"><i class="gm-dot"></i><div class="gm-txt"><b></b><small></small></div><button class="tgl sm gm-wake" type="button"></button></div>`),
    (el, m) => {
      const waking = S.waking.has(m.id);
      const st = m.local ? 'hub' : m.online === true ? 'on' : m.online === false ? 'off' : 'unk';
      el.className = `gm gm-${st}${waking ? ' waking' : ''}`;
      setText(el.querySelector('b'), m.name);
      const parts = [m.local ? 'HUB' : m.online ? 'online' : m.online === false ? (m.age != null ? `offline ${ago(m.age)}` : 'offline') : 'status unknown'];
      if (m.addr) parts.push(m.addr);
      if (m.mac) parts.push(m.mac);
      setText(el.querySelector('small'), parts.join(' · '));
      const b = el.querySelector('.gm-wake');
      b.hidden = m.local || !m.mac || (m.online === true && !waking);
      b.disabled = waking;
      setText(b, waking ? 'WAKING…' : '⏻ WAKE');
      el.title = m.mac ? '' : 'No MAC address known yet: it is learned while the machine is online';
    });
}

/* ───────── selection & keyboard ───────── */
const tiles = () => [...document.querySelectorAll('.g-main .gt')].filter(t => t.offsetParent);
const firstTile = () => tiles()[0];
function select(id, scroll = true) {
  S.sel = id || null;
  document.querySelectorAll('.gt.sel').forEach(t => t.classList.remove('sel'));
  if (!id) return;
  const el = document.querySelector(`.g-main .gt[data-key="${CSS.escape(id)}"]`);
  if (!el) return;
  el.classList.add('sel');
  if (scroll) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function move(dx, dy) {
  const all = tiles(); if (!all.length) return;
  const cur = all.findIndex(t => t.dataset.key === S.sel);
  if (cur < 0) return select(all[0].dataset.key);
  if (dx) return select(all[Math.max(0, Math.min(all.length - 1, cur + dx))].dataset.key);
  const r = all[cur].getBoundingClientRect(), cx = r.left + r.width / 2;
  const cand = all.map(t => [t, t.getBoundingClientRect()]).filter(([, b]) => dy > 0 ? b.top > r.top + 4 : b.top < r.top - 4);
  if (!cand.length) return;
  const rowTop = dy > 0 ? Math.min(...cand.map(([, b]) => b.top)) : Math.max(...cand.map(([, b]) => b.top));
  const row = cand.filter(([, b]) => Math.abs(b.top - rowTop) < 4);
  row.sort((a, b) => Math.abs(a[1].left + a[1].width / 2 - cx) - Math.abs(b[1].left + b[1].width / 2 - cx));
  select(row[0][0].dataset.key);
}
function svc(id) { return S.services.find(s => s.id === id); }
function launch(s, sameTab) {
  if (!s) return;
  const h = hits[s.id] || [0, 0]; hits[s.id] = [h[0] + 1, Date.now()];
  store.set('gate.hits', JSON.stringify(hits));
  if (sameTab) location.href = resolve(s.url);
  else window.open(resolve(s.url), '_blank', 'noopener,noreferrer');
}

const q = $('#q');
q.addEventListener('input', () => { S.q = q.value.trim(); S.sel = null; render(); if (!S.q) window.scrollTo({ top: 0 }); });
q.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); const s = svc(S.sel || firstTile()?.dataset.key); if (s) { launch(s, e.shiftKey); } }
  else if (e.key === 'Escape') { if (q.value) { q.value = ''; S.q = ''; render(); } else q.blur(); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); move(0, e.key === 'ArrowDown' ? 1 : -1); }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && modal.close()) return;
  if (!$('#modal').hidden || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t !== document.body && t.closest('input, textarea, select') && t !== q) return;
  if (t === q) return;
  const k = e.key;
  if (k === '/') { e.preventDefault(); q.focus(); q.select(); }
  else if (k.startsWith('Arrow')) { e.preventDefault(); move(k === 'ArrowRight' ? 1 : k === 'ArrowLeft' ? -1 : 0, k === 'ArrowDown' ? 1 : k === 'ArrowUp' ? -1 : 0); }
  else if (k === 'Enter' && S.sel && !t.closest('a, button')) { e.preventDefault(); launch(svc(S.sel), e.shiftKey); }
  else if ((k === 'e' || k === 'E') && S.sel && S.edit) editItem(svc(S.sel));
  else if ((k === 'p' || k === 'P') && S.sel && S.edit) togglePin(svc(S.sel));
  else if ((k === 'n' || k === 'N') && S.edit) editItem(null);
  else if (k === 't') cycleTheme(1);
  else if (k === 'T') cycleTheme(-1);
  else if (k === '?') showHelp();
  else if (k.length === 1 && /[\p{L}\p{N}]/u.test(k)) { q.focus(); } // just start typing to search
});

document.querySelector('.g-main').addEventListener('click', e => {
  const x = e.target.closest('[data-x]')?.dataset.x;
  if (x === 'clear') { q.value = ''; S.q = ''; render(); return; }
  if (x === 'add') return editItem(null);
  const more = e.target.closest('.gt-more');
  if (more) return editItem(svc(more.closest('.gt').dataset.key));
  const a = e.target.closest('.gt-a');
  if (a) { const s = svc(a.closest('.gt').dataset.key); if (s) { const h = hits[s.id] || [0, 0]; hits[s.id] = [h[0] + 1, Date.now()]; store.set('gate.hits', JSON.stringify(hits)); } }
  const wake = e.target.closest('.gm-wake');
  if (wake) wakeMachine(wake.closest('.gm').dataset.key);
});
$('#groupBy').addEventListener('click', e => {
  const b = e.target.closest('button[data-g]'); if (!b) return;
  S.by = b.dataset.g; store.set('gate.by', S.by); syncGroupBy(); render();
});
function syncGroupBy() { $('#groupBy').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.g === S.by)); }
syncGroupBy();
$('#hiddenBtn').addEventListener('click', () => {
  S.showHidden = !S.showHidden;
  $('#hiddenBtn').classList.toggle('on', S.showHidden); $('#hiddenBtn').setAttribute('aria-pressed', String(S.showHidden));
  render();
});
$('#addBtn').addEventListener('click', () => editItem(null));
$('#helpLink').addEventListener('click', e => { e.preventDefault(); showHelp(); });

/* ───────── edit / bookmarks ───────── */
function editItem(s) {
  const isNew = !s, isLink = isNew || s.kind === 'link' && s.id.startsWith('link/');
  const base = s?.auto || {};
  const f = html(`<form class="g-form" autocomplete="off">
    <label>NAME<input name="name" maxlength="60" ${isLink ? 'required' : ''}></label>
    <label>URL<input name="url" maxlength="500" inputmode="url" ${isLink ? 'required' : ''} placeholder="https://…"></label>
    <label>GROUP<input name="group" maxlength="40" list="gGroups"></label>
    <label>ICON<input name="icon" maxlength="500" placeholder="emoji, or an image URL · blank = the site's favicon"></label>
    <div class="g-chk"><label class="chk"><input type="checkbox" name="pinned"> PIN TO FAVOURITES</label>
      <label class="chk"><input type="checkbox" name="hidden"> HIDE</label></div>
    <p class="g-note"></p>
    <div class="row"><button class="tgl" type="button" data-x="del" hidden></button><span class="sp"></span>
      <button class="tgl" type="button" data-x="no">CANCEL</button><button class="tgl on" type="submit">SAVE</button></div>
    <datalist id="gGroups"></datalist>
  </form>`);
  const F = f.elements;
  f.querySelector('#gGroups').innerHTML = [...new Set(S.services.map(x => x.group).filter(Boolean))].sort().map(g => `<option value="${esc(g)}">`).join('');
  if (s) {
    if (isLink) { F.name.value = s.name; F.url.value = s.url; F.group.value = s.group === 'LINKS' ? '' : s.group || ''; F.icon.value = s.icon || ''; }
    else {
      // overrides: leave a field empty to keep what HEARTH discovered
      F.name.value = s.custom && s.name !== base.name ? s.name : ''; F.name.placeholder = base.name || s.name;
      F.url.value = s.custom && s.url !== base.url ? s.url : ''; F.url.placeholder = shortUrl(base.url || s.url) + '  (auto)';
      F.group.value = s.custom && s.group !== base.group ? s.group || '' : ''; F.group.placeholder = (base.group || s.group || '') + (base.group || s.group ? '  (auto)' : '');
      F.icon.value = s.custom && s.icon !== base.icon ? s.icon || '' : '';
    }
    F.pinned.checked = s.pinned; F.hidden.checked = s.hidden;
  }
  const note = f.querySelector('.g-note');
  note.textContent = isNew ? 'Bookmarks are stored on the hub, so everyone who opens GATE sees them.'
    : isLink ? '' : s.kind === 'container' ? `Discovered from ${s.ct} on ${nodeName(s.node)}. Empty fields keep the discovered values; Docker labels (hearth.gate.*) work too.`
      : s.kind === 'beacon' ? 'From HEARTH_CHECKS.' : 'From HEARTH_GATE_LINKS.';
  const del = f.querySelector('[data-x=del]');
  if (s && (isLink || s.custom)) { del.hidden = false; del.textContent = isLink ? 'DELETE' : 'RESET'; }
  f.addEventListener('click', async e => {
    const x = e.target.closest('[data-x]')?.dataset.x;
    if (x === 'no') modal.close();
    if (x === 'del') {
      try { await postJSON('api/gate/item/delete', { id: s.id }); modal.close(); toast(isLink ? `Deleted ${s.name}` : `${s.name}: back to discovered settings`, 'ok'); poll(); }
      catch (err) { toast(err.message, 'crit'); }
    }
  });
  f.addEventListener('submit', async e => {
    e.preventDefault();
    const body = { id: isNew ? '' : s.id, name: F.name.value, url: F.url.value, group: F.group.value, icon: F.icon.value, pinned: F.pinned.checked, hidden: F.hidden.checked };
    try { await postJSON('api/gate/item', body); modal.close(); toast(isNew ? `Added ${body.name}` : `Saved ${body.name || s.name}`, 'ok'); poll(); }
    catch (err) { toast(err.message, 'crit'); }
  });
  modal.open(isNew ? 'NEW LINK // 追加' : `EDIT // ${s.name}`, f, 'sm');
  (isLink || !s ? F.name : F.pinned).focus();
}
async function togglePin(s) {
  if (!s) return;
  const isLink = s.kind === 'link' && s.id.startsWith('link/'), base = s.auto || {};
  const keep = k => (isLink || (s.custom && s[k] !== base[k]) ? s[k] : '');
  try {
    await postJSON('api/gate/item', { id: s.id, name: keep('name'), url: keep('url'), group: keep('group') === 'LINKS' && isLink ? '' : keep('group'), icon: keep('icon'), hidden: s.hidden, pinned: !s.pinned });
    toast(`${s.name} ${s.pinned ? 'unpinned' : 'pinned ★'}`, 'ok'); poll();
  } catch (err) { toast(err.message, 'crit'); }
}

/* ───────── wake-on-LAN ───────── */
async function wakeMachine(id) {
  const m = S.machines.find(x => x.id === id); if (!m) return;
  try {
    await postJSON('api/gate/wake', { id });
    S.waking.set(id, Date.now()); renderMachines();
    toast(`⏻ Magic packet sent to ${m.name}. Give it a minute or two to boot.`, 'ok');
  } catch (err) { toast(`${m.name}: ${err.message}`, 'crit'); }
}

/* ───────── help ───────── */
function showHelp() {
  const keys = [['/', 'Search (or just start typing)'], ['↑ ↓ ← →', 'Move between services'], ['Enter', 'Open in a new tab'],
    ['Shift Enter', 'Open in this tab'], ['Esc', 'Clear the search'], ['E', 'Edit the selected service'], ['P', 'Pin / unpin the selected service'],
    ['N', 'New bookmark'], ['T', 'Next theme'], ['?', 'This help']];
  modal.open('KEYBOARD // 操作', `<div class="keys">${keys.map(([k, d]) => `<span>${k.split(' ').map(x => `<kbd>${esc(x)}</kbd>`).join(' ')}</span><span>${esc(d)}</span>`).join('')}</div>`, 'sm');
}

if (QS.get('q')) { q.value = QS.get('q'); S.q = q.value.trim(); }
poll();
