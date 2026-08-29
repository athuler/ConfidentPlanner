/* Confident Planner map: borough choropleth -> click for in-borough grid heatmap; filters re-query the API. */
const BREAKS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.01];
const SCALE = ["#f1eef6", "#d0d1e6", "#a6bddb", "#74a9cf", "#2b8cbe", "#045a8d"]; // light -> dark = low -> high approval
const GREY = "#c9ced4";

const map = L.map("map", { zoomControl: true, minZoom: 9, maxBoundsViscosity: 1 }).setView([51.5, -0.1], 10);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors",
}).addTo(map); // desaturated via CSS (.leaflet-tile-pane filter) - no API key needed
let maskLayer = null;
// the overlay modal sits inside #map: keep its clicks and wheel events from reaching Leaflet
(() => { const box = document.getElementById("point-box"); L.DomEvent.disableClickPropagation(box); L.DomEvent.disableScrollPropagation(box); })();

let boroughLayer = null, gridLayer = null, currentBorough = null, boroughsGeo = null;
let debounceTimer = null;

function color(rate) {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return GREY;
  for (let i = 0; i < BREAKS.length; i++) if (rate < BREAKS[i]) return SCALE[i];
  return SCALE[SCALE.length - 1];
}
function pct(x) { return x === null || x === undefined ? "n/a" : (100 * x).toFixed(0) + "%"; }

function filterParams() {
  const f = document.getElementById("filters");
  const p = new URLSearchParams();
  p.set("flood", f.flood.value);
  p.set("conservation", f.conservation.value);
  const months = [...f.querySelectorAll("#months input:checked")].map(i => i.value);
  const days = [...f.querySelectorAll("#days input:checked")].map(i => i.value);
  const types = [...f.querySelectorAll("#app-types input:checked")].map(i => i.value);
  const density = [...f.querySelectorAll("#density button[aria-pressed=true]")].map(b => b.dataset.value);
  if (density.length) p.set("density", density.join(","));
  if (months.length) p.set("months", months.join(","));
  if (days.length) p.set("days", days.join(","));
  if (types.length) p.set("app_types", types.join(","));
  if (f.year_min.value) p.set("year_min", f.year_min.value);
  if (f.year_max.value) p.set("year_max", f.year_max.value);
  return p;
}

async function getJSON(url) {
  const t0 = performance.now();
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  const j = await r.json();
  console.log(`${url} ${(performance.now() - t0).toFixed(0)} ms`);
  return j;
}

function showDataset(ds) {
  const years = ds.years && ds.years.length ? `${ds.years[0]}–${ds.years[ds.years.length - 1]}` : "–";
  document.getElementById("dataset").textContent =
    `${ds.rows_decided.toLocaleString()} decided of ${ds.rows_total.toLocaleString()} applications · years ${years} · ${ds.files.length} file(s)`;
}

function tooltipHtml(name, r) {
  if (!r) return `<b>${name}</b><br>no data`;
  return `<b>${name}</b><br>${pct(r.rate)} approved<br>${r.approved.toLocaleString()} of ${r.n.toLocaleString()} decided`;
}

async function loadBoroughs() {
  const [geo, rates, mask] = await Promise.all([getJSON("/api/boroughs.geojson"), getJSON("/api/rates?" + filterParams()), getJSON("/api/london_mask.geojson")]);
  boroughsGeo = geo;
  maskLayer = L.geoJSON(mask, { interactive: false, style: { color: "#9aa3ad", weight: 1.5, fillColor: "#ffffff", fillOpacity: 0.92 } }).addTo(map);
  boroughLayer = L.geoJSON(geo, {
    style: f => styleFor(f, rates.boroughs),
    onEachFeature: (f, layer) => {
      layer.bindTooltip(tooltipHtml(f.properties.name, rates.boroughs[f.properties.name]), { sticky: true, className: "rate-tip" });
      layer.on("click", e => {
        closeTooltips();
        L.DomEvent.stop(e);  // never let the same click reach the map handler
        if (currentBorough) assessPoint(e.latlng);
        else openBorough(f.properties.name, layer);
      });
      layer.on("mouseover", () => { if (!currentBorough) layer.setStyle({ weight: 3 }); });
      layer.on("mouseout", () => { if (!currentBorough) layer.setStyle({ weight: 1 }); });
    },
  }).addTo(map);
  map.fitBounds(boroughLayer.getBounds());
  // generous horizontal slack: the point box (right) and sidebar (left) cover parts of the viewport
  const b = boroughLayer.getBounds();
  const dLng = (b.getEast() - b.getWest()) * 0.6, dLat = (b.getNorth() - b.getSouth()) * 0.25;
  map.setMaxBounds(L.latLngBounds([b.getSouth() - dLat, b.getWest() - dLng], [b.getNorth() + dLat, b.getEast() + dLng]));
  applyRates(rates);
}

