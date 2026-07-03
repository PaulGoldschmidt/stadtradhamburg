"use strict";

const $ = (sel) => document.querySelector(sel);
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}

// green → yellow → red for stockout risk in [0,1]
function riskColor(r) {
  if (r == null) return "#9aa4b2";
  const c0 = [46, 204, 113], c1 = [241, 196, 15], c2 = [231, 76, 60];
  const [a, b, t] = r < 0.5 ? [c0, c1, r / 0.5] : [c1, c2, (r - 0.5) / 0.5];
  const m = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${m[0]},${m[1]},${m[2]})`;
}
// emptiness risk: forecast if present, else current emptiness (1 - occupancy)
const riskOf = (s) => (s.p_empty != null ? s.p_empty : s.occ != null ? 1 - s.occ : null);

// ---------- live cards ----------
async function loadCards() {
  const s = await api("/api/stats/today");
  const cards = [
    ["Trips today", fmt(s.trips)],
    ["Distance ridden", `${fmt(s.km)}<small> km</small>`],
    ["Bikes in use", fmt(s.bikes_in_use)],
    ["Bikes moved", fmt(s.bikes_moved)],
    ["Empty stations now", fmt(s.stations_empty_now)],
    ["Avg trip", `${fmt(s.avg_min)}<small> min</small>`],
  ];
  $("#cards").innerHTML = cards
    .map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`)
    .join("");
}

async function loadFreshness() {
  const h = await api("/api/health");
  const last = new Date(h.last_observation);
  const txt = `Data through ${last.toLocaleString("en-GB", { timeZone: "Europe/Berlin" })} · ${fmt(h.stations)} stations`
    + (h.has_forecast ? " · forecast live" : "");
  $("#freshness").textContent = txt;
  $("#foot-fresh").textContent = "Last updated " + new Date().toLocaleTimeString("en-GB");
}

// ---------- map ----------
let map, markerLayer;
function initMap() {
  map = L.map("map", { scrollWheelZoom: false }).setView([53.55, 10.0], 12);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap © CARTO", maxZoom: 19,
  }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
}
async function loadStations() {
  const stations = await api("/api/stations");
  markerLayer.clearLayers();
  for (const s of stations) {
    const r = riskOf(s);
    L.circleMarker([s.lat, s.lon], {
      radius: 4 + 8 * (r == null ? 0.2 : r),
      color: null, weight: 0, fillColor: riskColor(r), fillOpacity: 0.85,
    })
      .bindTooltip(`${s.name} — ${s.bikes} bikes`, { direction: "top" })
      .on("click", () => openStation(s.station_id))
      .addTo(markerLayer);
  }
}

// ---------- station panel ----------
let typicalChart, timelineChart;
async function openStation(id) {
  const d = await api("/api/station/" + id);
  const fc = d.forecast;
  const risk = fc && fc.p_empty != null ? fc.p_empty : null;
  const riskBadge = risk == null ? "" :
    `<span class="risk" style="background:${riskColor(risk)}">${Math.round(risk * 100)}% empty in 60 min</span>`;
  $("#station-body").innerHTML = `
    <h3>${d.name}</h3>
    ${riskBadge}
    <div class="stat-row">
      <div><div class="k">Bikes now</div><div class="v">${fmt(d.bikes)}</div></div>
      <div><div class="k">Docks free</div><div class="v">${fmt(d.docks)}</div></div>
      ${fc && fc.pred_bikes != null ? `<div><div class="k">In 60 min</div><div class="v">${Number(fc.pred_bikes).toFixed(1)}</div></div>` : ""}
    </div>
    <div class="cap">capacity ≈ ${fmt(d.capacity_now)} · as of ${d.as_of ? new Date(d.as_of).toLocaleTimeString("en-GB", { timeZone: "Europe/Berlin" }) : "?"}</div>
    <p class="cap" style="margin-top:12px">Typical bikes available by hour</p>
    <canvas id="c-typical" height="120"></canvas>
    <p class="cap">Last 3 days</p>
    <canvas id="c-timeline" height="110"></canvas>`;
  $("#station").classList.remove("hidden");

  typicalChart?.destroy();
  typicalChart = new Chart($("#c-typical"), {
    type: "line",
    data: {
      labels: d.typical_day.map((x) => x.hour),
      datasets: [{ data: d.typical_day.map((x) => x.avg_bikes), borderColor: "#e2001a", backgroundColor: "rgba(226,0,26,.12)", fill: true, tension: 0.35, pointRadius: 0 }],
    },
    options: miniOpts("h"),
  });
  timelineChart?.destroy();
  timelineChart = new Chart($("#c-timeline"), {
    type: "line",
    data: {
      labels: d.timeline.map((x) => x.ts),
      datasets: [{ data: d.timeline.map((x) => x.bikes), borderColor: "#2563eb", pointRadius: 0, borderWidth: 1.5, tension: 0.25 }],
    },
    options: { ...miniOpts(), scales: { x: { display: false }, y: { beginAtZero: true, ticks: { maxTicksLimit: 4 } } } },
  });
}
function miniOpts(xtitle) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { ticks: { maxTicksLimit: 8, font: { size: 9 } }, title: { display: !!xtitle } }, y: { beginAtZero: true, ticks: { maxTicksLimit: 4 } } },
  };
}
$("#station-close").onclick = () => $("#station").classList.add("hidden");

