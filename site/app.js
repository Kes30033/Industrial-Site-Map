/* Industrial Site Map: client-side scoring and map. Data comes from data/*.json built by pipeline/build.py. */
(() => {
  'use strict';

  const CFG = window.ISM_CONFIG;
  const FACTORS = CFG.factors;
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const int = (n) => (n == null || isNaN(n) ? '–' : Math.round(n).toLocaleString('en-US'));
  const BINS = [0.01, 0.05, 0.10, 0.20, 0.35, 0.50]; // top-share cut points -> ramp steps 6..1
  const BIN_LABELS = ['Bottom half', 'Top 50%', 'Top 35%', 'Top 20%', 'Top 10%', 'Top 5%', 'Top 1%'];

  const state = {
    weights: {},
    filters: { state: '', interstate: '', rail: '', port: '', rtw: false, metro: false },
    selected: -1,
    perState: false,
    layers: { intermodal: true, ports: true, airports: false, yards: false, rail: false, interstates: false },
  };

  let D = null;          // loaded data
  let N = 0;             // number of counties
  const S = {};          // factor id -> Float32Array of 0..100 scores
  let composite, rank, bin, eligible, inScope, ranked = [];
  let map, countyLayer, stateLayer, selLayer, renderer;
  const featureByIdx = [];
  const layerByIdx = [];
  const overlay = {};
  let colors = {};

  // ------------------------------------------------------------------ load
  async function load() {
    const names = ['metrics', 'states', 'areas', 'points', 'meta'];
    const get = (f) => fetch(`data/${f}`).then((r) => {
      if (!r.ok) throw new Error(`${f}: HTTP ${r.status}`);
      return r.json();
    });
    const [metrics, states, areas, points, meta, topo] = await Promise.all([...names.map((n) => get(`${n}.json`)), get('counties.topo.json')]);
    D = { m: metrics, states, areas, points, meta, topo };
    N = metrics.fips.length;
    D.idx = new Map(metrics.fips.map((f, i) => [f, i]));
  }

  // ------------------------------------------------------------------ scoring
  function percentileScores(raw, invert) {
    const vals = [];
    for (let i = 0; i < raw.length; i++) if (raw[i] != null && !isNaN(raw[i])) vals.push(raw[i]);
    vals.sort((a, b) => a - b);
    const n = vals.length;
    const lower = (v) => { let lo = 0, hi = n; while (lo < hi) { const m = (lo + hi) >> 1; if (vals[m] < v) lo = m + 1; else hi = m; } return lo; };
    const upper = (v) => { let lo = 0, hi = n; while (lo < hi) { const m = (lo + hi) >> 1; if (vals[m] <= v) lo = m + 1; else hi = m; } return lo; };
    return (v) => {
      if (v == null || isNaN(v) || n < 2) return 50;
      const p = 100 * ((lower(v) + upper(v) - 1) / 2) / (n - 1);
      return invert ? 100 - p : p;
    };
  }

  function quantile(sorted, q) {
    const pos = (sorted.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  // log10 scale: 5th-percentile county -> 0, 99.5th -> 100
  function logScale(raw) {
    const logs = [];
    for (const v of raw) if (v != null && !isNaN(v)) logs.push(Math.log10(Math.max(0, v) + 1));
    logs.sort((a, b) => a - b);
    const lo = quantile(logs, 0.05), hi = quantile(logs, 0.995);
    return (v) => (v == null || isNaN(v) ? 50 : Math.max(0, Math.min(100, (100 * (Math.log10(Math.max(0, v) + 1) - lo)) / (hi - lo))));
  }

  function rawValues(f) {
    if (f.src) return D.m[f.src];
    return D.m.st.map((st) => {
      const s = D.states[st];
      if (!s) return null;
      const v = s[f.state];
      return typeof v === 'boolean' ? (v ? 1 : 0) : v;
    });
  }

  function computeFactorScores() {
    for (const f of FACTORS) {
      const raw = rawValues(f);
      const out = new Float32Array(N);
      let fn;
      if (f.type === 'dist') fn = (d) => (d == null ? 50 : 100 * Math.pow(0.5, d / f.half));
      else if (f.type === 'pct') fn = percentileScores(raw, false);
      else if (f.type === 'pct_inv') fn = percentileScores(raw, true);
      else if (f.type === 'log') fn = logScale(raw);
      else if (f.type === 'ratio') fn = (v) => (v == null ? 50 : Math.max(0, Math.min(100, (100 * (v - f.lo)) / (f.hi - f.lo))));
      else if (f.type === 'ratio_inv') fn = (v) => (v == null ? 50 : 100 - Math.max(0, Math.min(100, (100 * (v - f.lo)) / (f.hi - f.lo))));
      else if (f.type === 'rank') fn = (r) => (r == null ? 50 : Math.max(0, Math.min(100, (100 * (50 - r)) / 49)));
      else if (f.type === 'bool') fn = (b) => (b == null ? 50 : b ? 100 : 0);
      else if (f.type === 'state_inv') {
        const stVals = Object.values(D.states).map((s) => s[f.state]);
        fn = percentileScores(stVals, true);
      }
      for (let i = 0; i < N; i++) out[i] = fn(raw[i]);
      S[f.id] = out;
    }
  }

  function passesMust(i) {
    const fl = state.filters, m = D.m;
    if (fl.interstate && m.d_interstate[i] > +fl.interstate) return false;
    if (fl.rail && m.d_class1[i] > +fl.rail) return false;
    if (fl.port && m.d_port[i] > +fl.port) return false;
    if (fl.rtw && !(D.states[m.st[i]] || {}).rtw) return false;
    if (fl.metro && !String(m.area[i]).startsWith('00')) return false;
    return true;
  }

  function computeScores() {
    const w = FACTORS.map((f) => state.weights[f.id] || 0);
    const wsum = w.reduce((a, b) => a + b, 0);
    composite = new Float32Array(N);
    eligible = new Uint8Array(N);
    inScope = new Uint8Array(N);
    rank = new Int32Array(N).fill(-1);
    bin = new Int8Array(N).fill(-1);
    for (let i = 0; i < N; i++) {
      let s = 0;
      if (wsum > 0) { for (let k = 0; k < FACTORS.length; k++) if (w[k]) s += w[k] * S[FACTORS[k].id][i]; s /= wsum; } else s = 50;
      composite[i] = s;
      inScope[i] = !state.filters.state || D.m.st[i] === state.filters.state ? 1 : 0;
      eligible[i] = inScope[i] && passesMust(i) ? 1 : 0;
    }
    ranked = [];
    for (let i = 0; i < N; i++) if (eligible[i]) ranked.push(i);
    ranked.sort((a, b) => composite[b] - composite[a]);
    const n = ranked.length;
    ranked.forEach((i, k) => {
      rank[i] = k + 1;
      const top = k / Math.max(1, n);
      let b = 0;
      for (let j = 0; j < BINS.length; j++) if (top < BINS[j]) { b = 6 - j; break; }
      bin[i] = b;
    });
  }

  function recompute() {
    computeScores();
    restyle();
    renderRanking();
    renderWeightsShare();
    if (state.selected >= 0) renderDetail(state.selected);
    writeHash();
  }

  // ------------------------------------------------------------------ map
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    colors = {
      ramp: [0, 1, 2, 3, 4, 5, 6].map((k) => v(`--r${k}`)),
      excluded: v('--excluded'), ink: v('--ink'), ink2: v('--ink-2'), panel: v('--panel'), accent: v('--accent'),
      dark: document.documentElement.dataset.theme === 'dark' ||
        (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches),
    };
  }

  // Esri World Light/Dark Gray Canvas: a quiet base plus a separate labels layer drawn above the counties
  function tileUrl(kind) {
    const tone = colors.dark ? 'Dark' : 'Light';
    const layer = kind === 'labels' ? 'Reference' : 'Base';
    return `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${tone}_Gray_${layer}/MapServer/tile/{z}/{y}/{x}`;
  }

  function countyStyle(i) {
    if (i == null || i < 0 || !composite) return { fillOpacity: 0, opacity: 0 };
    const border = { color: colors.panel, weight: 0.35, opacity: 0.55 };
    if (!inScope[i]) return { ...border, fillColor: colors.excluded, fillOpacity: 0.12, opacity: 0.2 };
    if (!eligible[i]) return { ...border, fillColor: colors.excluded, fillOpacity: 0.6 };
    return { ...border, fillColor: colors.ramp[bin[i]], fillOpacity: 0.86 };
  }

  function restyle() {
    for (let i = 0; i < layerByIdx.length; i++) if (layerByIdx[i]) layerByIdx[i].setStyle(countyStyle(i));
    renderLegend();
  }

  function initMap() {
    readColors();
    map = L.map('map', { zoomControl: false, minZoom: 3, maxZoom: 12, zoomSnap: 0.25, attributionControl: true });
    L.control.zoom({ position: 'topright' }).addTo(map);
    map.createPane('labels');
    map.getPane('labels').style.zIndex = 450;
    map.getPane('labels').style.pointerEvents = 'none';
    const attr = 'Basemap &copy; <a href="https://www.esri.com">Esri</a>, HERE, Garmin, &copy; OpenStreetMap contributors';
    overlay.base = L.tileLayer(tileUrl('base'), { attribution: attr, maxNativeZoom: 16, maxZoom: 19 }).addTo(map);
    overlay.labels = L.tileLayer(tileUrl('labels'), { pane: 'labels', maxNativeZoom: 16, maxZoom: 19 }).addTo(map);
    renderer = L.canvas({ padding: 0.4, tolerance: 2 });

    const fc = topojson.feature(D.topo, D.topo.objects.counties);
    countyLayer = L.geoJSON(fc, {
      renderer,
      style: (f) => countyStyle(D.idx.get(String(f.id))),
      onEachFeature: (f, layer) => {
        const i = D.idx.get(String(f.id));
        if (i == null) return;
        featureByIdx[i] = f;
        layerByIdx[i] = layer;
        layer.bindTooltip(() => tooltipHtml(i), { sticky: true, className: 'ism-tip', direction: 'top', offset: [0, -10], opacity: 1 });
        layer.on('mouseover', () => { layer.setStyle({ weight: 1.6, color: colors.ink, opacity: 0.9 }); });
        layer.on('mouseout', () => { layer.setStyle(countyStyle(i)); });
        layer.on('click', () => select(i, false));
      },
    }).addTo(map);

    const sf = topojson.feature(D.topo, D.topo.objects.states);
    stateLayer = L.geoJSON(sf, { renderer, interactive: false, style: () => ({ color: colors.ink2, weight: 0.9, opacity: 0.55, fill: false }) }).addTo(map);

    map.fitBounds([[24.6, -124.8], [49.2, -66.9]], { padding: [10, 10] });
    buildOverlays();
  }

  function tooltipHtml(i) {
    const m = D.m;
    const sc = Math.round(composite[i]);
    let line;
    if (!inScope[i]) line = 'Outside the selected state';
    else if (!eligible[i]) line = `Score ${sc}. Excluded by must-haves`;
    else line = `Score ${sc}, rank ${int(rank[i])} of ${int(ranked.length)}`;
    return `<strong>${esc(m.name[i])}, ${esc(m.st[i])}</strong><span class="muted">${line}</span>`;
  }

  // ------------------------------------------------------------------ overlays (points + lines)
  const LAYER_DEFS = [
    { id: 'intermodal', label: 'Intermodal rail terminals', key: 'square' },
    { id: 'ports', label: 'Ports, 5M+ tons/yr', key: 'circle' },
    { id: 'airports', label: 'Air cargo airports', key: 'triangle' },
    { id: 'yards', label: 'Rail yards', key: 'dot' },
    { id: 'rail', label: 'Class I rail lines', key: 'dash' },
    { id: 'interstates', label: 'Interstates', key: 'line' },
  ];

  function shapeSvg(kind, size) {
    const s = size, h = s / 2;
    const fill = 'var(--ink)', stroke = 'var(--panel)';
    if (kind === 'square') return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><rect x="1" y="1" width="${s - 2}" height="${s - 2}" fill="${fill}" stroke="${stroke}" stroke-width="1.5"/></svg>`;
    if (kind === 'triangle') return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><path d="M${h} 1 L${s - 1} ${s - 1.5} L1 ${s - 1.5}Z" fill="${fill}" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
    if (kind === 'circle') return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><circle cx="${h}" cy="${h}" r="${h - 1}" fill="${fill}" fill-opacity=".85" stroke="${stroke}" stroke-width="1.5"/></svg>`;
    if (kind === 'dot') return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><circle cx="${h}" cy="${h}" r="${h / 2}" fill="${fill}" stroke="${stroke}" stroke-width="1"/></svg>`;
    if (kind === 'dash') return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><path d="M0 ${h}H${s}" stroke="${fill}" stroke-width="2" stroke-dasharray="3 2"/></svg>`;
    return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}"><path d="M0 ${h}H${s}" stroke="${fill}" stroke-width="2.5"/></svg>`;
  }

  function divMarker(lat, lon, kind, size, tip) {
    const icon = L.divIcon({ className: 'ism-pt', html: shapeSvg(kind, size), iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    return L.marker([lat, lon], { icon, keyboard: false, riseOnHover: true })
      .bindTooltip(tip, { className: 'ism-tip', direction: 'top', offset: [0, -size / 2] });
  }

  function buildOverlays() {
    const P = D.points;
    const rr = P.railroads || {};
    overlay.intermodal = L.layerGroup(P.intermodal.map(([la, lo, name, rail, city]) =>
      divMarker(la, lo, 'square', 8, `<strong>${esc(name)}</strong><span class="muted">Intermodal terminal, ${esc(city)}<br>${esc(rail)}</span>`)));
    overlay.ports = L.layerGroup(P.ports.filter((p) => p[3] >= 5e6).map(([la, lo, name, tons, type]) => {
      const size = Math.round(6 + Math.min(8, Math.sqrt(tons / 1e6) * 0.8));
      return divMarker(la, lo, 'circle', size, `<strong>${esc(name)}</strong><span class="muted">${(tons / 1e6).toFixed(1)}M short tons a year, ${esc(portType(type))}</span>`);
    }));
    overlay.airports = L.layerGroup(P.airports.filter((a) => a[4] >= 250e6).map(([la, lo, code, name, lbs, city]) =>
      divMarker(la, lo, 'triangle', 10, `<strong>${esc(code)} ${esc(name)}</strong><span class="muted">${fmtLbs(lbs)} all-cargo landed weight a year</span>`)));
    overlay.yards = L.layerGroup(P.yards.map(([la, lo, name, owner]) =>
      L.circleMarker([la, lo], { renderer, radius: 2.4, weight: 0.8, color: colors.panel, fillColor: colors.ink, fillOpacity: 0.9 })
        .bindTooltip(`<strong>${esc(name)}</strong><span class="muted">Rail yard, ${esc(rr[owner] || owner)}</span>`, { className: 'ism-tip', direction: 'top' })));

    const body = $('#layers-body');
    body.innerHTML = LAYER_DEFS.map((l) => `
      <label class="layer-row"><input type="checkbox" data-layer="${l.id}" ${state.layers[l.id] ? 'checked' : ''}>
        <span class="layer-key">${shapeSvg(l.key, 12)}</span>${esc(l.label)}</label>`).join('');
    body.addEventListener('change', (e) => {
      const id = e.target.dataset.layer;
      if (!id) return;
      state.layers[id] = e.target.checked;
      applyLayers();
    });
    $('.layers-toggle').addEventListener('click', (e) => {
      const open = e.currentTarget.getAttribute('aria-expanded') === 'true';
      e.currentTarget.setAttribute('aria-expanded', String(!open));
      body.hidden = open;
    });
    if (matchMedia('(max-width: 900px)').matches) $('.layers-toggle').click();
    applyLayers();
  }

  async function lineLayer(id) {
    if (overlay[id]) return overlay[id];
    const file = id === 'rail' ? 'rail.topo.json' : 'interstates.topo.json';
    const topo = await fetch(`data/${file}`).then((r) => r.json());
    const obj = topo.objects[Object.keys(topo.objects)[0]];
    const fc = topojson.feature(topo, obj);
    overlay[id] = L.geoJSON(fc, {
      renderer, interactive: false,
      style: () => (id === 'rail'
        ? { color: colors.ink, weight: 1.3, opacity: 0.85, dashArray: '4 3' }
        : { color: colors.ink, weight: 1.8, opacity: 0.6 }),
    });
    return overlay[id];
  }

  async function applyLayers() {
    for (const id of ['rail', 'interstates']) {
      if (state.layers[id]) { const l = await lineLayer(id); if (!map.hasLayer(l)) l.addTo(map); }
      else if (overlay[id] && map.hasLayer(overlay[id])) map.removeLayer(overlay[id]);
    }
    for (const id of ['yards', 'intermodal', 'ports', 'airports']) {
      const l = overlay[id];
      if (state.layers[id]) { if (map.hasLayer(l)) map.removeLayer(l); l.addTo(map); } // re-add keeps points above lines
      else if (map.hasLayer(l)) map.removeLayer(l);
    }
    if (selLayer) selLayer.bringToFront();
  }

  // ------------------------------------------------------------------ legend
  function renderLegend() {
    const n = ranked.length;
    $('#legend').innerHTML = `
      <h3>County score rank</h3>
      <div class="legend-scale" aria-hidden="true">${colors.ramp.map((c) => `<span style="background:${c}"></span>`).join('')}</div>
      <div class="legend-labels"><span>Bottom half</span><span>Top 10%</span><span>Top 1%</span></div>
      ${n < N ? '<div class="legend-ex"><i></i>Excluded or outside the selected state</div>' : ''}`;
  }

  // ------------------------------------------------------------------ controls
  function isCustom() {
    return FACTORS.some((f) => (state.weights[f.id] || 0) !== (CFG.defaults[f.id] || 0));
  }

  // A source entry is a key into data/meta.json `sources` or an inline {name, title, url}
  function sourceInfo(s) {
    if (typeof s === 'object') return s;
    const m = (D.meta.sources || {})[s] || {};
    const url = String(m.url || '').includes('<') ? String(m.url).split('/industry/')[0] : m.url;
    const title = m.title ? `${m.title}${m.vintage && m.vintage !== 'None' ? `, ${m.vintage}` : ''}` : '';
    return { name: CFG.sourceNames[s] || m.title || s, title, url };
  }

  function sourceLinks(f) {
    return (f.sources || []).map(sourceInfo).filter((s) => s.url)
      .map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.title)}">${esc(s.name)}</a>`).join(', ');
  }

  const levelWord = (v) => (v === 0 ? 'Ignored' : v <= 3 ? 'Low' : v <= 6 ? 'Medium' : v <= 9 ? 'High' : 'Highest');

  function buildControls() {
    const wrap = $('#weights');
    const open = !matchMedia('(max-width: 900px)').matches;
    wrap.innerHTML = CFG.groups.map((g) => `
      <details class="wgroup" data-group="${g.id}" ${open ? 'open' : ''}>
        <summary>${esc(g.label)}<span class="share" data-share="${g.id}"></span></summary>
        <div class="wrows">${FACTORS.filter((f) => f.group === g.id).map((f) => {
          const links = sourceLinks(f);
          return `
          <div class="wrow" data-row="${f.id}">
            <label for="w-${f.id}" title="${esc(f.about)}">${esc(f.label)}</label>
            <output for="w-${f.id}" id="o-${f.id}"></output>
            <input type="range" id="w-${f.id}" min="0" max="10" step="1" data-factor="${f.id}" aria-describedby="o-${f.id} m-${f.id}">
            <p class="wmeta" id="m-${f.id}"><span class="better">${esc(f.better || '')}</span>${links ? `<span class="src">Data: ${links}</span>` : ''}</p>
          </div>`;
        }).join('')}
        </div>
      </details>`).join('');
    let raf = 0;
    wrap.addEventListener('input', (e) => {
      const id = e.target.dataset.factor;
      if (!id) return;
      state.weights[id] = +e.target.value;
      syncSlider(id);
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(recompute);
    });
    $('#reset-weights').addEventListener('click', resetWeights);

    const sel = $('#f-state');
    const sts = Object.entries(D.states).sort((a, b) => a[1].name.localeCompare(b[1].name));
    sel.insertAdjacentHTML('beforeend', sts.map(([ab, s]) => `<option value="${ab}">${esc(s.name)}</option>`).join(''));
    const bindFilter = (id, key, isCheck) => {
      const el = $(id);
      el.addEventListener('change', () => {
        state.filters[key] = isCheck ? el.checked : el.value;
        if (key === 'state') zoomToState(el.value);
        recompute();
      });
    };
    bindFilter('#f-state', 'state');
    bindFilter('#f-interstate', 'interstate');
    bindFilter('#f-rail', 'rail');
    bindFilter('#f-port', 'port');
    bindFilter('#f-rtw', 'rtw', true);
    bindFilter('#f-metro', 'metro', true);

    $('#per-state').addEventListener('change', (e) => { state.perState = e.target.checked; renderRanking(); });
    $('#back').addEventListener('click', () => select(-1));
    $('#download').addEventListener('click', downloadCsv);
    $('#open-about').addEventListener('click', openAbout);
    $('#close-about').addEventListener('click', () => $('#about').close());
    $('#about').addEventListener('click', (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
    $('#theme-toggle').addEventListener('click', cycleTheme);

    const dl = $('#county-list');
    dl.innerHTML = D.m.fips.map((f, i) => `<option value="${esc(D.m.name[i])}, ${D.m.st[i]}">`).join('');
    const search = $('#search');
    const lookup = new Map(D.m.fips.map((f, i) => [`${D.m.name[i]}, ${D.m.st[i]}`.toLowerCase(), i]));
    const go = () => {
      const i = lookup.get(search.value.trim().toLowerCase());
      if (i != null) { select(i, true); search.blur(); }
    };
    search.addEventListener('change', go);
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });

    const built = D.meta.built ? new Date(D.meta.built + 'T12:00:00') : null;
    $('#built').textContent = built ? `Data refreshed ${built.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}` : '';
    updateThemeLabel();
  }

  function syncSlider(id) {
    const el = $(`#w-${id}`);
    const v = state.weights[id] || 0;
    el.value = v;
    el.style.setProperty('--fill', `${v * 10}%`);
    $(`#o-${id}`).textContent = `${levelWord(v)} · ${v}`;
    el.setAttribute('aria-valuetext', `${v} of 10, ${levelWord(v).toLowerCase()}`);
    el.closest('.wrow').classList.toggle('off', v === 0);
  }

  function renderWeightsShare() {
    const tot = FACTORS.reduce((a, f) => a + (state.weights[f.id] || 0), 0) || 1;
    for (const g of CFG.groups) {
      const s = FACTORS.filter((f) => f.group === g.id).reduce((a, f) => a + (state.weights[f.id] || 0), 0);
      $(`[data-share="${g.id}"]`).textContent = `${Math.round((100 * s) / tot)}% of score`;
    }
  }

  function resetWeights() {
    state.weights = { ...CFG.defaults };
    FACTORS.forEach((f) => syncSlider(f.id));
    recompute();
  }

  function zoomToState(ab) {
    if (!ab) { map.flyToBounds([[24.6, -124.8], [49.2, -66.9]], { duration: 0.6 }); return; }
    const b = L.latLngBounds([]);
    for (let i = 0; i < N; i++) if (D.m.st[i] === ab && layerByIdx[i]) b.extend(layerByIdx[i].getBounds());
    if (b.isValid()) map.flyToBounds(b, { padding: [24, 24], duration: 0.6 });
  }

  // ------------------------------------------------------------------ ranking
  function renderRanking() {
    const fl = state.filters;
    const scope = fl.state ? D.states[fl.state].name : 'the U.S.';
    const total = fl.state ? D.m.st.filter((s) => s === fl.state).length : N;
    const excl = total - ranked.length;
    $('#ranking-sub').textContent = `${isCustom() ? 'Your weights' : 'Equal weights'}. ${int(ranked.length)} counties in ${scope} ranked${excl ? `, ${int(excl)} excluded by must-haves` : ''}.`;
    const list = $('#rank-list');
    if (!ranked.length) {
      list.innerHTML = '<li class="empty">No county meets every must-have. Loosen a distance limit or clear a checkbox.</li>';
      return;
    }
    const ps = $('#per-state');
    ps.disabled = !!fl.state;
    let rows = ranked;
    if (state.perState && !fl.state) {
      const seen = new Set();
      rows = ranked.filter((i) => (seen.has(D.m.st[i]) ? false : (seen.add(D.m.st[i]), true)));
    }
    list.innerHTML = rows.slice(0, state.perState && !fl.state ? 51 : 30).map((i) => {
      const sc = composite[i];
      const area = D.areas.areas[D.m.area[i]];
      const sub = area && area.type === 'metro' ? area.name.split(',')[0].split('-').slice(0, 2).join('–') + ' metro' : `${D.states[D.m.st[i]].name}, rural`;
      return `<li><button type="button" class="rank-item" data-i="${i}">
        <span class="rk">${rank[i]}</span>
        <span class="nm">${esc(D.m.name[i])}, ${D.m.st[i]}<small>${esc(sub)}</small></span>
        <span class="sc">${Math.round(sc)}<span class="mini-bar"><i style="width:${sc.toFixed(1)}%"></i></span></span>
      </button></li>`;
    }).join('');
    list.onclick = (e) => {
      const b = e.target.closest('.rank-item');
      if (b) select(+b.dataset.i, true);
    };
  }

  // ------------------------------------------------------------------ selection + detail
  function select(i, zoom) {
    state.selected = i;
    if (selLayer) { map.removeLayer(selLayer); selLayer = null; }
    if (i < 0) {
      $('#detail').hidden = true;
      $('#ranking').hidden = false;
      writeHash();
      return;
    }
    const f = featureByIdx[i];
    if (f) {
      selLayer = L.geoJSON(f, { renderer, interactive: false, style: { color: colors.accent, weight: 3, opacity: 1, fill: false } }).addTo(map);
      if (zoom) {
        const b = selLayer.getBounds();
        const target = map.getBoundsZoom(b) > 9 ? b.getCenter() : null;
        if (target) map.flyTo(target, Math.max(map.getZoom(), 8), { duration: 0.6 });
        else map.flyToBounds(b.pad(1.2), { duration: 0.6, maxZoom: 9 });
      }
    }
    renderDetail(i);
    $('#ranking').hidden = true;
    $('#detail').hidden = false;
    $('.results').scrollTop = 0;
    writeHash();
  }

  const mi = (d) => (d == null ? '–' : d < 1 ? 'under 1 mi' : d < 10 ? `${d.toFixed(1)} mi` : `${Math.round(d)} mi`);
  const fmtPop = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} million` : int(n));
  const fmtLbs = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)}B lbs` : `${Math.round(n / 1e6)}M lbs`);
  const portType = (t) => ({ Coastal: 'coastal port', 'Great Lakes': 'Great Lakes port', Internal: 'inland river port' }[t] || t);
  const relPct = (r) => (r == null ? '–' : Math.abs(r - 1) < 0.005 ? 'Same as U.S. median' : `${Math.round(Math.abs(r - 1) * 100)}% ${r < 1 ? 'below' : 'above'} U.S. median`);
  const ordinalRank = (r) => (r == null ? 'Not ranked' : `#${r} of 50`);

  function rawText(f, i) {
    const m = D.m, P = D.points, st = D.states[m.st[i]] || {};
    switch (f.id) {
      case 'interstate': return `${mi(m.d_interstate[i])} to ${m.interstate[i] || 'an Interstate'}`;
      case 'class1': return m.class1_own && !m.class1_own[i] ? `${mi(m.d_class1[i])} to Class I-served track owned by ${m.class1[i]}` : `${mi(m.d_class1[i])} to ${m.class1[i] || 'Class I'} track`;
      case 'yard': { const y = P.yards[m.yard_i[i]]; return `${mi(m.d_yard[i])} to ${y[2]}, ${P.railroads[y[3]] || y[3]}`; }
      case 'intermodal': {
        const t = P.intermodal[m.intermodal_i[i]];
        const city = t[2].toLowerCase().includes(t[4].split(',')[0].toLowerCase()) ? '' : `, ${t[4]}`;
        const rail = t[3] && t[3].length <= 24 ? ` (${t[3]})` : '';
        return `${mi(m.d_intermodal[i])} to ${t[2]}${city}${rail}`;
      }
      case 'port': { const p = P.ports[m.port_i[i]]; return `${mi(m.d_port[i])} to ${p[2]} (${(p[3] / 1e6).toFixed(0)}M tons)`; }
      case 'air': { const a = P.airports[m.air_i[i]]; return `${mi(m.d_air[i])} to ${a[2]}, ${a[5]} (${fmtLbs(a[4])})`; }
      case 'market': return `${fmtPop(m.market_pop[i])} people`;
      case 'trades': return `${int(m.shed_trades[i])} workers within ${D.meta.labor_radius_mi} mi`;
      case 'logi': return `${int(m.shed_logi[i])} workers within ${D.meta.labor_radius_mi} mi`;
      case 'metal': return `${int(m.shed_metal[i])} jobs within ${D.meta.labor_radius_mi} mi`;
      case 'whse': return `${int(m.shed_whse[i])} jobs within ${D.meta.labor_radius_mi} mi`;
      case 'lq_metal': return m.lq_metal[i] == null ? '–' : `${m.lq_metal[i].toFixed(2)}× the U.S. share of jobs`;
      case 'lq_elec': return m.lq_elec[i] == null ? '–' : `${m.lq_elec[i].toFixed(2)}× the U.S. share of jobs`;
      case 'wage_trade': return relPct(m.wage_trade[i]);
      case 'wage_logi': return relPct(m.wage_logi[i]);
      case 'tax': return ordinalRank(st.tf_overall);
      case 'corp': return `${ordinalRank(st.tf_corp)}${st.corp_rate ? `, top rate ${st.corp_rate}%` : st.corp_note ? `, ${st.corp_note.toLowerCase()}` : ''}`;
      case 'prop': return ordinalRank(st.tf_prop);
      case 'friendly': return ordinalRank(st.cnbc_bf);
      case 'regs': return st.restrictions ? `${int(st.restrictions)} restrictions (${st.restr_year})` : 'No data';
      case 'rtw': return st.rtw ? 'Yes' : 'No';
      case 'power': return st.elec ? `${st.elec.toFixed(2)}¢ per kWh` : '–';
      default: return '';
    }
  }

  function renderDetail(i) {
    const m = D.m;
    const st = D.states[m.st[i]] || {};
    const area = D.areas.areas[m.area[i]];
    const sc = composite[i];
    const n = ranked.length;
    let standing;
    if (!inScope[i]) standing = `Outside ${esc(D.states[state.filters.state].name)}, so not ranked.`;
    else if (!eligible[i]) standing = 'Not ranked: fails a must-have.';
    else {
      const top = (rank[i] - 1) / Math.max(1, n);
      const label = top < 0.5 ? BIN_LABELS[bin[i]].replace('Top', 'top') : 'bottom half';
      standing = `Rank <b>#${int(rank[i])}</b> of ${int(n)}<br>In the ${label} with ${isCustom() ? 'your' : 'equal'} weights`;
    }
    const areaLine = area ? (area.type === 'metro' ? `${area.name} metro area` : area.type === 'nonmetro' ? 'Outside metro areas' : '') : '';

    const groupHtml = (g) => FACTORS.filter((f) => f.group === g).map((f) => {
      const s = S[f.id][i];
      const on = (state.weights[f.id] || 0) > 0;
      return `<div class="frow ${on ? '' : 'off'}" title="${esc(f.about)}">
        <span class="fl">${esc(f.label)}</span>
        <span class="fs">${Math.round(s)}<span class="mini-bar"><i style="width:${s.toFixed(1)}%"></i></span></span>
        <span class="fv">${esc(rawText(f, i))}</span>
      </div>`;
    }).join('');

    const occRows = CFG.occupations.map(([code, label]) => {
      const v = area && area.occ[code];
      const sv = D.areas.areas['S' + stateFips(m.st[i])];
      const fb = !v || v[0] == null ? sv && sv.occ[code] : null;
      const row = fb || v || [null, null, null];
      const lq = row[1];
      return `<tr><td>${esc(label)}${fb ? ' <span title="Not published for this area; state figure shown">*</span>' : ''}</td>
        <td>${int(row[0])}</td><td class="${lq >= 1.25 ? 'hi' : ''}">${lq == null ? '–' : lq.toFixed(2) + '×'}</td>
        <td>${row[2] == null ? '–' : '$' + int(row[2])}</td></tr>`;
    }).join('');

    const rel = D.meta.sources || {};
    $('#detail-body').innerHTML = `
      <div class="d-title">
        <h2>${esc(m.name[i])}</h2>
        <p>${esc(st.name || m.st[i])}, population ${int(m.pop[i])}${areaLine ? `<br>${esc(areaLine)}` : ''}</p>
      </div>
      <div class="d-score"><span class="big">${Math.round(sc)}</span><span class="of">${standing}</span></div>
      <div class="d-actions">
        <a href="https://www.google.com/maps/@${m.lat[i]},${m.lon[i]},11z" target="_blank" rel="noopener">Open in Google Maps</a>
        <a href="#" id="zoom-county">Zoom to county</a>
      </div>
      <section class="d-sec"><h3>Freight access</h3>${groupHtml('freight')}</section>
      <section class="d-sec"><h3>Workforce</h3>${groupHtml('labor')}</section>
      <section class="d-sec">
        <h3>Local labor market</h3>
        <p class="sub">${esc(area ? area.name : '')}, BLS ${esc((rel.oews || {}).vintage || '')}. Concentration of 1.25× or more in bold.</p>
        <table class="occ"><thead><tr><th>Occupation</th><th>Jobs</th><th>Concentration</th><th>Median pay</th></tr></thead><tbody>${occRows}</tbody></table>
      </section>
      <section class="d-sec"><h3>Taxes, regulation &amp; costs</h3>${groupHtml('business')}</section>
      <section class="d-sec">
        <h3>More about ${esc(st.name || '')}</h3>
        <dl class="kv">
          <dt>CNBC Top States for Business</dt><dd>${ordinalRank(st.cnbc_overall)}</dd>
          <dt>CNBC cost of doing business</dt><dd>${ordinalRank(st.cnbc_cost)}</dd>
          <dt>CNBC infrastructure</dt><dd>${ordinalRank(st.cnbc_infra)}</dd>
          <dt>Tax Foundation sales tax</dt><dd>${ordinalRank(st.tf_sales)}</dd>
          <dt>Tax Foundation unemployment insurance tax</dt><dd>${ordinalRank(st.tf_ui)}</dd>
          <dt>Manufacturing jobs in county</dt><dd>${int(m.w_mfg[i])}</dd>
        </dl>
      </section>`;
    $('#zoom-county').addEventListener('click', (e) => {
      e.preventDefault();
      if (selLayer) map.flyToBounds(selLayer.getBounds(), { padding: [30, 30], duration: 0.6, maxZoom: 10 });
    });
  }

  function stateFips(ab) {
    for (let i = 0; i < N; i++) if (D.m.st[i] === ab) return D.m.fips[i].slice(0, 2);
    return '';
  }

  // ------------------------------------------------------------------ CSV
  function downloadCsv() {
    const m = D.m;
    const head = ['rank', 'county', 'state', 'fips', 'score', ...FACTORS.map((f) => `${f.id}_score`),
      'miles_interstate', 'miles_class1_rail', 'miles_rail_yard', 'miles_intermodal', 'miles_port_5mt', 'miles_air_cargo',
      'pop_250mi', 'trade_workers_45mi', 'metal_jobs_45mi'];
    const q = (s) => `"${String(s).replace(/"/g, '""')}"`;
    const rows = ranked.map((i) => [rank[i], q(m.name[i]), m.st[i], q(m.fips[i]), composite[i].toFixed(1),
      ...FACTORS.map((f) => S[f.id][i].toFixed(1)),
      m.d_interstate[i], m.d_class1[i], m.d_yard[i], m.d_intermodal[i], m.d_port[i], m.d_air[i],
      m.market_pop[i], m.shed_trades[i], m.shed_metal[i]].join(','));
    const blob = new Blob([[head.join(','), ...rows].join('\n')], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `county-scores${isCustom() ? '-custom' : ''}${state.filters.state ? '-' + state.filters.state : ''}.csv`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  // ------------------------------------------------------------------ about
  function openAbout() {
    const src = D.meta.sources || {};
    const fmtType = (f) => ({
      dist: `100 at the site, halving every ${f.half} miles`,
      pct: 'Percentile among all U.S. counties',
      pct_inv: 'Percentile among all counties, reversed (lower is better)',
      log: 'Log scale: each 10× more scores the same number of points more',
      ratio: `${f.lo} or less scores 0, ${f.hi} or more scores 100`,
      ratio_inv: `${f.lo} or less scores 100, ${f.hi} or more scores 0`,
      rank: 'State rank 1–50 mapped to 100–0',
      state_inv: 'Percentile among states, reversed (lower is better)',
      bool: '100 if yes, 0 if no',
    }[f.type]);
    const order = ['county_shapes', 'population', 'cenpop', 'gazetteer', 'acs_c24010', 'qcew', 'oews', 'cbsa', 'interstates',
      'class1_rail', 'rail_yards', 'intermodal', 'ports', 'air_cargo', 'tax_index', 'corp_rates', 'cnbc', 'regulation', 'electricity'];
    const srcRows = order.filter((k) => src[k]).map((k) => {
      const s = src[k];
      const url = String(s.url || '').includes('<') ? String(s.url).split('/industry/')[0] : s.url;
      return `<li><a href="${esc(url)}" target="_blank" rel="noopener">${esc(s.title)}</a>${s.vintage && s.vintage !== 'None' ? `, ${esc(s.vintage)}` : ''}. Retrieved ${esc(s.fetched)}.</li>`;
    }).join('');
    $('#about-body').innerHTML = `
      <p>Every county gets a 0–100 score for each factor below. The overall score is the weighted average of those factor scores using the sliders, so a factor at weight 0 is ignored and a factor at 10 counts ten times as much as one at 1. Counties are then colored by rank among the counties that pass your must-haves: the darkest blue is the top 1%.</p>
      <p><strong>Distances</strong> are straight-line miles from each county’s center of population (Census 2020), not drive times. A county can be large, so always check the specific parcel. <strong>Workforce</strong> counts add up every county whose population center is within ${D.meta.labor_radius_mi} miles, a typical commute. <strong>Occupation concentration and pay</strong> come from the metro area a county belongs to; rural counties use their state’s combined nonmetro areas. <strong>Taxes and regulation</strong> are state-level, so every county in a state shares them.</p>
      <h3>Factors</h3>
      <table><thead><tr><th>Factor</th><th>What it measures</th><th>Scoring</th><th>Data</th></tr></thead><tbody>
        ${FACTORS.map((f) => `<tr><td>${esc(f.label)}</td><td>${esc(f.about)}</td><td>${esc(fmtType(f))}</td><td>${sourceLinks(f)}</td></tr>`).join('')}
      </tbody></table>
      <h3>Data sources</h3>
      <ul>${srcRows}</ul>
      <p>The data refreshes automatically on the 4th of each month through a GitHub Actions workflow in this site’s repository. Basemap © Esri, HERE, Garmin and OpenStreetMap contributors.</p>
      <h3>Limits</h3>
      <ul>
        <li>Scores compare counties with each other; they are a shortlist tool, not a site evaluation. Land and building costs, utilities capacity, zoning, incentives and local permitting are not included.</li>
        <li>Small counties’ industry jobs can be suppressed by BLS for confidentiality and count as zero; the 45-mile workforce totals soften this.</li>
        <li>Port distance uses principal ports by total tonnage, which includes bulk and petroleum ports; check container service for your freight.</li>
      </ul>`;
    $('#about').showModal();
  }

  // ------------------------------------------------------------------ theme
  function currentThemeSetting() { return document.documentElement.dataset.theme || 'auto'; }
  function updateThemeLabel() { $('#theme-toggle').textContent = `Theme: ${currentThemeSetting()}`; }
  function cycleTheme() {
    const next = { auto: 'light', light: 'dark', dark: 'auto' }[currentThemeSetting()];
    if (next === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { if (next === 'auto') localStorage.removeItem('ism-theme'); else localStorage.setItem('ism-theme', next); } catch (e) { /* storage unavailable */ }
    updateThemeLabel();
    applyTheme();
  }
  function applyTheme() {
    readColors();
    overlay.base.setUrl(tileUrl('base'));
    overlay.labels.setUrl(tileUrl('labels'));
    stateLayer.setStyle({ color: colors.ink2 });
    for (const id of ['rail', 'interstates']) if (overlay[id]) overlay[id].setStyle({ color: colors.ink });
    overlay.yards.eachLayer((l) => l.setStyle({ color: colors.panel, fillColor: colors.ink }));
    if (selLayer) selLayer.setStyle({ color: colors.accent });
    restyle();
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (currentThemeSetting() === 'auto' && map) applyTheme(); });

  // ------------------------------------------------------------------ URL state
  function writeHash() {
    const p = new URLSearchParams();
    if (isCustom()) p.set('w', encodeWeights());
    const fl = state.filters;
    if (fl.state) p.set('st', fl.state);
    if (fl.interstate) p.set('i', fl.interstate);
    if (fl.rail) p.set('r', fl.rail);
    if (fl.port) p.set('p', fl.port);
    if (fl.rtw) p.set('rtw', '1');
    if (fl.metro) p.set('metro', '1');
    if (state.selected >= 0) p.set('c', D.m.fips[state.selected]);
    history.replaceState(null, '', '#' + p.toString());
  }

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    // Links from before the facility picker was removed carry f=<preset>; use those weights when no w= is given
    const legacy = CFG.presets.find((pr) => pr.id === p.get('f'));
    state.weights = { ...(legacy ? legacy.w : CFG.defaults) };
    const w = p.get('w');
    if (w && w.length === FACTORS.length) {
      FACTORS.forEach((fa, k) => { const v = w[k] === 'a' ? 10 : +w[k]; if (!isNaN(v)) state.weights[fa.id] = v; });
    }
    const fl = state.filters;
    fl.state = D.states[p.get('st')] ? p.get('st') : '';
    fl.interstate = p.get('i') || '';
    fl.rail = p.get('r') || '';
    fl.port = p.get('p') || '';
    fl.rtw = p.get('rtw') === '1';
    fl.metro = p.get('metro') === '1';
    $('#f-state').value = fl.state;
    $('#f-interstate').value = fl.interstate;
    $('#f-rail').value = fl.rail;
    $('#f-port').value = fl.port;
    $('#f-rtw').checked = fl.rtw;
    $('#f-metro').checked = fl.metro;
    const c = p.get('c');
    return c && D.idx.has(c) ? D.idx.get(c) : -1;
  }

  // weights 0..10 are written one character each in the URL; 10 is written as "a"
  function encodeWeights() { return FACTORS.map((f) => { const v = state.weights[f.id] || 0; return v === 10 ? 'a' : String(v); }).join(''); }

  // ------------------------------------------------------------------ boot
  async function boot() {
    try {
      await load();
    } catch (err) {
      const el = $('#loading');
      el.classList.add('error');
      el.textContent = `The county data could not be loaded (${err.message}). Reload the page; if it keeps failing, the data build may still be running.`;
      return;
    }
    computeFactorScores();
    buildControls();
    const sel = readHash();
    FACTORS.forEach((f) => syncSlider(f.id));
    computeScores();
    initMap();
    recompute();
    if (state.filters.state) zoomToState(state.filters.state);
    if (sel >= 0) select(sel, true);
    $('#loading').hidden = true;
  }

  boot();
})();