function closeTooltips() {
  if (boroughLayer) boroughLayer.eachLayer(l => l.closeTooltip());
  if (gridLayer) gridLayer.eachLayer(l => l.closeTooltip());
}
map.on("zoomstart movestart", closeTooltips);

function styleFor(f, boroughRates) {
  const r = boroughRates[f.properties.name];
  if (currentBorough) {
    if (currentBorough === f.properties.name) return { color: "#223", weight: 3, fillColor: "#ffffff", fillOpacity: 0 };
    return { color: "#d3d8de", weight: 1, fillColor: "#ffffff", fillOpacity: 0.85 };  // fade everything else away
  }
  return { color: "#334", weight: 1, fillColor: color(r ? r.rate : null), fillOpacity: 0.75 };
}

function setBoroughInteractivity(focused) {
  boroughLayer.eachLayer(l => {
    const name = l.feature.properties.name;
    l.closeTooltip();
    if (focused) {
      l.unbindTooltip();                                   // no borough popovers while focused
      const el = l.getElement();
      if (el) el.classList.toggle("leaflet-interactive", name === currentBorough); // faded boroughs ignore the mouse
    } else {
      l.bindTooltip(tooltipHtml(name, lastBoroughRates[name]), { sticky: true, className: "rate-tip" });
      const el = l.getElement();
      if (el) el.classList.add("leaflet-interactive");
    }
  });
}

function applyRates(rates) {
  document.getElementById("overall-rate").textContent = pct(rates.overall.rate);
  document.getElementById("overall-n").textContent = `${rates.overall.approved.toLocaleString()} approved of ${rates.overall.n.toLocaleString()} decided (current filters)`;
  showDataset(rates.dataset);
  boroughLayer.eachLayer(layer => {
    const name = layer.feature.properties.name;
    layer.setStyle(styleFor(layer.feature, rates.boroughs));
    if (layer.getTooltip()) layer.setTooltipContent(tooltipHtml(name, rates.boroughs[name]));
  });
}

async function refreshRates() {
  const rates = await getJSON("/api/rates?" + filterParams());
  applyRates(rates);
  if (currentBorough) { await drawGrid(); if (boxMode === "borough") await showBoroughBox(); }
}

async function drawGrid() {
  const grid = await getJSON(`/api/heatmap/${encodeURIComponent(currentBorough)}?` + filterParams());
  closeTooltips();
  if (gridLayer) map.removeLayer(gridLayer);
  gridLayer = L.geoJSON(grid, {
    style: f => ({ color: "#222", weight: 0.4, fillColor: color(f.properties.rate), fillOpacity: 0.7 }),
    onEachFeature: (f, layer) => layer.bindTooltip(`${pct(f.properties.rate)} approved<br>${f.properties.approved} of ${f.properties.n} decided in this ${grid.cell_m} m cell`, { sticky: true, className: "rate-tip" }),
  }).addTo(map);
  document.getElementById("view-title").textContent = `${currentBorough}: ${pct(grid.stats.rate)} (${grid.stats.n.toLocaleString()} decided, ${grid.features.length} cells)`;
}

async function openBorough(name, layer) {
  currentBorough = name;
  console.log("open borough", name);
  document.getElementById("back").hidden = false;
  map.fitBounds(layer.getBounds(), { paddingTopLeft: [20, 20], paddingBottomRight: [360, 20] }); // keep the borough clear of the point box
  boroughLayer.eachLayer(l => l.setStyle(styleFor(l.feature, lastBoroughRates)));
  setBoroughInteractivity(true);
  clearPointMarker();
  await Promise.all([drawGrid(), showBoroughBox()]);
}

function backToLondon() {
  closeTooltips();
  currentBorough = null;
  clearPointMarker();
  document.getElementById("point-box").hidden = true;
  if (gridLayer) { map.removeLayer(gridLayer); gridLayer = null; }
  document.getElementById("back").hidden = true;
  document.getElementById("view-title").textContent = "All boroughs";
  setBoroughInteractivity(false);
  map.fitBounds(boroughLayer.getBounds());
  refreshRates();
}

let lastBoroughRates = {};
const _applyRates = applyRates;
applyRates = function (rates) { lastBoroughRates = rates.boroughs; _applyRates(rates); };

