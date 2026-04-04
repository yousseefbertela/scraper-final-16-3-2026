const $ = id => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
const fmt = n => Number(n).toLocaleString();
const fmtDate = s => s ? new Date(s).toLocaleString('en-GB', {day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '--';

let state = { view: 'dashboard', currentCar: null, currentGroup: null, overviewData: null };

// ── Navigation ─────────────────────────────────────────────────────────────────
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const v = $('view-' + name);
  if (v) v.classList.add('active');
  const btn = document.querySelector('.nav-item[data-view="' + name + '"]');
  if (btn) btn.classList.add('active');
  state.view = name;
  updateBreadcrumb();
  const scraperNav = $('scraper-nav');
  if (scraperNav) scraperNav.style.display = name === 'target' ? 'block' : 'none';
}
function updateBreadcrumb() {
  const map = { dashboard:'Overview', catalog:'Catalog', search:'Part Search', groups:'Groups', parts:'Parts' };
  let crumb = map[state.view] || state.view;
  if (state.view === 'groups' && state.currentCar) crumb = 'Catalog / ' + state.currentCar.model;
  if (state.view === 'parts' && state.currentGroup) crumb = 'Catalog / ' + (state.currentCar ? state.currentCar.model : '') + ' / ' + state.currentGroup.group_name;
  $('breadcrumb').textContent = crumb;
}

// ── Export ─────────────────────────────────────────────────────────────────────
function exportData(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg> Preparing...';
  fetch('/api/export').then(r => { if (!r.ok) throw new Error('Export failed'); return r.blob(); })
    .then(blob => { const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'bmw-parts-export.json'; document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url); toast('Export downloaded'); })
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

function loadingHTML() { return '<div class="loading"><div class="spinner"></div>Loading...</div>'; }
function errorHTML(msg) { return '<div class="error-box">Error: ' + msg + '</div>'; }

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ── Dashboard ──────────────────────────────────────────────────────────────────
async function loadDashboard(force) {
  if (state.overviewData && !force) { renderDashboard(state.overviewData); return; }
  $('car-cards').innerHTML = loadingHTML();
  try { const data = await api('/api/overview'); state.overviewData = data; renderDashboard(data); }
  catch(e) { $('car-cards').innerHTML = errorHTML(e.message); }
}
function renderDashboard(data) {
  $('s-cars').textContent = fmt(data.total_cars);
  $('s-parts').textContent = fmt(data.total_parts);
  $('s-groups').textContent = fmt(data.total_groups);
  $('s-subgroups').textContent = fmt(data.total_subgroups);
  const grid = $('car-cards'); grid.innerHTML = '';
  data.cars.forEach(car => {
    const card = el('div', 'car-card');
    const prod = car.prod_month ? car.prod_month.substring(0,4) + '/' + car.prod_month.substring(4,6) : '';
    card.innerHTML = '<div class="car-card-model">BMW ' + car.model + '</div><div class="car-card-series">' + (car.series||'') + '</div><div class="car-card-typecode">' + car.type_code.substring(0,4) + '</div><div class="car-card-meta"><span class="tag blue">' + (car.market||'') + '</span><span class="tag">' + (car.engine||'N/A') + '</span><span class="tag">' + (car.body||'') + '</span>' + (prod ? '<span class="tag">'+prod+'</span>' : '') + '</div><div class="car-card-stats"><div class="car-stat"><strong>' + fmt(car.parts_count) + '</strong> parts</div><div class="car-stat"><strong>' + (car.in_progress && !car.parts_count ? car.groups_done+' done' : car.groups_count) + '</strong> groups</div></div><div class="car-status ' + (car.completed ? 'status-done' : 'status-wip') + '"><span class="status-dot"></span>' + (car.completed ? 'Scraped complete' : 'In progress \u2014 group ' + (car.in_progress_group||'?')) + '</div>';
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
    $('groups-meta').innerHTML = '<span class="meta-item">Type Code<span>' + car.type_code + '</span></span><span class="meta-item">Series<span>' + (car.series||'--') + '</span></span><span class="meta-item">Market<span>' + (car.market||'--') + '</span></span><span class="meta-item">Body<span>' + (car.body||'--') + '</span></span><span class="meta-item">Engine<span>' + (car.engine||'--') + '</span></span>' + (prod ? '<span class="meta-item">Production<span>'+prod+'</span></span>' : '');
    if (car.in_progress && (!car.groups || car.groups.length === 0)) {
      $('groups-list').innerHTML = '<div class="wip-notice"><p>Scraping in progress \u2014 currently on group <strong>' + (car.in_progress_group||'?') + '</strong> (' + (car.groups_done||0) + ' group(s) completed so far).</p><p>Parts data will appear here once the first group is saved.</p></div>';
    } else { renderGroups(car.groups, typeCode); }
    updateBreadcrumb();
  } catch(e) { $('groups-list').innerHTML = errorHTML(e.message); }
}
function renderGroups(groups, typeCode) {
  const list = $('groups-list'); list.innerHTML = '';
  groups.forEach(g => {
    const card = el('div', 'group-card');
    card.innerHTML = '<div class="group-id">Group ' + g.group_id + '</div><div class="group-name">' + g.group_name + '</div><div class="group-footer"><span class="group-stat"><strong>' + g.subgroup_count + '</strong> subgroups</span><span class="group-stat"><strong>' + fmt(g.parts_count) + '</strong> parts</span></div>';
    card.addEventListener('click', () => openGroup(typeCode, g.group_id, g.group_name));
    list.appendChild(card);
  });
}

// ── Parts view ─────────────────────────────────────────────────────────────────
async function openGroup(typeCode, groupId, groupName) {
  state.currentGroup = { group_id: groupId, group_name: groupName };
  $('parts-title').textContent = groupName;
  $('parts-content').innerHTML = loadingHTML();
  showView('parts'); updateBreadcrumb();
  try { const data = await api('/api/cars/' + encodeURIComponent(typeCode) + '/groups/' + groupId); renderParts(data); }
  catch(e) { $('parts-content').innerHTML = errorHTML(e.message); }
}
function renderParts(data) {
  const wrap = $('parts-content'); wrap.innerHTML = '';
  if (!data.subgroups || data.subgroups.length === 0) { wrap.innerHTML = '<p class="no-parts">No subgroups found.</p>'; return; }
  data.subgroups.forEach(sg => {
    const block = el('div', 'subgroup-block');
    const parts = sg.parts || [];
    const keys = parts.length ? Object.keys(parts[0]).filter(k => k !== 'ref_no') : [];
    const headerDiv = el('div', 'subgroup-header');
    const imgBlock = sg.diagram_image_url
      ? '<div class="diagram-block"><img class="diagram-thumb" src="' + sg.diagram_image_url + '" alt="diagram" loading="lazy"/><a class="diagram-url" href="' + sg.diagram_image_url + '" target="_blank">' + sg.diagram_image_url.split('/').pop() + '</a></div>'
      : '<div class="diagram-block diagram-missing">No diagram</div>';
    headerDiv.innerHTML = '<div class="subgroup-info"><div class="subgroup-id">' + sg.subgroup_id + '</div><div class="subgroup-name">' + sg.subgroup_name + '</div>' + (sg.scraped_at ? '<div class="subgroup-scraped">Scraped ' + fmtDate(sg.scraped_at) + '</div>' : '') + '</div>' + imgBlock;
    block.appendChild(headerDiv);
    if (parts.length === 0) { block.appendChild(el('p', 'no-parts', 'No parts listed.')); }
    else {
      const tableWrap = el('div', 'parts-table-wrap');
      const table = document.createElement('table');
      const thead = document.createElement('thead'); const headerRow = document.createElement('tr');
      ['Ref', ...keys].forEach(k => { const th = document.createElement('th'); th.textContent = k.replace(/_/g,' '); headerRow.appendChild(th); });
      thead.appendChild(headerRow); table.appendChild(thead);
      const tbody = document.createElement('tbody');
      parts.forEach(p => { const tr = document.createElement('tr'); [p.ref_no||'--', ...keys.map(k => p[k]||'--')].forEach(v => { const td = document.createElement('td'); td.textContent = v; tr.appendChild(td); }); tbody.appendChild(tr); });
      table.appendChild(tbody); tableWrap.appendChild(table); block.appendChild(tableWrap);
    }
    wrap.appendChild(block);
  });
  wrap.querySelectorAll('.diagram-thumb').forEach(img => {
    img.addEventListener('click', () => { $('modal-img').src = img.src; $('modal-overlay').classList.add('open'); });
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
  const listEl = $('catalog-car-list'); listEl.innerHTML = '';
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
    const wrap = $('search-results'); wrap.innerHTML = '';
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

// ── Shared state ───────────────────────────────────────────────────────────────
let customColumns = [];
let confirmCallback = null;
let colDialogCallback = null;
let editCarState = null;   // { scraperId, car, prevValues }

// ── Undo Stack ─────────────────────────────────────────────────────────────────
const undoStack = [];
function pushUndo(action) {
  undoStack.push(action);
  if (undoStack.length > 10) undoStack.shift();
  updateUndoBtn();
}
function updateUndoBtn() {
  const btn = $('undo-btn');
  if (!btn) return;
  btn.disabled = undoStack.length === 0;
  btn.title = undoStack.length > 0 ? 'Undo last ' + undoStack.length + ' action(s)' : 'Nothing to undo';
}
async function performUndo() {
  if (!undoStack.length) return;
  const action = undoStack.pop(); updateUndoBtn();
  try {
    if (action.type === 'remove') {
      await apiMutate('POST', '/api/target-list/scraper/' + action.scraperId + '/cars', { code: action.car.code });
    } else if (action.type === 'add') {
      await apiMutate('DELETE', '/api/target-list/scraper/' + action.scraperId + '/cars/' + action.car.code);
    } else if (action.type === 'custom') {
      await apiMutate('PATCH', '/api/target-list/scraper/' + action.scraperId + '/cars/' + action.car.code + '/custom', { colId: action.colId, value: action.oldValue });
    } else if (action.type === 'edit') {
      await apiMutate('PATCH', '/api/target-list/scraper/' + action.scraperId + '/cars/' + action.car.code, action.prev);
    } else if (action.type === 'move') {
      await apiMutate('POST', '/api/target-list/move-car', { fromScraperId: action.toScraperId, carCode: action.car.code, toScraperId: action.fromScraperId });
    }
    toast('Undone: ' + action.desc);
    loadTargetList();
  } catch(e) { undoStack.push(action); updateUndoBtn(); toast('Undo failed: ' + e.message); }
}

// ── Edit Car Dialog ────────────────────────────────────────────────────────────
function showEditCar(scraperId, car) {
  editCarState = { scraperId, car };
  $('edit-car-code').value    = car.code   || '';
  $('edit-car-model').value   = car.model  || '';
  $('edit-car-series').value  = car.series || '';
  $('edit-car-market').value  = car.market || '';
  $('edit-car-engine').value  = car.engine || '';
  $('edit-car-prod').value    = car.prod_month || '';
  $('edit-car-overlay').classList.add('open');
  setTimeout(() => $('edit-car-model').focus(), 50);
}

async function saveEditCar() {
  if (!editCarState) return;
  const { scraperId, car } = editCarState;
  const body = {
    model:      $('edit-car-model').value.trim(),
    series:     $('edit-car-series').value.trim(),
    market:     $('edit-car-market').value.trim(),
    engine:     $('edit-car-engine').value.trim(),
    prod_month: $('edit-car-prod').value.trim(),
  };
  const saveBtn = $('edit-car-save');
  saveBtn.disabled = true; saveBtn.textContent = 'Saving...';
  try {
    const result = await apiMutate('PATCH', '/api/target-list/scraper/' + scraperId + '/cars/' + car.code, body);
    pushUndo({ type: 'edit', scraperId, car: { code: car.code }, prev: result.prev, desc: 'edit ' + car.code });
    $('edit-car-overlay').classList.remove('open');
    editCarState = null;
    toast('Saved ' + car.code);
    loadTargetList();
  } catch(e) { toast('Save failed: ' + e.message); }
  finally { saveBtn.disabled = false; saveBtn.textContent = 'Save Changes'; }
}

// ── Move Car Dropdown ──────────────────────────────────────────────────────────
let moveDropState = null;

const MOVE_GROUPS = [
  { id: 0, label: 'Previously Scraped' },
  { id: 1, label: 'Scraper 1' }, { id: 2, label: 'Scraper 2' },
  { id: 3, label: 'Scraper 3' }, { id: 4, label: 'Scraper 4' },
  { id: 5, label: 'Scraper 5' }, { id: 6, label: 'Scraper 6' },
  { id: 7, label: 'Scraper 7' }, { id: 8, label: 'Scraper 8' },
  { id: 9, label: 'Navigation Problem' },
];

function showMoveDropdown(btn, scraperId, code) {
  hideMoveDropdown();
  moveDropState = { scraperId, code };
  const dd = $('move-dropdown');
  dd.innerHTML = MOVE_GROUPS.map(function(g) {
    const isCurrent = g.id === scraperId;
    return '<div class="move-option' + (isCurrent ? ' current' : '') + '" data-to="' + g.id + '">' +
      g.label + (isCurrent ? ' <span class="move-current-tag">current</span>' : '') + '</div>';
  }).join('');
  const rect = btn.getBoundingClientRect();
  dd.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
  dd.style.left = Math.max(4, rect.left + window.scrollX - 80) + 'px';
  dd.classList.add('open');
  dd.querySelectorAll('.move-option:not(.current)').forEach(function(opt) {
    opt.addEventListener('click', function() {
      doMoveCar(scraperId, code, parseInt(opt.dataset.to));
    });
  });
}

function hideMoveDropdown() {
  $('move-dropdown').classList.remove('open');
  $('move-dropdown').innerHTML = '';
  moveDropState = null;
}

async function doMoveCar(fromId, code, toId) {
  hideMoveDropdown();
  const toLabel = (MOVE_GROUPS.find(function(g) { return g.id === toId; }) || {}).label || ('Scraper ' + toId);
  try {
    const result = await apiMutate('POST', '/api/target-list/move-car', { fromScraperId: fromId, carCode: code, toScraperId: toId });
    pushUndo({ type: 'move', fromScraperId: fromId, toScraperId: toId, car: result.car, desc: 'move ' + code + ' \u2192 ' + toLabel });
    toast(code + ' moved to ' + toLabel);
    loadTargetList();
  } catch(e) { toast('Move failed: ' + e.message); }
}

async function apiMutate(method, path, body) {
  const opts = { method };
  if (body !== undefined) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); let msg = t; try { msg = JSON.parse(t).error || t; } catch(_){} throw new Error(msg); }
  return r.json();
}
function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Confirm Dialog ─────────────────────────────────────────────────────────────
function showConfirm(titleText, bodyText, onOk) {
  $('confirm-title-el').textContent = titleText;
  $('confirm-car-text').textContent = bodyText;
  $('confirm-overlay').classList.add('open');
  confirmCallback = onOk;
}

// ── Column Dialog ──────────────────────────────────────────────────────────────
function showColDialog(title, currentValue, onOk) {
  $('col-dialog-title').textContent = title;
  $('col-dialog-input').value = currentValue || '';
  $('col-dialog-overlay').classList.add('open');
  setTimeout(() => $('col-dialog-input').focus(), 50);
  colDialogCallback = onOk;
}

// ── Column CRUD ────────────────────────────────────────────────────────────────
function addColumn() {
  showColDialog('New Column', '', async function(title) {
    try { await apiMutate('POST', '/api/columns', { title }); toast('Column "' + title + '" added'); loadTargetList(); }
    catch(e) { toast('Failed: ' + e.message); }
  });
}
function renameColumn(colId, currentTitle) {
  showColDialog('Rename Column', currentTitle, async function(newTitle) {
    try { await apiMutate('PATCH', '/api/columns/' + colId, { title: newTitle }); toast('Renamed to "' + newTitle + '"'); loadTargetList(); }
    catch(e) { toast('Failed: ' + e.message); }
  });
}
function deleteColumn(colId, colTitle) {
  showConfirm('Delete Column?', '"' + colTitle + '" and all its data will be removed', async function() {
    $('confirm-overlay').classList.remove('open');
    try { await apiMutate('DELETE', '/api/columns/' + colId); toast('Column deleted'); loadTargetList(); }
    catch(e) { toast('Failed: ' + e.message); }
  });
}

// ── Target List ────────────────────────────────────────────────────────────────
async function loadTargetList() {
  const viewEl = $('view-target');
  const savedScroll = viewEl ? viewEl.scrollTop : 0;
  $('target-content').innerHTML = loadingHTML();
  try {
    const [data, colData] = await Promise.all([api('/api/target-list'), api('/api/columns')]);
    customColumns = colData.columns || [];
    renderTargetList(data);
    if (viewEl) viewEl.scrollTop = savedScroll;
  } catch(e) { $('target-content').innerHTML = errorHTML(e.message); }
}

function renderTargetList(data) {
  window._lastTargetData = data;
  const scrapers = data.scrapers || [];
  const totalAll   = scrapers.reduce((s, sc) => s + sc.total,   0);
  const scrapedAll = scrapers.reduce((s, sc) => s + sc.scraped, 0);
  const remaining  = totalAll - scrapedAll;

  $('target-stat-grid').innerHTML =
    '<div class="stat-card"><div class="stat-value">' + totalAll + '</div><div class="stat-label">Total Cars</div></div>' +
    '<div class="stat-card"><div class="stat-value" style="color:var(--green)">' + scrapedAll + '</div><div class="stat-label">Scraped</div></div>' +
    '<div class="stat-card"><div class="stat-value" style="color:var(--yellow)">' + remaining + '</div><div class="stat-label">Remaining</div></div>' +
    '<div class="stat-card"><div class="stat-value">' + (totalAll ? Math.round((scrapedAll/totalAll)*100) : 0) + '%</div><div class="stat-label">Progress</div></div>';

  // Section title + undo
  const st = $('target-section-title');
  st.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin:24px 0 12px;';
  const undoBtn = el('button', 'undo-btn');
  undoBtn.id = 'undo-btn';
  undoBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7v6h6"/><path d="M3 13A9 9 0 1 0 5.6 5.6L3 8"/></svg> Undo';
  undoBtn.disabled = undoStack.length === 0;
  undoBtn.addEventListener('click', performUndo);
  st.innerHTML = '<span>All Scrapers (' + scrapers.length + ')</span>';
  st.appendChild(undoBtn);

  const wrap = $('target-content'); wrap.innerHTML = '';

  // Build sidebar scraper navigation
  const scraperNav = $('scraper-nav');
  if (scraperNav) {
    scraperNav.innerHTML = '<div class="scraper-nav-title">Jump to</div>' +
      scrapers.map(function(sc) {
        const isDone = sc.scraped === sc.total && sc.total > 0;
        const label = sc.label || ('Scraper ' + sc.scraper_id);
        return '<button class="scraper-nav-btn" data-section="scraper-section-' + sc.scraper_id + '">' +
          '<span class="scraper-nav-dot" style="background:' + (isDone ? 'var(--green)' : sc.scraped > 0 ? 'var(--yellow)' : 'var(--text3)') + '"></span>' +
          '<span class="scraper-nav-label">' + escHtml(label) + '</span>' +
          '<span class="scraper-nav-count">' + sc.scraped + '/' + sc.total + '</span>' +
          '</button>';
      }).join('');
    scraperNav.querySelectorAll('.scraper-nav-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        const target = document.getElementById(btn.dataset.section);
        const viewEl = $('view-target');
        if (target && viewEl) viewEl.scrollTo({ top: target.offsetTop - 16, behavior: 'smooth' });
      });
    });
  }

  scrapers.forEach(function(sc) {
    const editable = sc.scraper_id > 0;
    const section = el('div', 'scraper-section');
    section.id = 'scraper-section-' + sc.scraper_id;
    const header = el('div', 'scraper-section-header');
    const isDone = sc.scraped === sc.total && sc.total > 0;

    // Title span
    const titleEl = el('span', 'scraper-section-title', sc.label || 'Scraper ' + sc.scraper_id);
    // Stats span
    const statsEl = el('span', 'scraper-section-stats');
    statsEl.innerHTML = '<span style="color:var(--green)">' + sc.scraped + ' scraped</span>&nbsp;/&nbsp;' + sc.total + ' total<span class="status-badge ' + (isDone ? 'badge-done' : 'badge-pending') + '" style="margin-left:10px">' + (isDone ? '\u2713 Complete' : sc.scraped + ' / ' + sc.total) + '</span>';

    header.appendChild(titleEl);
    header.appendChild(statsEl);

    if (editable) {
      const addColBtn = el('button', 'add-col-btn', '+ Column');
      addColBtn.addEventListener('click', addColumn);
      header.appendChild(addColBtn);
    }

    section.appendChild(header);
    section.appendChild(buildTable(sc.cars, sc.scraper_id, editable));
    if (editable) section.appendChild(buildAddCarRow(sc.scraper_id));
    wrap.appendChild(section);
  });

  // Total row
  const summary = el('div', 'scraper-section scraper-section-summary');
  summary.innerHTML = '<div class="scraper-section-header"><span class="scraper-section-title">Total</span><span class="scraper-section-stats"><span style="color:var(--green)">' + scrapedAll + ' scraped</span>&nbsp;/&nbsp;' + totalAll + ' total &nbsp;\u00b7&nbsp;<span style="color:var(--yellow)">' + remaining + ' remaining</span>&nbsp;\u00b7&nbsp;<strong>' + (totalAll ? Math.round((scrapedAll/totalAll)*100) : 0) + '%</strong></span></div>';
  wrap.appendChild(summary);
}

