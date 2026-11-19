/* ARSENAL // 工廠 — maintenance bay client */
'use strict';

const bootDone = bootSeq([
  'ARSENAL BAY v3.3 // 工廠',
  'pressurising maintenance dock ..... <span class="ok">OK</span>',
  'inventorying image registry ....... <span class="ok">OK</span>',
  'arming update gantry // 更新 ....... <span class="ok">OK</span>',
  'mounting backup vault // 保管庫 .... <span class="ok">OK</span>',
  'opening service journal ........... <span class="ok">OK</span>',
  '>> 整備準備完了 — bay ready, commander',
]);

const A = { d: null, ok: false, view: store.get('arsenal.view') || 'todo', minutes: 60, jobs: new Map(), primed: false, pending: new Set() };
const UPD = {
  update: ['UPDATE AVAILABLE', 'warn'], pulled: ['NEW IMAGE PULLED · RECREATE', 'warn'], current: ['UP TO DATE', 'ok'],
  local: ['LOCAL BUILD', 'dim'], pinned: ['PINNED DIGEST', 'dim'], untagged: ['UNTAGGED', 'dim'], missing: ['IMAGE MISSING', 'dim'],
  error: ['CHECK FAILED', 'crit'], unknown: ['NOT CHECKED YET', 'dim'],
};
const PRUNE = [['images', 'Dangling images', 'untagged layers left behind by updates and rebuilds'],
  ['images-all', 'All unused images', 'every image no container uses, even tagged ones (they re-download when needed)'],
  ['containers', 'Stopped containers', 'containers that exited; their named volumes are kept'],
  ['build', 'Build cache', 'cached layers from docker build']];
const nodeById = id => A.d?.nodes.find(n => n.id === id);
const can = n => !!(A.d?.hub_control && n?.control && n.online);

async function poll() {
  try {
    const r = await fetch('api/arsenal');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    A.d = await r.json(); A.ok = true;
    render();
    bootDone();
  } catch (e) {
    A.ok = false; renderLink(); setText($('#footInfo'), `hub unreachable (${e.message})`);
  }
  setTimeout(poll, document.hidden ? 15000 : 3000);
}
function renderLink() { const l = $('#link'); l.classList.toggle('on', A.ok); setText(l.querySelector('span'), A.ok ? 'LIVE' : 'NO LINK'); }

async function act(node, action, extra = {}) {
  const key = `${node}|${action}|${extra.id || extra.arg || ''}`;
  if (A.pending.has(key)) return null;
  A.pending.add(key); render();
  try { return await postJSON('api/arsenal/action', { node, action, ...extra }); }
  catch (e) { toast(`${nodeById(node)?.name || node}: ${e.message}`, 'crit'); return null; }
  finally { A.pending.delete(key); render(); }
}
function confirmBox(title, msgHtml, okLabel, fn, danger = true) {
  const body = html(`<div class="confirm"><p></p><div class="row"><button class="tgl" type="button" data-x="no">CANCEL</button><button class="tgl on ${danger ? 'danger' : ''}" type="button" data-x="yes"></button></div></div>`);
  body.querySelector('p').innerHTML = msgHtml; body.querySelector('[data-x=yes]').textContent = okLabel;
  body.addEventListener('click', e => { const x = e.target.closest('[data-x]')?.dataset.x; if (!x) return; modal.close(); if (x === 'yes') fn(); });
  modal.open(title, body, 'sm');
  body.querySelector('[data-x=no]').focus();
}

/* ───────── render ───────── */
function render() {
  const d = A.d; if (!d) return;
  renderLink();
  const off = d.nodes.filter(n => n.online && !n.control);
  const ro = $('#readonly');
  ro.hidden = d.hub_control && !off.length;
  if (!ro.hidden) {
    const msg = d.hub_control
      ? `Control is off on ${off.map(n => n.name).join(', ')}: set <code>HEARTH_ALLOW_CONTROL=1</code> there to update, prune and run stacks. Everything else works.`
      : 'Updating, pruning and stack control need <code>HEARTH_ALLOW_CONTROL=1</code> on the hub, and on each agent you want to control. Checks, maintenance windows and the journal work without it.';
    if (ro.dataset.m !== msg) { ro.dataset.m = msg; ro.innerHTML = `<b>${d.hub_control ? 'PARTIAL CONTROL // 一部' : 'READ-ONLY // 閲覧専用'}</b> ${msg}`; }
  }
  trackJobs();
  renderStats(); renderUpdates(); renderMachines(); renderBackups(); renderMaint(); renderStacks(); renderJournal();
  setText($('#footInfo'), `hub ${d.nodes.find(n => n.local)?.name || '?'} · ${d.nodes.filter(n => n.online).length}/${d.nodes.length} machines online · refreshed ${fmt.hm(d.t)}`);
}