function buildFilters(opts) {
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const density = document.getElementById("density");
  const ICONS = { low: "🏡", medium: "🏘️", high: "🏙️" };
  const group = document.createElement("div"); group.className = "btn-group";
  (opts.density_bands || []).forEach(b => {
    const range = b.max === null ? `> ${b.min.toLocaleString()}` : b.min === 0 ? `< ${b.max.toLocaleString()}` : `${b.min.toLocaleString()}–${b.max.toLocaleString()}`;
    group.insertAdjacentHTML("beforeend", `<button type="button" data-value="${b.value}" aria-pressed="false" title="${b.n.toLocaleString()} decided applications">
        <span class="icon">${ICONS[b.value] || ""}</span><span class="lbl">${b.value}</span><span class="rng">${range} /km²</span></button>`);
  });
  density.appendChild(group);
  group.querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
    btn.setAttribute("aria-pressed", btn.getAttribute("aria-pressed") === "true" ? "false" : "true");
    f.dispatchEvent(new Event("change"));   // same debounced refresh path as the other filters
  }));
  const months = document.getElementById("months");
  opts.months.forEach(m => months.insertAdjacentHTML("beforeend", `<label><input type="checkbox" name="months" value="${m}"> ${names[m - 1]}</label>`));
  const days = document.getElementById("days");
  opts.days.forEach(d => days.insertAdjacentHTML("beforeend", `<label><input type="checkbox" name="days" value="${d}"> ${d.slice(0, 3)}</label>`));
  const types = document.getElementById("app-types");
  opts.app_types.forEach(t => types.insertAdjacentHTML("beforeend", `<label><input type="checkbox" name="app_types" value="${t.value.replace(/"/g, "&quot;")}"> ${t.value} <small>(${t.n.toLocaleString()})</small></label>`));
  const f = document.getElementById("filters");
  if (opts.year_min) { f.year_min.placeholder = opts.year_min; f.year_min.min = opts.year_min; f.year_max.min = opts.year_min; }
  if (opts.year_max) { f.year_max.placeholder = opts.year_max; f.year_min.max = opts.year_max; f.year_max.max = opts.year_max; }
  const scale = document.getElementById("legend-scale");
  const labels = ["<50%", "50–60", "60–70", "70–80", "80–90", "90+"];
  SCALE.forEach((c, i) => scale.insertAdjacentHTML("beforeend", `<div style="background:${c}">${labels[i]}</div>`));

  f.addEventListener("change", () => { clearTimeout(debounceTimer); debounceTimer = setTimeout(refreshRates, 150); });
  document.getElementById("reset").addEventListener("click", () => { f.reset(); f.querySelectorAll("#density button").forEach(b => b.setAttribute("aria-pressed", "false")); refreshRates(); });
  document.getElementById("back").addEventListener("click", backToLondon);
}

(async function init() {
  try {
    const o = await getJSON("/api/options");
    buildFilters(o.options);
    showDataset(o.dataset);
    await loadBoroughs();
  } catch (e) {
    console.error(e);
    document.getElementById("dataset").textContent = "Failed to load: " + e.message;
  }
})();

/* ---- point assessment ---------------------------------------------------- */
let pointMarker = null, pointLatLng = null;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

map.on("click", e => { closeTooltips(); if (currentBorough) assessPoint(e.latlng); });

function pointParams(latlng) {
  const p = filterParams();
  p.set("lat", latlng.lat.toFixed(6)); p.set("lon", latlng.lng.toFixed(6));
  const box = document.getElementById("point-box");
  const day = box.querySelector("select[name=day]"), month = box.querySelector("select[name=month]");
  if (day && day.value) p.set("days", day.value);
  if (month && month.value) p.set("months", month.value);
  return p;
}

async function assessPoint(latlng) {
  pointLatLng = latlng;
  if (!pointMarker) pointMarker = L.marker(latlng).addTo(map); else pointMarker.setLatLng(latlng);
  const box = document.getElementById("point-box");
  const prevDay = box.querySelector("select[name=day]")?.value || "";
  const prevMonth = box.querySelector("select[name=month]")?.value || "";
  box.hidden = false;
  box.innerHTML = `<h2>Assessing point… <button class="close" title="close">×</button></h2>`;
  try {
    const r = await getJSON("/api/point?" + pointParams(latlng));
    renderPoint(r, prevDay, prevMonth);
  } catch (e) { box.innerHTML = `<h2>Point <button class="close">×</button></h2><div class="muted">${e.message}</div>`; }
  boxMode = "point";
  box.querySelector(".close").onclick = closePointBox;
}

let boxMode = null; // "borough" | "point" | null

