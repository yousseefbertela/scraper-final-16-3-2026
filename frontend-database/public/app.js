const $ = id => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html) e.innerHTML = html; return e; };
const fmt = n => Number(n).toLocaleString();
const fmtDate = s => s ? new Date(s).toLocaleString('en-GB', {day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '--';

let state = { view: 'dashboard', currentCar: null, currentGroup: null, overviewData: null };

// ── Navigation ─────────────────────────────────────────────────────────────────
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const v = $('view-' + name);
  if (v) v.classList.add('active');
  const btn = document.querySelector(`.nav-item[data-view="${name}"]`);
  if (btn) btn.classList.add('active');
  state.view = name;
  updateBreadcrumb();
}

function updateBreadcrumb() {
  const map = { dashboard: 'Overview', catalog: 'Catalog', search: 'Part Search', progress: 'Scrape Log', groups: 'Groups', parts: 'Parts' };
  let crumb = map[state.view] || state.view;
  if (state.view === 'groups' && state.currentCar) crumb = `Catalog / ${state.currentCar.model}`;
  if (state.view === 'parts' && state.currentGroup) crumb = `Catalog / ${state.currentCar ? state.currentCar.model : ''} / ${state.currentGroup.group_name}`;
  $('breadcrumb').textContent = crumb;
}

// ── Export ─────────────────────────────────────────────────────────────────────
function exportData(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg> Preparing…';
  fetch('/api/export')
    .then(r => {
      if (!r.ok) throw new Error('Export failed');
      return r.blob();
    })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'bmw-parts-export.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast('Export downloaded');
    })
    .catch(e => toast('Export failed: ' + e.message))
    .finally(() => { btn.disabled = false; btn.innerHTML = orig; });
}

// ── Toast ──────────────────────────────────────────────────────────────────────
let toastTimer;
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
}

// ── Loading helpers ────────────────────────────────────────────────────────────
function loadingHTML() { return '<div class="loading"><div class="spinner"></div>Loading...</div>'; }
function errorHTML(msg) { return `<div class="error-box">Error: ${msg}</div>`; }

// ── API ────────────────────────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ── Dashboard ──────────────────────────────────────────────────────────────────
async function loadDashboard(force) {
  if (state.overviewData && !force) { renderDashboard(state.overviewData); return; }
  $('car-cards').innerHTML = loadingHTML();
  try {
    const data = await api('/api/overview');
    state.overviewData = data;
    renderDashboard(data);
  } catch(e) { $('car-cards').innerHTML = errorHTML(e.message); }
}

function renderDashboard(data) {
  $('s-cars').textContent = fmt(data.total_cars);
  $('s-parts').textContent = fmt(data.total_parts);
  $('s-groups').textContent = fmt(data.total_groups);
  $('s-subgroups').textContent = fmt(data.total_subgroups);

  const grid = $('car-cards');
  grid.innerHTML = '';
  data.cars.forEach(car => {
    const card = el('div', 'car-card');
    const prod = car.prod_month ? car.prod_month.substring(0,4) + '/' + car.prod_month.substring(4,6) : '';
    card.innerHTML = `
      <div class="car-card-model">BMW ${car.model}</div>
      <div class="car-card-series">${car.series || ''}</div>
      <div class="car-card-typecode">${car.type_code.substring(0,4)}</div>
      <div class="car-card-meta">
        <span class="tag blue">${car.market || ''}</span>
        <span class="tag">${car.engine || 'N/A'}</span>
        <span class="tag">${car.body || ''}</span>
        ${prod ? '<span class="tag">' + prod + '</span>' : ''}
      </div>
      <div class="car-card-stats">
        <div class="car-stat"><strong>${fmt(car.parts_count)}</strong> parts</div>
        <div class="car-stat"><strong>${car.groups_count}</strong> groups</div>
      </div>
      <div class="car-status ${car.completed ? 'status-done' : 'status-wip'}">
        <span class="status-dot"></span>
        ${car.completed ? 'Scraped complete' : `In progress — group ${car.in_progress_group || '?'}`}
      </div>`;
    card.addEventListener('click', () => openCar(car.type_code));
    grid.appendChild(card);
  });
}