function allUpdates() {
  const out = [];
  for (const n of A.d.nodes) for (const u of n.arsenal?.updates || []) out.push({ ...u, node: n.id, nodeName: n.name, online: n.online });
  return out;
}
function backupsAll() {
  const out = [];
  for (const n of A.d.nodes) for (const b of n.arsenal?.backups || []) out.push({ ...b, node: n.id, nodeName: n.name, kindLabel: b.kind });
  for (const h of A.d.heartbeats) out.push({ name: h.name, node: null, nodeName: 'heartbeat', kind: 'heartbeat', kindLabel: 'heartbeat', last: h.last, fail: h.fail, max: h.max, lvl: h.lvl || 'warn', detail: h.msg });
  return out;
}

function renderStats() {
  const ups = allUpdates().filter(u => u.state === 'update' || u.state === 'pulled');
  const reboots = A.d.nodes.filter(n => n.arsenal?.host?.reboot);
  const bk = backupsAll(), bad = bk.filter(b => b.lvl !== 'ok');
  const win = A.d.maint.length;
  const chip = (cls, n, label, href) => `<a class="a-stat ${n ? cls : 'zero'}" href="${href}"><b>${n}</b>${label}</a>`;
  $('#stats').innerHTML = chip('warn', ups.length, ups.length === 1 ? 'UPDATE' : 'UPDATES', '#updates')
    + chip('warn', reboots.length, reboots.length === 1 ? 'REBOOT' : 'REBOOTS', '#machines')
    + (bk.length ? chip('crit', bad.length, bad.length === 1 ? 'BACKUP ISSUE' : 'BACKUP ISSUES', '#backups') : '')
    + (win ? chip('maint', win, 'MAINTENANCE', '#maint') : '');
}

function renderUpdates() {
  const all = allUpdates();
  const todo = all.filter(u => u.state === 'update' || u.state === 'pulled');
  const list = (A.view === 'todo' ? todo : all).sort((a, b) =>
    (a.state === 'update' || a.state === 'pulled' ? 0 : 1) - (b.state === 'update' || b.state === 'pulled' ? 0 : 1)
    || a.nodeName.localeCompare(b.nodeName) || a.name.localeCompare(b.name));
  const checked = A.d.nodes.map(n => n.arsenal?.checked).filter(Boolean);
  const checking = A.d.nodes.some(n => n.arsenal?.checking);
  setText($('#updMeta'), checking ? 'checking registries…' : checked.length ? `checked ${sinceMs(Math.min(...checked))} ago · ${all.length} containers` : 'first check runs shortly after start');
  const doable = todo.filter(u => can(nodeById(u.node)) && !u.self);
  const ua = $('#updAll'); ua.hidden = doable.length < 2; setText(ua, `⬆ UPDATE ALL (${doable.length})`);
  setText($('#updHint'), todo.length ? 'Each update pulls the image and recreates the container with the same settings; if anything fails the old container is put back.' : '');
  keyed($('#updList'), list, u => u.node + '/' + u.id,
    () => html(`<div class="a-tr" role="row"><span class="a-dot"></span><b class="a-name"></b><span class="a-node"></span><code class="a-img"></code><span class="a-state"></span><span class="a-when"></span><button class="tgl sm" type="button" data-act="update">⬆ UPDATE</button></div>`),
    (el, u) => {
      const [label, cls] = UPD[u.state] || UPD.unknown;
      el.className = `a-tr lvl-${cls}`;
      setText(el.querySelector('.a-name'), u.name);
      setText(el.querySelector('.a-node'), u.nodeName);
      setText(el.querySelector('.a-img'), u.image);
      const st = el.querySelector('.a-state'); setText(st, label); st.title = u.error || '';
      setText(el.querySelector('.a-when'), u.checked ? sinceMs(u.checked) + ' ago' : '');
      const b = el.querySelector('button'), n = nodeById(u.node), busy = A.pending.has(`${u.node}|update|${u.id}`) || jobRunning(u.node, 'update', u.name);
      b.hidden = !(u.state === 'update' || u.state === 'pulled');
      b.disabled = busy || !can(n) || u.self;
      setText(b, busy ? 'UPDATING…' : '⬆ UPDATE');
      b.title = u.self ? 'HEARTH updates itself with git pull && docker compose up -d --build'
        : !can(n) ? (n?.online ? 'Control is off on this machine (HEARTH_ALLOW_CONTROL=1)' : 'Machine offline') : `Pull ${u.image} and recreate ${u.name}`;
    });
  const empty = $('#updEmpty');
  empty.hidden = list.length > 0;
  if (!list.length) empty.textContent = A.view === 'todo'
    ? (all.length ? `All ${all.length} containers are on their newest image. ✓` : 'No containers reported yet. The first check runs a minute after HEARTH starts.')
    : 'No containers reported yet.';
}
function jobRunning(node, action, target) {
  return (nodeById(node)?.arsenal?.jobs || []).some(j => j.state === 'running' && j.action === action && j.target === target);
}
function trackJobs() {
  for (const n of A.d.nodes) for (const j of n.arsenal?.jobs || []) {
    const key = n.id + j.id, prev = A.jobs.get(key);
    if (A.primed && prev === 'running' && j.state !== 'running') {
      toast(`${n.name}: ${j.action} ${j.target} — ${j.msg || j.state}`, j.state === 'ok' ? 'ok' : 'crit');
    }
    A.jobs.set(key, j.state);
  }
  A.primed = true;
}