function buildTable(cars, scraperId, editable) {
  const tableWrap = el('div', 'target-table-wrap');
  const table = document.createElement('table');
  table.className = 'target-table';
  const thead = document.createElement('thead');

  let thHtml = '<tr><th>#</th><th>Code</th><th>Model</th><th>Series</th><th>Market</th><th>Engine</th>';
  customColumns.forEach(function(col) {
    thHtml += '<th class="custom-col-th">' +
      '<div class="col-th-inner">' +
        '<span class="col-title-text">' + escHtml(col.title) + '</span>' +
        '<div class="col-th-btns">' +
          '<button class="col-rename-btn" data-col-id="' + col.id + '" data-col-title="' + escHtml(col.title) + '" title="Rename">\u270e</button>' +
          '<button class="col-delete-btn" data-col-id="' + col.id + '" data-col-title="' + escHtml(col.title) + '" title="Delete">\u00d7</button>' +
        '</div>' +
      '</div></th>';
  });
  thHtml += '<th>Status</th>' + (editable ? '<th style="width:60px"></th>' : '') + '</tr>';
  thead.innerHTML = thHtml;
  table.appendChild(thead);

  thead.querySelectorAll('.col-rename-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { renameColumn(btn.dataset.colId, btn.dataset.colTitle); });
  });
  thead.querySelectorAll('.col-delete-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { deleteColumn(btn.dataset.colId, btn.dataset.colTitle); });
  });

  const tbody = document.createElement('tbody');
  cars.forEach(function(c) {
    const tr = document.createElement('tr');
    tr.className = c.scraped ? 'row-done' : '';
    let customTds = '';
    customColumns.forEach(function(col) {
      const val = escHtml((c.custom || {})[col.id] || '');
      if (c.scraped) {
        customTds += '<td style="color:var(--text2);font-size:12px">' + val + '</td>';
      } else {
        customTds += '<td><input class="custom-col-input" type="text" placeholder="..." value="' + val + '" data-code="' + c.code + '" data-scraper="' + scraperId + '" data-col-id="' + col.id + '"/></td>';
      }
    });
    const actionTd = editable
      ? (c.scraped
          ? '<td class="action-cell"><button class="move-btn" data-code="' + c.code + '" data-scraper="' + scraperId + '" title="Move to scraper">\u21c4</button></td>'
          : '<td class="action-cell"><button class="move-btn" data-code="' + c.code + '" data-scraper="' + scraperId + '" title="Move to scraper">\u21c4</button><button class="edit-btn" data-code="' + c.code + '" data-scraper="' + scraperId + '" title="Edit car">\u270e</button><button class="remove-btn" data-code="' + c.code + '" data-scraper="' + scraperId + '" data-model="' + escHtml(c.model) + '" title="Remove car">\u2715</button></td>')
      : '';
    tr.innerHTML =
      '<td style="color:var(--text3);font-size:12px">' + c.num + '</td>' +
      '<td><code class="type-prefix">' + c.code + '</code></td>' +
      '<td><strong>' + c.model + '</strong></td>' +
      '<td style="color:var(--text3);font-size:12px">' + (c.series||'--') + '</td>' +
      '<td>' + (c.market||'--') + '</td>' +
      '<td>' + (c.engine||'--') + '</td>' +
      customTds +
      '<td><span class="status-badge ' + (c.scraped ? 'badge-done' : 'badge-pending') + '">' + (c.scraped ? '\u2713 Scraped' : 'Pending') + '</span></td>' +
      actionTd;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  tableWrap.appendChild(table);

  // Wire custom inputs
  tableWrap.querySelectorAll('.custom-col-input').forEach(function(input) {
    var saved = input.value;
    input.addEventListener('focus', function() { saved = input.value; });
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
      if (e.key === 'Escape') { input.value = saved; input.blur(); }
    });
    input.addEventListener('blur', async function() {
      const nv = input.value;
      if (nv === saved) return;
      try {
        await apiMutate('PATCH', '/api/target-list/scraper/' + input.dataset.scraper + '/cars/' + input.dataset.code + '/custom', { colId: input.dataset.colId, value: nv });
        pushUndo({ type: 'custom', scraperId: parseInt(input.dataset.scraper), car: { code: input.dataset.code }, colId: input.dataset.colId, oldValue: saved, desc: 'edit ' + input.dataset.code });
        saved = nv; toast('Saved');
      } catch(e) { toast('Save failed: ' + e.message); input.value = saved; }
    });
  });

  // Wire move buttons
  tableWrap.querySelectorAll('.move-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      const scraperId = parseInt(btn.dataset.scraper);
      const code = btn.dataset.code;
      if (moveDropState && moveDropState.code === code) { hideMoveDropdown(); return; }
      showMoveDropdown(btn, scraperId, code);
    });
  });

  // Wire edit buttons
  tableWrap.querySelectorAll('.edit-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      const scraperId = parseInt(btn.dataset.scraper);
      const code = btn.dataset.code;
      // Find the car data from the current list
      const sc = (window._lastTargetData || { scrapers: [] }).scrapers.find(s => s.scraper_id === scraperId);
      const car = sc ? sc.cars.find(c => c.code === code) : null;
      if (car) showEditCar(scraperId, car);
    });
  });

  // Wire remove buttons
  tableWrap.querySelectorAll('.remove-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      showConfirm('Remove Car?', btn.dataset.code + ' \u2014 ' + btn.dataset.model, async function() {
        $('confirm-overlay').classList.remove('open');
        try {
          const result = await apiMutate('DELETE', '/api/target-list/scraper/' + btn.dataset.scraper + '/cars/' + btn.dataset.code);
          pushUndo({ type: 'remove', scraperId: parseInt(btn.dataset.scraper), car: result.removed, desc: 'remove ' + btn.dataset.code });
          toast('Removed ' + btn.dataset.code); loadTargetList();
        } catch(e) { toast('Remove failed: ' + e.message); }
      });
    });
  });

  return tableWrap;
}

