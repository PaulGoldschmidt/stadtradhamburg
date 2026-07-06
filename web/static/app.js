"use strict";

// Live dashboard: today's stats, CURRENT-status map, and history/weather charts.
// Forecast lives on its own page (/forecast). Depends on common.js.

// ---------- live cards ----------
async function loadCards() {
  const s = await api("/api/stats/today");
  // [label, value, hover-detail?] — row 1 = core counts, row 2 = superlatives w/ hover.
  const cards = [
    ["Trips today", fmt(s.trips)],
    ["Distance ridden", `${fmt(s.km)}<small> km</small>`],
    ["Bikes in use", fmt(s.bikes_in_use)],
    ["Bikes moved", fmt(s.bikes_moved)],
    ["Empty now", fmt(s.stations_empty_now), `${fmt(s.stations_empty_now)} of ${fmt(s.stations_online)} stations have no bikes`],
    ["Avg trip", `${fmt(s.avg_min)}<small> min</small>`],
    ["Longest ride", `${fmt(s.longest_km)}<small> km</small>`,
      s.longest_from ? `${s.longest_from} → ${s.longest_to} (${fmt(s.longest_min)} min)` : null],
    ["Most bikes", fmt(s.most_bikes),
      s.most_bikes_station ? `${s.most_bikes_station} — ${fmt(s.most_bikes)} bikes right now` : null],
    ["Busiest station", fmt(s.busiest_moves),
      s.busiest_station ? `${s.busiest_station} — ${fmt(s.busiest_moves)} arrivals + departures today` : null],
    ["Top bike", fmt(s.top_bike_trips),
      s.top_bike_number ? `Bike ${s.top_bike_number} — ${fmt(s.top_bike_trips)} trips today` : null],
    ["Peak hour", s.peak_hour != null ? `${hh(s.peak_hour)}<small>:00</small>` : "–",
      s.peak_hour != null ? `${fmt(s.peak_hour_trips)} trips between ${hh(s.peak_hour)}:00 and ${hh((s.peak_hour + 1) % 24)}:00` : null],
    ["Available now", fmt(s.available_now),
      `${fmt(s.available_now)} bikes free · ${fmt(s.docks_free_now)} open docks · ${fmt(s.stations_online)} stations`],
  ];
  renderCards("#cards", cards);
}

// ---------- map (current status) ----------
let map, markerLayer;
async function loadStations() {
  const stations = await api("/api/stations");
  markerLayer.clearLayers();
  const LOW = 6;  // bikes at/above this = well stocked (green); 0 = empty (red)
  for (const s of stations) {
    // Colour by bike AVAILABILITY, not occupancy ratio: the reported capacity is
    // a flat placeholder, so bikes/capacity is meaningless. Users care about the
    // count they can actually grab, which also matches the forecast's "empty = 0".
    const emptiness = s.bikes == null ? null : Math.max(0, Math.min(1, (LOW - s.bikes) / LOW));
    riskMarker(s.lat, s.lon, emptiness,
      `${esc(s.name)} — ${s.bikes} bikes / ${s.docks} docks`,
      () => openStation(s.station_id)).addTo(markerLayer);
  }
}

// ---------- station panel (current status) ----------
let typicalChart, timelineChart;
async function openStation(id) {
  const d = await api("/api/station/" + id);
  const occ = d.capacity_now ? d.bikes / d.capacity_now : null;
  $("#station-body").innerHTML = `
    <h3>${esc(d.name)}</h3>
    <div class="stat-row">
      <div><div class="k">Bikes now</div><div class="v">${fmt(d.bikes)}</div></div>
      <div><div class="k">Docks free</div><div class="v">${fmt(d.docks)}</div></div>
      <div><div class="k">Full</div><div class="v">${occ == null ? "–" : Math.round(occ * 100) + "%"}</div></div>
    </div>
    <div class="cap">capacity ≈ ${fmt(d.capacity_now)} · as of ${d.as_of ? new Date(d.as_of).toLocaleTimeString("en-GB", { timeZone: "Europe/Berlin" }) : "?"}</div>
    <p class="cap"><a href="/station/${d.station_id}">Full station stats →</a> · <a href="/forecast">Forecast →</a></p>
    <p class="cap" style="margin-top:8px">Typical bikes available by hour</p>
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
    options: miniOpts(true),
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
function miniOpts(showX) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false } },
    scales: { x: { ticks: { maxTicksLimit: 8, font: { size: 9 } }, display: !!showX }, y: { beginAtZero: true, ticks: { maxTicksLimit: 4 } } },
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
      datasets: [{ data: daily.map((d) => d.trips), borderColor: "#e2001a", backgroundColor: "rgba(226,0,26,.10)", fill: true, tension: 0.3, pointRadius: 0 }],
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
  renderOD("#od-most", top.od_most);
  renderNever("#od-never", top.od_never);
  if (top.od_never_count != null) {
    $("#od-never-note").textContent = `${fmt(top.od_never_count)} station pairs never ridden`;
  }

  const bikes = await api("/api/history/bikes");
  renderBikes("#bikes-most", bikes.most);
  renderForgotten("#bikes-forgotten", bikes.forgotten);
  if (bikes.active_count) {
    $("#bikes-forgotten-note").textContent =
      `${fmt(bikes.idle_count)} of ${fmt(bikes.active_count)} in-fleet bikes idle 7+ days`;
  }

  renderHeatmap("#heatmap", await api("/api/history/heatmap"));
}

function renderOD(sel, rows) {
  $(sel).innerHTML = (rows || []).map((r) => `
    <div class="od-row">
      <span class="od-route">${esc(r.origin)} <span class="od-arrow">→</span> ${esc(r.destination)}</span>
      <span class="od-trips">${fmt(r.trips)}<small> ${r.trips === 1 ? "trip" : "trips"}</small></span>
    </div>`).join("");
}

function renderNever(sel, rows) {
  $(sel).innerHTML = (rows || []).map((r) => `
    <div class="od-row">
      <span class="od-route">${esc(r.origin)} <span class="od-arrow">→</span> ${esc(r.destination)}</span>
      <span class="od-trips"><small>never ridden</small></span>
    </div>`).join("");
}

function renderBikes(sel, rows) {
  $(sel).innerHTML = (rows || []).map((r) => {
    const tip = r.last_station ? ` data-tip="${esc(`Last seen at ${r.last_station}`)}"` : "";
    return `
    <div class="od-row"${tip}>
      <span class="od-route">Bike <b>${r.bike_number}</b></span>
      <span class="od-trips">${fmt(r.trips)}<small> ${r.trips === 1 ? "trip" : "trips"} · ${fmt(r.km)} km</small></span>
    </div>`;
  }).join("");
}

function renderForgotten(sel, rows) {
  $(sel).innerHTML = (rows || []).map((r) => `
    <div class="od-row">
      <span class="od-route">Bike <b>${r.bike_number}</b><small> @ ${esc(r.station)}</small></span>
      <span class="od-trips">${fmt(r.days_idle)}<small> days idle</small></span>
    </div>`).join("");
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

// ---------- boot ----------
async function boot() {
  map = newMap("map");
  markerLayer = L.layerGroup().addTo(map);
  await Promise.allSettled([
    setFreshness($("#freshness")).then((h) => { $("#foot-fresh").textContent = "Updated " + new Date().toLocaleTimeString("en-GB"); }),
    loadCards(), loadStations(), loadHistory(), loadWeather(),
  ]);
  setInterval(() => { setFreshness($("#freshness")); loadCards(); loadStations(); }, 60000);
}
document.addEventListener("DOMContentLoaded", boot);