function renderMachines() {
  const nodes = A.d.nodes;
  setText($('#machMeta'), `${nodes.length} machine${nodes.length === 1 ? '' : 's'}`);
  keyed($('#machines'), nodes, n => n.id,
    () => html(`<div class="a-m"><header><i class="a-dot"></i><b></b><span class="a-os"></span><button class="tgl sm" type="button" data-act="check">⟳ CHECK</button></header>
      <div class="a-host"></div><div class="a-df"></div>
      <div class="a-prune"><button class="tgl sm" type="button" data-act="prune">🧹 CLEAN UP…</button></div>
      <ol class="a-jobs"></ol></div>`),
    (el, n) => {
      const a = n.arsenal || {}, h = a.host || {}, df = a.df;
      el.className = `a-m ${n.online ? '' : 'off'}`;
      setText(el.querySelector('header b'), n.name);
      setText(el.querySelector('.a-os'), [n.os, n.uptime != null ? `up ${fmt.dur(n.uptime)}` : null].filter(Boolean).join(' · '));
      const hostBits = [];
      if (!n.online) hostBits.push('<span class="tag crit">OFFLINE</span> showing the last report');
      else if (!n.arsenal) hostBits.push('<span class="tag dim">NO DATA</span> this agent runs an older HEARTH: rebuild it to see maintenance info');
      if (h.reboot) hostBits.push(`<span class="tag warn">REBOOT REQUIRED</span> ${esc(h.reason || '')}`);
      else if (n.arsenal) hostBits.push(`<span class="tag ok">NO REBOOT NEEDED</span> kernel ${esc(n.kernel || '?')}`);
      if (h.pending != null) hostBits.push(`<span class="tag ${h.pending ? 'warn' : 'ok'}">${h.pending} PACKAGE UPDATES</span>${h.security ? ` ${h.security} security` : ''}`);
      if (h.pkg_changed) hostBits.push(`<span class="dim">packages last changed ${sinceMs(h.pkg_changed)} ago</span>`);
      const hostHtml = hostBits.map(x => `<div>${x}</div>`).join('');
      const he = el.querySelector('.a-host'); if (he.dataset.h !== hostHtml) { he.dataset.h = hostHtml; he.innerHTML = hostHtml; }
      const dfHtml = df ? [
        ['IMAGES', fmt.bytes(df.images.size), `${df.images.n} · ${df.images.unused} unused`],
        ['RECLAIMABLE', fmt.bytes(df.images.dangling_size + df.containers.stopped_size + df.build.reclaim), `${df.images.dangling} dangling · ${df.containers.stopped} stopped`],
        ['BUILD CACHE', fmt.bytes(df.build.size), `${fmt.bytes(df.build.reclaim)} idle`],
        ['VOLUMES', fmt.bytes(df.volumes.size), `${df.volumes.n} · ${df.volumes.unused} unused`],
      ].map(([l, v, s]) => `<div><label>${l}</label><b>${v}</b><small>${esc(s)}</small></div>`).join('') + `<p class="dim">disk usage measured ${sinceMs(df.t)} ago</p>` : '<p class="dim">Docker disk usage is measured every 30 minutes.</p>';
      const de = el.querySelector('.a-df'); if (de.dataset.h !== dfHtml) { de.dataset.h = dfHtml; de.innerHTML = dfHtml; }
      const pb = el.querySelector('[data-act=prune]'); pb.disabled = !can(n) || !df;
      pb.title = can(n) ? '' : 'Control is off on this machine (HEARTH_ALLOW_CONTROL=1)';
      const cb = el.querySelector('[data-act=check]'), checking = a.checking || A.pending.has(`${n.id}|check|`);
      cb.disabled = !n.online || checking; setText(cb, checking ? 'CHECKING…' : '⟳ CHECK');
      const jobs = (a.jobs || []).slice(-4).reverse();
      keyed(el.querySelector('.a-jobs'), jobs, j => j.id, () => html('<li><span></span><b></b><em></em></li>'), (li, j) => {
        li.className = 'j-' + j.state;
        setText(li.querySelector('span'), j.state === 'running' ? '◌' : j.state === 'ok' ? '✓' : '✖');
        setText(li.querySelector('b'), `${j.action} ${j.target}`);
        setText(li.querySelector('em'), j.state === 'running' ? `running ${sinceMs(j.t)}` : `${j.msg || ''} · ${fmt.when(j.done || j.t)}`);
      });
    });
}