// ── Car groups view ────────────────────────────────────────────────────────────
async function openCar(typeCode) {
  state.currentCar = null;
  $('groups-title').textContent = 'Loading...';
  $('groups-meta').innerHTML = '';
  $('groups-list').innerHTML = loadingHTML();
  showView('groups');
  try {
    const car = await api('/api/cars/' + encodeURIComponent(typeCode));
    state.currentCar = car;
    const prod = car.prod_month ? car.prod_month.substring(0,4) + '/' + car.prod_month.substring(4,6) : '';
    $('groups-title').textContent = 'BMW ' + car.model;
    $('groups-meta').innerHTML = `
      <span class="meta-item">Type Code<span>${car.type_code}</span></span>
      <span class="meta-item">Series<span>${car.series || '--'}</span></span>
      <span class="meta-item">Market<span>${car.market || '--'}</span></span>
      <span class="meta-item">Body<span>${car.body || '--'}</span></span>
      <span class="meta-item">Engine<span>${car.engine || '--'}</span></span>
      ${prod ? '<span class="meta-item">Production<span>' + prod + '</span></span>' : ''}`;
    renderGroups(car.groups, typeCode);
    updateBreadcrumb();
  } catch(e) {
    $('groups-list').innerHTML = errorHTML(e.message);
  }
}

function renderGroups(groups, typeCode) {
  const list = $('groups-list');
  list.innerHTML = '';
  groups.forEach(g => {
    const card = el('div', 'group-card');
    card.innerHTML = `
      <div class="group-id">Group ${g.group_id}</div>
      <div class="group-name">${g.group_name}</div>
      <div class="group-footer">
        <span class="group-stat"><strong>${g.subgroup_count}</strong> subgroups</span>
        <span class="group-stat"><strong>${fmt(g.parts_count)}</strong> parts</span>
      </div>`;
    card.addEventListener('click', () => openGroup(typeCode, g.group_id, g.group_name));
    list.appendChild(card);
  });
}

// ── Parts view ─────────────────────────────────────────────────────────────────
async function openGroup(typeCode, groupId, groupName) {
  state.currentGroup = { group_id: groupId, group_name: groupName };
  $('parts-title').textContent = groupName;
  $('parts-content').innerHTML = loadingHTML();
  showView('parts');
  updateBreadcrumb();
  try {
    const data = await api('/api/cars/' + encodeURIComponent(typeCode) + '/groups/' + groupId);
    renderParts(data);
  } catch(e) {
    $('parts-content').innerHTML = errorHTML(e.message);
  }
}