function clearPointMarker() {
  pointLatLng = null;
  if (pointMarker) { map.removeLayer(pointMarker); pointMarker = null; }
}

function closePointBox() {
  clearPointMarker();
  if (currentBorough) showBoroughBox(); else { document.getElementById("point-box").hidden = true; boxMode = null; }
}

function yearChart(byYear, refByYear, refLabel) {
  const years = Object.keys(byYear || {}).map(Number).sort((a, b) => a - b);
  if (!years.length) return "<div class='muted'>no yearly data</div>";
  const W = 300, H = 90, top = 6, bottom = 16, left = 4, bw = (W - left * 2) / years.length;
  const y = r => top + (1 - r) * (H - top - bottom);
  let svg = `<svg viewBox="0 0 ${W} ${H}" class="yearchart" preserveAspectRatio="none">`;
  years.forEach((yr, i) => {
    const r = byYear[yr]; const x = left + i * bw;
    svg += `<rect x="${(x + 1).toFixed(1)}" y="${y(r.rate).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${(H - bottom - y(r.rate)).toFixed(1)}" fill="${color(r.rate)}"><title>${yr}: ${pct(r.rate)} (${r.n.toLocaleString()} decided)</title></rect>`;
    svg += `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 4}" font-size="8" text-anchor="middle" fill="#5b6573">${String(yr).slice(2)}</text>`;
    svg += `<text x="${(x + bw / 2).toFixed(1)}" y="${(y(r.rate) - 1.5).toFixed(1)}" font-size="7" text-anchor="middle" fill="#1d2430">${Math.round(100 * r.rate)}</text>`;
  });
  if (refByYear) {
    const pts = years.filter(yr => refByYear[yr] && refByYear[yr].rate !== null).map((yr, _, arr) => `${(left + (years.indexOf(yr) + 0.5) * bw).toFixed(1)},${y(refByYear[yr].rate).toFixed(1)}`);
    if (pts.length > 1) svg += `<polyline points="${pts.join(" ")}" fill="none" stroke="#1d2430" stroke-width="1" stroke-dasharray="3 2"><title>${refLabel}</title></polyline>`;
  }
  svg += `</svg>`;
  return svg + (refByYear ? `<div class="muted" style="font-size:11px">bars = approval rate by year (label = %); dashed = ${refLabel}</div>` : "");
}

function rateRow(label, r) {
  return `<tr><td>${label}</td><td>${r && r.n ? `${pct(r.rate)} <small class="muted">(${r.n.toLocaleString()})</small>` : "<small class='muted'>–</small>"}</td></tr>`;
}

async function showBoroughBox() {
  if (!currentBorough) return;
  const name = currentBorough;
  const box = document.getElementById("point-box");
  box.hidden = false; boxMode = "borough";
  try {
    const r = await getJSON(`/api/borough/${encodeURIComponent(name)}?` + filterParams());
    if (currentBorough !== name || boxMode !== "borough") return;
    const s = r.stats, L_ = r.london;
    box.innerHTML = `
      <h2>${name} <button class="close" title="close">×</button></h2>
      <div class="big">${pct(s.rate)}</div>
      <div class="muted">${s.approved.toLocaleString()} approved of ${s.n.toLocaleString()} decided (current filters) · London ${pct(L_.rate)}</div>
      <div class="muted" style="margin:6px 0 4px"><b>Click anywhere in the borough</b> for a point assessment.</div>
      <div class="section-title">By year</div>
      ${yearChart(r.by_year, r.london_by_year, "London")}
      <table>
        <tr><th colspan="2">By year</th></tr>${Object.keys(r.by_year).map(Number).sort((a, b) => a - b).map(yr => rateRow(String(yr), r.by_year[yr])).join("")}
        <tr><th colspan="2">Conservation area</th></tr>${rateRow("inside", r.conservation.inside)}${rateRow("outside", r.conservation.outside)}
        <tr><th colspan="2">Flood risk</th></tr>${rateRow("in zone", r.flood.in_zone)}${rateRow("not in zone", r.flood.not_in_zone)}
        <tr><th colspan="2">Population density (ward)</th></tr>${["low", "medium", "high"].map(b => rateRow(b, r.density[b])).join("")}
        <tr><th colspan="2">Day of the week</th></tr>${DAYS.map(d => rateRow(d.slice(0, 3), r.by_day[d])).join("")}
        <tr><th colspan="2">Month</th></tr>${MONTHS.map((m, i) => rateRow(m, r.by_month[i + 1])).join("")}
        <tr><th colspan="2">Application type</th></tr>${r.app_types.map(t => rateRow(t.value, t)).join("")}
      </table>`;
    box.querySelector(".close").onclick = () => { box.hidden = true; boxMode = null; };
  } catch (e) { box.innerHTML = `<h2>${name} <button class="close">×</button></h2><div class="muted">${e.message}</div>`; box.querySelector(".close").onclick = () => { box.hidden = true; boxMode = null; }; }
}

