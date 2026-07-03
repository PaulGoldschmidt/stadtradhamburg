"use strict";

// Forecast page: pick a horizon, see the predicted stockout risk map at that
// future timepoint. Depends on common.js.

let map, markerLayer, selected = null;
let byId = {};  // station_id → forecast row for the active horizon (panel + risk list)

const localTime = (iso) =>
  iso ? new Date(iso).toLocaleTimeString("en-GB", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit" }) : "?";

async function boot() {
  map = newMap("map");
  markerLayer = L.layerGroup().addTo(map);
  setFreshness($("#freshness"));

  const meta = await api("/api/forecast/horizons");
  if (!meta.available || !meta.horizons.length) {
    $(".panel").style.display = "none";
    $("#risk-list").closest(".card-lg").style.display = "none";
    $("#unavailable").style.display = "";
    return;
  }
  const box = $("#horizons");
  box.innerHTML = meta.horizons
    .map((h) => `<button class="pill" data-h="${h}">+${h} min</button>`).join("");
  box.querySelectorAll(".pill").forEach((b) => (b.onclick = () => select(+b.dataset.h)));
  select(meta.horizons.includes(60) ? 60 : meta.horizons[0]);
}

async function select(h) {
  selected = h;
  $("#horizons").querySelectorAll(".pill")
    .forEach((b) => b.classList.toggle("active", +b.dataset.h === h));

  const fc = await api("/api/forecast?horizon=" + h);
  byId = {};
  fc.stations.forEach((s) => (byId[s.station_id] = s));
  $("#target-label").textContent = `predicted state at ${localTime(fc.t_target)} (+${h} min)`;
  $("#risk-label").textContent = `at +${h} min`;

  markerLayer.clearLayers();
  for (const s of fc.stations) {
    if (s.lat == null) continue;
    riskMarker(s.lat, s.lon, s.p_empty,
      `${s.name} — ${Math.round(s.p_empty * 100)}% empty`,
      () => openStation(s.station_id)).addTo(markerLayer);
  }

  $("#risk-list").innerHTML = fc.stations.slice(0, 12).map((s) => `
    <div class="risk-row" data-id="${s.station_id}">
      <span class="risk-badge" style="background:${riskColor(s.p_empty)}">${Math.round(s.p_empty * 100)}%</span>
      <span class="risk-name">${s.name}</span>
      <span class="risk-sub">${fmt(s.bikes_now)} now → ${Number(s.pred_bikes).toFixed(1)} pred</span>
    </div>`).join("");
  $("#risk-list").querySelectorAll(".risk-row").forEach((r) => (r.onclick = () => {
    const s = byId[r.dataset.id];
    map.setView([s.lat, s.lon], 15);
    openStation(r.dataset.id);
  }));
}

function openStation(id) {
  const s = byId[id];
  if (!s) return;
  const p = s.p_empty;
  $("#station-body").innerHTML = `
    <h3>${s.name}</h3>
    <span class="risk" style="background:${riskColor(p)}">${Math.round(p * 100)}% chance empty</span>
    <div class="stat-row">
      <div><div class="k">Bikes now</div><div class="v">${fmt(s.bikes_now)}</div></div>
      <div><div class="k">Predicted (+${selected} min)</div><div class="v">${Number(s.pred_bikes).toFixed(1)}</div></div>
    </div>
    <div class="cap">capacity ≈ ${fmt(s.capacity)} · target ${localTime(s.t_target)}</div>`;
  $("#station").classList.remove("hidden");
}
$("#station-close").onclick = () => $("#station").classList.add("hidden");

document.addEventListener("DOMContentLoaded", boot);