// ---------- history charts ----------
async function loadHistory() {
  const daily = await api("/api/history/trips_daily");
  new Chart($("#chart-daily"), {
    type: "line",
    data: {
      labels: daily.map((d) => d.day),
      datasets: [{ label: "trips", data: daily.map((d) => d.trips), borderColor: "#e2001a", backgroundColor: "rgba(226,0,26,.10)", fill: true, tension: 0.3, pointRadius: 0 }],
    },
    options: baseOpts(),
  });

  const top = await api("/api/history/top_stations");
  new Chart($("#chart-stations"), {
    type: "bar",
    data: {
      labels: top.stations.map((s) => s.name),
      datasets: [{ data: top.stations.map((s) => s.moves), backgroundColor: "#2563eb" }],
    },
    options: { ...baseOpts(), indexAxis: "y", scales: { x: { beginAtZero: true }, y: { ticks: { font: { size: 11 } } } } },
  });

  const hm = await api("/api/history/heatmap");
  renderHeatmap(hm);
}

function renderHeatmap(hm) {
  const max = Math.max(1, ...hm.grid.flat());
  const el = $("#heatmap");
  let html = `<div class="lbl"></div>` +
    hm.hours.map((h) => `<div class="hr">${h % 3 === 0 ? h : ""}</div>`).join("");
  hm.weekdays.forEach((wd, i) => {
    html += `<div class="lbl">${wd}</div>`;
    html += hm.grid[i].map((v) => {
      const t = v / max;
      const bg = `rgb(${Math.round(238 - 208 * t)},${Math.round(242 - 178 * t)},${Math.round(247 - 72 * t)})`;
      return `<div class="cell" title="${wd} ${"–"} ${v} trips" style="background:${bg}"></div>`;
    }).join("");
  });
  el.innerHTML = html;
}

async function loadWeather() {
  const w = await api("/api/weather/ridership");
  if (!w.length) { $("#chart-weather").closest(".card-lg").style.display = "none"; return; }
  new Chart($("#chart-weather"), {
    type: "scatter",
    data: {
      datasets: [{
        data: w.map((d) => ({ x: d.temp_c, y: d.trips, day: d.day, rain: d.precip_mm })),
        pointRadius: (ctx) => 4 + Math.min(8, (ctx.raw.rain || 0) * 1.5),
        backgroundColor: (ctx) => (ctx.raw.rain > 0.5 ? "rgba(37,99,235,.7)" : "rgba(226,0,26,.6)"),
      }],
    },
    options: {
      ...baseOpts(),
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => `${c.raw.day}: ${fmt(c.raw.y)} trips, ${c.raw.x}°C, ${c.raw.rain}mm rain` } },
      },
      scales: { x: { title: { display: true, text: "avg temperature (°C)" } }, y: { title: { display: true, text: "trips" }, beginAtZero: true } },
    },
  });
}

function baseOpts() {
  return {
    responsive: true, maintainAspectRatio: true, aspectRatio: 1.9, animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { ticks: { maxTicksLimit: 8, font: { size: 10 } } }, y: { beginAtZero: true } },
  };
}

// ---------- boot ----------
async function boot() {
  initMap();
  await Promise.allSettled([loadFreshness(), loadCards(), loadStations(), loadHistory(), loadWeather()]);
  // live refresh (cards + map only)
  setInterval(() => { loadFreshness(); loadCards(); loadStations(); }, 60000);
}
document.addEventListener("DOMContentLoaded", boot);