function renderBackups() {
  const list = backupsAll();
  const bad = list.filter(b => b.lvl !== 'ok').length;
  setText($('#bkMeta'), list.length ? (bad ? `${bad} need attention` : 'all fresh') : 'none configured');
  const box = $('#backups');
  if (!list.length) {
    box.innerHTML = `<div class="empty">Nothing watched yet. Tell HEARTH where backups land, on the machine that has them:<br>
      <code>HEARTH_BACKUPS=photos=/mnt/backup/restic@26h, dumps=/srv/dumps/*.sql.gz@2d</code><br>
      or let a job ping the hub when it finishes: <code>HEARTH_HEARTBEATS=offsite=26h</code></div>`;
    return;
  }
  if (box.querySelector('.empty')) box.innerHTML = '';
  keyed(box, list, b => (b.node || 'hb') + '/' + b.name,
    () => html(`<div class="a-row"><i class="a-dot"></i><div class="a-main"><b></b><small></small></div><span class="a-age"></span><span class="a-extra"></span></div>`),
    (el, b) => {
      el.className = `a-row lvl-${b.lvl === 'ok' ? 'ok' : b.lvl}`;
      setText(el.querySelector('b'), b.name);
      const where = b.kind === 'heartbeat' ? 'heartbeat' : `${b.kindLabel} on ${b.nodeName}`;
      setText(el.querySelector('small'), [where, b.path, b.detail].filter(Boolean).join(' · '));
      setText(el.querySelector('.a-age'), b.last ? `${sinceMs(b.last)} ago / max ${fmt.dur(b.max)}` : `never / max ${fmt.dur(b.max)}`);
      const ex = el.querySelector('.a-extra');
      if (b.kind === 'heartbeat' && !ex.firstChild) {
        const url = `${location.origin}/api/ping/${encodeURIComponent(b.name)}`;
        ex.innerHTML = `<button class="tgl sm" type="button" title="${esc(url)} — append /fail when the job fails">COPY PING URL</button>`;
        ex.firstChild.addEventListener('click', () => {
          navigator.clipboard?.writeText(url).then(() => toast('Copied: ' + url, 'ok'), () => toast(url));
        });
      }
    });
}