function fmt(x, d = 0) { return x === null || x === undefined ? "n/a" : Number(x).toLocaleString(undefined, { maximumFractionDigits: d }); }

function renderPoint(r, day, month) {
  const f = r.features, nf = r.nearby_features, est = r.estimate;
  const opt = (list, cur, fmtFn) => `<option value="">as sidebar</option>` + list.map(v => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${fmtFn(v)}</option>`).join("");
  const byDay = day && r.by_day[day] ? r.by_day[day] : null;
  const byMonth = month && r.by_month[month] ? r.by_month[month] : null;
  const box = document.getElementById("point-box");
  box.innerHTML = `
    <h2>Point in ${f.borough || "outside London"} <span><button class="close" id="to-borough" title="borough stats" style="font-size:12px">◀ borough</button> <button class="close" title="close">×</button></span></h2>
    <div class="big">${pct(est.rate)}</div>
    <div class="muted">${est.approved} of ${est.n} decided applications within ${fmt(est.radius_m)} m (current filters)</div>
    <div class="muted">${pct(r.similar.rate)} among the ${r.similar.n} of those with the same conservation/flood status
      · borough average ${r.borough_rate ? pct(r.borough_rate.rate) : "n/a"}</div>
    <div class="row">
      <label>Day of the week<select name="day">${opt(DAYS, day, d => d)}</select></label>
      <label>Month<select name="month">${opt(r.by_month ? Object.keys(r.by_month).map(Number).sort((a, b) => a - b) : [], month, m => MONTHS[m - 1])}</select></label>
    </div>
    ${byDay ? `<div class="muted">${day}: ${pct(byDay.rate)} (${byDay.n} nearby)</div>` : ""}
    <div class="section-title">This ${fmt(r.cell.cell_m)} m cell: ${pct(r.cell.stats.rate)} <small class="muted">(${r.cell.stats.n} decided)</small></div>
    ${r.cell.by_year ? yearChart(r.cell.by_year, r.cell.borough_by_year, (f.borough || "borough") + " average") : `<div class="muted">not enough data for a yearly trend in this cell (n = ${r.cell.stats.n}; needs ≥ 3 decisions in each of ≥ 3 years)</div>`}
    ${byMonth ? `<div class="muted">${MONTHS[month - 1]}: ${pct(byMonth.rate)} (${byMonth.n} nearby)</div>` : ""}
    <table>
      <tr><td>Location</td><td>${r.lat.toFixed(5)}, ${r.lon.toFixed(5)}</td></tr>
      <tr><td>Borough</td><td>${f.borough || "–"}</td></tr>
      <tr><td>Ward (nearest apps)</td><td>${nf.ward || "–"}</td></tr>
      <tr><td>Conservation area</td><td class="${f.conservation_area ? "yes" : "no"}">${f.conservation_area ? "Yes – " + (f.conservation_area_name || "") : "No"}</td></tr>
      <tr><td>Flood risk</td><td class="${f.flood_zone ? "yes" : "no"}">${f.flood_zone ? "Zone " + f.flood_zone + (f.flood_risk_type.length ? " (" + f.flood_risk_type.join(", ") + ")" : "") : "Not in a flood zone"}</td></tr>
      <tr><td>Population density</td><td>${fmt(nf.population_density)} /km² ${nf.density_band ? "<b>(" + nf.density_band + ")</b>" : ""} <small>ward, Census 2021</small></td></tr>
      <tr><td>Distance to park</td><td>${fmt(nf.distance_to_park_m)} m</td></tr>
      <tr><td>Nearest application</td><td>${fmt(nf.nearest_app_m)} m away</td></tr>
    </table>`;
  box.querySelectorAll("select").forEach(sel => sel.onchange = () => assessPoint(pointLatLng));
  const tb = box.querySelector("#to-borough"); if (tb) tb.onclick = closePointBox;
}
document.getElementById("filters").addEventListener("change", () => { if (boxMode === "point" && pointLatLng) setTimeout(() => assessPoint(pointLatLng), 200); });

/* ESC: close the point box first if open, otherwise leave the selected borough */
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (boxMode === "point") { closePointBox(); return; }
  if (currentBorough) backToLondon();
});