function buildAddCarRow(scraperId) {
  const row = el('div', 'add-car-row');
  const input = document.createElement('input');
  input.className = 'add-car-input'; input.type = 'text'; input.placeholder = 'e.g. NU16'; input.maxLength = 4; input.spellcheck = false;
  const btn = el('button', 'add-car-btn', '+ Add Car');
  async function doAdd() {
    const code = input.value.trim().toUpperCase();
    if (code.length !== 4) { toast('Enter a valid 4-char code'); input.focus(); return; }
    btn.disabled = true; btn.textContent = 'Adding...';
    try {
      const result = await apiMutate('POST', '/api/target-list/scraper/' + scraperId + '/cars', { code });
      pushUndo({ type: 'add', scraperId, car: result.car, desc: 'add ' + code });
      toast('Added ' + code + (result.car.model !== code ? ' \u2014 ' + result.car.model : ''));
      input.value = ''; loadTargetList();
    } catch(e) { toast('Add failed: ' + e.message); }
    finally { btn.disabled = false; btn.textContent = '+ Add Car'; }
  }
  btn.addEventListener('click', doAdd);
  input.addEventListener('keydown', function(e) { if (e.key === 'Enter') doAdd(); });
  row.appendChild(input); row.appendChild(btn);
  return row;
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
  const btn = $('refresh-btn'); btn.classList.add('spinning'); state.overviewData = null;
  try { await loadDashboard(true); toast('Refreshed'); } catch(e) { toast('Refresh failed'); }
  btn.classList.remove('spinning');
});
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    const v = btn.dataset.view; showView(v);
    if (v === 'dashboard') loadDashboard();
    if (v === 'catalog') loadCatalog();
    if (v === 'target') loadTargetList();
  });
});
$('back-to-catalog').addEventListener('click', () => { showView('catalog'); loadCatalog(); });
$('back-to-groups').addEventListener('click', () => { if (state.currentCar) openCar(state.currentCar.type_code); else showView('groups'); });