function renderMaint() {
  const w = A.d.maint;
  setText($('#mtMeta'), w.length ? `${w.length} active · push alerts muted` : 'push alerts on');
  keyed($('#windows'), w, x => x.id,
    () => html(`<div class="a-row lvl-maint"><i class="a-dot"></i><div class="a-main"><b></b><small></small></div><span class="a-age"></span><span class="a-extra"><button class="tgl sm" type="button">END</button></span></div>`),
    (el, x) => {
      setText(el.querySelector('b'), x.scope.toUpperCase());
      setText(el.querySelector('small'), [x.reason, `opened ${fmt.when(x.start)} from ${x.who}`].filter(Boolean).join(' · '));
      setText(el.querySelector('.a-age'), `until ${fmt.hm(x.until)} · ${fmt.dur(Math.max(0, (x.until - Date.now()) / 1000))} left`);
      el.querySelector('button').onclick = async () => {
        try { await postJSON('api/maint/stop', { id: x.id }); toast('Maintenance window ended, push alerts are back on', 'ok'); poll(); }
        catch (e) { toast(e.message, 'crit'); }
      };
    });
  syncNodeSelect($('#mtNode'), 'Whole fleet');
}
function syncNodeSelect(sel, allLabel) {
  const want = A.d.nodes.map(n => n.id + ':' + n.name).join('|');
  if (sel.dataset.k === want) return;
  const cur = sel.value; sel.dataset.k = want;
  sel.innerHTML = `<option value="">${allLabel}</option>` + A.d.nodes.map(n => `<option value="${esc(n.id)}">${esc(n.name)}</option>`).join('');
  sel.value = A.d.nodes.some(n => n.id === cur) ? cur : '';
}

function renderStacks() {
  const groups = A.d.nodes.filter(n => n.stacks.length);
  setText($('#stkMeta'), `${groups.reduce((s, n) => s + n.stacks.length, 0)} compose projects`);
  const box = $('#stacks');
  keyed(box, groups, n => n.id, () => html('<div class="a-sg"><div class="sub-h"><b></b><span></span></div><div class="a-rows"></div></div>'), (g, n) => {
    setText(g.querySelector('.sub-h b'), n.name.toUpperCase());
    setText(g.querySelector('.sub-h span'), can(n) ? '' : n.online ? 'control off' : 'offline');
    keyed(g.querySelector('.a-rows'), n.stacks, s => s.name,
      () => html(`<div class="a-row"><i class="a-dot"></i><div class="a-main"><b></b><small></small></div><span class="a-extra">
        <button class="tgl sm" type="button" data-s="start" title="Start">▶</button><button class="tgl sm" type="button" data-s="restart" title="Restart">↻</button><button class="tgl sm" type="button" data-s="stop" title="Stop">■</button></span></div>`),
      (el, s) => {
        const lvl = s.bad ? 'crit' : s.running === s.total ? 'ok' : s.running ? 'warn' : 'dim';
        el.className = `a-row lvl-${lvl}`;
        setText(el.querySelector('b'), s.name);
        setText(el.querySelector('small'), `${s.running}/${s.total} running${s.bad ? ` · ${s.bad} failing` : ''}${s.self ? ' · includes HEARTH (skipped)' : ''}`);
        el.querySelectorAll('[data-s]').forEach(b => {
          const busy = jobRunning(n.id, b.dataset.s, `stack ${s.name}`) || A.pending.has(`${n.id}|stack|${s.name}:${b.dataset.s}`);
          b.disabled = !can(n) || busy;
          b.onclick = () => stackAction(n, s, b.dataset.s);
        });
      });
  });
  if (!groups.length) box.innerHTML = '<div class="empty">No docker compose projects found.</div>';
  else box.querySelector(':scope > .empty')?.remove();
}
function stackAction(n, s, what) {
  const go = async () => { const r = await act(n.id, 'stack', { arg: `${s.name}:${what}` }); if (r) toast(`${what} stack ${s.name} on ${n.name}…`); };
  if (what === 'start') return go();
  confirmBox(`${what.toUpperCase()} // ${s.name}`, `${what === 'stop' ? 'Stop' : 'Restart'} all ${s.total} containers of <b>${esc(s.name)}</b> on ${esc(n.name)}?`, what.toUpperCase(), go);
}

function renderJournal() {
  const notes = A.d.journal;
  setText($('#jrnMeta'), A.d.store ? `${notes.length} entries` : 'archive unavailable');
  syncNodeSelect($('#jrnNode'), 'Fleet');
  keyed($('#journal'), notes, x => x.id,
    () => html('<li><time></time><span class="chip"></span><span class="tx"></span><button class="a-x" type="button" aria-label="Delete note">✕</button></li>'),
    (li, x) => {
      setText(li.querySelector('time'), fmt.when(x.t));
      const c = li.querySelector('.chip'); setText(c, x.node ? (nodeById(x.node)?.name || x.node) : 'FLEET');
      setText(li.querySelector('.tx'), x.text);
      li.title = `logged from ${x.who || '?'}`;
      li.querySelector('.a-x').onclick = () => confirmBox('DELETE NOTE', `Delete “${esc(x.text)}”?`, 'DELETE', async () => {
        try { await postJSON('api/journal/delete', { id: x.id }); poll(); } catch (e) { toast(e.message, 'crit'); }
      });
    });
}

