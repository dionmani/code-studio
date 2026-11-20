/* Code Studio IDE client. Vanilla JS + Bootstrap 5 + Monaco (AMD, vendored).
 * The browser is a client only: every path goes to the server as a project-
 * relative string and is validated there. DOM is built with textContent. */
(() => {
  'use strict';

  // ---------- helpers ----------
  const $ = (s, r = document) => r.querySelector(s);
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const IS_STAFF = document.body.dataset.staff === '1';
  const MONACO_BASE = document.body.dataset.monaco;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };

  class ApiError extends Error {
    constructor(status, data) { super((data && data.error) || `HTTP ${status}`); this.status = status; this.data = data || {}; }
  }

  async function api(method, url, body) {
    const opts = { method, headers: { 'X-CSRFToken': CSRF, 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let resp;
    try { resp = await fetch(url, opts); } catch (e) { throw new ApiError(0, { error: 'Network error: IDE server unreachable' }); }
    if (resp.status === 401) { window.location.href = '/login/?next=/'; throw new ApiError(401, { error: 'Signed out' }); }
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) throw new ApiError(resp.status, data);
    return data;
  }

  function toast(message, kind = 'info', action = null) {
    const cls = { info: 'text-bg-secondary', ok: 'text-bg-success', warn: 'text-bg-warning', err: 'text-bg-danger' }[kind] || 'text-bg-secondary';
    const t = el('div', { class: `toast align-items-center border-0 ${cls}`, role: 'status' },
      el('div', { class: 'd-flex' }, el('div', { class: 'toast-body small', text: message }),
        action ? el('button', { class: 'btn btn-sm btn-light my-auto toast-action', type: 'button', onclick: () => { bt.hide(); action.onClick(); } }, action.label) : null,
        el('button', { class: 'btn-close btn-close-white me-2 m-auto', 'data-bs-dismiss': 'toast', 'aria-label': 'Close' })));
    $('#toasts').append(t);
    const bt = new bootstrap.Toast(t, { delay: action ? 12000 : kind === 'err' ? 8000 : 4000 });
    t.addEventListener('hidden.bs.toast', () => t.remove());
    bt.show();
  }
  const fail = (e) => toast(e.message || String(e), 'err');

  // ---------- dialogs (no window.confirm/prompt) ----------
  const dialogModal = new bootstrap.Modal($('#dialog-modal'));
  function dialog({ title, body, okText = 'OK', danger = false, input = null }) {
    return new Promise((resolve) => {
      $('#dialog-title').textContent = title;
      const b = clear($('#dialog-body'));
      if (body instanceof Node) b.append(body); else b.textContent = body || '';
      const wrap = $('#dialog-input-wrap'), inp = $('#dialog-input');
      wrap.classList.toggle('d-none', input === null);
      if (input !== null) inp.value = input;
      const ok = $('#dialog-ok');
      ok.textContent = okText;
      ok.className = `btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'}`;
      let done = false;
      const finish = (v) => { if (done) return; done = true; cleanup(); dialogModal.hide(); resolve(v); };
      const onOk = () => finish(input !== null ? inp.value : true);
      const onCancel = () => finish(input !== null ? null : false);
      const onKey = (e) => { if (e.key === 'Enter' && input !== null) { e.preventDefault(); onOk(); } };
      const onHidden = () => finish(input !== null ? null : false);
      function cleanup() {
        ok.removeEventListener('click', onOk); $('#dialog-cancel').removeEventListener('click', onCancel);
        inp.removeEventListener('keydown', onKey); $('#dialog-modal').removeEventListener('hidden.bs.modal', onHidden);
      }
      ok.addEventListener('click', onOk); $('#dialog-cancel').addEventListener('click', onCancel);
      inp.addEventListener('keydown', onKey); $('#dialog-modal').addEventListener('hidden.bs.modal', onHidden);
      dialogModal.show();
      if (input !== null) setTimeout(() => { inp.focus(); inp.select(); }, 200);
    });
  }
  const uiConfirm = (title, body, okText, danger) => dialog({ title, body, okText, danger });
  const uiPrompt = (title, body, value = '') => dialog({ title, body, input: value });

  // ---------- state ----------
  const state = {
    project: null, projects: [], roots: [],
    tabs: new Map(), active: null, selected: null, expanded: new Set(),
    engineOnline: false, commitDrafts: {}, commitGenerating: false, adapters: [], profiles: [], tasks: [], currentTask: null,
    // lineups: saved default (Settings) and this task's copy (New task)
    lineup: [], lineupLoaded: false, run: { order: [], roles: {} }, runChanged: false,
    draft: { order: [], roles: {} }, draftDirty: false,
    es: null, ws: null, wsRetry: 0, termHistory: [], termHistIdx: -1,
    problems: [], monaco: null, editor: null, diffEditor: null, conflictEditor: null, conflict: null,
  };
  const pid = () => state.project && state.project.id;
  // Generation tokens: a newer render of the same panel invalidates older in-flight ones,
  // so concurrent refreshes can never interleave/duplicate DOM rows.
  const gens = {};
  const nextGen = (k) => (gens[k] = (gens[k] || 0) + 1);
  const stale = (k, g) => gens[k] !== g;
  const P = (suffix) => `/api/projects/${pid()}/${suffix}`;

  // ---------- resizable panels ----------
  // Drag the handles between panels (or focus one and use the arrow keys;
  // Home = smallest, End = largest, double-click = default size). Sizes are
  // remembered in this browser only.
  (() => {
    const app = $('#app');
    const DEFAULTS = { left: 280, right: 360, bottom: 260 };
    const KEY = 'cs.layout';
    const MIN = { left: 160, right: 240, bottom: 90 };
    const CENTER_MIN_W = 320, EDITOR_MIN_H = 120;
    let sizes = { ...DEFAULTS };
    try { Object.assign(sizes, JSON.parse(store.get(KEY) || '{}')); } catch (e) { /* ignore bad data */ }

    const bounds = (which) => {
      const W = window.innerWidth;
      if (which === 'left') return [MIN.left, Math.max(MIN.left, W - sizes.right - CENTER_MIN_W)];
      if (which === 'right') return [MIN.right, Math.max(MIN.right, W - sizes.left - CENTER_MIN_W)];
      const center = $('.ide-center').getBoundingClientRect().height;
      const fixed = $('#editor-tabs').offsetHeight + $('#editor-toolbar').offsetHeight;
      return [MIN.bottom, Math.max(MIN.bottom, center - fixed - EDITOR_MIN_H)];
    };
    const clamp = (which, v) => { const [lo, hi] = bounds(which); return Math.round(Math.min(hi, Math.max(lo, v))); };

    function apply(save = false) {
      for (const k of Object.keys(sizes)) app.style.setProperty(`--cs-${k}`, `${sizes[k]}px`);
      for (const [k, id] of [['left', '#split-left'], ['right', '#split-right'], ['bottom', '#split-bottom']]) {
        const [lo, hi] = bounds(k);
        const h = $(id);
        h.setAttribute('aria-valuemin', lo); h.setAttribute('aria-valuemax', hi); h.setAttribute('aria-valuenow', sizes[k]);
      }
      if (save) store.set(KEY, JSON.stringify(sizes));
    }
    function set(which, v, save) { sizes[which] = clamp(which, v); apply(save); }

    function drag(which, handle, axis) {
      handle.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        handle.setPointerCapture(e.pointerId);
        handle.classList.add('dragging');
        document.body.classList.add('resizing', axis === 'x' ? 'resizing-col' : 'resizing-row');
        const start = axis === 'x' ? e.clientX : e.clientY, from = sizes[which];
        const dir = which === 'left' ? 1 : -1;          // right/bottom panels grow when dragging toward them
        const move = (ev) => set(which, from + dir * ((axis === 'x' ? ev.clientX : ev.clientY) - start), false);
        const up = () => {
          handle.removeEventListener('pointermove', move);
          handle.removeEventListener('pointerup', up);
          handle.removeEventListener('pointercancel', up);
          handle.classList.remove('dragging');
          document.body.classList.remove('resizing', 'resizing-col', 'resizing-row');
          apply(true);
        };
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', up);
        handle.addEventListener('pointercancel', up);
      });
      handle.addEventListener('dblclick', () => set(which, DEFAULTS[which], true));
      handle.addEventListener('keydown', (e) => {
        const step = e.shiftKey ? 64 : 16;
        const grow = which === 'left' ? 'ArrowRight' : which === 'right' ? 'ArrowLeft' : 'ArrowUp';
        const shrink = which === 'left' ? 'ArrowLeft' : which === 'right' ? 'ArrowRight' : 'ArrowDown';
        let v = null;
        if (e.key === grow) v = sizes[which] + step;
        else if (e.key === shrink) v = sizes[which] - step;
        else if (e.key === 'Home') v = bounds(which)[0];
        else if (e.key === 'End') v = bounds(which)[1];
        else if (e.key === 'Enter') v = DEFAULTS[which];
        if (v === null) return;
        e.preventDefault();
        set(which, v, true);
      });
    }
    drag('left', $('#split-left'), 'x');
    drag('right', $('#split-right'), 'x');
    drag('bottom', $('#split-bottom'), 'y');

    // keep saved sizes valid when the window shrinks
    let raf = 0;
    window.addEventListener('resize', () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { for (const k of ['left', 'right', 'bottom']) sizes[k] = clamp(k, sizes[k]); apply(false); });
    });
    apply(false);
    requestAnimationFrame(() => { for (const k of ['left', 'right', 'bottom']) sizes[k] = clamp(k, sizes[k]); apply(false); });
  })();

  // ---------- Monaco ----------
  function initMonaco() {
    return new Promise((resolve, reject) => {
      window.MonacoEnvironment = { getWorkerUrl: () => `${MONACO_BASE}/base/worker/workerMain.js` };
      window.require.config({ paths: { vs: MONACO_BASE } });
      window.require(['vs/editor/editor.main'], () => {
        state.monaco = window.monaco;
        state.editor = monaco.editor.create($('#editor'), {
          theme: 'vs-dark', automaticLayout: true, fontSize: 13, minimap: { enabled: true }, model: null,
        });
        state.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveActive());
        monaco.editor.onDidChangeMarkers(() => renderProblems());
        document.body.dataset.editorReady = '1';
        resolve();
      }, reject);
    });
  }

  // ---------- projects & roots ----------
  async function loadProjects() {
    const data = await api('GET', '/api/projects');
    state.projects = data.projects;
    const sel = clear($('#project-select'));
    sel.append(el('option', { value: '', text: '— select project —' }));
    for (const p of state.projects) sel.append(el('option', { value: p.id, text: `${p.name}${p.available ? '' : ' (unavailable)'}` }));
    if (pid()) sel.value = pid();
    const list = clear($('#project-list'));
    if (!state.projects.length) list.append(el('div', { class: 'empty', text: 'No projects yet. Register a folder below.' }));
    for (const p of state.projects) {
      list.append(el('div', { class: `item ${p.id === pid() ? 'active' : ''}`, onclick: () => selectProject(p.id), dataset: { projectId: p.id } },
        el('span', { class: 'grow', text: p.name, title: p.path || '' }),
        el('span', { class: `badge ${p.git_mode === 'git' ? 'text-bg-info' : 'text-bg-secondary'}`, text: p.git_mode === 'git' ? 'git' : 'folder' }),
        IS_STAFF ? el('button', { class: 'btn btn-xs', title: 'Unregister (files are kept)', text: '×', onclick: (e) => { e.stopPropagation(); unregisterProject(p); } }) : null));
    }
  }

  async function unregisterProject(p) {
    if (!await uiConfirm('Unregister project', `Remove "${p.name}" from Code Studio? Files on disk are NOT deleted.`, 'Unregister', true)) return;
    try { await api('DELETE', `/api/projects/${p.id}`); if (pid() === p.id) { state.project = null; } await loadProjects(); toast('Project unregistered'); } catch (e) { fail(e); }
  }

  async function loadRoots() {
    const data = await api('GET', '/api/roots');
    state.roots = data.roots;
    const sel = clear($('#reg-root'));
    for (const r of state.roots.filter((r) => r.enabled)) sel.append(el('option', { value: r.id, text: `${r.alias} — ${r.path}` }));
    const list = clear($('#root-list'));
    if (!state.roots.length) list.append(el('div', { class: 'empty', text: IS_STAFF ? 'No allowlisted roots. Add one below.' : 'No roots configured (ask an administrator).' }));
    for (const r of state.roots) {
      list.append(el('div', { class: 'item' },
        el('span', { class: 'grow mono', text: `${r.alias}: ${r.path}`, title: r.path }),
        el('span', { class: 'meta', text: r.available ? `${r.project_count} proj` : 'unavailable' }),
        IS_STAFF ? el('button', { class: 'btn btn-xs', text: '×', title: 'Remove root', onclick: () => removeRoot(r) }) : null));
    }
    document.querySelectorAll('.staff-only').forEach((n) => n.classList.toggle('d-none', !IS_STAFF));
    $('#register-section').classList.toggle('d-none', !IS_STAFF);
  }

  async function removeRoot(r) {
    if (!await uiConfirm('Remove allowlisted root', `Stop allowing ${r.path}?`, 'Remove', true)) return;
    try { await api('DELETE', `/api/roots/${r.id}`); await loadRoots(); } catch (e) { fail(e); }
  }

  // Disable a form's submit button while its request is in flight (no double submits).
  async function busy(form, fn) {
    const btn = form.querySelector('button[type=submit]');
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  }

  $('#root-form').addEventListener('submit', (e) => {
    e.preventDefault();
    busy(e.currentTarget, async () => {
      try {
        await api('POST', '/api/roots', { alias: $('#root-alias').value.trim(), path: $('#root-path').value.trim() });
        $('#root-alias').value = ''; $('#root-path').value = '';
        await loadRoots(); toast('Root added', 'ok');
      } catch (err) { fail(err); }
    });
  });

  $('#reg-browse').addEventListener('click', () => browseRoot($('#reg-path').value.trim()));
  async function browseRoot(path) {
    const rootId = $('#reg-root').value;
    if (!rootId) return toast('Add an allowlisted root first', 'warn');
    const box = clear($('#reg-browser'));
    box.classList.remove('d-none');
    try {
      const data = await api('GET', `/api/roots/${rootId}/browse?path=${encodeURIComponent(path)}`);
      $('#reg-path').value = data.path;
      if (data.path) {
        const up = data.path.split('/').slice(0, -1).join('/');
        box.append(el('div', { class: 'item', onclick: () => browseRoot(up) }, el('span', { class: 'grow', text: '.. (up)' })));
      }
      for (const d of data.dirs) {
        const p = data.path ? `${data.path}/${d.name}` : d.name;
        box.append(el('div', { class: 'item', onclick: () => browseRoot(p), dataset: { dir: d.name } },
          el('span', { class: 'grow', text: `📁 ${d.name}` }), d.is_git ? el('span', { class: 'badge text-bg-info', text: 'git' }) : null));
      }
      if (!data.dirs.length) box.append(el('div', { class: 'empty', text: 'No sub-folders' }));
    } catch (e) { fail(e); }
  }

  $('#register-form').addEventListener('submit', (e) => {
    e.preventDefault();
    busy(e.currentTarget, async () => {
      try {
        const p = await api('POST', '/api/projects', { root_id: Number($('#reg-root').value), relative_path: $('#reg-path').value.trim(), name: $('#reg-name').value.trim() });
        $('#reg-name').value = ''; $('#reg-browser').classList.add('d-none');
        toast(`Registered ${p.name} (${p.git_mode === 'git' ? 'Git repository' : 'unmanaged folder'})`, 'ok');
        await loadProjects(); await loadRoots(); selectProject(p.id);
      } catch (err) { fail(err); }
    });
  });

  $('#project-select').addEventListener('change', (e) => { if (e.target.value) selectProject(Number(e.target.value)); });

  async function selectProject(id) {
    if (pid() === id) return;
    const dirty = [...state.tabs.values()].filter((t) => t.dirty);
    if (dirty.length && !await uiConfirm('Unsaved changes', `${dirty.length} file(s) have unsaved changes. Discard them and switch project?`, 'Discard & switch', true)) {
      $('#project-select').value = pid() || ''; return;
    }
    for (const t of [...state.tabs.values()]) closeTab(t.path, true);
    state.project = state.projects.find((p) => p.id === id) || (await api('GET', `/api/projects/${id}`));
    state.selected = null; state.expanded = new Set(); state.currentTask = null;
    store.set('cs.project', String(id));
    $('#project-select').value = id;
    document.querySelectorAll('#project-list .item').forEach((n) => n.classList.toggle('active', Number(n.dataset.projectId) === id));
    $('#review-section').classList.add('d-none');
    if (state.es) { state.es.close(); state.es = null; }
    $('#task-log').textContent = 'Select a task to view its log.';
    await Promise.allSettled([loadTree(), refreshGit(), loadTasks(), loadSnapshots()]);
    connectTerminal();
    sysTerm.project();
  }

  // ---------- file tree ----------
  async function loadTree() {
    const g = nextGen('tree');
    const target = $('#file-tree');
    if (!pid()) { clear(target).append(el('div', { class: 'empty', text: 'Select or register a project.' })); return; }
    if (!state.project.available) { clear(target).append(el('div', { class: 'empty', text: state.project.unavailable_reason || 'Project unavailable' })); return; }
    const root = el('div');
    await renderDir('', root, g);
    if (stale('tree', g)) return;
    clear(target).append(...root.childNodes);
  }

  async function renderDir(path, container, g = gens.tree) {
    let data;
    try { data = await api('GET', `${P('tree')}?path=${encodeURIComponent(path)}`); } catch (e) { container.append(el('div', { class: 'empty', text: e.message })); return; }
    if (stale('tree', g)) return;
    if (!data.entries.length && !path) container.append(el('div', { class: 'empty', text: 'Empty folder' }));
    for (const entry of data.entries) {
      const isDir = entry.type === 'dir';
      const node = el('div', {
        class: `tree-node ${entry.escapes_root ? 'escapes' : ''} ${state.selected && state.selected.path === entry.path ? 'selected' : ''}`,
        role: 'treeitem', dataset: { path: entry.path, type: entry.type }, title: entry.escapes_root ? 'Link points outside the project root (blocked)' : entry.path,
      }, el('span', { class: 'twisty', text: isDir ? (state.expanded.has(entry.path) ? '▼' : '▶') : '' }),
      el('span', { text: `${isDir ? '📁' : '📄'} ${entry.name}${entry.link ? ' ↪' : ''}` }));
      const children = el('div', { class: 'tree-children' });
      node.addEventListener('click', async () => {
        if (entry.escapes_root) return toast('This link points outside the project root and is blocked', 'warn');
        document.querySelectorAll('.tree-node.selected').forEach((n) => n.classList.remove('selected'));
        node.classList.add('selected');
        state.selected = { path: entry.path, type: entry.type };
        if (isDir) {
          if (state.expanded.has(entry.path)) { state.expanded.delete(entry.path); clear(children); node.firstChild.textContent = '▶'; }
          else { state.expanded.add(entry.path); node.firstChild.textContent = '▼'; clear(children); await renderDir(entry.path, children); }
        } else openFile(entry.path);
      });
      container.append(node, children);
      if (isDir && state.expanded.has(entry.path)) await renderDir(entry.path, children, g);
    }
  }

  function targetDir() {
    if (!state.selected) return '';
    if (state.selected.type === 'dir') return state.selected.path;
    return state.selected.path.split('/').slice(0, -1).join('/');
  }
  const join = (a, b) => (a ? `${a}/${b}` : b);

  async function createEntry(kind) {
    if (!pid()) return toast('Select a project first', 'warn');
    const base = targetDir();
    const name = await uiPrompt(kind === 'dir' ? 'New folder' : 'New file', `Name (inside /${base}):`, '');
    if (!name) return;
    try {
      const res = await api('POST', P('file/create'), { path: join(base, name.trim()), kind });
      if (base) state.expanded.add(base);
      await loadTree();
      if (kind !== 'dir') openFile(res.path);
      toast(`Created ${res.path}`, 'ok');
    } catch (e) { fail(e); }
  }
  $('#btn-new-file').addEventListener('click', () => createEntry('file'));
  $('#btn-new-folder').addEventListener('click', () => createEntry('dir'));
  $('#btn-refresh-tree').addEventListener('click', () => loadTree());

  $('#btn-rename').addEventListener('click', async () => {
    if (!state.selected) return toast('Select a file or folder first', 'warn');
    const old = state.selected.path;
    const tab = state.tabs.get(old);
    if (tab && tab.dirty) return toast('Save or discard changes before renaming', 'warn');
    const np = await uiPrompt('Rename / move', `New project-relative path for ${old}:`, old);
    if (!np || np === old) return;
    try {
      const res = await api('POST', P('file/rename'), { path: old, new_path: np.trim() });
      for (const t of [...state.tabs.values()]) if (t.path === old || t.path.startsWith(old + '/')) closeTab(t.path, true);
      state.selected = { path: res.path, type: state.selected.type };
      await loadTree(); refreshGit();
      if (state.selected.type === 'file') openFile(res.path);
      toast(`Renamed to ${res.path}`, 'ok');
    } catch (e) { fail(e); }
  });

  $('#btn-delete').addEventListener('click', async () => {
    if (!state.selected) return toast('Select a file or folder first', 'warn');
    const { path, type } = state.selected;
    if (!await uiConfirm('Delete', `Delete ${type === 'dir' ? 'folder' : 'file'} "${path}"? A recovery snapshot is taken first.`, 'Delete', true)) return;
    try {
      const res = await api('POST', P('file/delete'), { path, confirm: true });
      for (const t of [...state.tabs.values()]) if (t.path === path || t.path.startsWith(path + '/')) closeTab(t.path, true);
      state.selected = null;
      await loadTree(); refreshGit(); loadSnapshots();
      toast(`Deleted ${path} (snapshot ${res.snapshot})`, 'ok');
    } catch (e) { fail(e); }
  });

  // ---------- editor tabs ----------
  function modelUri(path) { return monaco.Uri.from({ scheme: 'cs', authority: `p${pid()}`, path: '/' + path }); }

  async function openFile(path, line) {
    if (!state.monaco) return toast('Editor still loading…', 'warn');
    let tab = state.tabs.get(path);
    if (!tab) {
      let data;
      try { data = await api('GET', `${P('file')}?path=${encodeURIComponent(path)}`); } catch (e) { return fail(e); }
      let content = data.content;
      const readOnly = !data.editable;
      if (data.binary) content = '[binary file — not shown]';
      else if (data.too_large) content = '[file too large to open in the editor]';
      const uri = modelUri(path);
      const existing = monaco.editor.getModel(uri);
      if (existing) existing.dispose();
      const model = monaco.editor.createModel(content, data.language, uri);
      tab = { path, model, version: data.version, readOnly, dirty: false, savedAlt: model.getAlternativeVersionId(), note: data.note };
      model.onDidChangeContent(() => {
        tab.dirty = model.getAlternativeVersionId() !== tab.savedAlt;
        renderTabs(); updateToolbar();
      });
      state.tabs.set(path, tab);
      if (data.note) toast(data.note, 'warn');
    }
    activateTab(path);
    if (line) { state.editor.revealLineInCenter(line); state.editor.setPosition({ lineNumber: line, column: 1 }); state.editor.focus(); }
  }

  function activateTab(path) {
    closeDiff();
    state.active = path;
    const tab = state.tabs.get(path);
    $('#editor-placeholder').classList.toggle('d-none', !!tab);
    if (tab) { state.editor.setModel(tab.model); state.editor.updateOptions({ readOnly: tab.readOnly }); }
    else state.editor.setModel(null);
    renderTabs(); updateToolbar();
  }

  async function closeTab(path, force = false) {
    const tab = state.tabs.get(path);
    if (!tab) return;
    if (tab.dirty && !force && !await uiConfirm('Unsaved changes', `Close ${path} and discard unsaved changes?`, 'Discard', true)) return;
    state.tabs.delete(path);
    tab.model.dispose();
    if (state.active === path) {
      const next = [...state.tabs.keys()].pop();
      if (next) activateTab(next); else { state.active = null; activateTab(null); }
    } else renderTabs();
    state.problems = state.problems.filter((p) => p.path !== path || p.kind !== 'conflict');
    renderProblems();
  }

  function renderTabs() {
    const bar = clear($('#editor-tabs'));
    for (const tab of state.tabs.values()) {
      const name = tab.path.split('/').pop();
      bar.append(el('div', { class: `editor-tab ${tab.path === state.active ? 'active' : ''}`, title: tab.path, role: 'tab', dataset: { path: tab.path }, onclick: () => activateTab(tab.path) },
        el('span', { text: name }), tab.dirty ? el('span', { class: 'dirty', text: '●', title: 'Unsaved changes' }) : null,
        tab.readOnly ? el('span', { class: 'meta', text: '🔒' }) : null,
        el('button', { class: 'close', text: '×', 'aria-label': `Close ${name}`, onclick: (e) => { e.stopPropagation(); closeTab(tab.path); } })));
    }
  }

  function updateToolbar() {
    const tab = state.tabs.get(state.active);
    $('#btn-save').disabled = !tab || tab.readOnly || !tab.dirty;
    $('#editor-status').textContent = tab ? `${tab.path}${tab.readOnly ? ' (read-only)' : ''}${tab.dirty ? ' — unsaved' : ''}` : 'No file open';
  }

  async function saveActive(opts = {}) {
    const tab = state.tabs.get(state.active);
    if (!tab || tab.readOnly) return;
    await saveTab(tab, opts);
  }

  async function saveTab(tab, { force = false, content = null, baseToken = null } = {}) {
    const body = { path: tab.path, content: content !== null ? content : tab.model.getValue(), base_version: baseToken || (tab.version && tab.version.token), force };
    try {
      const res = await api('PUT', P('file'), body);
      tab.version = res.version;
      if (content !== null && content !== tab.model.getValue()) tab.model.setValue(content);
      tab.savedAlt = tab.model.getAlternativeVersionId();
      tab.dirty = false;
      state.problems = state.problems.filter((p) => !(p.path === tab.path && p.kind === 'conflict'));
      renderTabs(); updateToolbar(); renderProblems(); refreshGit();
      toast(`Saved ${tab.path}`, 'ok');
      return true;
    } catch (e) {
      if (e.status === 409) { addProblem({ kind: 'conflict', path: tab.path, message: e.message }); openConflict(tab, e.data); return false; }
      fail(e); return false;
    }
  }
  $('#btn-save').addEventListener('click', () => saveActive());
  document.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveActive(); } });
  window.addEventListener('beforeunload', (e) => { if ([...state.tabs.values()].some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ''; } });

  // ---------- conflict resolution ----------
  const conflictModal = new bootstrap.Modal($('#conflict-modal'));
  function openConflict(tab, data) {
    state.conflict = { tab, data };
    $('#conflict-path').textContent = tab.path;
    $('#conflict-overwrite').textContent = data.code === 'deleted' ? 'Re-create file with my version' : 'Save my (merged) version';
    $('#conflict-take-disk').disabled = data.code === 'deleted';
    conflictModal.show();
    setTimeout(() => {
      if (!state.conflictEditor) {
        state.conflictEditor = monaco.editor.createDiffEditor($('#conflict-diff'), { theme: 'vs-dark', automaticLayout: true, originalEditable: false, renderSideBySide: true });
      }
      const lang = tab.model.getLanguageId();
      const old = state.conflictEditor.getModel();
      state.conflictEditor.setModel({
        original: monaco.editor.createModel(data.current_content || '', lang),
        modified: monaco.editor.createModel(tab.model.getValue(), lang),
      });
      if (old) { old.original.dispose(); old.modified.dispose(); }
    }, 250);
  }
  $('#conflict-cancel').addEventListener('click', () => conflictModal.hide());
  $('#conflict-take-disk').addEventListener('click', () => {
    const { tab, data } = state.conflict;
    tab.model.setValue(data.current_content || '');
    tab.version = data.current_version;
    tab.savedAlt = tab.model.getAlternativeVersionId(); tab.dirty = false;
    state.problems = state.problems.filter((p) => !(p.path === tab.path && p.kind === 'conflict'));
    renderTabs(); updateToolbar(); renderProblems(); conflictModal.hide();
    toast('Reloaded the disk version', 'info');
  });
  $('#conflict-overwrite').addEventListener('click', async () => {
    const { tab } = state.conflict;
    const merged = state.conflictEditor.getModel().modified.getValue();
    conflictModal.hide();
    // force=true with the *original* base token: the server snapshots the disk version first.
    await saveTab(tab, { force: true, content: merged });
  });

  // ---------- diff viewer ----------
  function showDiff(title, original, modified, path) {
    if (!state.monaco) return;
    const lang = guessLang(path);
    $('#editor').classList.add('d-none');
    $('#editor-placeholder').classList.add('d-none');
    const host = $('#diff-editor');
    host.classList.remove('d-none');
    if (!state.diffEditor) state.diffEditor = monaco.editor.createDiffEditor(host, { theme: 'vs-dark', automaticLayout: true, readOnly: true, renderSideBySide: window.innerWidth > 900 });
    const old = state.diffEditor.getModel();
    state.diffEditor.setModel({ original: monaco.editor.createModel(original || '', lang), modified: monaco.editor.createModel(modified || '', lang) });
    if (old) { old.original.dispose(); old.modified.dispose(); }
    $('#btn-diff-close').classList.remove('d-none');
    $('#editor-status').textContent = `Diff: ${title}`;
  }
  function closeDiff() {
    if ($('#diff-editor').classList.contains('d-none')) return;
    $('#diff-editor').classList.add('d-none');
    $('#editor').classList.remove('d-none');
    $('#btn-diff-close').classList.add('d-none');
    $('#editor-placeholder').classList.toggle('d-none', !!state.tabs.get(state.active));
    updateToolbar();
  }
  $('#btn-diff-close').addEventListener('click', closeDiff);
  function guessLang(path) {
    if (!state.monaco || !path) return 'plaintext';
    const ext = '.' + path.split('.').pop().toLowerCase();
    const lang = monaco.languages.getLanguages().find((l) => (l.extensions || []).includes(ext));
    return lang ? lang.id : 'plaintext';
  }

  // ---------- search ----------
  $('#search-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!pid()) return toast('Select a project first', 'warn');
    const q = $('#search-input').value;
    if (!q) return;
    const box = clear($('#search-results'));
    box.append(el('div', { class: 'empty', text: 'Searching…' }));
    try {
      const params = new URLSearchParams({ q, regex: $('#search-regex').checked ? '1' : '0', case: $('#search-case').checked ? '1' : '0' });
      const data = await api('GET', `${P('search')}?${params}`);
      clear(box);
      box.append(el('div', { class: 'empty', text: `${data.results.length} match(es) in ${data.files_scanned} file(s)${data.truncated ? ' (truncated)' : ''}` }));
      let last = null;
      for (const r of data.results) {
        if (r.path !== last) { box.append(el('div', { class: 'fw-semibold mt-1 px-1 mono', text: r.path })); last = r.path; }
        box.append(el('div', { class: 'item search-hit', onclick: () => openFile(r.path, r.line || undefined) },
          el('span', { class: 'meta', text: r.line ? String(r.line) : '' }), el('span', { class: 'grow mono', text: r.text })));
      }
    } catch (err) { clear(box); fail(err); }
  });

  // ---------- git ----------
  async function refreshGit() {
    const g = nextGen('git');
    const host = $('#git-panel');
    const panel = el('div');
    const badge = $('#git-badge');
    const finish = () => { if (!stale('git', g)) clear(host).append(panel); };
    if (!pid()) { badge.textContent = 'no project'; badge.className = 'badge text-bg-secondary me-2'; panel.append(el('div', { class: 'empty', text: 'Select a project.' })); return finish(); }
    if (state.project.git_mode !== 'git') {
      badge.textContent = 'unmanaged folder'; badge.className = 'badge text-bg-secondary me-2';
      panel.append(el('div', { class: 'empty', text: 'This project is an unmanaged folder. Git features are off and Code Studio will never initialize a repository. Edits are protected by recovery snapshots instead.' }));
      return finish();
    }
    let st;
    try { st = await api('GET', P('git/status')); } catch (e) { panel.append(el('div', { class: 'empty', text: e.message })); return finish(); }
    if (stale('git', g)) return;
    badge.textContent = `⎇ ${st.branch || 'detached'}${st.clean ? '' : ' *'}`; badge.className = 'badge text-bg-info me-2';
    const remotes = Object.keys(st.remotes || {});
    panel.append(el('div', { class: 'd-flex align-items-center gap-1 mb-2' },
      el('span', { class: 'fw-semibold text-nowrap text-truncate', title: st.head ? `HEAD ${st.head}` : 'no commits yet',
        text: `⎇ ${st.branch || 'detached'}` }),
      el('span', { class: 'meta mono text-nowrap', text: st.head ? st.head.slice(0, 7) : '' }),
      el('span', { class: 'ms-auto' }),
      el('button', { class: 'btn btn-xs', text: '⟳', title: 'Refresh', onclick: refreshGit }),
      el('button', { class: 'btn btn-xs', text: 'Pull', disabled: !remotes.length, onclick: () => remoteOp('pull', remotes) }),
      el('button', { class: 'btn btn-xs', text: 'Push', disabled: !remotes.length, onclick: () => remoteOp('push', remotes) }),
      el('button', { class: 'btn btn-xs', text: '⚙', title: 'Git settings: logins, commit author, AI commit messages, line endings', 'aria-label': 'Git settings', id: 'btn-git-settings', onclick: () => gitSettings.open() })));
    const le = st.line_endings;
    if (le && le.effective === 'crlf') {
      panel.append(el('div', { class: 'meta mb-2', id: 'git-line-endings', title: le.source },
        `Line endings: Windows checkout (CRLF ↔ LF) · ${le.setting === 'auto' ? 'auto-detected' : 'set manually'}`));
    }
    const section = (title, items, action, actionLabel, staged) => {
      panel.append(el('div', { class: 'section-title d-flex' }, el('span', { text: `${title} (${items.length})` }),
        items.length && action ? el('button', { class: 'btn btn-xs ms-auto', text: `${actionLabel} all`, onclick: () => gitAction(action, items.map((i) => i.path)) }) : null));
      const list = el('div', { class: 'list mb-2', dataset: { section: title.toLowerCase() } });
      for (const it of items) {
        list.append(el('div', { class: 'item git-item', title: it.path, dataset: { path: it.path } },
          el('span', { class: `meta ${it.status === 'deleted' ? 'diff-del' : 'diff-add'}`, text: it.status[0].toUpperCase() }),
          el('span', { class: 'grow mono', text: it.path, onclick: () => gitDiff(it.path, staged) }),
          el('button', { class: 'btn btn-xs', text: actionLabel, title: `${action} ${it.path}`, onclick: () => gitAction(action, [it.path]) })));
      }
      panel.append(list);
    };
    section('Staged', st.staged, 'unstage', '−', true);
    section('Changes', st.unstaged, 'stage', '+', false);
    section('Untracked', st.untracked, 'stage', '+', false);
    // The draft survives panel refreshes (and an AI reply that lands after one).
    const msg = el('textarea', { class: 'form-control form-control-sm mb-1', rows: 2, placeholder: 'Commit message', id: 'commit-message',
      oninput: (e) => { state.commitDrafts[pid()] = e.target.value; } });
    msg.value = state.commitDrafts[pid()] || '';
    const genBtn = el('button', { class: 'btn btn-xs', id: 'btn-commit-generate', text: state.commitGenerating ? '✨ Writing…' : '✨ Generate',
      disabled: st.clean || !!state.commitGenerating,
      title: st.staged.length ? 'Write a commit message for the staged changes with AI' : 'Write a commit message for all changes with AI (nothing is staged)',
      onclick: generateCommitMessage });
    panel.append(el('div', { class: 'd-flex align-items-center mb-1' },
      el('span', { class: 'meta', text: 'Message' }), el('span', { class: 'ms-auto' }), genBtn), msg,
      el('button', { class: 'btn btn-sm btn-primary w-100', text: `Commit ${st.staged.length} staged`, disabled: !st.staged.length, id: 'btn-commit',
        onclick: async () => {
          try {
            const r = await api('POST', P('git/commit'), { message: msg.value });
            delete state.commitDrafts[pid()]; toast(`Committed ${r.commit.slice(0, 8)}`, 'ok'); refreshGit();
          } catch (e) { fail(e); }
        } }));
    if (remotes.length) panel.append(el('div', { class: 'meta mt-2 mono', text: remotes.map((r) => `${r}: ${st.remotes[r]}`).join('\n') }));
    finish();
  }

  async function generateCommitMessage() {
    if (state.commitGenerating) return;
    const project = pid();
    const setBtn = (busy) => {
      const b = $('#btn-commit-generate');
      if (b) { b.disabled = busy; b.textContent = busy ? '✨ Writing…' : '✨ Generate'; }
    };
    state.commitGenerating = true; setBtn(true);
    try {
      const r = await api('POST', P('git/commit-message'), {});
      if (pid() !== project) return; // switched projects meanwhile
      state.commitDrafts[project] = r.message;
      const box = $('#commit-message');
      if (box) { box.value = r.message; box.rows = Math.min(8, Math.max(2, r.message.split('\n').length)); box.focus(); }
      toast(`Message written by ${r.adapter_name || r.adapter}${r.model ? ` (${r.model})` : ''}${r.staged ? '' : ' (from unstaged changes)'}. Review it before committing.`, 'ok');
    } catch (e) { fail(e); } finally { state.commitGenerating = false; setBtn(false); }
  }

  async function gitAction(action, paths) {
    try { await api('POST', P(`git/${action}`), { paths }); refreshGit(); } catch (e) { fail(e); }
  }
  async function gitDiff(path, staged) {
    try { const d = await api('GET', `${P('git/diff')}?path=${encodeURIComponent(path)}&staged=${staged ? 1 : 0}`); showDiff(`${path} (${staged ? 'staged vs HEAD' : 'working tree vs index'})`, d.original, d.modified, path); } catch (e) { fail(e); }
  }
  async function remoteOp(op, remotes) {
    let info = null;
    try { info = await api('GET', P('git/settings')); } catch (e) { /* still allow the attempt */ }
    const byName = new Map(((info && info.remotes) || []).map((r) => [r.name, r]));
    const remoteSel = el('select', { class: 'form-select form-select-sm mb-2', 'aria-label': 'Remote' }, remotes.map((r) => el('option', { value: r, text: r })));
    const credLine = el('div', { class: 'small' });
    const showCred = () => {
      clear(credLine);
      const r = byName.get(remoteSel.value);
      if (!r) return;
      if (!r.https) credLine.append(el('span', { class: 'text-body-secondary', text: `${r.url} (no login needed for this kind of remote)` }));
      else if (r.credential) credLine.append('Using your saved login ', el('strong', { text: `${r.credential.username}@${r.host}` }), '.');
      else credLine.append(el('span', { class: 'text-warning', text: `No login saved for ${r.host}. ` }),
        el('a', { href: '#', text: 'Add one in Git settings', onclick: (e) => { e.preventDefault(); $('#dialog-modal').addEventListener('hidden.bs.modal', () => gitSettings.open(), { once: true }); dialogModal.hide(); } }),
        ' (public repositories can still be pulled).');
    };
    remoteSel.addEventListener('change', showCred); showCred();
    const body = el('div', {},
      el('p', { text: op === 'push' ? 'Upload your commits on this branch to the remote repository.' : 'Download new commits from the remote (fast-forward only; refused if you have uncommitted changes).' }),
      remotes.length > 1 ? remoteSel : null, credLine);
    if (!await uiConfirm(`git ${op}`, body, op === 'push' ? 'Push' : 'Pull', op === 'push')) return;
    try {
      const r = await api('POST', P(`git/${op}`), { confirm: true, remote: remoteSel.value });
      toast(op === 'pull' ? (r.updated ? `Pulled: ${r.before.slice(0, 8)} → ${r.after.slice(0, 8)}` : 'Already up to date') : `Pushed ${r.ref}`, 'ok');
      refreshGit(); if (op === 'pull') { loadTree(); reloadCleanTabs(); }
    } catch (e) {
      fail(e);
      if (e.data && (e.data.code === 'git_auth_missing' || e.data.code === 'git_auth_rejected')) gitSettings.open();
    }
  }

  // ---------- Git settings modal (logins, author, line endings) ----------
  const gitSettings = (() => {
    const modalEl = $('#git-settings-modal');
    const modal = new bootstrap.Modal(modalEl);
    let editingHost = null;
    const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');
    const credErr = (m) => { const e = $('#gs-cred-error'); e.textContent = m || ''; e.classList.toggle('d-none', !m); };

    function resetCredForm() {
      editingHost = null; credErr('');
      $('#gs-cred-form-title').textContent = 'Add a login';
      $('#gs-host').disabled = false; $('#gs-host').value = 'github.com';
      $('#gs-username').value = ''; $('#gs-token').value = ''; $('#gs-token').placeholder = '';
      $('#gs-cred-cancel').classList.add('d-none');
    }

    async function loadCreds() {
      const data = await api('GET', '/api/git/credentials');
      const tb = clear($('#gs-cred-table tbody'));
      if (!data.credentials.length) tb.append(el('tr', {}, el('td', { colspan: '5', class: 'text-body-secondary', text: 'No logins saved yet.' })));
      for (const c of data.credentials) {
        tb.append(el('tr', { dataset: { host: c.host } },
          el('td', { class: 'fw-semibold', text: c.host }), el('td', { text: c.username }),
          el('td', { class: 'mono', text: c.token_hint ? `••••${c.token_hint}` : '••••' }),
          el('td', { text: fmtWhen(c.last_used_at) }),
          el('td', { class: 'text-end text-nowrap' },
            el('button', { type: 'button', class: 'btn btn-xs btn-outline-primary me-1', text: 'Edit', onclick: () => {
              editingHost = c.host; credErr('');
              $('#gs-cred-form-title').textContent = `Update login for ${c.host}`;
              $('#gs-host').value = c.host; $('#gs-host').disabled = true;
              $('#gs-username').value = c.username; $('#gs-token').value = '';
              $('#gs-token').placeholder = 'Leave blank to keep the saved token';
              $('#gs-cred-cancel').classList.remove('d-none'); $('#gs-username').focus();
            } }),
            el('button', { type: 'button', class: 'btn btn-xs btn-outline-danger', text: 'Remove', onclick: async () => {
              try { await api('DELETE', `/api/git/credentials/${c.id}`); toast(`Removed login for ${c.host}`, 'ok'); await refreshAll(); } catch (e) { credErr(e.message); }
            } }))));
      }
    }

    async function loadProject() {
      const box = clear($('#gs-remotes')); const acc = $('#gs-access'); acc.classList.add('d-none'); clear(acc);
      const leSel = $('#gs-line-endings');
      if (!pid() || !state.project || state.project.git_mode !== 'git') {
        box.append(el('div', { class: 'text-body-secondary', text: 'Select a Git project to see its remote and line-ending settings.' }));
        leSel.disabled = true; clear($('#gs-le-status')); return;
      }
      let info;
      try { info = await api('GET', P('git/settings')); } catch (e) { box.append(el('div', { class: 'text-danger', text: e.message })); return; }
      if (!info.remotes.length) box.append(el('div', { class: 'text-body-secondary', text: 'This repository has no remote configured.' }));
      for (const r of info.remotes) {
        const status = !r.https ? el('span', { class: 'text-body-secondary', text: 'no login needed' })
          : r.credential ? el('span', { class: 'text-success', text: `login: ${r.credential.username}@${r.host}` })
            : el('span', { class: 'text-warning', text: `no login saved for ${r.host}` });
        const testBtn = el('button', { type: 'button', class: 'btn btn-xs btn-outline-secondary ms-2', text: 'Test access', onclick: () => testAccess(r.name, testBtn) });
        box.append(el('div', { class: 'd-flex flex-wrap align-items-center gap-2 mb-1' },
          el('span', { class: 'fw-semibold', text: r.name }), el('span', { class: 'mono text-break', text: r.url }), status, r.https ? testBtn : null));
        if (r.https && !r.credential && !editingHost) $('#gs-host').value = r.host;
      }
      clear(leSel);
      for (const [v, label] of info.choices) leSel.append(el('option', { value: v, text: label }));
      leSel.value = info.line_endings.setting; leSel.disabled = !IS_STAFF;
      $('#gs-le-status').textContent = `In effect: ${info.line_endings.effective === 'crlf' ? 'Windows (CRLF ↔ LF)' : 'no conversion'} — ${info.line_endings.source}`;
    }

    async function testAccess(remote, btn) {
      const acc = clear($('#gs-access')); acc.classList.remove('d-none');
      btn.disabled = true; acc.append(el('span', { class: 'text-body-secondary', text: `Checking ${remote}…` }));
      try {
        const r = await api('POST', P('git/check-access'), { remote });
        clear(acc);
        if (!r.checked) { acc.append(el('div', { class: 'text-body-secondary', text: r.message })); return; }
        const line = (ok, what, code) => el('div', { class: ok ? 'text-success' : 'text-danger' },
          `${ok ? '✓' : '✗'} ${what}${ok ? '' : ` (HTTP ${code})`}`);
        acc.append(line(r.read, 'Can pull (read)', r.read_status), line(r.write, 'Can push (write)', r.write_status),
          el('div', { class: 'text-body-secondary', text: r.credential ? `Tested with ${r.credential}.` : 'Tested without a login.' }));
        if (!r.write) acc.append(el('div', { class: 'text-body-secondary', text: r.credential
          ? 'The token was not accepted for pushing: check it has Contents → Read and write for this repository and has not expired. '
            + 'For a repository owned by another personal account (you are a collaborator), a fine-grained token cannot be used: '
            + 'use a classic token with the "repo" scope, and accept the collaborator invitation first.'
          : 'Add a login below to be able to push.' }));
      } catch (e) { clear(acc); acc.append(el('div', { class: 'text-danger', text: e.message })); } finally { btn.disabled = false; }
    }

    async function loadIdentity() {
      const id = await api('GET', '/api/git/identity');
      $('#gs-name').value = id.name || ''; $('#gs-email').value = id.email || '';
      $('#gs-name').placeholder = id.effective;
    }

    let aiAgents = [];
    function showAiModelHints() {
      const a = aiAgents.find((x) => x.identifier === $('#gs-ai-adapter').value);
      const dl = clear($('#gs-ai-models'));
      const suggestions = a ? a.suggested_models : [...new Set(aiAgents.flatMap((x) => x.suggested_models))];
      for (const m of suggestions) dl.append(el('option', { value: m }));
      $('#gs-ai-model').placeholder = a ? (a.default_model ? `Default (${a.default_model})` : `${a.display_name} default`) : "Each agent's default";
    }
    async function loadAiPreference() {
      const p = await api('GET', '/api/git/commit-message-preference');
      aiAgents = p.agents;
      const sel = clear($('#gs-ai-adapter'));
      const order = (id) => { const i = p.automatic_order.indexOf(id); return i < 0 ? 99 : i; };
      sel.append(el('option', { value: '', text: 'Automatic (first available)' }));
      for (const a of [...aiAgents].sort((x, y) => order(x.identifier) - order(y.identifier))) {
        sel.append(el('option', { value: a.identifier, text: `${a.display_name}${a.available ? '' : ' (unavailable)'}` }));
      }
      if (p.adapter && !aiAgents.some((a) => a.identifier === p.adapter)) sel.append(el('option', { value: p.adapter, text: `${p.adapter} (unknown)` }));
      sel.value = p.adapter; $('#gs-ai-model').value = p.model;
      showAiModelHints();
    }

    async function refreshAll() { await Promise.allSettled([loadCreds(), loadProject(), loadIdentity(), loadAiPreference()]); }

    $('#gs-cred-form').addEventListener('submit', async (e) => {
      e.preventDefault(); credErr('');
      const btn = $('#gs-cred-save'); btn.disabled = true;
      try {
        const host = editingHost || $('#gs-host').value.trim();
        const c = await api('POST', '/api/git/credentials', { host, username: $('#gs-username').value.trim(), token: $('#gs-token').value });
        toast(`Saved login for ${c.host}`, 'ok'); resetCredForm(); await refreshAll();
      } catch (err) { credErr(err.message); } finally { btn.disabled = false; $('#gs-token').value = ''; }
    });
    $('#gs-cred-cancel').addEventListener('click', resetCredForm);
    $('#gs-id-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      try { await api('PATCH', '/api/git/identity', { name: $('#gs-name').value, email: $('#gs-email').value }); toast('Commit author saved', 'ok'); loadIdentity(); }
      catch (err) { fail(err); }
    });
    $('#gs-ai-adapter').addEventListener('change', () => { $('#gs-ai-model').value = ''; showAiModelHints(); });
    $('#gs-ai-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const p = await api('PATCH', '/api/git/commit-message-preference', { adapter: $('#gs-ai-adapter').value, model: $('#gs-ai-model').value.trim() });
        const a = aiAgents.find((x) => x.identifier === p.adapter);
        toast(`AI commit messages: ${a ? a.display_name : 'automatic'}${p.model ? ` · ${p.model}` : ''}`, 'ok');
        loadAiPreference();
      } catch (err) { fail(err); }
    });
    $('#gs-line-endings').addEventListener('change', async (e) => {
      try { await api('PATCH', P('git/settings'), { line_endings: e.target.value }); toast('Line-ending setting saved', 'ok'); await loadProject(); refreshGit(); }
      catch (err) { fail(err); loadProject(); }
    });
    modalEl.addEventListener('hidden.bs.modal', () => { resetCredForm(); $('#gs-token').value = ''; });

    function open() { resetCredForm(); refreshAll(); modal.show(); }
    return { open };
  })();

  // ---------- problems ----------
  function addProblem(p) { state.problems = state.problems.filter((x) => !(x.kind === p.kind && x.path === p.path)); state.problems.push(p); renderProblems(); }
  function renderProblems() {
    const box = clear($('#problems'));
    const items = [...state.problems];
    if (state.monaco) {
      for (const m of monaco.editor.getModelMarkers({})) {
        if (m.resource.scheme !== 'cs') continue;
        items.push({ kind: m.severity >= 8 ? 'error' : 'warning', path: m.resource.path.slice(1), line: m.startLineNumber, message: m.message });
      }
    }
    $('#problem-count').textContent = String(items.length);
    $('#problem-count').className = `badge ${items.some((i) => i.kind === 'conflict' || i.kind === 'error') ? 'text-bg-danger' : 'text-bg-secondary'}`;
    if (!items.length) { box.append(el('div', { class: 'empty', text: 'No problems.' })); return; }
    for (const p of items) {
      box.append(el('div', { class: 'item', onclick: () => p.path && openFile(p.path, p.line) },
        el('span', { class: `badge ${p.kind === 'conflict' ? 'text-bg-danger' : p.kind === 'error' ? 'text-bg-danger' : 'text-bg-warning'}`, text: p.kind }),
        el('span', { class: 'mono', text: `${p.path || ''}${p.line ? ':' + p.line : ''}` }), el('span', { class: 'grow', text: p.message })));
    }
  }

  // ---------- recovery & audit ----------
  async function loadSnapshots() {
    const g = nextGen('snapshots');
    if (!pid()) { clear($('#snapshot-list')); return; }
    const box = el('div');
    try {
      const data = await api('GET', P('snapshots'));
      if (stale('snapshots', g)) return;
      clear($('#snapshot-list')).append(box);
      if (!data.snapshots.length) box.append(el('div', { class: 'empty', text: 'No snapshots yet.' }));
      for (const s of data.snapshots) {
        box.append(el('div', { class: 'item', dataset: { snapshot: s.id } },
          el('span', { class: 'meta mono', text: s.created_at.replace('T', ' ').slice(0, 19) }),
          el('span', { class: 'badge text-bg-secondary', text: s.op }),
          el('span', { class: 'grow mono', text: s.paths.join(', '), title: s.note || '' }),
          el('button', { class: 'btn btn-xs', text: 'Restore', onclick: () => restoreSnapshot(s) })));
      }
    } catch (e) { box.append(el('div', { class: 'empty', text: e.message })); }
  }
  async function restoreSnapshot(s) {
    if (!await uiConfirm('Restore snapshot', `Restore ${s.paths.join(', ')} to the state from ${s.created_at}? The current state is snapshotted first.`, 'Restore', true)) return;
    try { await api('POST', P(`snapshots/${s.id}/restore`), { confirm: true }); toast('Snapshot restored', 'ok'); loadTree(); loadSnapshots(); refreshGit(); reloadCleanTabs(s.paths); } catch (e) { fail(e); }
  }
  $('#btn-refresh-snapshots').addEventListener('click', loadSnapshots);

  async function loadAudit() {
    const g = nextGen('audit');
    try {
      const data = await api('GET', `/api/audit${pid() ? `?project=${pid()}` : ''}`);
      if (stale('audit', g)) return;
      const box = clear($('#audit-list'));
      for (const e of data.events) {
        box.append(el('div', { class: 'item' }, el('span', { class: 'meta', text: e.at.replace('T', ' ').slice(0, 19) }),
          el('span', { class: `badge ${e.outcome === 'success' ? 'text-bg-secondary' : e.outcome === 'denied' ? 'text-bg-danger' : 'text-bg-warning'}`, text: e.outcome }),
          el('span', { text: `${e.actor || '-'} ${e.action}` }), el('span', { class: 'grow', text: e.target, title: JSON.stringify(e.detail) })));
      }
      if (!data.events.length) box.append(el('div', { class: 'empty', text: 'No audit events.' }));
    } catch (e) { fail(e); }
  }
  $('#btn-refresh-audit').addEventListener('click', loadAudit);
  $('#tabbtn-audit').addEventListener('shown.bs.tab', loadAudit);

  async function reloadCleanTabs(paths) {
    for (const tab of [...state.tabs.values()]) {
      if (paths && !paths.some((p) => tab.path === p || tab.path.startsWith(p + '/'))) continue;
      try {
        const data = await api('GET', `${P('file')}?path=${encodeURIComponent(tab.path)}`);
        if (data.version.sha256 === (tab.version && tab.version.sha256)) continue;
        if (tab.dirty) { addProblem({ kind: 'conflict', path: tab.path, message: 'File changed on disk while you have unsaved edits; saving will ask you to resolve.' }); continue; }
        tab.model.setValue(data.content || ''); tab.version = data.version; tab.savedAlt = tab.model.getAlternativeVersionId(); tab.dirty = false;
      } catch (e) { if (e.status === 404) closeTab(tab.path, true); }
    }
    renderTabs(); updateToolbar();
  }

  // ---------- terminal ----------
  const term = $('#term-output');
  const ANSI = /\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07/g;
  function termWrite(text, cls) {
    const span = el('span', { class: cls || null, text: text.replace(ANSI, '').replace(/\r\n/g, '\n') });
    term.append(span);
    while (term.textContent.length > 200000 && term.firstChild) term.removeChild(term.firstChild);
    term.scrollTop = term.scrollHeight;
  }
  function connectTerminal() {
    if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
    const input = $('#term-input');
    input.disabled = true;
    if (!pid() || !state.project.available) return;
    const projectId = pid();
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/terminal/${projectId}/`);
    state.ws = ws;
    ws.onopen = () => { state.wsRetry = 0; };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'ready') {
        termWrite(`Connected: ${msg.root} (${msg.shell}). Commands run inside the project root only.\n`, 't-sys');
        setPrompt(msg.cwd); input.disabled = false; input.placeholder = 'Type a command and press Enter';
        if (Array.isArray(msg.shells)) {
          const labels = { powershell: 'PowerShell', cmd: 'cmd', bash: 'bash', sh: 'sh' };
          const sel = clear($('#term-shell'));
          for (const s of msg.shells) sel.append(el('option', { value: s, text: labels[s] || s }));
        }
        $('#term-shell').value = msg.shell;
      } else if (msg.type === 'output') termWrite(msg.data);
      else if (msg.type === 'denied') termWrite(`✖ ${msg.reason}\n`, 't-denied');
      else if (msg.type === 'error') termWrite(`! ${msg.message}\n`, 't-err');
      else if (msg.type === 'exit') {
        if (msg.code) termWrite(`[exit ${msg.code}]\n`, 't-sys');
        setPrompt(msg.cwd); input.disabled = false; input.focus();
      }
    };
    ws.onclose = (ev) => {
      input.disabled = true;
      if (pid() !== projectId) return;
      termWrite(`[disconnected${ev.code ? ' ' + ev.code : ''}]\n`, 't-sys');
      if (ev.code >= 4400 && ev.code < 4500 && ev.code !== 4408) return;
      if (state.wsRetry < 5) { state.wsRetry += 1; setTimeout(() => { if (pid() === projectId) connectTerminal(); }, 1000 * state.wsRetry); }
    };
  }
  function setPrompt(cwd) { $('#term-prompt').textContent = `${cwd}>`; }
  $('#term-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#term-input');
    const cmd = input.value;
    if (!state.ws || state.ws.readyState !== 1) return toast('Terminal not connected', 'warn');
    if (['clear', 'cls'].includes(cmd.trim().toLowerCase())) { clear(term); input.value = ''; return; }
    termWrite(`${$('#term-prompt').textContent} ${cmd}\n`, 't-sys');
    if (cmd.trim()) { state.termHistory.push(cmd); state.termHistIdx = state.termHistory.length; }
    state.ws.send(JSON.stringify({ type: 'run', command: cmd }));
    input.value = ''; input.disabled = true;
  });
  $('#term-input').addEventListener('keydown', (e) => {
    const h = state.termHistory;
    if (e.key === 'ArrowUp' && h.length) { state.termHistIdx = Math.max(0, state.termHistIdx - 1); e.target.value = h[state.termHistIdx]; e.preventDefault(); }
    if (e.key === 'ArrowDown' && h.length) { state.termHistIdx = Math.min(h.length, state.termHistIdx + 1); e.target.value = h[state.termHistIdx] || ''; e.preventDefault(); }
  });
  $('#term-stop').addEventListener('click', () => { if (state.ws && state.ws.readyState === 1) { state.ws.send(JSON.stringify({ type: 'cancel' })); termWrite('^C\n', 't-sys'); } });
  $('#term-clear').addEventListener('click', () => clear(term));
  $('#term-shell').addEventListener('change', (e) => { if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ type: 'shell', shell: e.target.value })); });

  // ---------- system terminal (local copy only) ----------
  // A real shell in a server-side pseudo console, rendered with xterm.js. One
  // shell per page; it starts in the project root when the tab is first shown
  // and is ended when the project changes or the page closes.
  const sysTerm = (() => {
    if (document.body.dataset.systemTerminal !== '1' || !window.Terminal || !window.FitAddon) return { project() {} };
    const host = $('#systerm-host');
    const status = (text) => { $('#systerm-status').textContent = text; };
    const xt = new window.Terminal({
      cursorBlink: true, fontFamily: getComputedStyle(document.body).getPropertyValue('--cs-mono') || 'Consolas, monospace',
      fontSize: 13, scrollback: 10000, allowProposedApi: false,
      theme: { background: '#141517', foreground: '#d4d4d4', cursor: '#d4d4d4', selectionBackground: '#264f78' },
    });
    const fit = new window.FitAddon.FitAddon();
    xt.loadAddon(fit);
    let ws = null; let opened = false; let startedFor = null; let awaitingRestart = false;
    const visible = () => $('#tab-systerm').classList.contains('active');
    const send = (msg) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); };

    // Copy with a selection, paste through the browser (xterm would send ^C / ^V).
    xt.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown' || !(e.ctrlKey || e.metaKey)) return true;
      const k = e.key.toLowerCase();
      if (k === 'c' && (e.shiftKey || xt.hasSelection())) {
        if (xt.hasSelection()) navigator.clipboard?.writeText(xt.getSelection()).catch(() => {});
        xt.clearSelection(); e.preventDefault(); return false;
      }
      if (k === 'v') return false;  // native paste event -> xterm onData
      return true;
    });
    xt.onData((data) => {
      if (awaitingRestart) { awaitingRestart = false; start(); return; }
      send({ type: 'input', data });
    });
    xt.onResize(({ cols, rows }) => send({ type: 'resize', cols, rows }));

    function refit() { if (opened && visible() && host.clientWidth > 0) { try { fit.fit(); } catch (e) { /* hidden */ } } }
    new ResizeObserver(() => { clearTimeout(refit.t); refit.t = setTimeout(refit, 50); }).observe(host);

    function stop() {
      if (ws) { ws.onclose = null; ws.onmessage = null; ws.close(); ws = null; }
      startedFor = null; awaitingRestart = false;
    }
    function start() {
      stop();
      const projectId = pid();
      if (!projectId || !state.project || !state.project.available) { status('select a project'); return; }
      if (!opened) { xt.open(host); opened = true; }
      refit();
      startedFor = projectId;
      const shell = $('#systerm-shell').value || store.get('cs.systerm.shell') || '';
      const q = new URLSearchParams({ shell, cols: xt.cols, rows: xt.rows });
      const sock = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/systerm/${projectId}/?${q}`);
      ws = sock;
      status('starting…');
      sock.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'output') xt.write(msg.data);
        else if (msg.type === 'ready') {
          const sel = clear($('#systerm-shell'));
          const labels = { pwsh: 'PowerShell 7', powershell: 'Windows PowerShell', cmd: 'cmd' };
          for (const s of msg.shells) sel.append(el('option', { value: s, text: labels[s] || s }));
          sel.value = msg.shell;
          status(`${labels[msg.shell] || msg.shell} · ${msg.cwd}`);
          xt.focus();
        } else if (msg.type === 'exit') {
          xt.write(`\r\n\x1b[90m[process exited${msg.code === null || msg.code === undefined ? '' : ' with code ' + msg.code}] press any key to restart\x1b[0m\r\n`);
          status('exited'); awaitingRestart = true;
        } else if (msg.type === 'error') xt.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`);
      };
      sock.onclose = (ev) => {
        if (ws !== sock) return;
        ws = null;
        if (awaitingRestart) return;
        const why = { 4401: 'signed out', 4403: 'disabled or not a local connection', 4404: 'project not found' }[ev.code];
        xt.write(`\r\n\x1b[90m[disconnected${why ? ': ' + why : ''}] press any key to restart\x1b[0m\r\n`);
        status('disconnected'); awaitingRestart = true;
      };
    }
    $('#tabbtn-systerm').addEventListener('shown.bs.tab', () => {
      if (!ws && !awaitingRestart) start(); else { refit(); xt.focus(); }
    });
    $('#systerm-restart').addEventListener('click', () => { xt.reset(); start(); });
    $('#systerm-kill').addEventListener('click', () => {
      if (!ws) return;
      stop(); xt.write('\r\n\x1b[90m[killed] press any key to restart\x1b[0m\r\n'); status('killed'); awaitingRestart = true;
    });
    $('#systerm-shell').addEventListener('change', (e) => { store.set('cs.systerm.shell', e.target.value); xt.reset(); start(); });
    window.addEventListener('pagehide', stop);
    return {
      // Called after a project switch: a shell belongs to the project it started in.
      project() {
        if (startedFor === pid()) return;
        const had = ws || awaitingRestart;
        stop();
        if (had) { xt.reset(); if (visible()) start(); else status('not started'); }
      },
    };
  })();

  // ---------- engine & agents ----------
  async function pollEngine() {
    try {
      const h = await api('GET', '/api/engine/status');
      setEngine(h.online, h.online ? `engine v${h.api_version} online` : 'engine offline');
    } catch (e) { setEngine(false, 'engine status unknown'); }
  }
  function setEngine(online, label) {
    const was = state.engineOnline;
    state.engineOnline = online;
    const b = $('#engine-badge');
    b.textContent = label; b.className = `badge me-auto ${online ? 'text-bg-success' : 'text-bg-danger'}`;
    $('#engine-offline-banner').classList.toggle('d-none', online);
    $('#task-submit').disabled = !online;
    if (online && !was) loadAgents();
  }

  // On/off switch for agents that need one (Antigravity). Admins only; takes effect without a restart.
  function agentSwitch(a) {
    if (typeof a.user_enabled !== 'boolean') return null;
    if (!IS_STAFF) return el('span', { class: 'meta', text: a.user_enabled ? 'on' : 'off' });
    const on = a.user_enabled;
    return el('button', { type: 'button', class: `btn btn-xs ${on ? 'btn-outline-secondary' : 'btn-outline-success'} agent-switch`,
      dataset: { agentSwitch: a.identifier }, text: on ? 'Turn off' : 'Turn on',
      title: on ? `Stop offering ${a.display_name} for new tasks` : `Use ${a.display_name} (log in with 'agy' first)`,
      onclick: async (e) => {
        e.stopPropagation();
        e.currentTarget.disabled = true;
        try {
          await api('POST', `/api/agents/${encodeURIComponent(a.identifier)}/enabled`, { enabled: !on });
          toast(`${a.display_name} turned ${on ? 'off' : 'on'}`, 'ok');
        } catch (err) { fail(err); }
        loadAgents();
      } });
  }

  async function loadAgents() {
    // several callers (boot, Settings, saves) can overlap: only the newest response may update the page
    const g = nextGen('agents');
    try {
      const [data, team] = await Promise.all([api('GET', '/api/agents'),
        api('GET', '/api/team').catch(() => null)]);
      if (stale('agents', g)) return;
      state.adapters = data.adapters; state.profiles = data.profiles;
      if (team) { state.lineup = team.steps || []; state.lineupLoaded = true; }
      if (!data.engine_online) setEngine(false, 'engine offline');
      const al = clear($('#adapter-list'));
      if (!state.adapters.length) al.append(el('div', { class: 'empty', text: 'No agents reported by the Agent Engine.' }));
      for (const a of state.adapters) {
        al.append(el('div', { class: 'item', title: (a.notes || []).join('\n'), dataset: { adapter: a.identifier } },
          el('span', { class: `badge ${a.available ? 'text-bg-success' : 'text-bg-secondary'}`, text: a.available ? 'ready' : 'unavailable' }),
          el('span', { class: 'grow', text: a.display_name || a.identifier }),
          el('span', { class: 'meta', text: a.version ? a.version.slice(0, 24) : (a.notes && a.notes[0] ? a.notes[0].slice(0, 40) : '') }),
          agentSwitch(a)));
      }
      profileManager.refresh();
      // this task's lineup follows the default until the user changes it for this task
      if (!state.runChanged) resetRunToDefault(false);
      else pruneLineup(state.run);
      renderRunPicker();
      if (!state.draftDirty) resetDraft();
      renderLineupEditor();
    } catch (e) { if (!stale('agents', g)) fail(e); }
  }
  $('#btn-refresh-agents').addEventListener('click', loadAgents);

  // ---------- lineups: the ordered profiles a task runs ----------
  // One picker, used twice: Settings > Default lineup (saved for the user) and
  // New task > Change for this task (that task only). Agents run in the order
  // they were ticked (shown as 1, 2, 3 ...); each one receives the earlier results.
  const profileById = (id) => state.profiles.find((p) => p.id === id);
  const runnableProfile = (p) => {
    const a = p && state.adapters.find((x) => x.identifier === p.adapter);
    return !!(p && p.enabled && a && a.available);
  };
  function pruneLineup(model) {  // drop profiles that were deleted or turned off
    model.order = model.order.filter((id) => { const p = profileById(id); return p && p.enabled; });
  }
  function lineupFromSaved() {
    const model = { order: [], roles: {} };
    for (const step of state.lineup) { model.order.push(step.profile_id); model.roles[step.profile_id] = step.role; }
    pruneLineup(model);
    return model;
  }
  function lineupSteps(model) {
    return model.order.map((id) => profileById(id)).filter(Boolean)
      .map((p) => ({ p, role: (model.roles[p.id] || p.role || '').trim() || p.role, ok: runnableProfile(p) }));
  }
  function paintSteps(box, model) {
    box.querySelectorAll('.item').forEach((n) => {
      const idx = model.order.indexOf(Number(n.dataset.profileId));
      const b = n.querySelector('.assign-step');
      b.textContent = idx >= 0 ? String(idx + 1) : '';
      b.classList.toggle('d-none', idx < 0);
      n.querySelector('.assign-check').setAttribute('aria-label', idx >= 0 ? `Step ${idx + 1}` : 'Not selected');
    });
  }
  function lineupPicker(box, model, { allowUnavailable, onChange }) {
    clear(box);
    const usable = state.profiles.filter((p) => p.enabled);
    if (!usable.length) {
      box.append(el('div', { class: 'empty', text: 'No enabled profiles yet. Create one in Settings → Profiles.' }));
      return;
    }
    for (const p of usable) {
      const ok = runnableProfile(p);
      const cb = el('input', { type: 'checkbox', class: 'assign-check', value: p.id, disabled: !ok && !allowUnavailable,
        onchange: (e) => {
          model.order = model.order.filter((id) => id !== p.id);
          if (e.target.checked) model.order.push(p.id);
          paintSteps(box, model); onChange();
        } });
      cb.checked = model.order.includes(p.id);
      box.append(el('label', { class: 'item', dataset: { profileId: p.id } },
        cb,
        el('span', { class: 'badge rounded-pill text-bg-primary assign-step d-none', 'aria-hidden': 'true' }),
        el('span', { class: 'grow', text: p.name }),
        el('input', { class: 'form-control form-control-sm assign-role', value: model.roles[p.id] || p.role, style: 'width:110px',
          'aria-label': `Role for ${p.name}`, oninput: (e) => { model.roles[p.id] = e.target.value; onChange(); } }),
        ok ? null : el('span', { class: 'meta', text: 'unavailable' })));
    }
    paintSteps(box, model);
  }
  function describeLineup(steps) {
    const names = steps.map((s) => `${s.p.name} (${s.role})`);
    return names.length > 1 ? `Runs in order: ${names.join(' → ')}` : `Runs: ${names[0]}`;
  }

  // New task: summary of what will run, and the per-task picker
  function resetRunToDefault(render = true) {
    state.run = lineupFromSaved();
    state.runChanged = false;
    if (render) renderRunPicker();
  }
  function renderRunPicker() {
    lineupPicker($('#task-assignments'), state.run, { allowUnavailable: false,
      onChange: () => { state.runChanged = true; renderTaskLineup(); } });
    renderTaskLineup();
  }
  function renderTaskLineup() {
    const box = clear($('#task-pipeline'));
    const steps = lineupSteps(state.run);
    const runs = steps.filter((s) => s.ok), skipped = steps.filter((s) => !s.ok);
    if (!runs.length) {
      box.append(el('div', { class: 'text-warning', text: state.lineup.length || state.runChanged
        ? 'No available agents picked. Use "Change for this task" below.'
        : 'No default lineup yet. Set one in Settings → Default lineup, or use "Change for this task".' }));
    } else {
      box.append(el('div', { text: describeLineup(runs) }));
    }
    if (skipped.length) box.append(el('div', { class: 'lineup-note', text: `Skipped (agent unavailable): ${skipped.map((s) => s.p.name).join(', ')}` }));
    if (state.runChanged) box.append(el('div', { class: 'lineup-note', text: 'Changed for this task (your default lineup is unchanged).' }));
    $('#task-reset').disabled = !state.runChanged;
  }
  $('#task-reset').addEventListener('click', () => resetRunToDefault());

  // Settings > Default lineup
  function resetDraft() { state.draft = lineupFromSaved(); state.draftDirty = false; }
  function renderLineupEditor() {
    lineupPicker($('#lineup-editor'), state.draft, { allowUnavailable: true,
      onChange: () => { state.draftDirty = true; renderLineupSummary(); } });
    renderLineupSummary();
  }
  function renderLineupSummary() {
    const steps = lineupSteps(state.draft);
    const box = clear($('#lineup-pipeline'));
    box.append(el('div', { text: steps.length ? describeLineup(steps) : 'No profiles picked: new tasks start with an empty lineup.' }));
    if (steps.some((s) => !s.ok)) box.append(el('div', { text: 'Agents marked unavailable are skipped until they are ready again.' }));
    if (state.draftDirty) box.append(el('div', { class: 'text-warning', text: 'Unsaved changes.' }));
    $('#lineup-save').disabled = !state.draftDirty;
  }
  $('#lineup-clear').addEventListener('click', () => {
    state.draft = { order: [], roles: {} }; state.draftDirty = true; renderLineupEditor();
  });
  $('#lineup-save').addEventListener('click', async () => {
    const steps = lineupSteps(state.draft).map((s) => ({ profile_id: s.p.id, role: s.role }));
    const btn = $('#lineup-save'); btn.disabled = true;
    try {
      const saved = await api('PUT', '/api/team', { steps });
      state.lineup = saved.steps; state.draftDirty = false;
      if (!state.runChanged) resetRunToDefault();
      renderLineupEditor();
      toast('Default lineup saved', 'ok');
    } catch (err) { fail(err); renderLineupSummary(); }
  });

  // ---------- Settings (user menu): agent team, profiles, default lineup ----------
  const settings = (() => {
    const modalEl = $('#settings-modal');
    const modal = new bootstrap.Modal(modalEl);
    function show(section = 'agents') {
      bootstrap.Tab.getOrCreateInstance($(`#settings-tab-${section}`)).show();
      modal.show();
      loadAgents();
    }
    modalEl.addEventListener('hidden.bs.modal', () => {
      if (state.draftDirty) toast('Default lineup changes were not saved', 'warn');
      resetDraft(); renderLineupEditor();
    });
    $('#settings-item').addEventListener('click', () => show('agents'));
    $('#btn-agent-settings').addEventListener('click', () => show('agents'));
    $('#task-edit-default').addEventListener('click', () => show('lineup'));
    return { show, modalEl };
  })();
  $('#btn-refresh-agents').addEventListener('click', loadAgents);

  // ---------- Settings > Project checks (per project) ----------
  const projectChecks = (() => {
    const showError = (msg) => { const e = $('#pc-error'); e.textContent = msg || ''; e.classList.toggle('d-none', !msg); };
    const lines = (v) => v.split('\n').map((x) => x.trim()).filter(Boolean);
    function venvStatus(d) {
      const s = $('#pc-venv-status');
      if (d.python_env === '-') s.textContent = 'None: commands use the Python on the agent machine\'s PATH. Leave blank to auto-detect.';
      else if (d.python_env_found) s.textContent = `Using ${d.python_env_found}: its Scripts/bin folder comes first on PATH for agents and checks. Type - for none.`;
      else s.textContent = 'No virtual environment found (.venv, venv or env): commands use the Python on the agent machine\'s PATH. Type - for none.';
    }
    async function load() {
      showError('');
      const has = !!pid();
      $('#pc-none').classList.toggle('d-none', has); $('#pc-form').classList.toggle('d-none', !has);
      $('#pc-save').classList.toggle('d-none', !has || !IS_STAFF);
      if (!has) { $('#pc-project').textContent = ''; return; }
      const proj = (state.projects || []).find((x) => x.id === pid());
      $('#pc-project').textContent = proj ? `— ${proj.name}` : '';
      try {
        const d = await api('GET', P('checks'));
        $('#pc-commands').value = d.verify_commands.join('\n');
        $('#pc-env').value = Object.entries(d.env).map(([k, v]) => `${k}=${v}`).join('\n');
        $('#pc-exclude').value = d.exclude.join('\n');
        $('#pc-venv').value = d.python_env || '';
        venvStatus(d);
        for (const id of ['#pc-commands', '#pc-env', '#pc-exclude', '#pc-venv']) $(id).readOnly = !d.can_edit;
        $('#pc-readonly-note').classList.toggle('d-none', d.can_edit);
      } catch (e) { showError(e.message); }
    }
    $('#pc-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      showError('');
      const env = {};
      for (const line of lines($('#pc-env').value)) {
        if (line.startsWith('#')) continue;
        const i = line.indexOf('=');
        if (i < 1) return showError(`"${line}": write NAME=value`);
        env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
      const btn = $('#pc-save'); btn.disabled = true;
      try {
        const d = await api('PUT', P('checks'), { verify_commands: lines($('#pc-commands').value), env,
          exclude: lines($('#pc-exclude').value), python_env: $('#pc-venv').value.trim() });
        venvStatus(d);
        toast('Project checks saved', 'ok');
      } catch (err) { showError(err.message); } finally { btn.disabled = false; }
    });
    $('#settings-tab-checks').addEventListener('shown.bs.tab', load);
    return { load };
  })();

  // ---------- agent profiles (manage modal) ----------
  function agentLabel(identifier) {
    const a = state.adapters.find((x) => x.identifier === identifier);
    return (a && a.display_name) || identifier;
  }
  // "OpenAI Codex CLI" -> "Codex", "Test fixture (deterministic, …)" -> "Test fixture"
  const shortAgentLabel = (identifier) =>
    agentLabel(identifier).replace(/\s*\(.*\)\s*$/, '').replace(/^(OpenAI|Google)\s+/, '').replace(/\s+CLI$/, '');

  const profileManager = (() => {
    const ROLES = [
      ['implementer', 'Writes and changes code'],
      ['reviewer', 'Reviews earlier results and points out problems'],
      ['architect', 'Plans the approach before anyone edits'],
      ['tester', 'Writes or runs tests'],
      ['documenter', 'Writes docs and comments'],
    ];
    const MODEL_HINTS = { 'claude-code': ['opus', 'sonnet', 'haiku'], codex: [], antigravity: [] };
    const modalEl = settings.modalEl;  // the Profiles section of Settings
    let editing = null;          // profile id, or 'new'
    let nameTouched = false;
    let deleteArmed = null;

    function suggestedName() {
      const role = $('#pm-role').value.trim();
      const agent = shortAgentLabel($('#pm-adapter').value);
      return role ? `${agent} ${role}` : agent;
    }
    function updateName() { if (!nameTouched) $('#pm-name').value = suggestedName(); }

    function updateAdapterStatus() {
      const id = $('#pm-adapter').value;
      const a = state.adapters.find((x) => x.identifier === id);
      const st = clear($('#pm-adapter-status'));
      if (!a) return;
      st.append(el('span', { class: `badge me-1 ${a.available ? 'text-bg-success' : 'text-bg-secondary'}`, text: a.available ? 'ready' : 'unavailable' }),
        a.available ? (a.version || '') : ((a.notes || [])[0] || 'Not detected by the Agent Engine'));
      const isVerify = id === 'verify';
      $('#pm-maxturns-wrap').classList.toggle('d-none', id !== 'claude-code');
      $('#pm-commands-wrap').classList.toggle('d-none', id !== 'antigravity');
      $('#pm-access-edit').closest('fieldset').classList.toggle('d-none', isVerify);
      $('#pm-model').closest('.col-sm-6').classList.toggle('d-none', isVerify);
      if (isVerify) {
        st.append(el('div', { class: 'mt-1', text: 'Runs this project\'s check commands (Settings → Project checks) in a throwaway copy and passes the real results to later steps. Not an AI; never changes files.' }));
        if (editing === 'new' && ['implementer', ''].includes($('#pm-role').value.trim())) setRole('verify');
        $('#pm-access-ro').checked = true;  // after setRole, which picks an access default for new profiles
      }
      updateCommandsState();
      const dl = clear($('#pm-model-options'));
      for (const m of MODEL_HINTS[id] || []) dl.append(el('option', { value: m }));
    }

    function setRole(role, fromInput = false) {
      if (!fromInput) $('#pm-role').value = role;
      document.querySelectorAll('#pm-role-chips button').forEach((b) => {
        const on = b.dataset.role === role;
        b.classList.toggle('btn-primary', on); b.classList.toggle('btn-outline-secondary', !on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      // sensible default: reviewers/architects usually shouldn't edit
      if (editing === 'new' && !fromInput) {
        const ro = role === 'reviewer' || role === 'architect';
        $(ro ? '#pm-access-ro' : '#pm-access-edit').checked = true;
      }
      updateCommandsState();
      updateName();
    }

    function updateCommandsState() {
      const ro = $('#pm-access-ro').checked;
      $('#pm-commands').disabled = ro;
      $('#pm-commands').title = ro ? 'Read-only profiles never run shell commands' : '';
    }
    document.querySelectorAll('input[name="pm-access"]').forEach((r) => r.addEventListener('change', updateCommandsState));

    function showError(msg) { const e = $('#pm-error'); e.textContent = msg || ''; e.classList.toggle('d-none', !msg); }

    function showList() {
      editing = null; disarmDelete();
      $('#pm-title').textContent = 'Agent profiles';
      $('#pm-list-view').classList.remove('d-none'); $('#pm-form').classList.add('d-none');
      $('#pm-list-footer').classList.remove('d-none'); $('#pm-form-footer').classList.add('d-none');
      $('#pm-new').classList.toggle('d-none', !IS_STAFF);
      $('#pm-readonly-note').classList.toggle('d-none', IS_STAFF);
      renderTable();
    }

    function renderTable() {
      const tb = clear($('#pm-table tbody'));
      if (!state.profiles.length) {
        tb.append(el('tr', {}, el('td', { colspan: '6', class: 'text-body-secondary', text: 'No profiles yet.' })));
        return;
      }
      for (const p of state.profiles) {
        const a = state.adapters.find((x) => x.identifier === p.adapter);
        const sw = el('input', { class: 'form-check-input', type: 'checkbox', role: 'switch', 'aria-label': `Enable ${p.name}`, disabled: !IS_STAFF,
          onchange: async (e) => {
            try { await api('PATCH', `/api/profiles/${p.id}`, { enabled: e.target.checked }); await loadAgents(); }
            catch (err) { e.target.checked = !e.target.checked; fail(err); }
          } });
        sw.checked = p.enabled;
        tb.append(el('tr', { dataset: { profileId: p.id } },
          el('td', { class: 'fw-semibold', text: p.name }),
          el('td', {}, el('span', { class: `badge me-1 ${a && a.available ? 'text-bg-success' : 'text-bg-secondary'}`, text: a && a.available ? 'ready' : 'unavailable' }), agentLabel(p.adapter)),
          el('td', { text: p.role }),
          el('td', { text: p.execution_policy.read_only ? 'Read-only' : 'Can edit' }),
          el('td', { class: 'text-center' }, el('div', { class: 'form-check form-switch d-inline-block m-0' }, sw)),
          el('td', { class: 'text-end text-nowrap' },
            IS_STAFF ? el('button', { type: 'button', class: 'btn btn-xs btn-outline-primary', text: 'Edit', onclick: () => showForm(p.id) }) : null)));
      }
    }

    function showForm(id) {
      editing = id; disarmDelete(); showError('');
      const p = id === 'new' ? null : state.profiles.find((x) => x.id === id);
      if (id !== 'new' && !p) return showList();
      const pol = (p && p.execution_policy) || {};
      $('#pm-title').textContent = p ? `Edit profile: ${p.name}` : 'New agent profile';
      // agent options: available first
      const sel = clear($('#pm-adapter'));
      const adapters = [...state.adapters].sort((x, y) => (y.available - x.available));
      for (const a of adapters) sel.append(el('option', { value: a.identifier, text: `${a.display_name || a.identifier}${a.available ? '' : ' (unavailable)'}` }));
      if (p && !adapters.some((a) => a.identifier === p.adapter)) sel.append(el('option', { value: p.adapter, text: `${p.adapter} (unknown)` }));
      sel.value = p ? p.adapter : (adapters[0] ? adapters[0].identifier : '');
      // role chips
      const chips = clear($('#pm-role-chips')), dl = clear($('#pm-role-options'));
      for (const [r, help] of ROLES) {
        chips.append(el('button', { type: 'button', class: 'btn btn-xs btn-outline-secondary', text: r, title: help, dataset: { role: r }, onclick: () => setRole(r) }));
        dl.append(el('option', { value: r, label: help }));
      }
      nameTouched = !!p;
      $('#pm-name').value = p ? p.name : '';
      $(pol.read_only ? '#pm-access-ro' : '#pm-access-edit').checked = true;
      $('#pm-model').value = pol.model || '';
      $('#pm-timeout').value = Math.round((pol.timeout_seconds || 900) / 60);
      $('#pm-maxturns').value = pol.max_turns || '';
      $('#pm-commands').value = (pol.allowed_commands || []).join('\n');
      $('#pm-enabled').checked = p ? p.enabled : true;
      $('#pm-advanced').open = !!(pol.model || pol.max_turns || (pol.allowed_commands || []).length || (pol.timeout_seconds && pol.timeout_seconds !== 900));
      setRole(p ? p.role : 'implementer');
      updateAdapterStatus();
      $('#pm-delete').classList.toggle('d-none', !p);
      $('#pm-delete').title = p && p.task_count ? `Used by ${p.task_count} past task(s); their history is kept` : '';
      $('#pm-list-view').classList.add('d-none'); $('#pm-form').classList.remove('d-none');
      $('#pm-list-footer').classList.add('d-none'); $('#pm-form-footer').classList.remove('d-none');
      setTimeout(() => $('#pm-adapter').focus(), 50);
    }

    function disarmDelete() {
      if (deleteArmed) clearTimeout(deleteArmed);
      deleteArmed = null;
      const b = $('#pm-delete'); b.textContent = 'Delete'; b.classList.replace('btn-danger', 'btn-outline-danger');
    }

    async function save(e) {
      e.preventDefault();
      showError('');
      const name = $('#pm-name').value.trim(), role = $('#pm-role').value.trim();
      const minutes = Number($('#pm-timeout').value || 15);
      if (!$('#pm-adapter').value) return showError('Choose an agent.');
      if (!role) return showError('Choose or type a role.');
      if (!name) return showError('Give the profile a name.');
      if (!Number.isInteger(minutes) || minutes < 1 || minutes > 60) return showError('Time limit must be 1–60 minutes.');
      const body = {
        name, role, adapter: $('#pm-adapter').value, enabled: $('#pm-enabled').checked,
        execution_policy: {
          read_only: $('#pm-access-ro').checked || $('#pm-adapter').value === 'verify',
          model: $('#pm-adapter').value === 'verify' ? null : ($('#pm-model').value.trim() || null), timeout_seconds: minutes * 60,
          max_turns: $('#pm-adapter').value === 'claude-code' && $('#pm-maxturns').value ? Number($('#pm-maxturns').value) : null,
          allowed_commands: $('#pm-adapter').value === 'antigravity' && !$('#pm-access-ro').checked
            ? $('#pm-commands').value.split('\n').map((s) => s.trim()).filter(Boolean) : [],
        },
      };
      const btn = $('#pm-save'); btn.disabled = true;
      try {
        if (editing === 'new') await api('POST', '/api/profiles', body);
        else await api('PATCH', `/api/profiles/${editing}`, body);
        toast(editing === 'new' ? `Profile "${name}" created` : `Profile "${name}" saved`, 'ok');
        await loadAgents();
        showList();
      } catch (err) { showError(err.message); } finally { btn.disabled = false; }
    }

    async function remove() {
      const p = state.profiles.find((x) => x.id === editing);
      if (!p) return;
      if (!deleteArmed) {
        const b = $('#pm-delete'); b.textContent = 'Click again to delete'; b.classList.replace('btn-outline-danger', 'btn-danger');
        deleteArmed = setTimeout(disarmDelete, 4000);
        return;
      }
      disarmDelete();
      try {
        await api('DELETE', `/api/profiles/${p.id}`);
        toast(`Profile "${p.name}" deleted`, 'ok');
        await loadAgents(); showList();
      } catch (err) { showError(err.message); }
    }

    $('#pm-form').addEventListener('submit', save);
    $('#pm-new').addEventListener('click', () => showForm('new'));
    $('#pm-back').addEventListener('click', showList);
    $('#pm-delete').addEventListener('click', remove);
    $('#pm-adapter').addEventListener('change', () => { updateAdapterStatus(); updateName(); });
    $('#pm-role').addEventListener('input', () => setRole($('#pm-role').value.trim(), true));
    $('#pm-name').addEventListener('input', () => { nameTouched = $('#pm-name').value.trim() !== ''; });
    modalEl.addEventListener('hidden.bs.modal', () => { if (editing !== null) showList(); editing = null; disarmDelete(); });

    function open(id) {
      settings.show('profiles');
      if (id && IS_STAFF) showForm(id); else showList();
    }
    // keep the list view current when profiles reload (unless a profile is being edited)
    function refresh() {
      if (editing === null) renderTable();
      // the form was opened before the agent list arrived: fill its Agent choices now
      else if (editing === 'new' && !$('#pm-adapter').options.length && state.adapters.length) showForm('new');
    }
    return { open, refresh };
  })();

  // ---------- images attached to a task or a follow-up: pick, paste or drop ----------
  // Limits match cs_common/images.py (the server checks again, and sniffs the real type).
  const IMG_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
  const IMG_MAX = 6, IMG_MAX_MB = 5, IMG_TOTAL_MB = 15;
  const fmtKB = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  function imagePicker(host, textarea) {
    let items = [];  // { name, size, dataUrl }
    const input = el('input', { type: 'file', accept: IMG_TYPES.join(','), multiple: true, class: 'd-none' });
    const count = el('span', { class: 'meta' });
    const thumbs = el('div', { class: 'attach-thumbs' });
    host.append(el('div', { class: 'attach-bar' },
      el('button', { class: 'btn btn-xs btn-outline-secondary', type: 'button', onclick: () => input.click(),
        title: `Attach up to ${IMG_MAX} images (PNG, JPEG, GIF or WebP, ${IMG_MAX_MB} MB each). You can also paste or drop them into the text box.` }, '📎 Add images'),
      count, input), thumbs);
    function render() {
      clear(thumbs).append(...items.map((it, i) => el('figure', { class: 'attach-thumb', title: `${it.name} (${fmtKB(it.size)})` },
        el('img', { src: it.dataUrl, alt: it.name }),
        el('button', { class: 'attach-remove', type: 'button', 'aria-label': `Remove ${it.name}`, title: 'Remove',
          onclick: () => { items.splice(i, 1); render(); } }, '×'))));
      count.textContent = items.length ? `${items.length} image${items.length === 1 ? '' : 's'} attached` : '';
    }
    const readUrl = (f) => new Promise((resolve, reject) => {
      const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(r.error); r.readAsDataURL(f);
    });
    async function add(files) {
      for (const f of files) {
        const name = f.name || `pasted-image.${f.type.split('/')[1] || 'png'}`;
        if (!IMG_TYPES.includes(f.type)) { toast(`"${name}" is not a PNG, JPEG, GIF or WebP image`, 'warn'); continue; }
        if (items.length >= IMG_MAX) { toast(`Attach at most ${IMG_MAX} images`, 'warn'); break; }
        if (f.size > IMG_MAX_MB * 1048576) { toast(`"${name}" is larger than ${IMG_MAX_MB} MB`, 'warn'); continue; }
        if (items.reduce((n, x) => n + x.size, 0) + f.size > IMG_TOTAL_MB * 1048576) { toast(`Images may be at most ${IMG_TOTAL_MB} MB together`, 'warn'); break; }
        try { items.push({ name, size: f.size, dataUrl: await readUrl(f) }); } catch (e) { toast(`Could not read "${name}"`, 'warn'); }
      }
      render();
    }
    const imageFiles = (dt) => [...((dt && dt.files) || [])].filter((f) => f.type.startsWith('image/'));
    input.addEventListener('change', () => { add([...input.files]); input.value = ''; });
    textarea.addEventListener('paste', (e) => {
      const files = imageFiles(e.clipboardData);
      if (!files.length) return;
      if (!e.clipboardData.getData('text/plain')) e.preventDefault();  // a plain screenshot: don't paste its name
      add(files);
    });
    textarea.addEventListener('dragover', (e) => {
      if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); textarea.classList.add('attach-drop'); }
    });
    textarea.addEventListener('dragleave', () => textarea.classList.remove('attach-drop'));
    textarea.addEventListener('drop', (e) => {
      textarea.classList.remove('attach-drop');
      const files = imageFiles(e.dataTransfer);
      if (files.length) { e.preventDefault(); add(files); }
    });
    return {
      payload: () => items.map((it) => ({ name: it.name, data: it.dataUrl.slice(it.dataUrl.indexOf(',') + 1) })),
      clear() { items = []; render(); },
    };
  }
  // attached images shown in a summary: thumbnails that open the full image
  function imageStrip(list, label) {
    if (!list || !list.length) return null;
    return el('div', { class: 'ts-images' }, label ? el('span', { class: 'meta', text: label }) : null,
      el('div', { class: 'attach-thumbs' }, list.map((im) => el('a', { class: 'attach-thumb', href: im.url, target: '_blank', rel: 'noopener',
        title: `${im.name}${im.size ? ` (${fmtKB(im.size)})` : ''}: open full size` },
        el('img', { src: im.url, alt: im.name, loading: 'lazy' })))));
  }
  const taskImages = imagePicker($('#task-images'), $('#task-objective'));
  const followupImages = imagePicker($('#followup-images'), $('#followup-message'));

  $('#task-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!pid()) return toast('Select a project first', 'warn');
    const assignments = lineupSteps(state.run).filter((s) => s.ok).map((s) => ({ profile_id: s.p.id, role: s.role }));
    if (!assignments.length) return toast('Pick at least one available agent profile', 'warn');
    try {
      const body = { objective: $('#task-objective').value, assignments };
      const images = taskImages.payload();
      if (images.length) body.images = images;
      const btn = $('#task-submit');
      btn.disabled = true;
      let t;
      try { t = await api('POST', P('tasks'), body); } finally { btn.disabled = false; }
      $('#task-objective').value = '';
      taskImages.clear();
      resetRunToDefault();                     // a change "for this task" ends with that task
      $('#task-change').open = false;
      toast('Task submitted', 'ok');
      await loadTasks(); selectTask(t.id);
    } catch (err) { fail(err); loadTasks(); }
  });

  const STATE_CLS = { awaiting_review: 'text-bg-warning', running: 'text-bg-primary', preparing: 'text-bg-primary', queued: 'text-bg-info', applied: 'text-bg-success', completed: 'text-bg-success', failed: 'text-bg-danger', engine_offline: 'text-bg-danger', cancelled: 'text-bg-secondary', rejected: 'text-bg-secondary', interrupted: 'text-bg-danger' };
  const TERMINAL = new Set(['awaiting_review', 'completed', 'failed', 'cancelled', 'applied', 'rejected', 'interrupted', 'engine_offline']);
  let taskPoll = null;
  async function loadTasks() {
    const g = nextGen('tasks');
    if (!pid()) { clear($('#task-list')); return; }
    try {
      const data = await api('GET', P('tasks'));
      if (stale('tasks', g)) return;
      const box = clear($('#task-list'));
      const before = state.taskStates || null;
      state.taskStates = Object.fromEntries(data.tasks.map((t) => [t.id, t.state]));
      if (before) for (const t of data.tasks) {
        if (before[t.id] && !TERMINAL.has(before[t.id]) && TERMINAL.has(t.state)) notifyTaskFinished(t);
      }
      state.tasks = data.tasks;
      if (!data.tasks.length) box.append(el('div', { class: 'empty', text: 'No tasks yet.' }));
      for (const t of data.tasks) {
        box.append(el('div', { class: `item ${state.currentTask === t.id ? 'active' : ''}`, onclick: () => selectTask(t.id), dataset: { task: t.id, state: t.state } },
          el('span', { class: `badge state-badge ${STATE_CLS[t.state] || 'text-bg-secondary'}`, text: t.state.replace('_', ' ') }),
          el('span', { class: 'grow', text: t.objective.split('\n')[0], title: t.objective }),
          el('span', { class: 'meta', text: t.assignments.map((a) => a.role).join('→') })));
      }
      clearTimeout(taskPoll);
      if (data.tasks.some((t) => !TERMINAL.has(t.state))) taskPoll = setTimeout(loadTasks, 4000);
    } catch (e) { if (!stale('tasks', g)) clear($('#task-list')).append(el('div', { class: 'empty', text: e.message })); }
  }
  $('#btn-refresh-tasks').addEventListener('click', loadTasks);

  function fmtEvent(ev) {
    const p = ev.payload || {};
    const ts = new Date((typeof ev.ts === 'number' ? ev.ts * 1000 : Date.parse(ev.ts))).toLocaleTimeString();
    let text = p.text || p.error || '';
    if (ev.type === 'task.state') text = `state → ${p.state}${p.error ? ' (' + p.error + ')' : ''}`;
    else if (ev.type === 'agent.tool_use') text = `${p.tool}: ${p.input || ''}`;
    else if (ev.type === 'assignment.started') text = `${p.role} via ${p.adapter} (${p.profile})`;
    else if (ev.type === 'assignment.finished') text = `status=${p.status}${p.exit_code !== undefined && p.exit_code !== null ? ' exit=' + p.exit_code : ''}${p.error ? ' ' + p.error : ''}`;
    else if (ev.type === 'changes.ready') text = `${p.count} changed file(s)`;
    else if (ev.type === 'workspace.ready') text = `staging copy: ${p.files} files`;
    else if (ev.type === 'agent.result') text = `RESULT: ${p.text || ''}${p.cost_usd ? ` ($${Number(p.cost_usd).toFixed(4)})` : ''}`;
    else if (!text) text = JSON.stringify(p);
    return `[${ts}] #${ev.seq} ${ev.type.padEnd(20)} ${text}\n`;
  }

  // ---------- task summary (what happened + each agent's reply) ----------
  // Agents reply in Markdown. It is rendered by building DOM nodes (never
  // innerHTML); links are allowed only for http(s) and open in a new tab.
  const MD_INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|(?<![\w])_([^_\s][^_]*)_(?![\w])|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  function mdInline(text) {
    const out = [];
    const re = new RegExp(MD_INLINE.source, 'g');
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[2] !== undefined) out.push(el('code', { text: m[2] }));
      else if (m[3] !== undefined || m[4] !== undefined) out.push(el('strong', {}, mdInline(m[3] ?? m[4])));
      else if (m[5] !== undefined || m[6] !== undefined) out.push(el('em', {}, mdInline(m[5] ?? m[6])));
      else if (m[7] !== undefined) out.push(el('a', { href: m[8], target: '_blank', rel: 'noopener noreferrer' }, mdInline(m[7])));
      else out.push(el('a', { href: m[9], target: '_blank', rel: 'noopener noreferrer', text: m[9] }));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function renderMarkdown(src) {
    const root = el('div', { class: 'md' });
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    let para = [], list = null;
    const flushPara = () => { if (para.length) { root.append(el('p', {}, mdInline(para.join(' ')))); para = []; } };
    const flushList = () => { list = null; };
    const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)/);
      if (fence) {
        flushPara(); flushList();
        const body = [];
        while (++i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i]);
        root.append(el('pre', { class: 'md-code' }, el('code', { text: body.join('\n') })));
        continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      const h = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) { flushPara(); flushList(); root.append(el(`h${Math.min(6, h[1].length + 2)}`, { class: 'md-h' }, mdInline(h[2]))); continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); flushList(); root.append(el('hr')); continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flushPara(); flushList();
        const table = el('table', { class: 'table table-sm table-bordered md-table' });
        table.append(el('thead', {}, el('tr', {}, cells(line).map((c) => el('th', {}, mdInline(c))))));
        const tb = el('tbody'); table.append(tb);
        i++;
        while (i + 1 < lines.length && /^\s*\|.*\|\s*$/.test(lines[i + 1])) tb.append(el('tr', {}, cells(lines[++i]).map((c) => el('td', {}, mdInline(c)))));
        root.append(el('div', { class: 'table-responsive' }, table));
        continue;
      }
      const q = line.match(/^\s*>\s?(.*)$/);
      if (q) { flushPara(); flushList(); root.append(el('blockquote', { class: 'md-quote' }, mdInline(q[1]))); continue; }
      const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        flushPara();
        const ordered = /\d/.test(li[2]);
        const depth = Math.floor(li[1].replace(/\t/g, '  ').length / 2);
        if (!list || list.ordered !== ordered) { list = { ordered, node: el(ordered ? 'ol' : 'ul') }; root.append(list.node); }
        const task = li[3].match(/^\[([ xX])\]\s+(.*)$/);
        list.node.append(el('li', { class: depth ? `md-depth-${Math.min(depth, 4)}` : null },
          task ? [task[1].trim() ? '☑ ' : '☐ ', ...mdInline(task[2])] : mdInline(li[3])));
        continue;
      }
      if (list && /^\s{2,}\S/.test(line)) { list.node.lastChild.append(' ', ...mdInline(line.trim())); continue; }
      flushList();
      para.push(line.trim());
    }
    flushPara();
    return root;
  }

  function fmtDuration(ms) {
    if (!(ms >= 0)) return '';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }
  const span = (a, b) => (a && b ? Date.parse(b) - Date.parse(a) : NaN);

  // One plain-language sentence for the outcome.
  function outcomeLine(t) {
    const o = baseOutcome(t);
    // the latest Verify step decides whether the work was shown to pass its checks
    const lastCheck = [...t.assignments].reverse().find((a) => a.stats && typeof a.stats.checks_total === 'number');
    if (lastCheck && TERMINAL.has(t.state) && lastCheck.stats.checks_failed)
      return { cls: 'danger', text: `${o.text} Checks failed in step ${lastCheck.order + 1}: ${lastCheck.stats.checks_failed} of ${lastCheck.stats.checks_total}.` };
    if (lastCheck && TERMINAL.has(t.state)) return { ...o, text: `${o.text} All checks passed in step ${lastCheck.order + 1}.` };
    return o;
  }
  function baseOutcome(t) {
    const n = (t.changes_summary || []).length;
    const files = `${n} file${n === 1 ? '' : 's'}`;
    const failedStep = [...t.assignments].reverse().find((a) => ['failed', 'timed_out', 'cancelled'].includes(a.status));
    switch (t.state) {
      case 'awaiting_review': return { cls: 'warning', text: `Finished. The agents changed ${files}. Nothing touches your project until you approve it in the Agent team panel.` };
      case 'completed': return { cls: 'success', text: 'Finished. No files were changed, so the replies below are the result.' };
      case 'applied': return { cls: 'success', text: `Finished, and the changes were applied to the project${n ? ` (${files} proposed)` : ''}.` };
      case 'rejected': return { cls: 'secondary', text: 'Finished. The proposed changes were rejected, so the project was not touched.' };
      case 'failed': return { cls: 'danger', text: failedStep ? `Stopped at step ${failedStep.order + 1} (${failedStep.role}): ${failedStep.error || failedStep.status}. Later steps did not run.` : `Failed: ${t.error || 'unknown error'}` };
      case 'cancelled': return { cls: 'secondary', text: 'Cancelled. Replies from steps that finished are shown below.' };
      case 'interrupted': return { cls: 'danger', text: 'Interrupted. The agent engine restarted while this task was running.' };
      case 'engine_offline': return { cls: 'danger', text: `Not started. The agent engine was offline${t.error ? ': ' + t.error : ''}.` };
      default: {
        const cur = t.assignments.find((a) => a.status === 'running');
        return { cls: 'primary', text: cur ? `Working: step ${cur.order + 1} of ${t.assignments.length} (${cur.role}). Live output is in the Task log tab.` : `${t.state.replace('_', ' ')}…` };
      }
    }
  }

  const STEP_LABEL = { succeeded: 'done', failed: 'failed', timed_out: 'timed out', cancelled: 'cancelled', skipped: 'skipped', pending: 'waiting', running: 'working…' };
  const STEP_CLS = { succeeded: 'text-bg-success', failed: 'text-bg-danger', timed_out: 'text-bg-danger', cancelled: 'text-bg-secondary', skipped: 'text-bg-secondary', pending: 'text-bg-secondary', running: 'text-bg-primary' };

  function copyText(text, what) {
    const done = () => toast(`${what} copied`, 'ok');
    const fallback = () => {
      const ta = el('textarea', { class: 'visually-hidden' }); ta.value = text; document.body.append(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { toast('Copy failed. Select the text and copy it yourself.', 'warn'); }
      ta.remove();
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  function buildSummary(t, { compact = false } = {}) {
    const root = el('div', { class: 'ts', dataset: { taskId: t.id } });
    const o = outcomeLine(t);
    const total = fmtDuration(span(t.started_at, t.finished_at));
    const head = el('div', { class: 'ts-head' },
      el('span', { class: `badge state-badge ${STATE_CLS[t.state] || 'text-bg-secondary'}`, text: t.state.replace('_', ' ') }),
      el('span', { class: 'ts-objective', text: t.objective.split('\n')[0], title: t.objective }),
      el('span', { class: 'meta ms-auto', text: [total && `took ${total}`, t.requester && `by ${t.requester}`].filter(Boolean).join(' · ') }),
      compact ? el('button', { class: 'btn btn-xs', type: 'button', id: 'btn-summary-enlarge', title: 'Open the summary in a large window', onclick: () => openSummaryModal() }, '⤢ Enlarge') : null);
    root.append(head, el('div', { class: `ts-outcome border-${o.cls}`, role: 'status', dataset: { outcome: t.state } }, o.text));
    const objImages = imageStrip(t.attachments, 'Images attached to the task:');
    if (objImages) root.append(objImages);

    const replies = t.assignments.filter((a) => a.result);
    if (replies.length > 1) root.append(el('div', { class: 'ts-actions' },
      el('button', { class: 'btn btn-xs', type: 'button', onclick: () => copyText(replies.map((a) => `## Step ${a.order + 1}: ${a.role}\n\n${a.result}`).join('\n\n'), 'All replies') }, '⧉ Copy all replies')));

    for (const a of t.assignments) {
      const st = a.stats || {};
      const facts = [fmtDuration(span(a.started_at, a.finished_at)) || (st.duration_ms ? fmtDuration(st.duration_ms) : ''),
        st.num_turns ? `${st.num_turns} turn${st.num_turns === 1 ? '' : 's'}` : '',
        typeof st.cost_usd === 'number' ? `$${st.cost_usd.toFixed(4)}` : ''].filter(Boolean).join(' · ');
      const step = el('section', { class: 'ts-step', dataset: { step: a.order, status: a.status } },
        el('div', { class: 'ts-step-head' },
          el('span', { class: 'assign-step', text: String(a.order + 1) }),
          el('strong', { text: a.role }),
          a.followup_of !== null && a.followup_of !== undefined ? el('span', { class: 'badge text-bg-info', title: 'Continued the conversation of an earlier step', text: `follow-up to step ${a.followup_of + 1}` }) : null,
          el('span', { class: 'meta', text: `${shortAgentLabel(a.adapter)}${a.profile ? ` · ${a.profile}` : ''}` }),
          el('span', { class: `badge ${STEP_CLS[a.status] || 'text-bg-secondary'}`, text: STEP_LABEL[a.status] || a.status }),
          typeof st.checks_total === 'number' ? el('span', { class: `badge ${st.checks_failed ? 'text-bg-danger' : 'text-bg-success'}`, dataset: { checks: st.checks_failed ? 'failed' : 'passed' },
            text: st.checks_failed ? `${st.checks_failed} of ${st.checks_total} checks failed` : `${st.checks_total} check${st.checks_total === 1 ? '' : 's'} passed` }) : null,
          el('span', { class: 'meta ms-auto', text: facts }),
          a.result ? el('button', { class: 'btn btn-xs', type: 'button', title: 'Copy this reply (Markdown)', onclick: () => copyText(a.result, 'Reply') }, '⧉ Copy') : null));
      if (a.message) step.append(el('blockquote', { class: 'ts-followup-msg', title: 'Your follow-up message' }, el('strong', { text: 'You: ' }), a.message));
      if (a.attachments && a.attachments.length) step.append(imageStrip(a.attachments));
      if (a.result) step.append(el('div', { class: 'ts-reply' }, renderMarkdown(a.result)));
      if (a.error && !['succeeded'].includes(a.status)) step.append(el('div', { class: 'ts-error mono small', text: a.error }));
      if (!a.result && !a.error) step.append(el('div', { class: 'empty', text: { pending: 'Waiting for the earlier steps.', running: 'Working… the reply appears here when this step finishes.', skipped: 'Did not run because an earlier step failed or the task was cancelled.' }[a.status] || 'No reply was returned.' }));
      root.append(step);
    }

    const ch = t.changes_summary || [];
    if (ch.length) {
      const counts = ch.reduce((m, c) => ((m[c.action] = (m[c.action] || 0) + 1), m), {});
      const box = el('section', { class: 'ts-step ts-changes' },
        el('div', { class: 'ts-step-head' }, el('strong', { text: 'Files changed' }),
          el('span', { class: 'meta', text: Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ') })));
      const ul = el('ul', { class: 'ts-files mono small' });
      for (const c of ch.slice(0, 200)) ul.append(el('li', {}, el('span', { class: `badge ${c.action === 'deleted' ? 'text-bg-danger' : c.action === 'added' ? 'text-bg-success' : 'text-bg-info'}`, text: c.action }), ' ', c.path));
      if (ch.length > 200) ul.append(el('li', { class: 'meta', text: `…and ${ch.length - 200} more` }));
      box.append(ul);
      if (t.state === 'awaiting_review') box.append(el('div', { class: 'meta', text: 'To see a diff, click the file in the Agent team panel on the right. Then approve or reject.' }));
      root.append(box);
    }
    return root;
  }

  let lastSummary = null;
  const summaryModalEl = $('#summary-modal');
  const summaryModal = new bootstrap.Modal(summaryModalEl);
  // Selected-task header: big state pill + one row per pipeline step, color-coded by status.
  function renderReviewMeta(t) {
    const box = $('#review-meta');
    box.replaceChildren();
    const head = document.createElement('div');
    head.className = 'rm-head';
    const lbl = document.createElement('span');
    lbl.className = 'rm-label';
    lbl.textContent = 'State';
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = `rm-pill rm-${t.state}`;
    pill.title = 'Open the task summary';
    pill.textContent = String(t.state).replace(/_/g, ' ');
    pill.addEventListener('click', () => openSummaryModal());
    head.append(lbl, pill);
    box.append(head);
    if (t.error) {
      const err = document.createElement('div');
      err.className = 'rm-error';
      err.textContent = t.error;
      box.append(err);
    }
    if (t.assignments && t.assignments.length) {
      const ol = document.createElement('ol');
      ol.className = 'rm-steps';
      for (const a of t.assignments) {
        const li = document.createElement('li');
        li.className = `rm-step rm-${a.status}`;
        li.setAttribute('role', 'button');
        li.tabIndex = 0;
        li.title = `View this step's output (${stepFileName(a)})`;
        li.addEventListener('click', () => openStepOutput(t, a.order));
        li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openStepOutput(t, a.order); } });
        const dot = document.createElement('span');
        dot.className = 'rm-dot';
        const role = document.createElement('span');
        role.className = 'rm-role';
        role.textContent = a.role;
        const st = document.createElement('span');
        st.className = 'rm-status';
        st.textContent = String(a.status).replace(/_/g, ' ');
        li.append(dot, role, st);
        ol.append(li);
      }
      box.append(ol);
    }
    if (openStep && openStep.taskId === t.id) renderStepModal(t);  // keep an open step view live
  }

  // ---------- one step's full output: the same record the agents read from .codestudio-task/ ----------
  const stepModalEl = $('#step-modal');
  const stepModal = new bootstrap.Modal(stepModalEl);
  let openStep = null;  // { taskId, order, md, file }
  function stepFileName(a) {
    const slug = String(a.role || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
    return `step-${String(a.order + 1).padStart(2, '0')}-${slug || 'step'}.md`;
  }
  function stepMarkdown(a) {
    const agent = `${a.adapter}${a.profile && a.profile !== a.adapter ? ` (${a.profile})` : ''}`;
    const out = [`# Step ${a.order + 1}: ${a.role}`, '', `- Agent: ${agent}`, `- Status: ${a.status}`];
    if (a.read_only) out.push('- Read-only: yes');
    if (a.followup_of !== null && a.followup_of !== undefined) out.push(`- Follow-up to step ${a.followup_of + 1}`);
    if (a.message) out.push('', '## Follow-up message from the user', '', a.message);
    out.push('', '## Reply', '', a.result || '(no reply)');
    if (a.error && a.status !== 'succeeded') out.push('', '## Error', '', a.error);
    return out.join('\n') + '\n';
  }
  function renderStepModal(t) {
    const a = t.assignments.find((x) => x.order === openStep.order);
    if (!a) return;
    openStep.md = stepMarkdown(a);
    openStep.file = stepFileName(a);
    const st = a.stats || {};
    const facts = [fmtDuration(span(a.started_at, a.finished_at)) || (st.duration_ms ? fmtDuration(st.duration_ms) : ''),
      st.num_turns ? `${st.num_turns} turn${st.num_turns === 1 ? '' : 's'}` : '',
      typeof st.cost_usd === 'number' ? `$${st.cost_usd.toFixed(4)}` : ''].filter(Boolean).join(' · ');
    $('#step-modal-title').textContent = `Step ${a.order + 1}: ${a.role}`;
    $('#step-modal-file').textContent = openStep.file;
    const body = clear($('#step-modal-body'));
    body.append(el('div', { class: 'ts-step-head mb-2' },
      el('span', { class: 'meta', text: `${shortAgentLabel(a.adapter)}${a.profile ? ` · ${a.profile}` : ''}${a.read_only ? ' · read-only' : ''}` }),
      el('span', { class: `badge ${STEP_CLS[a.status] || 'text-bg-secondary'}`, text: STEP_LABEL[a.status] || a.status }),
      a.followup_of !== null && a.followup_of !== undefined ? el('span', { class: 'badge text-bg-info', text: `follow-up to step ${a.followup_of + 1}` }) : null,
      el('span', { class: 'meta ms-auto', text: facts })));
    if (a.message) body.append(el('blockquote', { class: 'ts-followup-msg' }, el('strong', { text: 'You: ' }), a.message));
    if (a.attachments && a.attachments.length) body.append(imageStrip(a.attachments));
    if (a.result) body.append(el('div', { class: 'ts-reply' }, renderMarkdown(a.result)));
    if (a.error && a.status !== 'succeeded') body.append(el('div', { class: 'ts-error mono small', text: a.error }));
    if (!a.result && !a.error) body.append(el('div', { class: 'empty', text: { pending: 'This step has not started yet.', running: 'Working… the output appears here when this step finishes.', skipped: 'Did not run because an earlier step failed or the task was cancelled.' }[a.status] || 'No reply was returned.' }));
    const has = Boolean(a.result || a.error);
    $('#step-modal-copy').disabled = !has;
    $('#step-modal-download').disabled = !has;
  }
  function openStepOutput(t, order) {
    openStep = { taskId: t.id, order };
    renderStepModal(t);
    stepModal.show();
  }
  stepModalEl.addEventListener('hidden.bs.modal', () => { openStep = null; });
  $('#step-modal-copy').addEventListener('click', () => { if (openStep && openStep.md) copyText(openStep.md, 'Step output'); });
  $('#step-modal-download').addEventListener('click', () => {
    if (!openStep || !openStep.md) return;
    const url = URL.createObjectURL(new Blob([openStep.md], { type: 'text/markdown;charset=utf-8' }));
    const link = el('a', { href: url, download: openStep.file });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  function renderSummary(t) {
    lastSummary = t;
    const box = $('#task-summary');
    const keep = box.scrollTop;
    clear(box).append(buildSummary(t, { compact: true }));
    box.scrollTop = keep;
    if (summaryModalEl.classList.contains('show')) clear($('#summary-modal-body')).append(buildSummary(t));
  }
  function openSummaryModal() {
    if (!lastSummary) return;
    $('#summary-modal-title').textContent = `Task summary: ${lastSummary.objective.split('\n')[0].slice(0, 80)}`;
    clear($('#summary-modal-body')).append(buildSummary(lastSummary));
    summaryModal.show();
  }
  const showSummaryTab = () => bootstrap.Tab.getOrCreateInstance($('#tabbtn-tasksummary')).show();
  $('#btn-task-summary').addEventListener('click', () => { showSummaryTab(); openSummaryModal(); });

  const FINISH_MSG = { awaiting_review: ['Task finished: changes are ready for review', 'warn'], completed: ['Task finished', 'ok'], failed: ['Task failed', 'err'], cancelled: ['Task cancelled', 'info'], interrupted: ['Task interrupted', 'err'] };
  const notified = new Set();
  function notifyTaskFinished(t) {
    const key = `${t.id}:${t.assignments.length}`;  // a follow-up adds a step, so it notifies again
    if (notified.has(key) || !FINISH_MSG[t.state]) return;
    notified.add(key);
    const [msg, kind] = FINISH_MSG[t.state];
    const title = t.objective.split('\n')[0];
    toast(`${msg}: "${title.length > 60 ? title.slice(0, 60) + '…' : title}"`, kind,
      { label: 'View summary', onClick: () => selectTask(t.id, { summary: true }) });
  }

  async function selectTask(id, { summary = null } = {}) {
    state.currentTask = id;
    document.querySelectorAll('#task-list .item').forEach((n) => n.classList.toggle('active', n.dataset.task === id));
    const log = $('#task-log');
    log.textContent = '';
    clear($('#task-summary')).append(el('div', { class: 'empty', text: 'Loading summary…' }));
    lastSummary = null;
    // finished tasks open on their summary; running ones on the live log
    const known = (state.tasks || []).find((t) => t.id === id);
    const wantSummary = summary ?? (known ? TERMINAL.has(known.state) : false);
    bootstrap.Tab.getOrCreateInstance($(wantSummary ? '#tabbtn-tasksummary' : '#tabbtn-tasklog')).show();
    if (state.es) state.es.close();
    const es = new EventSource(`/api/tasks/${id}/events`);
    state.es = es;
    es.onmessage = (m) => {
      if (state.currentTask !== id) return es.close();
      const ev = JSON.parse(m.data);
      if (ev.type === 'end') { log.append(document.createTextNode(`--- stream ended (state: ${ev.state}) ---\n`)); es.close(); queueTaskUpdate(id, true); return; }
      if (ev.type === 'engine_offline') { log.append(document.createTextNode(`--- engine offline: ${ev.error} ---\n`)); es.close(); return; }
      log.append(document.createTextNode(fmtEvent(ev)));
      log.scrollTop = log.scrollHeight;
      if (ev.type === 'task.state') { queueTaskList(); queueTaskUpdate(id, TERMINAL.has(ev.payload.state)); }
      else if (ev.type === 'assignment.started' || ev.type === 'assignment.finished') queueTaskUpdate(id);
    };
    es.onerror = () => { /* EventSource reconnects with Last-Event-ID */ };
    afterTaskUpdate(id);
  }

  // Opening a task replays its whole event history, so updates arrive in
  // bursts: coalesce them into one request at a time (the latest wins).
  const upd = { timer: null, running: false, again: false, id: null, finished: false };
  function queueTaskUpdate(id, finished = false) {
    if (upd.id !== id) upd.finished = false;
    upd.id = id; upd.finished = upd.finished || finished;
    clearTimeout(upd.timer);
    upd.timer = setTimeout(runTaskUpdate, 150);
  }
  async function runTaskUpdate() {
    if (upd.running) { upd.again = true; return; }
    upd.running = true;
    const { id, finished } = upd;
    upd.finished = false;
    try { await afterTaskUpdate(id, { finished }); } finally {
      upd.running = false;
      if (upd.again) { upd.again = false; runTaskUpdate(); }
    }
  }
  let listTimer = null;
  const queueTaskList = () => { clearTimeout(listTimer); listTimer = setTimeout(loadTasks, 150); };

  async function afterTaskUpdate(id, { finished = false } = {}) {
    if (state.currentTask !== id) return;
    const g = nextGen('review');
    let t;
    try { t = await api('GET', `/api/tasks/${id}`); } catch (e) { return fail(e); }
    if (stale('review', g) || state.currentTask !== id) return;
    renderSummary(t);
    // when the selected task just finished while its log was showing, flip to the summary
    if (finished && TERMINAL.has(t.state) && $('#tabbtn-tasklog').classList.contains('active')) showSummaryTab();
    const sec = $('#review-section');
    sec.classList.remove('d-none');
    renderReviewMeta(t);
    $('#btn-cancel-task').classList.toggle('d-none', TERMINAL.has(t.state));
    renderFollowUp(t);
    const canReview = t.state === 'awaiting_review';
    $('#btn-approve').classList.toggle('d-none', !canReview);
    $('#btn-reject').classList.toggle('d-none', !canReview);
    const box = el('div');
    const show = () => { if (!stale('review', g)) clear($('#review-files')).append(...box.childNodes); };
    if (['awaiting_review', 'failed', 'cancelled', 'applied', 'rejected'].includes(t.state) && t.engine_task_id) {
      try {
        const data = await api('GET', `/api/tasks/${id}/changes`);
        if (stale('review', g)) return;
        state.reviewChanges = data.changes;
        if (!data.changes.length) box.append(el('div', { class: 'empty', text: 'No file changes proposed.' }));
        for (const c of data.changes) {
          box.append(el('div', { class: 'item', dataset: { path: c.path } },
            canReview ? el('input', { type: 'checkbox', class: 'review-check', value: c.path, checked: c.applicable, disabled: !c.applicable }) : null,
            el('span', { class: `badge ${c.action === 'deleted' ? 'text-bg-danger' : c.action === 'added' ? 'text-bg-success' : 'text-bg-info'}`, text: c.action }),
            el('span', { class: 'grow mono', text: c.path, title: c.conflict_reason || 'Click to view diff', onclick: () => showDiff(`${c.path} (proposed by agent)`, c.base_text || '', c.new_text || (c.binary ? '[binary]' : ''), c.path) }),
            c.applicable ? null : el('span', { class: 'meta text-danger', text: c.conflict_reason })));
        }
      } catch (e) { box.append(el('div', { class: 'empty', text: e.message })); }
    } else box.append(el('div', { class: 'empty', text: TERMINAL.has(t.state) ? 'No changes.' : 'Agents are working… changes appear here for review.' }));
    show();
  }

  // ---------- follow-up: continue a step's conversation in the same working copy ----------
  function renderFollowUp(t) {
    const box = $('#followup-box');
    const fu = TERMINAL.has(t.state) ? t.follow_up : null;
    box.classList.toggle('d-none', !fu);
    if (!fu) return;
    const form = $('#followup-form');
    const why = $('#followup-unavailable');
    form.classList.toggle('d-none', !fu.available);
    why.classList.toggle('d-none', fu.available);
    why.textContent = fu.reason || '';
    if (!fu.available) return;
    if (form.dataset.task !== t.id) { $('#followup-message').value = ''; $('#followup-access').value = ''; followupImages.clear(); }
    const sel = $('#followup-step');
    const keep = form.dataset.task === t.id ? sel.value : '';
    form.dataset.task = t.id;
    const steps = t.assignments.filter((a) => a.can_resume);
    clear(sel).append(...steps.map((a) => el('option', { value: String(a.order),
      text: `Step ${a.order + 1}: ${a.role} · ${shortAgentLabel(a.adapter)}${a.profile ? ` (${a.profile})` : ''}${a.read_only ? ' · read-only' : ''}` })));
    sel.value = steps.some((a) => String(a.order) === keep) ? keep : String(steps[steps.length - 1].order);
    sel.disabled = steps.length < 2;
  }
  $('#followup-message').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#followup-form').requestSubmit(); }
  });
  $('#followup-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = state.currentTask;
    const message = $('#followup-message').value.trim();
    if (!id || !message) return toast('Write what the agent should do next', 'warn');
    const access = $('#followup-access').value;
    const body = { message, step: Number($('#followup-step').value) };
    if (access) body.read_only = access === 'read';
    const images = followupImages.payload();
    if (images.length) body.images = images;
    const btn = $('#btn-followup');
    btn.disabled = true;
    try {
      await api('POST', `/api/tasks/${id}/followup`, body);
      $('#followup-message').value = '';
      followupImages.clear();
      toast('Follow-up sent', 'ok');
      await loadTasks();
      selectTask(id, { summary: false });  // reopen the live log; the old stream ended with the last run
    } catch (err) { fail(err); afterTaskUpdate(id); } finally { btn.disabled = false; }
  });

  $('#btn-approve').addEventListener('click', async () => {
    const id = state.currentTask;
    const files = [...new Set([...document.querySelectorAll('.review-check:checked')].map((n) => n.value))];
    if (!files.length) return toast('Select files to apply', 'warn');
    const body = el('div', {}, el('p', { text: `Apply ${files.length} file change(s) to the live project? A recovery snapshot is taken first; if any file changed since the task started, nothing is applied.` }),
      el('ul', { class: 'mono small' }, files.slice(0, 20).map((f) => el('li', { text: f }))));
    if (!await uiConfirm('Approve agent changes', body, 'Apply changes')) return;
    try {
      const r = await api('POST', `/api/tasks/${id}/approve`, { files, confirm: true });
      toast(`Applied ${r.applied.length} file(s) (snapshot ${r.snapshot})`, 'ok');
      loadTree(); refreshGit(); loadSnapshots(); reloadCleanTabs(r.applied); loadTasks(); afterTaskUpdate(id);
    } catch (e) {
      if (e.status === 409 && e.data.conflicts) {
        for (const c of e.data.conflicts) addProblem({ kind: 'conflict', path: c.path, message: `Agent change not applied: ${c.reason}` });
        toast('Conflicts detected — nothing was applied. See Problems.', 'err');
        afterTaskUpdate(id);
      } else fail(e);
    }
  });
  $('#btn-reject').addEventListener('click', async () => {
    if (!await uiConfirm('Reject changes', 'Discard all changes proposed by this task?', 'Reject', true)) return;
    try { await api('POST', `/api/tasks/${state.currentTask}/reject`, {}); toast('Changes rejected'); loadTasks(); afterTaskUpdate(state.currentTask); } catch (e) { fail(e); }
  });
  $('#btn-cancel-task').addEventListener('click', async () => {
    try { await api('POST', `/api/tasks/${state.currentTask}/cancel`, {}); toast('Cancellation requested'); setTimeout(() => { loadTasks(); afterTaskUpdate(state.currentTask); }, 1500); } catch (e) { fail(e); }
  });

  $('#mobile-toggle').addEventListener('click', () => $('#app').classList.toggle('show-all'));

  // ---------- local copy: is the server on a newer build? ----------
  async function checkForUpdate() {
    if (document.body.dataset.localInstall !== '1') return;
    try {
      const u = await api('GET', '/api/local/update-status');
      const b = $('#update-badge');
      if (b && u.update_available) {
        b.classList.remove('d-none');
        b.title = `Server has ${u.server.version} (${u.server.build}); this copy is ${u.local.version} (${u.local.build})`;
      }
    } catch (e) { /* offline or server unreachable: stay quiet */ }
  }

  // ---------- boot ----------
  async function boot() {
    pollEngine(); setInterval(pollEngine, 10000);
    checkForUpdate(); setInterval(checkForUpdate, 30 * 60 * 1000);
    try { await initMonaco(); } catch (e) { toast('Failed to load the editor', 'err'); }
    await Promise.allSettled([loadRoots(), loadProjects(), loadAgents()]);
    const last = Number(store.get('cs.project'));
    if (last && state.projects.some((p) => p.id === last)) selectProject(last);
    else if (state.projects.length === 1) selectProject(state.projects[0].id);
    window.codeStudio = { state, openFile, selectProject };  // for debugging & E2E tests
  }
  boot();
})();