function renderParts(data) {
  const wrap = $('parts-content');
  wrap.innerHTML = '';
  if (!data.subgroups || data.subgroups.length === 0) {
    wrap.innerHTML = '<p class="no-parts">No subgroups found.</p>'; return;
  }
  data.subgroups.forEach(sg => {
    const block = el('div', 'subgroup-block');
    const parts = sg.parts || [];
    const keys = parts.length ? Object.keys(parts[0]).filter(k => k !== 'ref_no') : [];
    const headerDiv = el('div', 'subgroup-header');
    const imgBlock = sg.diagram_image_url
      ? '<div class="diagram-block"><img class="diagram-thumb" src="' + sg.diagram_image_url + '" alt="diagram" loading="lazy"/><a class="diagram-url" href="' + sg.diagram_image_url + '" target="_blank">' + sg.diagram_image_url.split('/').pop() + '</a></div>'
      : '<div class="diagram-block diagram-missing">No diagram</div>';
    headerDiv.innerHTML =
      '<div class="subgroup-info">' +
        '<div class="subgroup-id">' + sg.subgroup_id + '</div>' +
        '<div class="subgroup-name">' + sg.subgroup_name + '</div>' +
        (sg.scraped_at ? '<div class="subgroup-scraped">Scraped ' + fmtDate(sg.scraped_at) + '</div>' : '') +
      '</div>' + imgBlock;
    block.appendChild(headerDiv);
    if (parts.length === 0) {
      block.appendChild(el('p', 'no-parts', 'No parts listed.'));
    } else {
      const tableWrap = el('div', 'parts-table-wrap');
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      ['Ref', ...keys].forEach(k => {
        const th = document.createElement('th');
        th.textContent = k.replace(/_/g,' ');
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      table.appendChild(thead);
      const tbody = document.createElement('tbody');
      parts.forEach(p => {
        const tr = document.createElement('tr');
        [p.ref_no || '--', ...keys.map(k => p[k] || '--')].forEach(v => {
          const td = document.createElement('td');
          td.textContent = v;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      tableWrap.appendChild(table);
      block.appendChild(tableWrap);
    }
    wrap.appendChild(block);
  });
  // Diagram click to enlarge
  wrap.querySelectorAll('.diagram-thumb').forEach(img => {
    img.addEventListener('click', () => {
      $('modal-img').src = img.src;
      $('modal-overlay').classList.add('open');
    });
  });
}

// ── Catalog view ───────────────────────────────────────────────────────────────
function loadCatalog() {
  const listEl = $('catalog-car-list');
  if (state.overviewData) { renderCatalogList(state.overviewData.cars); return; }
  listEl.innerHTML = loadingHTML();
  api('/api/overview').then(data => { state.overviewData = data; renderCatalogList(data.cars); }).catch(e => { listEl.innerHTML = errorHTML(e.message); });
}
function renderCatalogList(cars) {
  const listEl = $('catalog-car-list');
  listEl.innerHTML = '';
  cars.forEach(car => {
    const card = el('div', 'car-card');
    card.innerHTML = '<div class="car-card-model">BMW ' + car.model + '</div><div class="car-card-series">' + (car.series||'') + '</div><div class="car-card-meta"><span class="tag blue">' + (car.market||'') + '</span><span class="tag">' + (car.engine||'N/A') + '</span></div><div class="car-card-stats"><div class="car-stat"><strong>' + fmt(car.parts_count) + '</strong> parts</div></div>';
    card.addEventListener('click', () => openCar(car.type_code));
    listEl.appendChild(card);
  });
}

// ── Search ─────────────────────────────────────────────────────────────────────
let searchTimer;
function initSearch() {
  const input = $('search-input');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) { $('search-results').innerHTML = '<p class="search-hint">Type at least 2 characters...</p>'; return; }
    $('search-results').innerHTML = loadingHTML();
    searchTimer = setTimeout(() => runSearch(q), 400);
  });
  $('search-results').innerHTML = '<p class="search-hint">Search by part number, description, reference...</p>';
}
async function runSearch(q) {
  try {
    const data = await api('/api/search?q=' + encodeURIComponent(q));
    const wrap = $('search-results');
    wrap.innerHTML = '';
    if (data.truncated) wrap.appendChild(el('p', 'truncated-note', 'Showing first 100 results.'));
    if (!data.results.length) { wrap.innerHTML += '<p class="search-hint">No parts found.</p>'; return; }
    data.results.forEach(r => {
      const div = el('div', 'search-result');
      const partKeys = Object.entries(r.part||{}).filter(([k]) => k !== 'ref_no');
      div.innerHTML = '<div class="sr-header"><span class="sr-model">BMW ' + r.model + '</span><span class="sr-path">' + r.group_name + ' / ' + r.subgroup_name + '</span></div><div class="sr-part"><span class="sr-field"><span class="key">Ref:</span><span class="val">' + (r.part.ref_no||'--') + '</span></span>' + partKeys.slice(0,5).map(([k,v]) => '<span class="sr-field"><span class="key">' + k.replace(/_/g,' ') + ':</span><span class="val">' + v + '</span></span>').join('') + '</div>';
      wrap.appendChild(div);
    });
  } catch(e) { $('search-results').innerHTML = errorHTML(e.message); }
}

// ── Target List ────────────────────────────────────────────────────────────────
async function loadTargetList() {
  $('target-content').innerHTML = loadingHTML();
  try {
    const data = await api('/api/target-list');
    renderTargetList(data);
  } catch(e) { $('target-content').innerHTML = errorHTML(e.message); }
}

function renderTargetList(data) {
  const stats = $('target-stat-grid');
  const remaining = data.total - data.scraped;
  stats.innerHTML = `
    <div class="stat-card"><div class="stat-value">${data.total}</div><div class="stat-label">Type Codes</div></div>
    <div class="stat-card"><div class="stat-value" style="color:var(--green)">${data.scraped}</div><div class="stat-label">Scraped</div></div>
    <div class="stat-card"><div class="stat-value" style="color:var(--yellow)">${remaining}</div><div class="stat-label">Remaining</div></div>
    <div class="stat-card"><div class="stat-value">${data.total ? Math.round((data.scraped/data.total)*100) : 0}%</div><div class="stat-label">Progress</div></div>`;

  $('target-section-title').textContent = `All Type Codes (${data.total})`;

  const wrap = $('target-content');
  wrap.innerHTML = '';
  const tableWrap = el('div', 'target-table-wrap');
  const table = document.createElement('table');
  table.className = 'target-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>#</th><th>Prefix</th><th>Model</th><th>Series</th><th>Body</th><th>Engine</th><th>Variants</th><th>Status</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  data.groups.forEach((g, i) => {
    const tr = document.createElement('tr');
    tr.className = g.scraped ? 'row-done' : '';
    tr.innerHTML = `
      <td style="color:var(--text3);font-size:12px">${i+1}</td>
      <td><code class="type-prefix">${g.prefix}</code></td>
      <td><strong>BMW ${g.model}</strong></td>
      <td style="color:var(--text3);font-size:12px">${g.series_label}</td>
      <td style="color:var(--text3)">${g.body || '--'}</td>
      <td>${g.engine || '--'}</td>
      <td style="color:var(--text3)">${g.variant_count}</td>
      <td><span class="status-badge ${g.scraped ? 'badge-done' : 'badge-pending'}">${g.scraped ? '✓ Scraped' : 'Pending'}</span></td>`;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  wrap.appendChild(tableWrap);
}

// ── Modal ──────────────────────────────────────────────────────────────────────
function initModal() {
  const overlay = document.createElement('div');
  overlay.id = 'modal-overlay';
  overlay.innerHTML = '<button id="modal-close">Close</button><img id="modal-img" src="" alt="diagram"/>';
  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay || e.target.id === 'modal-close') overlay.classList.remove('open'); });
}

// ── Wiring ─────────────────────────────────────────────────────────────────────
$('refresh-btn').addEventListener('click', async () => {
  const btn = $('refresh-btn');
  btn.classList.add('spinning');
  state.overviewData = null;
  try { await loadDashboard(true); toast('Refreshed'); } catch(e) { toast('Refresh failed'); }
  btn.classList.remove('spinning');
});
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    const v = btn.dataset.view;
    showView(v);
    if (v === 'dashboard') loadDashboard();
    if (v === 'catalog') loadCatalog();
    if (v === 'target') loadTargetList();
  });
});
$('back-to-catalog').addEventListener('click', () => { showView('catalog'); loadCatalog(); });
$('back-to-groups').addEventListener('click', () => { if (state.currentCar) openCar(state.currentCar.type_code); else showView('groups'); });

// ── Init ───────────────────────────────────────────────────────────────────────
initModal();
initSearch();
loadDashboard();
