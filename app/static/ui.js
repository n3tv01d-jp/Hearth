/* HEARTH shared UI helpers for the GATE and ARSENAL pages: themes, modal, toasts, API calls */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const QS = new URLSearchParams(location.search);
const store = {
  get(k) { try { return localStorage.getItem('hearth.' + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem('hearth.' + k, v); } catch { /* private mode */ } },
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
function html(str) { const t = document.createElement('template'); t.innerHTML = str.trim(); return t.content.firstChild; }
function keyed(parent, items, keyFn, create, update) {
  const old = new Map();
  for (const el of [...parent.children]) old.set(el.dataset.key, el);
  let prev = null;
  for (const it of items) {
    const k = String(keyFn(it));
    let el = old.get(k);
    if (el) old.delete(k); else { el = create(it); el.dataset.key = k; }
    update(el, it);
    const want = prev ? prev.nextSibling : parent.firstChild;
    if (el !== want) parent.insertBefore(el, want);
    prev = el;
  }
  old.forEach(el => el.remove());
}
const ago = s => (s == null ? '—' : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`);

/* ───────── themes (shared with HEARTH via localStorage) ───────── */
const THEMES = [
  ['hearth', 'HEARTH', '炉心'], ['redline', 'REDLINE', '赤警報'], ['mecha', 'MECHA', '機甲'], ['sakura', 'SAKURA', '桜'],
  ['eclipse', 'ECLIPSE', '月蝕'], ['kurenai', 'KURENAI', '紅'], ['abyss', 'ABYSS', '深海'], ['aurum', 'AURUM', '黄金'],
  ['specter', 'SPECTER', '幽霊'],
].map(([id, name, jp]) => ({ id, name, jp }));
function applyTheme(id) {
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  if (t.id === 'hearth') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t.id;
  store.set('theme', t.id);
  setText($('#themeName'), t.name);
  $('#themeMenu').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.t === t.id));
}
(function buildThemeMenu() {
  const menu = $('#themeMenu'), btn = $('#themeBtn'), root = document.documentElement;
  menu.innerHTML = THEMES.map(t => `<button type="button" data-t="${t.id}"><span class="sw" data-p="${t.id}"><i></i><i></i><i></i></span>${t.name}<em>${t.jp}</em></button>`).join('');
  for (const t of THEMES) {
    const prev = root.dataset.theme;
    if (t.id === 'hearth') delete root.dataset.theme; else root.dataset.theme = t.id;
    const cs = getComputedStyle(root), cols = ['--a1', '--a2', '--a3'].map(v => cs.getPropertyValue(v).trim());
    if (prev) root.dataset.theme = prev; else delete root.dataset.theme;
    menu.querySelectorAll(`[data-p="${t.id}"] i`).forEach((i, k) => { i.style.background = cols[k]; });
  }
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); });
  menu.addEventListener('click', e => { const b = e.target.closest('button'); if (b) { applyTheme(b.dataset.t); close(); } });
  document.addEventListener('click', close);
  applyTheme(QS.get('theme') || store.get('theme') || 'hearth');
})();
function cycleTheme(dir) {
  const cur = document.documentElement.dataset.theme || 'hearth', i = THEMES.findIndex(t => t.id === cur);
  const t = THEMES[(i + dir + THEMES.length) % THEMES.length];
  applyTheme(t.id); toast(`THEME // ${t.name} ${t.jp}`);
}

/* ───────── modal / toast / api ───────── */
const modal = {
  onclose: null, opener: null,
  open(title, body, cls = '') {
    if (!$('#modal').hidden) this.close();
    this.opener = document.activeElement;
    setText($('#modalTitle'), title);
    const b = $('#modalBody'); b.innerHTML = '';
    if (typeof body === 'string') b.innerHTML = body; else if (body) b.append(body);
    $('#modal').className = 'modal ' + cls; $('#modal').hidden = false;
    $('#modalClose').focus();
    return b;
  },
  close() {
    if ($('#modal').hidden) return false;
    $('#modal').hidden = true;
    const fn = this.onclose; this.onclose = null; if (fn) fn();
    if (this.opener && document.contains(this.opener)) this.opener.focus({ preventScroll: true });
    return true;
  },
};
$('#modalClose').addEventListener('click', () => modal.close());
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') modal.close(); });
function toast(msg, lvl = '') {
  const t = html(`<div class="toast ${lvl}"></div>`); t.textContent = msg;
  $('#toasts').append(t);
  setTimeout(() => t.classList.add('out'), 3600); setTimeout(() => t.remove(), 4200);
}
async function postJSON(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Hearth': '1' }, body: JSON.stringify(body || {}) });
  let j = null; try { j = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw new Error(j?.detail || `HTTP ${r.status}`);
  return j;
}

const fmt = {
  bytes(b) {
    if (b == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i === 0 || b >= 100 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i];
  },
  dur(s) {
    if (s == null) return '—';
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  },
  hm(ms) { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; },
  when(ms) {
    const d = new Date(ms), today = new Date().toDateString() === d.toDateString();
    return today ? fmt.hm(ms) : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + fmt.hm(ms);
  },
};
const sinceMs = ms => (ms ? ago(Math.max(0, Math.round((Date.now() - ms) / 1000))) : '—');

/* ───────── boot sequence (same intro as HEARTH, honours its "Boot intro" setting) ───────── */
function bootSeq(lines) {
  const root = $('#boot'), el = $('#bootLog'), bar = $('#bootBar');
  const st = { start: performance.now(), lines: false, data: false, done: false };
  const finish = (now = false) => { if (st.done) return; st.done = true; setTimeout(() => root.classList.add('done'), now ? 0 : 250); };
  if (!root) return () => {};
  if (QS.get('boot') === '0' || (QS.get('boot') !== '1' && store.get('boot') === '0')) { st.done = true; root.hidden = true; return () => {}; }
  const check = () => { if (!st.done && ((st.lines && st.data) || performance.now() - st.start > 5000)) finish(); };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.innerHTML = lines.join('\n'); bar.style.width = '100%'; st.lines = true;
  } else {
    let i = 0;
    const step = () => {
      el.innerHTML = lines.slice(0, ++i).join('\n');
      bar.style.width = (i / lines.length * 100) + '%';
      if (i < lines.length) setTimeout(step, 150 + Math.random() * 120); else { st.lines = true; check(); }
    };
    step();
  }
  const timer = setInterval(() => { check(); if (st.done) clearInterval(timer); }, 200);
  root.addEventListener('click', () => finish(true));
  return () => { st.data = true; check(); };
}
