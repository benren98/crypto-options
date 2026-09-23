// Helpers communs des dashboards (DOM sûr, formats fr-FR, plugins et fabriques Chart.js).
// Injecté DANS l'IIFE de chaque page par les générateurs (scope partagé avec le code de page).
// ── Helpers DOM (texte toujours via textContent / text nodes) ──────────────
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.setAttribute("style", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
function mount(id, ...kids) { const el = document.getElementById(id); el.replaceChildren(...kids.flat(Infinity).filter(Boolean)); return el; }
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// ── Formats ────────────────────────────────────────────────────────────────
const MINUS = "−";
// Espace insécable normale plutôt que l'espace fine fr-FR (U+202F), illisible en petite taille
function num(v, d = 0) { return Number(v).toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d }).replace(/ /g, " "); }
function usd(v, o = {}) {
  if (v == null || !isFinite(v)) return "—";
  const d = o.d ?? 0, a = Math.abs(v);
  const sign = v < 0 ? MINUS : (o.sign && v > 0 ? "+" : "");
  if (o.compact && a >= 10000) return sign + num(a / 1000, 1) + " k$";
  return sign + num(a, d) + " $";
}
function pct(v, d = 1, sign = false) {
  if (v == null || !isFinite(v)) return "—";
  const s = v < 0 ? MINUS : (sign && v > 0 ? "+" : "");
  return s + num(Math.abs(v), d) + " %";
}
function sgn(v, d = 3, unit = "") {
  if (v == null || !isFinite(v)) return "—";
  const s = v < 0 ? MINUS : (v > 0 ? "+" : "");
  return s + num(Math.abs(v), d) + unit;
}
function tone(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
function money(v, o = {}) { return h("span", { class: "num " + (o.plain ? "" : tone(v)) }, usd(v, { sign: true, ...o })); }
const dFmt  = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short" });
const dtFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const mFmt  = new Intl.DateTimeFormat("fr-FR", { month: "short", year: "2-digit" });
function fdate(iso) { return iso ? dFmt.format(new Date(iso)) : "—"; }
function fdt(iso) { return iso ? dtFmt.format(new Date(iso)) : "—"; }
function fmonth(m) { return mFmt.format(new Date(m + "-01T00:00:00Z")); }
function ago(hours) {
  if (hours == null) return "jamais";
  if (hours < 1) return "il y a " + Math.max(1, Math.round(hours * 60)) + " min";
  if (hours < 48) return "il y a " + num(hours, hours < 10 ? 1 : 0) + " h";
  return "il y a " + num(hours / 24, 0) + " j";
}
const tzName = (() => { try { return new Intl.DateTimeFormat("fr-FR", { timeZoneName: "short" }).formatToParts(new Date()).find(p => p.type === "timeZoneName").value; } catch (e) { return ""; } })();

const LVL = {
  good: { icon: "✓", label: "OK" }, warning: { icon: "!", label: "À surveiller" },
  critical: { icon: "✕", label: "Action" }, idle: { icon: "–", label: "Au repos" },
  info: { icon: "i", label: "Info" }, serious: { icon: "!", label: "Sérieux" }, na: { icon: "?", label: "n/a" },
};
function badge(level, text) { return h("span", { class: "badge lvl-" + level }, h("i", { "aria-hidden": "true" }, LVL[level]?.icon || "•"), text); }

// ── Plugins Chart.js (crosshair, étiquettes de fin, lignes de référence) ───
const EXTRA = new WeakMap();   // options JS hors du résolveur d'options Chart.js
const crosshair = {
  id: "vrpCrosshair",
  afterDraw(chart) {
    if (chart.config.type !== "line") return;
    const act = chart.tooltip && chart.tooltip.getActiveElements();
    if (!act || !act.length) return;
    const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};
const refLines = {
  id: "vrpRefLines",
  afterDatasetsDraw(chart) {
    const ex = EXTRA.get(chart.canvas); if (!ex || !ex.refLines) return;
    const ctx = chart.ctx, a = chart.chartArea, y = chart.scales.y;
    for (const l of ex.refLines) {
      const py = y.getPixelForValue(l.value);
      if (py < a.top - 1 || py > a.bottom + 1) continue;
      ctx.save(); ctx.strokeStyle = l.color || css("--ink-2"); ctx.lineWidth = 1; ctx.setLineDash(l.dash || []);
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke();
      if (l.label) {
        ctx.setLineDash([]); ctx.fillStyle = css("--muted"); ctx.font = "11px system-ui,sans-serif";
        ctx.textBaseline = "bottom"; ctx.fillText(l.label, a.left + 4, py - 3);
      }
      ctx.restore();
    }
  },
};
const endLabels = {
  id: "vrpEndLabels",
  afterDatasetsDraw(chart) {
    const ex = EXTRA.get(chart.canvas); if (!ex || !ex.endFmt) return;
    const pts = [];
    chart.data.datasets.forEach((ds, i) => {
      if (!ds.vrpEnd) return;
      const meta = chart.getDatasetMeta(i); if (meta.hidden) return;
      const val = (p) => (p != null && typeof p === "object") ? p.y : p;
      let j = ds.data.length - 1; while (j >= 0 && val(ds.data[j]) == null) j--;
      if (j < 0 || !meta.data[j]) return;
      pts.push({ x: meta.data[j].x, y: meta.data[j].y, v: val(ds.data[j]) });
    });
    pts.sort((a, b) => a.y - b.y);
    const ctx = chart.ctx;
    pts.forEach((p, k) => {
      ctx.save(); ctx.fillStyle = css("--ink-2"); ctx.font = "600 11px system-ui,sans-serif"; ctx.textAlign = "right";
      const above = k === 0;
      ctx.textBaseline = above ? "bottom" : "top";
      ctx.fillText(ex.endFmt(p.v), p.x - 2, above ? p.y - 6 : p.y + 6);
      ctx.restore();
    });
  },
};
if (window.Chart) Chart.register(crosshair, refLines, endLabels);

const CHARTS = [];
function tooltipOpts(fmt, titleFmt) {
  return {
    backgroundColor: css("--surface"), borderColor: css("--border"), borderWidth: 1,
    titleColor: css("--ink-2"), bodyColor: css("--ink"), padding: 10, cornerRadius: 8,
    titleFont: { size: 11, weight: "500" }, bodyFont: { size: 12, weight: "600" },
    boxWidth: 12, boxHeight: 2, boxPadding: 6, displayColors: true,
    callbacks: {
      title: (items) => items.length ? titleFmt(items[0].chart.scales.x.type === "linear" ? items[0].parsed.x : items[0].label) : "",
      label: (c) => c.parsed.y == null ? null : "  " + fmt(c.parsed.y) + "   " + c.dataset.label,
      labelColor: (c) => ({ borderColor: c.dataset.borderColor || c.dataset.backgroundColor, backgroundColor: c.dataset.vrpKey || c.dataset.borderColor || c.dataset.backgroundColor, borderWidth: 0 }),
    },
  };
}
function axes(yFmt, xFmt, o = {}) {
  // o.time : axe X linéaire en millisecondes (vraie échelle de temps, les trous restent des trous)
  const x = o.time
    ? { type: "linear", grid: { display: false }, border: { color: css("--axis") }, min: o.xmin, max: o.xmax,
        ticks: { color: css("--muted"), maxRotation: 0, autoSkip: true, maxTicksLimit: o.maxX || 6, font: { size: 11 },
                 callback: (v) => xFmt(v) } }
    : { stacked: !!o.stacked, grid: { display: false }, border: { color: css("--axis") },
        ticks: { color: css("--muted"), maxRotation: 0, autoSkip: true, maxTicksLimit: o.maxX || 6, font: { size: 11 },
                 callback(v) { return xFmt(this.getLabelForValue(v)); } } };
  return {
    x,
    y: { stacked: !!o.stacked, grid: { color: css("--grid"), drawTicks: false }, border: { display: false },
         suggestedMin: o.min, suggestedMax: o.max,
         ticks: { color: css("--muted"), padding: 8, font: { size: 11 }, maxTicksLimit: 6, callback: (v) => yFmt(v) } },
  };
}
const GAP_MS = 6 * 3600 * 1000;   // au-delà : la ligne est coupée (pas d'interpolation d'un trou)
function lineDs(label, data, color, endLabel, gap) {
  return { label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
           pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface"),
           pointHoverBackgroundColor: color, tension: 0, spanGaps: gap ?? GAP_MS, borderJoinStyle: "round",
           borderCapStyle: "round", vrpEnd: !!endLabel };
}
const xy = (arr, tk, vk) => arr.map(p => ({ x: Date.parse(p[tk]), y: p[vk] == null ? null : p[vk] }));
function chartCard(id, title, sub, legendItems, build, table) {
  const box = h("div", { class: "chartbox" });
  const nodes = [h("div", { class: "cardhead" }, h("div", {}, h("h3", {}, title), sub ? h("div", { class: "sub" }, sub) : null))];
  if (legendItems && legendItems.length > 1) nodes.push(legend(legendItems));
  nodes.push(box);
  if (table) nodes.push(dataTable(table.headers, table.rows));
  mount(id, nodes);
  if (!window.Chart) { box.append(h("div", { class: "empty" }, "Graphique indisponible (Chart.js non chargé)")); return; }
  const r = build(box);
  if (r === null) box.append(h("div", { class: "empty" }, "Pas encore de données sur cette période"));
}
function canvasIn(box, label, extra) {
  const c = h("canvas", { role: "img", "aria-label": label });
  box.append(c);
  EXTRA.set(c, extra || {});
  return c;
}
function legend(items) {
  return h("div", { class: "legend" }, items.map(it =>
    h("span", { class: "key" }, h("span", { class: "sw " + (it.kind || "line"), style: "background:" + it.color }), it.name)));
}
function dataTable(headers, rows) {
  return h("details", { class: "dt" }, h("summary", {}, "Voir les données"),
    h("div", { class: "tscroll" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, headers.map((x, i) => h("th", { class: i ? "r" : "" }, x)))),
      h("tbody", {}, rows.map(r => h("tr", {}, r.map((x, i) => h("td", { class: i ? "r num" : "" }, x))))))));
}