// ── Init ───────────────────────────────────────────────────────────────────────
$('confirm-cancel').addEventListener('click', function() { $('confirm-overlay').classList.remove('open'); confirmCallback = null; });
$('confirm-ok').addEventListener('click', function() { if (confirmCallback) confirmCallback(); confirmCallback = null; });
$('col-dialog-cancel').addEventListener('click', function() { $('col-dialog-overlay').classList.remove('open'); colDialogCallback = null; });
$('col-dialog-ok').addEventListener('click', function() {
  const val = $('col-dialog-input').value.trim();
  if (!val) { $('col-dialog-input').focus(); return; }
  $('col-dialog-overlay').classList.remove('open');
  if (colDialogCallback) colDialogCallback(val);
  colDialogCallback = null;
});
$('col-dialog-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') $('col-dialog-ok').click();
  if (e.key === 'Escape') $('col-dialog-cancel').click();
});
$('edit-car-cancel').addEventListener('click', function() { $('edit-car-overlay').classList.remove('open'); editCarState = null; });
$('edit-car-save').addEventListener('click', saveEditCar);
['edit-car-model','edit-car-series','edit-car-market','edit-car-engine','edit-car-prod'].forEach(function(id) {
  $(id).addEventListener('keydown', function(e) {
    if (e.key === 'Enter') saveEditCar();
    if (e.key === 'Escape') $('edit-car-cancel').click();
  });
});
document.addEventListener('click', function(e) {
  if (moveDropState && !e.target.closest('#move-dropdown') && !e.target.classList.contains('move-btn')) {
    hideMoveDropdown();
  }
});
initModal();
initSearch();
loadDashboard();