/* ───────── actions ───────── */
$('#updList').addEventListener('click', e => {
  const b = e.target.closest('[data-act=update]'); if (!b) return;
  const [node, id] = b.closest('.a-tr').dataset.key.split('/');
  const u = allUpdates().find(x => x.node === node && x.id === id); if (!u) return;
  confirmBox(`UPDATE // ${u.name}`, `Pull <code>${esc(u.image)}</code> and recreate <b>${esc(u.name)}</b> on ${esc(u.nodeName)}?<br><br>It restarts with exactly the same settings (env, volumes, ports, networks). If anything fails, the old container is put back.`, 'UPDATE', async () => {
    const r = await act(node, 'update', { id }); if (r) toast(`Updating ${u.name}… this can take a minute`);
  }, false);
});
$('#updAll').addEventListener('click', () => {
  const list = allUpdates().filter(u => (u.state === 'update' || u.state === 'pulled') && can(nodeById(u.node)) && !u.self);
  confirmBox('UPDATE ALL', `Update ${list.length} containers?<br><br>${list.map(u => `<b>${esc(u.name)}</b> <span class="dim">on ${esc(u.nodeName)}</span>`).join('<br>')}`, `UPDATE ${list.length}`, async () => {
    for (const u of list) await act(u.node, 'update', { id: u.id });
    toast(`Started ${list.length} updates`);
  }, false);
});
$('#updView').addEventListener('click', e => {
  const b = e.target.closest('button[data-v]'); if (!b) return;
  A.view = b.dataset.v; store.set('arsenal.view', A.view);
  $('#updView').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); render();
});
$('#updView').querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.v === A.view));
$('#checkAll').addEventListener('click', async () => {
  const nodes = A.d?.nodes.filter(n => n.online) || [];
  await Promise.all(nodes.map(n => act(n.id, 'check')));
  toast(`Checking ${nodes.length} machine${nodes.length === 1 ? '' : 's'} against their registries…`);
});
$('#machines').addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const n = nodeById(b.closest('.a-m').dataset.key); if (!n) return;
  if (b.dataset.act === 'check') { act(n.id, 'check').then(r => r && toast(`Checking ${n.name}…`)); return; }
  const df = n.arsenal?.df;
  const size = { images: df?.images.dangling_size, 'images-all': df?.images.unused_size, containers: df?.containers.stopped_size, build: df?.build.reclaim };
  const body = html(`<div class="a-prunes">${PRUNE.map(([k, l, d]) => `<button type="button" data-p="${k}"><b>${l}</b><span>${fmt.bytes(size[k] || 0)}</span><small>${d}</small></button>`).join('')}
    <p class="dim">Volumes are never pruned from here.</p></div>`);
  body.addEventListener('click', async ev => {
    const p = ev.target.closest('[data-p]')?.dataset.p; if (!p) return;
    modal.close();
    const r = await act(n.id, 'prune', { arg: p }); if (r) toast(`Cleaning up ${n.name}…`);
  });
  modal.open(`CLEAN UP // ${n.name}`, body, 'sm');
});
$('#mtDur').addEventListener('click', e => {
  const b = e.target.closest('[data-m]'); if (!b) return;
  A.minutes = Number(b.dataset.m); $('#mtDur').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
});
$('#mtForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    const w = await postJSON('api/maint', { node: f.node.value || null, minutes: A.minutes, reason: f.reason.value });
    f.reason.value = ''; toast(`🔧 Maintenance on ${w.scope} until ${fmt.hm(w.until)}. Push alerts muted.`, 'ok'); poll();
  } catch (err) { toast(err.message, 'crit'); }
});
$('#jrnForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements;
  try { await postJSON('api/journal', { text: f.text.value, node: f.node.value || null }); f.text.value = ''; toast('Logged ✎', 'ok'); poll(); }
  catch (err) { toast(err.message, 'crit'); }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') modal.close();
  if (e.target.closest('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey || !$('#modal').hidden) return;
  if (e.key === 't') cycleTheme(1); else if (e.key === 'T') cycleTheme(-1);
  else if (e.key === 'j') { e.preventDefault(); $('#jrnForm input').focus(); }
});

poll();
