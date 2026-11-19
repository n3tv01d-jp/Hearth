/* HEARTH canvas line chart — glow lines, gradient fill, null gaps, hover tooltip, theme-aware colours. */
'use strict';

/* Palette resolved from CSS variables so charts follow the active theme. */
const PAL = {};
function refreshPalette() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ['a1', 'a2', 'a3', 'green', 'amber', 'red']) PAL[k] = cs.getPropertyValue('--' + k).trim() || '#3ef3ff';
  PAL.dim = cs.getPropertyValue('--dim').trim();
  PAL.grid = `rgba(${cs.getPropertyValue('--a1-rgb').trim()},0.08)`;
}
refreshPalette();

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
const escT = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const markColor = l => PAL[l === 'crit' ? 'red' : l === 'warn' ? 'amber' : l === 'note' ? 'a3' : 'green'];
function hexA(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map(c => c + c).join('') : h, 16);
  return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
}

class Chart {
  /**
   * opts: len (fixed slot count, data right-aligned), max, min, autoMin, labels, fmt (y formatter),
   *       xlabel(i, n) -> string for tooltip/axis, xaxis (draw 3 x labels), empty (placeholder text),
   *       markers: [{ i, lvl, items: [event] }] event ticks, markerFmt(event) -> tooltip text
   * series: [{ name, data, color: 'a1'|'#hex', fill, width, fmt, dash }]
   */
  constructor(canvas, opts = {}) {
    this.c = canvas; this.ctx = canvas.getContext('2d'); this.o = opts; this.series = []; this.hover = null;
    this.tip = document.createElement('div'); this.tip.className = 'tip'; this.tip.hidden = true;
    canvas.parentElement.append(this.tip);
    canvas.addEventListener('pointermove', e => this.onMove(e));
    canvas.addEventListener('pointerleave', () => { this.hover = null; this.tip.hidden = true; this.draw(); });
    new ResizeObserver(() => this.resize()).observe(canvas);
    Chart.all.push(this);
    this.resize();
  }
  resize() {
    const r = this.c.getBoundingClientRect(), d = devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    this.c.width = Math.max(1, Math.round(r.width * d)); this.c.height = Math.max(1, Math.round(r.height * d));
    this.ctx.setTransform(d, 0, 0, d, 0, 0);
    this.draw();
  }
  set(series, opts) { this.series = series; if (opts) Object.assign(this.o, opts); this.draw(); if (this.hover != null) this.showTip(); }
  slots() { return this.o.len || Math.max(2, ...this.series.map(s => s.data.length)); }
  geom() {
    const top = this.o.labels ? 14 : 4, bot = this.h - (this.o.xaxis ? 16 : 2);
    const n = this.slots();
    return { top, bot, span: bot - top, n, step: this.w / (n - 1) };
  }
  onMove(e) {
    const r = this.c.getBoundingClientRect(), { n, step } = this.geom();
    const slot = Math.round((e.clientX - r.left) / step);
    this.hover = Math.max(0, Math.min(n - 1, slot));
    this.draw(); this.showTip();
  }
  valueAt(s, slot) { const off = this.slots() - s.data.length; const i = slot - off; return i >= 0 ? s.data[i] : undefined; }
  /* event markers within a few pixels of the hovered slot: dense ranges (30d = 360 slots) are hard to hit exactly */
  markersNear(slot, step) { return (this.o.markers || []).filter(m => Math.abs(m.i - slot) * step <= 6); }
  showTip() {
    const { n, step } = this.geom(), slot = this.hover;
    const rows = this.series.map(s => {
      const v = this.valueAt(s, slot), f = s.fmt || this.o.fmt || (x => x.toFixed(1));
      return `<div><i style="background:${PAL[s.color] || s.color}"></i>${escT(s.name || '')} <b>${v == null ? '—' : f(v)}</b></div>`;
    }).join('');
    const xl = this.o.xlabel ? `<div class="tt">${this.o.xlabel(slot, n)}</div>` : '';
    const evs = this.markersNear(slot, step).flatMap(m => m.items), mf = this.o.markerFmt || (e => e.msg);
    const ev = evs.slice(0, 4).map(e => `<div class="ev ${e.lvl}">◆ ${escT(mf(e))}</div>`).join('')
      + (evs.length > 4 ? `<div class="ev">+${evs.length - 4} more</div>` : '');
    this.tip.innerHTML = xl + rows + ev;
    this.tip.hidden = false;
    const x = slot * step, tw = this.tip.offsetWidth;
    this.tip.style.left = (x + 12 + tw > this.w ? x - tw - 12 : x + 12) + 'px';
    this.tip.style.top = '6px';
  }
  draw() {
    const { ctx, w, h, o } = this;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const { top, bot, span, n, step } = this.geom();
    let min = o.min ?? 0, max = o.max;
    let lo = Infinity, hi = -Infinity, any = false;
    for (const s of this.series) for (const v of s.data) if (v != null) { any = true; if (v < lo) lo = v; if (v > hi) hi = v; }
    if (o.autoMin && any) { min = Math.max(0, Math.floor((lo - 5) / 5) * 5); max = Math.max(min + 10, Math.ceil((hi + 3) / 5) * 5); }
    else if (max == null) max = niceMax((any ? hi : 0) * 1.1);
    // grid
    ctx.lineWidth = 1; ctx.strokeStyle = PAL.grid; ctx.beginPath();
    for (let i = 0; i <= 4; i++) { const y = Math.round(top + span * i / 4) + .5; ctx.moveTo(0, y); ctx.lineTo(w, y); }
    const vstep = Math.max(1, Math.round(n / 8));
    for (let i = n - 1; i >= 0; i -= vstep) { const x = Math.round(i * step) + .5; ctx.moveTo(x, top); ctx.lineTo(x, bot); }
    ctx.stroke();
    if (!any && o.empty) {
      ctx.font = '12px "Share Tech Mono", monospace'; ctx.fillStyle = PAL.dim; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(o.empty, w / 2, top + span / 2); ctx.textAlign = 'start';
    }
    const y = v => bot - Math.min(1, Math.max(0, (v - min) / (max - min))) * span;
    const marks = o.markers || [];
    if (marks.length) { // faint guide line under the data for every event bucket
      ctx.save(); ctx.setLineDash([2, 3]);
      for (const m of marks) {
        const x = Math.round(m.i * step) + .5;
        ctx.strokeStyle = hexA(markColor(m.lvl), .4); ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bot); ctx.stroke();
      }
      ctx.restore();
    }
    for (const s of this.series) {
      const col = PAL[s.color] || s.color, off = n - s.data.length;
      // one stroke path + one fill path; each run of non-null values is closed down to the baseline
      const path = new Path2D(), fill = new Path2D();
      let inRun = false, lastX = 0, drawn = false;
      s.data.forEach((v, i) => {
        const px = (i + off) * step;
        if (v == null) {
          if (inRun) { fill.lineTo(lastX, bot); fill.closePath(); inRun = false; }
          return;
        }
        const py = y(v);
        if (!inRun) { path.moveTo(px, py); fill.moveTo(px, bot); inRun = true; } else path.lineTo(px, py);
        fill.lineTo(px, py); lastX = px; drawn = true;
      });
      if (inRun) { fill.lineTo(lastX, bot); fill.closePath(); }
      if (!drawn) continue;
      if (s.fill !== false) {
        const g = ctx.createLinearGradient(0, top, 0, bot);
        g.addColorStop(0, hexA(col, .3)); g.addColorStop(1, hexA(col, 0));
        ctx.fillStyle = g; ctx.fill(fill);
      }
      ctx.save();
      ctx.strokeStyle = col; ctx.lineWidth = s.width || 1.6; ctx.lineJoin = 'round';
      if (s.dash) ctx.setLineDash(s.dash === true ? [5, 4] : s.dash);
      ctx.shadowColor = col; ctx.shadowBlur = 8; ctx.stroke(path); ctx.restore();
      const lv = s.data[s.data.length - 1];
      if (lv != null && !o.xaxis) { ctx.fillStyle = col; ctx.beginPath(); ctx.arc(w - 2, y(lv), 2.5, 0, 7); ctx.fill(); }
    }
    for (const m of marks) { // event ticks along the top edge
      const x = m.i * step;
      ctx.fillStyle = markColor(m.lvl); ctx.beginPath(); ctx.moveTo(x - 4, top - 2); ctx.lineTo(x + 4, top - 2); ctx.lineTo(x, top + 5); ctx.closePath(); ctx.fill();
    }
    // hover crosshair
    if (this.hover != null) {
      const x = Math.round(this.hover * step) + .5;
      ctx.strokeStyle = hexA(PAL.a1, .5); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bot); ctx.stroke(); ctx.setLineDash([]);
      for (const s of this.series) {
        const v = this.valueAt(s, this.hover); if (v == null) continue;
        ctx.fillStyle = PAL[s.color] || s.color; ctx.beginPath(); ctx.arc(x, y(v), 3.5, 0, 7); ctx.fill();
      }
    }
    ctx.font = '10px "Share Tech Mono", monospace'; ctx.fillStyle = PAL.dim;
    if (o.labels) {
      const f = o.fmt || (v => v.toFixed(0));
      ctx.textBaseline = 'top';
      ctx.fillText(f(max), 6, 2); ctx.fillText(f(min + (max - min) / 2), 6, top + span / 2 - 5);
    }
    if (o.xaxis && o.xlabel) {
      ctx.textBaseline = 'bottom';
      const pts = [[0, 'left'], [Math.floor((n - 1) / 2), 'center'], [n - 1, 'right']];
      for (const [i, al] of pts) { ctx.textAlign = al; ctx.fillText(o.xlabel(i, n, true), al === 'left' ? 2 : al === 'right' ? w - 2 : i * step, h); }
      ctx.textAlign = 'start';
    }
  }
}
Chart.all = [];
