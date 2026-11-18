/* HEARTH // 炉心監視システム — realtime client */
'use strict';

const H = 120;
const $ = (s, r = document) => r.querySelector(s);
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const QS = new URLSearchParams(location.search);
const store = {
  get(k) { try { return localStorage.getItem('hearth.' + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem('hearth.' + k, v); } catch { /* private mode */ } },
};

const S = {
  hist: { cpu: [], mem: [], hot: [], net: {}, temp: {} },
  data: null, node: QS.get('node') || store.get('node'), fleet: [], sel: null,
  filter: '', showStopped: true, tab: 'lan',
  log: new Map(), logPrimed: false,
  range: store.get('range') || '1h', archive: null, summary: null, storeOk: true,
};

/* ───────── formatting ───────── */
const fmt = {
  bytes(b) {
    if (b == null) return '—';
    const neg = b < 0; b = Math.abs(b);
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (neg ? '-' : '') + (i === 0 || b >= 100 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i];
  },
  bits(Bps) {
    if (Bps == null) return '—';
    let b = Bps * 8; const u = ['b/s', 'Kb/s', 'Mb/s', 'Gb/s', 'Tb/s']; let i = 0;
    while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
    return (i === 0 || b >= 100 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i];
  },
  dur(s) {
    if (s == null) return '—';
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
  },
  ago(s) { return s == null ? '—' : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`; },
  hours(h) { return h == null ? '—' : h >= 8760 ? (h / 8760).toFixed(1) + ' y' : h >= 48 ? Math.round(h / 24) + ' d' : h + ' h'; },
  time(ms) { return new Date(ms).toLocaleTimeString([], { hour12: false }); },
  hm(ms) { const d = new Date(ms); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; },
  temp(c) { return c == null ? '—' : c.toFixed(1) + '°C'; },
};
const pad2 = n => String(n).padStart(2, '0');
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const pushH = (a, v) => { a.push(v); while (a.length > H) a.shift(); };
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
function html(str) { const t = document.createElement('template'); t.innerHTML = str.trim(); return t.content.firstChild; }

/* keyed DOM reconciliation: keeps elements stable between ticks */
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
function apiUrl(path, params) {
  const u = new URL(path, location.href);
  for (const [k, v] of Object.entries(params || {})) if (v != null) u.searchParams.set(k, v);
  return u;
}

/* ───────── boot sequence ───────── */
const BOOT = [
  'HEARTH SYSTEM v3.0 // 炉心監視システム',
  'initialising telemetry bus ........ <span class="ok">OK</span>',
  'binding /proc · /sys/class/hwmon ... <span class="ok">OK</span>',
  'opening archive // 履歴 ............ <span class="ok">OK</span>',
  'listening for fleet beacons ........ <span class="ok">OK</span>',
  'waking navigator KAGARI-AI ......... <span class="ok">OK</span>',
  '>> 起動完了 — all systems green',
];
const boot = { start: performance.now(), linesDone: false, data: false, finished: false };
(function runBoot() {
  const el = $('#bootLog'), bar = $('#bootBar');
  if (QS.get('boot') === '0') { boot.finished = true; $('#boot').hidden = true; return; }
  if (REDUCED) { el.innerHTML = BOOT.join('\n'); boot.linesDone = true; bar.style.width = '100%'; return; }
  let i = 0;
  const step = () => {
    el.innerHTML = BOOT.slice(0, ++i).join('\n');
    bar.style.width = (i / BOOT.length * 100) + '%';
    if (i < BOOT.length) setTimeout(step, 150 + Math.random() * 120); else boot.linesDone = true;
  };
  step();
})();
setInterval(() => {
  if (boot.finished) return;
  if ((boot.linesDone && boot.data) || performance.now() - boot.start > 5000) {
    boot.finished = true; setTimeout(() => $('#boot').classList.add('done'), 250);
  }
}, 200);
$('#boot').addEventListener('click', () => { boot.finished = true; $('#boot').classList.add('done'); });

/* ───────── clock ───────── */
function clock() {
  const d = new Date();
  setText($('#clock'), `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`);
  setText($('#date'), `${d.getFullYear()}.${pad2(d.getMonth() + 1)}.${pad2(d.getDate())} (${'日月火水木金土'[d.getDay()]})`);
}
clock(); setInterval(clock, 1000);

/* ───────── reactor spin ───────── */
const spin = { a: 0, b: 0, speed: 12, last: performance.now() };
(function rot(now) {
  const dt = (now - spin.last) / 1000; spin.last = now;
  if (!REDUCED) {
    spin.a = (spin.a + dt * spin.speed) % 360; spin.b = (spin.b - dt * spin.speed * 1.7) % 360;
    $('#spinA').setAttribute('transform', `rotate(${spin.a.toFixed(2)} 100 100)`);
    $('#spinB').setAttribute('transform', `rotate(${spin.b.toFixed(2)} 100 100)`);
  }
  requestAnimationFrame(rot);
})(performance.now());

/* ───────── themes ───────── */
const THEMES = [
  { id: 'hearth', name: 'HEARTH', jp: '炉心' },
  { id: 'redline', name: 'REDLINE', jp: '赤警報' },
  { id: 'mecha', name: 'MECHA', jp: '機甲' },
  { id: 'sakura', name: 'SAKURA', jp: '桜' },
  { id: 'eclipse', name: 'ECLIPSE', jp: '月蝕' },
];
function applyTheme(id) {
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  if (t.id === 'hearth') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t.id;
  store.set('theme', t.id);
  setText($('#themeName'), t.name);
  $('#themeMenu').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.t === t.id));
  refreshPalette(); Chart.all.forEach(c => c.draw());
}
(function buildThemeMenu() {
  const menu = $('#themeMenu'), btn = $('#themeBtn');
  menu.innerHTML = THEMES.map(t => `<button type="button" data-t="${t.id}">
    <span class="sw" data-p="${t.id}"><i></i><i></i><i></i></span>${t.name}<em>${t.jp}</em></button>`).join('');
  // swatches: read each theme's accents by briefly applying it to <html>
  for (const t of THEMES) {
    const root = document.documentElement, prev = root.dataset.theme;
    if (t.id === 'hearth') delete root.dataset.theme; else root.dataset.theme = t.id;
    const cs = getComputedStyle(root), cols = ['--a1', '--a2', '--a3'].map(v => cs.getPropertyValue(v).trim());
    if (prev) root.dataset.theme = prev; else delete root.dataset.theme;
    menu.querySelectorAll(`[data-p="${t.id}"] i`).forEach((i, k) => { i.style.background = cols[k]; });
  }
  btn.addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); });
  menu.addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; applyTheme(b.dataset.t); menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); SFX.play('blip'); });
  document.addEventListener('click', () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
  applyTheme(QS.get('theme') || store.get('theme') || 'hearth');
})();

/* ───────── sound effects (synthesised, off by default) ───────── */
const SFX = {
  ctx: null, on: store.get('sfx') === '1', lastAt: 0,
  ensure() {
    if (!this.ctx) { try { this.ctx = new AudioContext(); } catch { return null; } }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  },
  tone(f, t, d, type = 'sine', g = 0.06) {
    const c = this.ctx, o = c.createOscillator(), a = c.createGain();
    o.type = type; o.frequency.value = f;
    a.gain.setValueAtTime(0, t); a.gain.linearRampToValueAtTime(g, t + 0.01); a.gain.exponentialRampToValueAtTime(0.0001, t + d);
    o.connect(a).connect(c.destination); o.start(t); o.stop(t + d + 0.02);
  },
  play(kind, force) {
    if (!this.on && !force) return;
    const c = this.ensure(); if (!c) return;
    const t = c.currentTime + 0.02;
    if (kind === 'crit') [0, .22, .44].forEach((o, i) => this.tone(i % 2 ? 660 : 880, t + o, .18, 'square', .045));
    else if (kind === 'warn') [0, .16].forEach(o => this.tone(520, t + o, .12, 'triangle', .07));
    else if (kind === 'rec') [523, 659, 784, 1047].forEach((f, i) => this.tone(f, t + i * .09, .3, 'sine', .06));
    else this.tone(1250, t, .05, 'sine', .04);
  },
};
function syncSfxBtn() { const b = $('#sfxBtn'); b.classList.toggle('on', SFX.on); b.setAttribute('aria-pressed', String(SFX.on)); setText(b, SFX.on ? 'SFX ON' : 'SFX OFF'); }
$('#sfxBtn').addEventListener('click', () => { SFX.on = !SFX.on; store.set('sfx', SFX.on ? '1' : '0'); syncSfxBtn(); if (SFX.on) SFX.play('rec'); });
document.addEventListener('pointerdown', () => { if (SFX.on) SFX.ensure(); }, { once: true });
syncSfxBtn();

/* ───────── charts ───────── */
const liveX = (i, n) => (i === n - 1 ? 'now' : `−${n - 1 - i}s`);
const charts = {
  cpu: new Chart($('#cpuChart'), { max: 100, len: H, xlabel: liveX, fmt: v => v.toFixed(0) + '%' }),
  net: new Chart($('#netChart'), { labels: true, len: H, xlabel: liveX, fmt: v => fmt.bits(v) }),
  hot: new Chart($('#hotChart'), { autoMin: true, len: H, xlabel: liveX, fmt: v => v.toFixed(1) + '°C' }),
  hCpu: new Chart($('#hCpu'), { max: 100, xaxis: true, fmt: v => v.toFixed(0) + '%', empty: 'collecting…' }),
  hTemp: new Chart($('#hTemp'), { autoMin: true, xaxis: true, fmt: v => v.toFixed(1) + '°C', empty: 'collecting…' }),
  hNet: new Chart($('#hNet'), { labels: true, xaxis: true, fmt: v => fmt.bits(v), empty: 'collecting…' }),
};

/* ───────── ticker ───────── */
/* [label, key, class, slot width in ch] — fixed slot widths keep the track width constant,
   so changing values never shift the scroll animation (it's a % of the track width). */
const TICK_ITEMS = [
  ['NODE', 'host', '', 16], ['CPU', 'cpu', '', 4], ['MEM', 'mem', 'm', 4], ['▼ RX', 'rx', '', 10], ['▲ TX', 'tx', 'm', 10],
  ['CONTAINERS', 'ct', '', 7], ['FLEET', 'fleet', 'm', 12], ['CORE TEMP', 'temp', '', 5], ['ステータス', 'status', 'm', 13],
  ['UPTIME', 'up', '', 12],
];
$('#ticker').innerHTML = (s => s + s)(TICK_ITEMS.map(([l, k, c, w]) =>
  `<span class="${c}">◆ ${l} <b data-tk="${k}" style="width:${w}ch">—</b></span>`).join(''));
function setTick(k, v) { document.querySelectorAll(`[data-tk="${k}"]`).forEach(e => setText(e, v)); }

/* ───────── interactions ───────── */
$('#dFilter').addEventListener('input', e => { S.filter = e.target.value.trim().toLowerCase(); if (S.data) renderDocker(S.data); });
$('#dStopped').addEventListener('click', e => {
  S.showStopped = !S.showStopped; e.currentTarget.classList.toggle('on', S.showStopped); if (S.data) renderDocker(S.data);
});
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
  S.tab = b.dataset.tab;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  $('#lanList').hidden = S.tab !== 'lan'; $('#peerList').hidden = S.tab !== 'peers';
}));
$('#ifaces').addEventListener('click', e => { const b = e.target.closest('.iface'); if (!b) return; S.sel = b.dataset.key; if (S.data) renderNet(S.data); });
$('#histRange').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  S.range = b.dataset.r; store.set('range', S.range); loadArchive();
});
$('#fleet').addEventListener('click', e => { const b = e.target.closest('.node'); if (b) selectNode(b.dataset.key); });

/* ───────── data intake ───────── */
function loadHistory(h) {
  h = h || {};
  S.hist = { cpu: (h.cpu || []).slice(-H), mem: (h.mem || []).slice(-H), hot: (h.hot || []).slice(-H), net: h.net || {}, temp: h.temp || {} };
}
function record(d) {
  pushH(S.hist.cpu, d.cpu.total); pushH(S.hist.mem, d.mem.pct); pushH(S.hist.hot, d.hot ? d.hot.c : null);
  for (const i of d.net.ifaces) { const h = S.hist.net[i.name] ||= { rx: [], tx: [] }; pushH(h.rx, i.rx); pushH(h.tx, i.tx); }
  for (const t of d.temps) pushH(S.hist.temp[t.id] ||= [], t.c);
}
function render(d) {
  S.data = d;
  for (const fn of [renderHeader, renderCore, renderNet, renderTherm, renderDocker, renderDisks, renderNodes, renderNav, renderTickerVals]) {
    try { fn(d); } catch (e) { console.error(fn.name, e); }
  }
  boot.data = true;
}
const curNode = () => S.fleet.find(n => n.id === S.node);

/* ───────── fleet ───────── */
function selectNode(id) {
  if (!id || id === S.node) return;
  S.node = id; store.set('node', id); S.sel = null; S.data = null;
  const u = new URL(location.href); u.searchParams.set('node', id); history.replaceState(null, '', u);
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'select', node: id }));
  renderFleet(); SFX.play('blip');
}
function renderFleet() {
  const multi = S.fleet.length > 1, box = $('#fleet');
  document.body.classList.toggle('multi', multi);
  box.hidden = !multi;
  if (multi) {
    keyed(box, S.fleet, n => n.id,
      () => html(`<button class="node" type="button"><i class="led"></i><span class="nm"></span><span class="tg"></span><span class="st"></span></button>`),
      (el, n) => {
        el.classList.toggle('sel', n.id === S.node); el.classList.toggle('lost', !n.online);
        el.querySelector('.led').className = 'led ' + (!n.online ? 'crit' : n.alerts ? 'warn' : 'on');
        setText(el.querySelector('.nm'), n.name);
        const tg = el.querySelector('.tg');
        setText(tg, !n.online ? 'LOST' : n.alerts ? `${n.alerts} ALERT${n.alerts > 1 ? 'S' : ''}` : n.local ? 'HUB' : (n.arch || 'AGENT').toUpperCase());
        tg.classList.toggle('alert', !n.online || n.alerts > 0);
        el.querySelector('.st').innerHTML = !n.online
          ? `signal lost ${esc(fmt.ago(n.age))} ago`
          : `<span>CPU <b>${n.cpu == null ? '—' : n.cpu.toFixed(0) + '%'}</b></span><span>MEM <b>${n.mem == null ? '—' : n.mem.toFixed(0) + '%'}</b></span><span>${n.hot == null ? '' : `<b>${n.hot.toFixed(0)}°</b>`}</span><span>CT <b>${n.running ?? '—'}/${n.total ?? '—'}</b></span>`;
        el.title = `${n.name}${n.addr ? ' · ' + n.addr : ''}`;
      });
  }
  const me = curNode();
  const stale = me && !me.online;
  document.body.classList.toggle('stale', !!stale);
  if (stale) setText($('#staleText'), `${me.name} last reported ${fmt.ago(me.age)} ago — showing last known telemetry.`);
  const on = S.fleet.filter(n => n.online).length;
  setTick('fleet', `${on}/${S.fleet.length} ONLINE`);
}

/* ───────── header ───────── */
function renderHeader(d) {
  setText($('#hHost'), d.host.name); setText($('#hOs'), d.host.os);
  setText($('#hKernel'), d.host.kernel); setText($('#hUp'), fmt.dur(d.host.uptime));
  setText($('#footInfo'), `${d.host.arch} · ${d.host.threads} threads · docker ${d.docker.version || 'offline'} · tick ${fmt.time(d.t)}`);
  const alerts = d.temps.filter(t => t.lvl !== 'ok');
  document.body.classList.toggle('alerting', alerts.length > 0);
  if (alerts.length) {
    const worst = alerts.reduce((a, b) => (b.c - b.crit > a.c - a.crit ? b : a));
    setText($('#alertText'), `${d.host.name} · ${worst.chip} ${worst.label} ${worst.c.toFixed(1)}°C ${worst.lvl === 'crit' ? '— CRITICAL' : '— above nominal'}${alerts.length > 1 ? ` (+${alerts.length - 1})` : ''}`);
  }
}

/* ───────── core ───────── */
function renderCore(d) {
  const cpu = d.cpu.total, mem = d.mem.pct;
  setText($('#cpuPct'), cpu.toFixed(0)); setText($('#memPct'), mem.toFixed(0));
  $('#ringCpu').style.strokeDashoffset = 100 - clamp(cpu, 0, 100);
  $('#ringMem').style.strokeDashoffset = 100 - clamp(mem, 0, 100);
  const r = $('.reactor'); r.classList.toggle('hot', cpu >= 90); r.classList.toggle('warm', cpu >= 70 && cpu < 90);
  $('#coreGlow').setAttribute('r', (50 + cpu * 0.12).toFixed(1));
  spin.speed = 8 + cpu * 1.6;
  setText($('#cpuModel'), d.host.cpu);
  setText($('#kLoad'), d.cpu.load.map(x => x.toFixed(2)).join(' '));
  setText($('#kFreq'), d.cpu.mhz ? (d.cpu.mhz >= 1000 ? (d.cpu.mhz / 1000).toFixed(2) + ' GHz' : d.cpu.mhz + ' MHz') : '—');
  setText($('#kThreads'), `${d.host.threads} / ${d.host.cores}c`);
  setText($('#kTemp'), d.hot ? d.hot.c.toFixed(1) + ' °C' : 'n/a');
  setText($('#kMem'), `${fmt.bytes(d.mem.used)} / ${fmt.bytes(d.mem.total)}`);
  setText($('#kSwap'), d.mem.swap_total ? `${fmt.bytes(d.mem.swap_used)} · ${d.mem.swap_pct.toFixed(0)}%` : 'none');
  const cores = $('#cores');
  if (cores.children.length !== d.cpu.cores.length) cores.innerHTML = d.cpu.cores.map(() => '<i></i>').join('');
  d.cpu.cores.forEach((v, i) => { const el = cores.children[i]; el.style.setProperty('--h', v + '%'); el.classList.toggle('hi', v >= 85); el.title = `core ${i}: ${v}%`; });
  charts.cpu.set([{ name: 'CPU', data: S.hist.cpu, color: 'a1' }, { name: 'MEM', data: S.hist.mem, color: 'a2', fill: false, width: 1.2 }]);
}

/* ───────── network ───────── */
function renderNet(d) {
  const ifs = d.net.ifaces;
  if (!S.sel || !ifs.some(i => i.name === S.sel)) S.sel = d.net.primary && ifs.some(i => i.name === d.net.primary) ? d.net.primary : ifs[0]?.name;
  const cur = ifs.find(i => i.name === S.sel);
  const h = S.hist.net[S.sel] || { rx: [], tx: [] };
  setText($('#netIface'), cur ? `${cur.name}${cur.ip ? ' · ' + cur.ip : ''}${cur.speed ? ' · ' + (cur.speed >= 1000 ? cur.speed / 1000 + 'G' : cur.speed + 'M') : ''}` : '—');
  setText($('#nRx'), fmt.bits(cur?.rx || 0)); setText($('#nTx'), fmt.bits(cur?.tx || 0));
  setText($('#nRxPk'), 'peak ' + fmt.bits(Math.max(0, ...h.rx))); setText($('#nTxPk'), 'peak ' + fmt.bits(Math.max(0, ...h.tx)));
  setText($('#nTrx'), fmt.bytes(cur?.total_rx)); setText($('#nTtx'), fmt.bytes(cur?.total_tx));
  const c = d.net.conns || {};
  setText($('#nEst'), String(c.established ?? '—')); setText($('#nListen'), c.listen ? String(c.listen.length) : '—');
  setText($('#nUdp'), String(c.udp ?? '—')); setText($('#nPeers'), String(c.peer_count ?? '—'));
  charts.net.set([{ name: 'RX', data: h.rx, color: 'a1' }, { name: 'TX', data: h.tx, color: 'a2' }]);
  keyed($('#ifaces'), ifs, i => i.name,
    () => html(`<button class="iface" type="button"><i class="led"></i><span class="n"></span><span class="r"><span class="d"></span> · <span class="u"></span></span></button>`),
    (el, i) => {
      el.classList.toggle('sel', i.name === S.sel);
      el.querySelector('.led').className = 'led ' + (i.up ? 'on' : '');
      const n = el.querySelector('.n'); n.textContent = i.name;
      const em = document.createElement('em'); em.textContent = i.kind.toUpperCase(); n.append(em);
      setText(el.querySelector('.d'), '▼ ' + fmt.bits(i.rx)); setText(el.querySelector('.u'), '▲ ' + fmt.bits(i.tx));
      el.title = `${i.name}  ${i.ip || ''}  ${i.mac || ''}  mtu ${i.mtu}  err ${i.err}  drop ${i.drop}`;
    });
}

/* ───────── thermal ───────── */
function tempClass(t) { return t.lvl === 'crit' ? 'crit' : t.lvl === 'warn' ? 'warn' : t.c >= t.warn - 15 ? 'warm' : ''; }
function renderTherm(d) {
  const hot = d.hot, hv = $('#hotVal');
  setText(hv, hot ? hot.c.toFixed(1) + '°C' : 'n/a');
  hv.className = hot && hot.lvl !== 'ok' ? hot.lvl : '';
  setText($('#hotLbl'), hot ? `${hot.chip} · ${hot.label}` : 'no sensors exposed');
  setText($('#thermMeta'), `${d.temps.length} sensors`);
  charts.hot.set([{ name: hot ? hot.label : 'CPU', data: S.hist.hot, color: hot && hot.lvl === 'crit' ? 'red' : hot && hot.lvl === 'warn' ? 'amber' : 'a1' }]);
  const groups = new Map();
  for (const t of d.temps) { if (!groups.has(t.chip)) groups.set(t.chip, []); groups.get(t.chip).push(t); }
  const box = $('#temps');
  if (!d.temps.length) { box.innerHTML = '<div class="empty">No hwmon sensors visible. Is /sys mounted from the host?</div>'; }
  else {
    if (box.querySelector('.empty')) box.innerHTML = '';
    keyed(box, [...groups.entries()], g => g[0],
      () => html(`<div class="chip-grp"><header><b></b><span></span></header><div class="tiles"></div></div>`),
      (el, [chip, list]) => {
        setText(el.querySelector('b'), chip.toUpperCase());
        setText(el.querySelector('header span'), `max ${Math.max(...list.map(t => t.c)).toFixed(1)}°`);
        keyed(el.querySelector('.tiles'), list, t => t.id,
          () => html(`<div class="tile"><span class="l"></span><span class="v"></span><i class="b"></i></div>`),
          (tl, t) => {
            tl.className = 'tile ' + tempClass(t);
            setText(tl.querySelector('.l'), t.label);
            const v = tl.querySelector('.v'), txt = t.c.toFixed(1);
            if (v.dataset.v !== txt) { v.dataset.v = txt; v.innerHTML = `${txt}<small>°C</small>`; }
            tl.querySelector('.b').style.setProperty('--w', clamp(t.c / t.crit * 100, 0, 100) + '%');
            tl.title = `${t.chip} ${t.label}: ${t.c}°C  (warn ${t.warn}°, crit ${t.crit}°)`;
          });
      });
  }
  keyed($('#fans'), d.fans, f => f.chip + f.label,
    () => html(`<span class="fan"><svg viewBox="0 0 12 12"><path d="M6 6C6 2 7.5 1 9 1.5 9 4 7 5 6 6Zm0 0c4 0 5 1.5 4.5 3C8 9 7 7 6 6Zm0 0c0 4-1.5 5-3 4.5C3 8 5 7 6 6Zm0 0C2 6 1 4.5 1.5 3 4 3 5 5 6 6Z"/></svg><span></span><b></b></span>`),
    (el, f) => {
      setText(el.querySelector('span'), f.label); setText(el.querySelector('b'), f.rpm + ' rpm');
      el.style.setProperty('--spd', f.rpm > 0 ? clamp(600 / f.rpm, 0.15, 4) + 's' : '0s');
    });
}

/* ───────── docker ───────── */
function ctClass(c) {
  if (c.state === 'running') return c.health === 'unhealthy' ? 'bad' : c.health === 'starting' ? 'starting' : '';
  if (c.state === 'paused') return 'paused';
  if (c.state === 'restarting' || c.state === 'dead') return 'bad';
  if (c.state === 'exited' && !/Exited \((0|143)\)/.test(c.status)) return 'down bad';
  return 'down';
}
function renderDocker(d) {
  const dk = d.docker, empty = $('#dEmpty'), cards = $('#cards');
  setText($('#dCount'), `${dk.running}/${dk.total} up`);
  setText($('#dVer'), dk.online ? `engine ${dk.version || '?'}` : 'engine OFFLINE');
  if (!dk.online) {
    empty.hidden = false; empty.innerHTML = `<b>ENGINE LINK LOST // 接続不可</b>`;
    empty.append(document.createTextNode(dk.error || 'Cannot reach the Docker socket.'));
    cards.innerHTML = ''; $('#dSum').innerHTML = ''; return;
  }
  const list = dk.containers
    .filter(c => S.showStopped || c.state === 'running')
    .filter(c => !S.filter || (c.name + ' ' + c.image + ' ' + (c.project || '')).toLowerCase().includes(S.filter))
    .sort((a, b) => (b.state === 'running') - (a.state === 'running') || (b.cpu || 0) - (a.cpu || 0) || a.name.localeCompare(b.name));
  empty.hidden = list.length > 0;
  if (!list.length) empty.innerHTML = `<b>NO UNITS // 該当なし</b>${dk.total ? 'No containers match the filter.' : 'No containers on this engine yet.'}`;
  const run = dk.containers.filter(c => c.state === 'running');
  const tCpu = run.reduce((s, c) => s + (c.cpu || 0), 0), tMem = run.reduce((s, c) => s + (c.mem || 0), 0);
  const bad = dk.containers.filter(c => ctClass(c).includes('bad')).length;
  $('#dSum').innerHTML = `<span class="g">RUN <b>${run.length}</b></span><span>CPU <b>${tCpu.toFixed(1)}%</b></span><span>MEM <b>${fmt.bytes(tMem)}</b></span>${bad ? `<span class="r">ALERT <b>${bad}</b></span>` : ''}`;
  keyed(cards, list, c => c.id,
    () => html(`<div class="ct">
      <div class="ct-top"><i class="led"></i><span class="ct-name"></span><span class="ct-tag"></span></div>
      <div class="ct-img"></div>
      <div class="bar-row"><label>CPU</label><div class="bar"><i></i></div><span class="v cpu"></span></div>
      <div class="bar-row"><label>MEM</label><div class="bar mag"><i></i></div><span class="v mem"></span></div>
      <div class="ct-foot"><span class="net"><span class="d"></span> <span class="u"></span></span><span class="st"></span></div>
    </div>`),
    (el, c) => {
      const cls = ctClass(c), running = c.state === 'running';
      el.className = 'ct ' + cls;
      el.querySelector('.led').className = 'led ' + (cls.includes('bad') ? 'crit' : running ? (cls ? 'warn' : 'on') : '');
      setText(el.querySelector('.ct-name'), c.name);
      setText(el.querySelector('.ct-tag'), c.project || '');
      setText(el.querySelector('.ct-img'), c.image + (c.ports.length ? '  :' + c.ports.join(' :') : ''));
      const cpu = running ? (c.cpu || 0) : 0, memPct = c.mem_limit ? (c.mem || 0) / c.mem_limit * 100 : 0;
      const cb = el.querySelector('.bar'); cb.classList.toggle('hi', cpu >= 80);
      cb.querySelector('i').style.setProperty('--w', clamp(cpu, 0, 100) + '%');
      el.querySelector('.bar.mag i').style.setProperty('--w', clamp(running ? memPct : 0, 0, 100) + '%');
      setText(el.querySelector('.v.cpu'), running ? cpu.toFixed(1) + '%' : '—');
      setText(el.querySelector('.v.mem'), running && c.mem != null ? fmt.bytes(c.mem) : '—');
      setText(el.querySelector('.net .d'), running ? '▼' + fmt.bits(c.rx || 0) : '');
      setText(el.querySelector('.net .u'), running ? '▲' + fmt.bits(c.tx || 0) : '');
      setText(el.querySelector('.st'), (c.health ? `♥ ${c.health} · ` : '') + c.status.replace(/\s*\((un)?healthy\)|\s*\(health: starting\)/, ''));
      el.title = `${c.name}\n${c.image}\nid ${c.id}${c.pids != null ? `\npids ${c.pids}` : ''}`;
    });
}

/* ───────── storage + S.M.A.R.T. ───────── */
function etaText(f) {
  if (!f) return ['trend: gathering samples…', ''];
  if (f.state === 'learning') return ['trend: learning (needs ~6h of samples)', ''];
  if (f.state === 'stable') return ['trend: stable', ''];
  const d = f.eta_days;
  const when = d < 1 ? `< 1 day` : d < 60 ? `~${Math.round(d)} days` : d < 730 ? `~${Math.round(d / 30)} months` : '> 2 years';
  return [`+${fmt.bytes(f.per_day)}/day · full in ${when}`, d < 7 ? 'crit' : d < 30 ? 'warn' : ''];
}
function renderDisks(d) {
  setText($('#ioMeta'), `R ${fmt.bytes(d.diskio.r)}/s · W ${fmt.bytes(d.diskio.w)}/s`);
  const box = $('#disks');
  if (!d.disks.length) box.innerHTML = '<div class="empty">Scanning filesystems…</div>';
  else {
    if (box.querySelector('.empty')) box.innerHTML = '';
    keyed(box, d.disks, k => k.mount,
      () => html(`<div class="disk"><div class="disk-top"><span class="mp"></span><span class="sz"></span></div><div class="seg"><i></i></div><span class="eta"></span></div>`),
      (el, k) => {
        el.className = 'disk ' + (k.pct >= 95 ? 'crit' : k.pct >= 85 ? 'warn' : '');
        const mp = el.querySelector('.mp'); mp.textContent = k.mount;
        const em = document.createElement('em'); em.textContent = `${k.fs} · ${k.dev}`; mp.append(em);
        setText(el.querySelector('.sz'), `${fmt.bytes(k.used)} / ${fmt.bytes(k.total)} · ${k.pct.toFixed(0)}%`);
        el.querySelector('.seg i').style.setProperty('--w', k.pct + '%');
        const [txt, lvl] = etaText(k.forecast), eta = el.querySelector('.eta');
        setText(eta, txt); eta.className = 'eta ' + lvl;
      });
  }
  const sm = d.smart || {}, drv = $('#drives');
  if (!sm.available) {
    setText($('#smartMeta'), 'offline');
    drv.innerHTML = `<div class="empty">S.M.A.R.T. unavailable — ${esc(sm.reason || 'unknown')}. See README → “Drive health”.</div>`;
    return;
  }
  setText($('#smartMeta'), `${sm.drives.length} drive${sm.drives.length === 1 ? '' : 's'}`);
  if (drv.querySelector('.empty')) drv.innerHTML = '';
  keyed(drv, sm.drives, x => x.dev,
    () => html(`<div class="drv"><div class="drv-top"><i class="led"></i><span class="md"></span><span class="hb"></span></div>
      <div class="drv-grid"><span><label>WEAR</label><b class="w"></b></span><span><label>TEMP</label><b class="t"></b></span><span><label>POWER-ON</label><b class="h"></b></span><span><label>WRITTEN</label><b class="wr"></b></span></div>
      <div class="bar"><i></i></div><div class="drv-note"></div></div>`),
    (el, x) => {
      if (x.error) {
        el.className = 'drv warn'; el.querySelector('.led').className = 'led warn';
        setText(el.querySelector('.md'), '/dev/' + x.dev); setText(el.querySelector('.hb'), 'NO DATA');
        setText(el.querySelector('.drv-note'), x.error); return;
      }
      el.className = 'drv ' + (x.lvl === 'ok' ? '' : x.lvl);
      el.querySelector('.led').className = 'led ' + (x.lvl === 'ok' ? 'on' : x.lvl);
      const md = el.querySelector('.md'); md.textContent = x.model;
      const em = document.createElement('em'); em.textContent = `${x.kind.toUpperCase()} · ${x.dev}${x.capacity ? ' · ' + fmt.bytes(x.capacity) : ''}`; md.append(em);
      setText(el.querySelector('.hb'), x.passed === false ? 'FAILED' : x.lvl === 'ok' ? 'PASSED' : x.lvl.toUpperCase());
      setText(el.querySelector('.w'), x.wear == null ? 'n/a' : x.wear + '%');
      setText(el.querySelector('.t'), x.temp == null ? '—' : x.temp + '°C');
      setText(el.querySelector('.h'), fmt.hours(x.hours));
      setText(el.querySelector('.wr'), x.written == null ? '—' : fmt.bytes(x.written));
      el.querySelector('.bar i').style.setProperty('--w', (x.wear == null ? 0 : clamp(x.wear, 0, 100)) + '%');
      setText(el.querySelector('.drv-note'), x.issue || '');
      el.title = `${x.model}  serial …${x.serial}`;
    });
}

/* ───────── archive (history) ───────── */
let archiveTimer = null;
async function loadArchive() {
  document.querySelectorAll('#histRange button').forEach(b => b.classList.toggle('on', b.dataset.r === S.range));
  clearTimeout(archiveTimer); archiveTimer = setTimeout(loadArchive, 60000);
  const node = S.node;
  try {
    const [h, sum] = await Promise.all([
      fetch(apiUrl('api/history', { node, range: S.range })).then(r => (r.ok ? r.json() : Promise.reject(r.status))),
      fetch(apiUrl('api/summary', { node, hours: 24 })).then(r => (r.ok ? r.json() : null)),
    ]);
    if (node !== S.node) return;
    S.archive = h; S.summary = sum; S.storeOk = true;
  } catch (e) {
    S.archive = null; S.storeOk = false;
  }
  renderArchive();
}
function renderArchive() {
  const h = S.archive;
  if (!h) {
    setText($('#histMeta'), S.storeOk ? 'loading…' : 'archive unavailable — is /data writable?');
    for (const c of [charts.hCpu, charts.hTemp, charts.hNet]) c.set([]);
    return;
  }
  const long = h.range === '7d' || h.range === '30d';
  const xl = (i, n, axis) => {
    const t = h.ts[i]; if (t == null) return '';
    const d = new Date(t);
    if (!long) return fmt.hm(t);
    return axis ? `${pad2(d.getMonth() + 1)}/${pad2(d.getDate())}` : `${pad2(d.getMonth() + 1)}/${pad2(d.getDate())} ${fmt.hm(t)}`;
  };
  const stepLbl = h.step >= 3600 ? h.step / 3600 + 'h' : h.step / 60 + 'm';
  setText($('#histMeta'), `${S.node} · ${h.range.toUpperCase()} · ${stepLbl} buckets · ${h.points ? h.points + ' filled' : 'no data yet'}`);
  const cpuPeak = Math.max(0, ...h.cpu_max.filter(v => v != null));
  const hotPeak = Math.max(0, ...h.hot.filter(v => v != null));
  setText($('#hCpuLbl'), h.points ? `peak ${cpuPeak.toFixed(0)}%` : '');
  setText($('#hTempLbl'), hotPeak ? `peak ${hotPeak.toFixed(1)}°C` : '');
  setText($('#hNetLbl'), h.points ? `avg ▼${fmt.bits(avg(h.rx))} ▲${fmt.bits(avg(h.tx))}` : '');
  charts.hCpu.set([{ name: 'CPU avg', data: h.cpu, color: 'a1' }, { name: 'CPU peak', data: h.cpu_max, color: 'a3', fill: false, width: 1 },
    { name: 'MEM', data: h.mem, color: 'a2', fill: false, width: 1.3 }], { xlabel: xl });
  charts.hTemp.set([{ name: 'CPU temp', data: h.hot, color: 'amber' }], { xlabel: xl });
  charts.hNet.set([{ name: 'RX', data: h.rx, color: 'a1' }, { name: 'TX', data: h.tx, color: 'a2' }], { xlabel: xl });
  const s = S.summary, box = $('#histSum');
  if (s && s.minutes) {
    box.innerHTML = `<span>24H CPU avg <b>${s.cpu_avg}%</b></span>`
      + `<span class="hl">peak <b>${s.cpu_peak?.toFixed(0)}%</b> @ ${fmt.hm(s.cpu_peak_t)}</span>`
      + (s.hot_peak != null ? `<span class="hl">hottest <b>${s.hot_peak.toFixed(1)}°C</b> @ ${fmt.hm(s.hot_peak_t)}</span>` : '')
      + `<span>traffic <b>▼${fmt.bytes(s.rx_bytes)} ▲${fmt.bytes(s.tx_bytes)}</b></span>`
      + `<span>incidents <b>${s.incidents}</b></span>`;
  } else box.innerHTML = '<span>The archive fills in as HEARTH runs — one sample per minute.</span>';
}
const avg = a => { const v = a.filter(x => x != null); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : 0; };

/* ───────── nodes (LAN) ───────── */
function renderNodes(d) {
  const lan = d.net.lan || [], peers = d.net.conns?.peers || [];
  setText($('#lanMeta'), `${lan.length} neighbors · ${d.net.conns?.peer_count ?? 0} peers`);
  const ll = $('#lanList'), pl = $('#peerList');
  const fleetIps = new Map(S.fleet.filter(n => n.addr).map(n => [n.addr, n.name]));
  if (!lan.length) ll.innerHTML = '<li><span class="aux">ARP table empty — host network mode required.</span></li>';
  else {
    if (!ll.firstElementChild?.dataset.key) ll.innerHTML = '';
    keyed(ll, lan, n => n.ip + n.dev,
      () => html(`<li><span class="ip"></span><span class="side"></span><span class="aux"></span></li>`),
      (el, n) => {
        const ip = el.querySelector('.ip'); ip.textContent = n.ip;
        if (fleetIps.has(n.ip)) { const b = document.createElement('span'); b.className = 'badge vpn'; b.textContent = fleetIps.get(n.ip); ip.append(b); }
        setText(el.querySelector('.aux'), n.mac); setText(el.querySelector('.side'), n.dev);
      });
  }
  if (!peers.length) pl.innerHTML = '<li><span class="aux">No established TCP sessions.</span></li>';
  else {
    if (!pl.firstElementChild?.dataset.key) pl.innerHTML = '';
    keyed(pl, peers, p => p.ip,
      () => html(`<li><span class="ip"></span><span class="side"></span><span class="aux"></span></li>`),
      (el, p) => {
        const ip = el.querySelector('.ip'); ip.textContent = p.ip;
        const b = document.createElement('span'); b.className = 'badge ' + p.scope; b.textContent = p.scope.toUpperCase(); ip.append(b);
        setText(el.querySelector('.aux'), 'ports ' + p.ports.join(', '));
        setText(el.querySelector('.side'), `${p.n} conn${p.n > 1 ? 's' : ''}`);
      });
  }
}

/* ───────── log (fleet-wide) ───────── */
function ingestEvents(events, reset) {
  if (reset) S.log = new Map();
  const fresh = [];
  for (const e of events || []) if (!S.log.has(e.id)) { S.log.set(e.id, e); fresh.push(e); }
  if (!fresh.length && !reset) return;
  const all = [...S.log.values()].sort((a, b) => b.id - a.id).slice(0, 100);
  S.log = new Map(all.map(e => [e.id, e]));
  setText($('#logMeta'), `${all.length} entries`);
  keyed($('#log'), all, e => e.id,
    e => {
      const li = html(`<li><time></time><span class="src"></span><span class="msg"><span class="nd"></span><span class="tx"></span></span></li>`);
      li.className = e.lvl; li.querySelector('time').textContent = fmt.time(e.t);
      li.querySelector('.src').textContent = e.src; li.querySelector('.nd').textContent = e.node || '';
      li.querySelector('.tx').textContent = e.msg; return li;
    }, () => {});
  // sound: only for events that arrive live, not the backlog
  if (S.logPrimed && fresh.length) {
    if (fresh.some(e => e.lvl === 'crit')) SFX.play('crit');
    else if (fresh.some(e => e.rec)) SFX.play('rec');
    else if (fresh.some(e => e.lvl === 'warn')) SFX.play('warn');
  }
  S.logPrimed = true;
}

/* ───────── navigator KAGARI ───────── */
const nav = { key: null, rot: 0, rotAt: 0, typer: null };
function greeting() {
  const h = new Date().getHours();
  return h < 5 ? ['(￣o￣) zzz', `Still up at ${fmt.hm(Date.now())}, commander? I'll keep the hearth warm.`, '夜更かしですね…']
    : h < 11 ? ['(｡•̀ᴗ-)✧', 'Good morning, commander! Systems warmed up and ready.', 'おはようございます！']
      : h < 18 ? ['(・ω・)ノ', 'Good afternoon! Everything is humming along nicely.', 'こんにちは！']
        : ['(´｡• ᵕ •｡`)', 'Good evening, commander. Quiet night on the network so far.', 'こんばんは。'];
}
function recentEvents(ms, pred) { const now = Date.now(); return [...S.log.values()].filter(e => now - e.t < ms && pred(e)).sort((a, b) => b.id - a.id); }
function navState(d) {
  const me = curNode(), node = S.node;
  const crit = d.temps.filter(t => t.lvl === 'crit').sort((a, b) => b.c - a.c)[0];
  if (crit) return { key: 'crit-' + crit.id, mood: 'crit', kao: '(×﹏×)', label: 'MELTDOWN RISK',
    en: `Commander!! ${crit.label} on ${d.host.name} is at ${crit.c.toFixed(0)}°C — the hearth is overheating!`, jp: '熱暴走の危険！冷却を急いで！' };
  const lost = S.fleet.filter(n => !n.online);
  if (lost.length) return { key: 'lost-' + lost.map(n => n.id).join(), mood: 'crit', kao: '(⊙_⊙;)', label: 'NODE SILENT',
    en: `${lost.map(n => n.name).join(' & ')} went silent ${fmt.ago(lost[0].age)} ago! I can't hear ${lost.length > 1 ? 'them' : 'it'} on the LAN anymore…`, jp: 'ノード応答なし！' };
  const rec = recentEvents(45000, e => e.rec)[0];
  if (rec) return { key: 'rec-' + rec.id, mood: 'happy', kao: '٩(◕‿◕｡)۶', label: 'RECOVERED',
    en: `Yay! ${rec.node && S.fleet.length > 1 ? rec.node + ': ' : ''}${rec.msg}! Everything's okay again~`, jp: '復旧しました！やったね！' };
  if (!d.docker.online) return { key: 'dock', mood: 'warn', kao: '(；一_一)', label: 'ENGINE LOST',
    en: `I can't reach the Docker engine on ${d.host.name}… is the socket mounted into my container?`, jp: 'ドッカーと通信できません。' };
  const bad = recentEvents(90000, e => e.node === node && e.src === 'docker' && (e.lvl === 'crit' || e.lvl === 'warn'))[0];
  if (bad) return { key: 'ev-' + bad.id, mood: 'warn', kao: '(╥﹏╥)', label: 'UNIT DOWN', en: `Uh-oh. ${bad.msg}. Should I be worried?`, jp: 'コンテナに異常発生…' };
  const drv = (d.smart?.drives || []).filter(x => x.lvl === 'crit' || x.lvl === 'warn').sort((a, b) => (b.lvl === 'crit') - (a.lvl === 'crit'))[0];
  if (drv) return { key: 'smart-' + drv.dev + drv.lvl, mood: drv.lvl, kao: '(｡•́︿•̀｡)', label: 'DRIVE HEALTH',
    en: `${drv.model} is reporting ${drv.issue || 'problems'}. Please back up anything precious!`, jp: 'ディスクの健康状態に注意。' };
  const full = d.disks.filter(k => k.pct >= 95).sort((a, b) => b.pct - a.pct)[0];
  if (full) return { key: 'full-' + full.mount, mood: 'warn', kao: '(・・;)ゞ', label: 'DISK FULL',
    en: `${full.mount} is ${full.pct.toFixed(0)}% full — only ${fmt.bytes(full.free)} left! Time for some spring cleaning~`, jp: '空き容量がほとんどありません。' };
  const soon = d.disks.filter(k => k.forecast?.eta_days != null && k.forecast.eta_days < 14).sort((a, b) => a.forecast.eta_days - b.forecast.eta_days)[0];
  if (soon) return { key: 'eta-' + soon.mount, mood: 'warn', kao: '(°ー°〃)', label: 'FILLING UP',
    en: `At this rate ${soon.mount} fills up in about ${Math.max(1, Math.round(soon.forecast.eta_days))} day${soon.forecast.eta_days < 1.5 ? '' : 's'}.`, jp: 'ディスクがもうすぐ満杯に。' };
  const warn = d.temps.filter(t => t.lvl === 'warn').sort((a, b) => b.c - a.c)[0];
  if (warn) return { key: 'warn-' + warn.id, mood: 'warn', kao: '(°ロ°;)', label: 'RUNNING HOT',
    en: `${warn.label} is running toasty at ${warn.c.toFixed(0)}°C. I'll keep watch~`, jp: '温度上昇中、監視を続けます。' };
  if (d.cpu.total >= 90) return { key: 'cpu', mood: 'warn', kao: '(ﾟДﾟ;)', label: 'FULL BURN',
    en: `CPU at ${d.cpu.total.toFixed(0)}% on ${d.host.name}! Every core is giving it everything!`, jp: '全コア全力稼働中！' };
  if (d.mem.pct >= 90) return { key: 'mem', mood: 'warn', kao: '(・_・;)', label: 'MEMORY TIGHT',
    en: `Memory's nearly full — ${fmt.bytes(d.mem.used)} of ${fmt.bytes(d.mem.total)}. Squeezy!`, jp: 'メモリ残量わずか。' };
  const prim = d.net.ifaces.find(i => i.name === d.net.primary);
  if (prim && prim.rx * 8 > 100e6) return { key: 'net', mood: 'happy', kao: 'ヽ(°〇°)ﾉ', label: 'HEAVY TRAFFIC',
    en: `Whoa, ${fmt.bits(prim.rx)} pouring in on ${prim.name}! Big download, commander?`, jp: '大量のデータを受信中！' };
  const [gk, ge, gj] = greeting();
  const s = S.summary;
  const idle = [
    { kao: gk, en: ge, jp: gj },
    { kao: '(｀・ω・´)ゞ', en: `All systems nominal on ${d.host.name}. ${d.docker.running} container${d.docker.running === 1 ? '' : 's'} standing by!`, jp: '全システム正常です。' },
    s && s.minutes > 30 ? { kao: '(｀・ω・´)📋', en: `Daily report! CPU averaged ${s.cpu_avg}% and peaked at ${s.cpu_peak?.toFixed(0)}% at ${fmt.hm(s.cpu_peak_t)}.${s.hot_peak != null ? ` Hottest was ${s.hot_peak.toFixed(0)}°C at ${fmt.hm(s.hot_peak_t)}.` : ''} We moved ▼${fmt.bytes(s.rx_bytes)} ▲${fmt.bytes(s.tx_bytes)}. ${s.incidents ? `${s.incidents} incident${s.incidents > 1 ? 's' : ''} logged.` : 'Zero incidents — perfect day!'}`, jp: '本日の報告です。' } : null,
    S.fleet.length > 1 ? { kao: '(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧', en: `${S.fleet.length} nodes reporting in across the LAN. The whole fleet is green!`, jp: '艦隊、全機正常。' } : null,
    { kao: '(￣▽￣)ノ', en: `Uptime ${fmt.dur(d.host.uptime)}. The hearth burns steady.`, jp: '炉心、安定稼働中。' },
    { kao: '( ´ ▽ ` )', en: d.hot ? `Hottest CPU sensor is a comfy ${d.hot.c.toFixed(0)}°C.` : `I can't see any temperature sensors here, but I'm sure it's cozy!`, jp: '温度は快適です。' },
    { kao: '(・ω・)', en: `CPU ${d.cpu.total.toFixed(0)}% · RAM ${d.mem.pct.toFixed(0)}%. Plenty of headroom left.`, jp: '余裕あり、問題なし。' },
  ].filter(Boolean);
  if (Date.now() - nav.rotAt > 14000) { nav.rot = (nav.rot + 1) % idle.length; nav.rotAt = Date.now(); }
  const pick = idle[nav.rot % idle.length];
  return { key: 'idle-' + nav.rot + node, mood: 'ok', label: me && !me.local ? `NOMINAL · ${me.name}` : 'NOMINAL', ...pick };
}
function typeTo(el, text) {
  clearInterval(nav.typer);
  if (REDUCED) { el.textContent = text; return; }
  let i = 0; el.textContent = '';
  const chars = [...text];
  nav.typer = setInterval(() => { el.textContent = chars.slice(0, ++i).join(''); if (i >= chars.length) clearInterval(nav.typer); }, 20);
}
function renderNav(d) {
  const s = navState(d);
  if (s.key === nav.key) return;
  nav.key = s.key;
  $('#navFace').className = 'nav-face ' + (s.mood === 'ok' ? '' : s.mood);
  const m = $('#navMood'); m.textContent = '● ' + s.label; m.className = 'nt-mood ' + (s.mood === 'happy' ? '' : s.mood);
  setText($('#navKao'), s.kao); setText($('#navJp'), s.jp);
  typeTo($('#navLine'), s.en);
}

function renderTickerVals(d) {
  const prim = d.net.ifaces.find(i => i.name === d.net.primary) || d.net.ifaces[0];
  setTick('host', d.host.name); setTick('cpu', d.cpu.total.toFixed(0) + '%'); setTick('mem', d.mem.pct.toFixed(0) + '%');
  setTick('rx', fmt.bits(prim?.rx || 0)); setTick('tx', fmt.bits(prim?.tx || 0));
  setTick('ct', `${d.docker.running}/${d.docker.total}`);
  setTick('temp', d.hot ? d.hot.c.toFixed(0) + '°C' : 'n/a'); setTick('up', fmt.dur(d.host.uptime));
  setTick('status', document.body.classList.contains('alerting') || document.body.classList.contains('stale') ? '警告 WARNING' : '正常 NOMINAL');
}

/* ───────── websocket ───────── */
let ws = null, retry = 1000;
function connect() {
  const u = apiUrl('ws', { node: S.node }); u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(u);
  const link = $('#link');
  ws.onopen = () => { retry = 1000; link.classList.add('on'); setText(link.querySelector('span'), 'LINK'); };
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'hello') {
      const changed = m.node !== S.node || !S.archive;
      S.node = m.node; S.fleet = m.fleet || [];
      loadHistory(m.history);
      ingestEvents(m.events, true);
      renderFleet();
      if (m.data) render(m.data);
      if (changed) { S.archive = null; S.summary = null; loadArchive(); }
    } else if (m.type === 'tick') {
      if (m.node !== S.node) return;
      record(m.data); render(m.data);
    } else if (m.type === 'fleet') {
      S.fleet = m.nodes || []; renderFleet(); ingestEvents(m.events, false);
    }
  };
  ws.onclose = () => {
    link.classList.remove('on'); setText(link.querySelector('span'), 'NO LINK');
    setTimeout(connect, retry); retry = Math.min(retry * 1.6, 15000);
  };
  ws.onerror = () => ws.close();
}
connect();
