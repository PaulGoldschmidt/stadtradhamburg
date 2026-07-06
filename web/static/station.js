"use strict";

// Per-station page: live status cards, when-people-ride heatmap, top
// destinations, and availability charts. Depends on common.js; the station id
// comes from the /station/<id> URL path.

const SID = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());

function setCards(d, ins) {
  const cards = [
    ["Bikes now", fmt(d.bikes)],
    ["Docks free", fmt(d.docks)],
    ["Departures", fmt(ins.departures), "Trips that started here"],
    ["Arrivals", fmt(ins.arrivals), "Trips that ended here"],
    ["Avg trip from here", ins.avg_min != null ? `${fmt(ins.avg_min)}<small> min</small>` : "–",
      ins.avg_km != null ? `${fmt(ins.avg_km)} km straight-line on average` : null],
    ["Longest ride from here", ins.longest_km != null ? `${fmt(ins.longest_km)}<small> km</small>` : "–",
      ins.longest_to ? `to ${ins.longest_to} — ${fmt(ins.longest_min)} min, on ${ins.longest_day}` : null],
    ["Top destination", fmt(ins.top_destinations?.[0]?.trips),
      ins.top_destinations?.[0] ? `${ins.top_destinations[0].name} — ${fmt(ins.top_destinations[0].trips)} trips from here` : null],
    ["Peak hour", ins.peak_hour != null ? `${hh(ins.peak_hour)}<small>:00</small>` : "–",
      ins.peak_hour != null ? `${ins.peak_weekday} ${hh(ins.peak_hour)}:00–${hh((ins.peak_hour + 1) % 24)}:00 — ${fmt(ins.peak_moves)} arrivals + departures` : null],
  ];
  renderCards("#cards", cards);
}

function setTopDest(ins) {
  const rows = ins.top_destinations || [];
  $("#top-dest").innerHTML = rows.length
    ? rows.map((r) => `
      <div class="od-row">
        <span class="od-route">${esc(ins.name)} <span class="od-arrow">→</span> ${esc(r.name)}</span>
        <span class="od-trips">${fmt(r.trips)}<small> ${r.trips === 1 ? "trip" : "trips"} · ${fmt(r.avg_km)} km avg</small></span>
      </div>`).join("")
    : `<p style="color:var(--muted);font-size:13px;margin:4px 0">No trips from this station observed yet.</p>`;
}

function setCharts(d) {
  new Chart($("#c-typical"), {
    type: "line",
    data: {
      labels: (d.typical_day || []).map((x) => x.hour),
      datasets: [{ data: (d.typical_day || []).map((x) => x.avg_bikes), borderColor: "#e2001a", backgroundColor: "rgba(226,0,26,.12)", fill: true, tension: 0.35, pointRadius: 0 }],
    },
    options: baseOpts(),
  });
  const tl = d.timeline || [];
  new Chart($("#c-timeline"), {
    type: "line",
    data: {
      labels: tl.map((x) => new Date(x.ts).toLocaleString("en-GB", { timeZone: "Europe/Berlin", weekday: "short", hour: "2-digit", minute: "2-digit" })),
      datasets: [{ data: tl.map((x) => x.bikes), borderColor: "#2563eb", pointRadius: 0, borderWidth: 1.5, tension: 0.25 }],
    },
    options: baseOpts(),
  });
}

async function boot() {
  setFreshness($("#freshness")).catch(() => {});
  let d, ins;
  try {
    [d, ins] = await Promise.all([api(`/api/station/${SID}`), api(`/api/station/${SID}/insights`)]);
  } catch (e) {
    $("#station-title").textContent = "Unknown station";
    $("#station-sub").innerHTML = `No station with this id. <a href="/">Back to the map →</a>`;
    return;
  }
  document.title = `${ins.name} — StadtRad Hamburg`;
  $("#station-title").textContent = ins.name;
  const asOf = d.as_of ? new Date(d.as_of).toLocaleString("en-GB", { timeZone: "Europe/Berlin" }) : "?";
  $("#station-sub").textContent =
    `Capacity ≈ ${fmt(d.capacity_now)} · live status as of ${asOf}. ` +
    `Trips are reconstructed from 5-minute GBFS snapshots, so counts are lower bounds.`;

  setCards(d, ins);
  renderHeatmap("#heatmap", ins.heatmap, "moves");
  const total = (ins.departures || 0) + (ins.arrivals || 0);
  $("#heatmap-note").textContent = ins.busiest_day
    ? `${fmt(total)} moves · busiest day ${ins.busiest_day} (${fmt(ins.busiest_moves)})`
    : "no trips yet";
  setTopDest(ins);
  setCharts(d);

  setInterval(() => setFreshness($("#freshness")).catch(() => {}), 60000);
}
document.addEventListener("DOMContentLoaded", boot);
