"use strict";

// Shared helpers used by both the live (app.js) and forecast (forecast.js) pages.

const $ = (sel, root = document) => root.querySelector(sel);
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}

// green → yellow → red over [0,1] (0 = good/plenty, 1 = empty/high-risk)
function riskColor(r) {
  if (r == null) return "#9aa4b2";
  const c0 = [46, 204, 113], c1 = [241, 196, 15], c2 = [231, 76, 60];
  const [a, b, t] = r < 0.5 ? [c0, c1, r / 0.5] : [c1, c2, (r - 0.5) / 0.5];
  const m = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${m[0]},${m[1]},${m[2]})`;
}

function newMap(id) {
  const m = L.map(id, { scrollWheelZoom: false }).setView([53.55, 10.0], 12);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap © CARTO", maxZoom: 19,
  }).addTo(m);
  return m;
}

// A station marker whose colour + size encode a 0..1 risk value.
function riskMarker(lat, lon, risk, tooltip, onClick) {
  const marker = L.circleMarker([lat, lon], {
    radius: 4 + 8 * (risk == null ? 0.2 : risk),
    color: null, weight: 0, fillColor: riskColor(risk), fillOpacity: 0.85,
  });
  if (tooltip) marker.bindTooltip(tooltip, { direction: "top" });
  if (onClick) marker.on("click", onClick);
  return marker;
}

// Weekday × hour trip heatmap (live dashboard + station pages). `hm` is the
// API shape {weekdays, hours, grid}.
function renderHeatmap(sel, hm) {
  const max = Math.max(1, ...hm.grid.flat());
  let html = `<div class="lbl"></div>` +
    hm.hours.map((h) => `<div class="hr">${h % 3 === 0 ? h : ""}</div>`).join("");
  hm.weekdays.forEach((wd, i) => {
    html += `<div class="lbl">${wd}</div>`;
    html += hm.grid[i].map((v) => {
      const t = v / max;
      const bg = `rgb(${Math.round(238 - 208 * t)},${Math.round(242 - 178 * t)},${Math.round(247 - 72 * t)})`;
      return `<div class="cell" title="${wd} · ${v} trips" style="background:${bg}"></div>`;
    }).join("");
  });
  $(sel).innerHTML = html;
}

// Default Chart.js options for the full-width history charts.
function baseOpts() {
  return {
    responsive: true, maintainAspectRatio: true, aspectRatio: 1.9, animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { ticks: { maxTicksLimit: 8, font: { size: 10 } } }, y: { beginAtZero: true } },
  };
}

async function setFreshness(el) {
  const h = await api("/api/health");
  const last = new Date(h.last_observation);
  el.textContent =
    `Data through ${last.toLocaleString("en-GB", { timeZone: "Europe/Berlin" })} · ${fmt(h.stations)} stations`
    + (h.has_forecast ? " · forecast live" : "");
  return h;
}
