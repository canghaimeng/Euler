/* SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0 */
/* Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
   Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0. */
/* Read-only proof overview, reader, live research and relation graph. All runtime assets are local.
   Interface text comes from i18n.js; source content is rendered unchanged and marked data-original. */
'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const text = (value) => value == null ? '' : String(value);
const node = (tag, cls, value) => {
  const result = document.createElement(tag);
  if (cls) result.className = cls;
  if (value != null) result.textContent = text(value);
  return result;
};
const markOriginal = (element) => { element.dataset.original = ''; element.setAttribute('translate', 'no'); return element; };
const src = (tag, cls, value) => markOriginal(node(tag, cls, value));
const icon = (name) => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name); svg.append(use); svg.setAttribute('aria-hidden', 'true');
  return svg;
};
const number = (value) => fmtNumber(value);
const truncate = (value, length = 90) => text(value).length > length ? text(value).slice(0, length) + '…' : text(value);
const lower = (value) => text(value).toLowerCase();
const date = (value) => {
  if (!value) return null;
  const result = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(result.getTime()) ? null : result;
};
const fullTime = (value) => { const when = date(value); return when ? fmtDateTime(when) : t('time.unrecorded'); };
const age = (value) => { const when = date(value); return when ? fmtRelative(Math.max(0, (Date.now() - when.getTime()) / 1000)) : t('time.unrecorded'); };
const ageNode = (tag, cls, value) => { const result = node(tag, cls, age(value)); if (date(value)) { result.dataset.age = date(value).toISOString(); result.title = fullTime(value); } return result; };
const STATUS_COLORS = { documented: 'blue',
  accepted: '', verified: '', correct: '', passed: '', pass: '', running: '', active: '', in_progress: '', complete: '', completed: '', discharged: '', solved: '',
  valid: 'blue', ready_for_review: 'blue', open: 'blue', proposed: 'blue', pending: 'blue', created: 'blue', on_demand: 'blue',
  waiting_dispatch: 'neutral', idle: 'neutral', stopped: 'neutral', terminated: 'neutral', parked: 'neutral', paused: 'neutral', unknown: 'neutral', not_started: 'neutral',
  managed_interrupted: 'amber', invalid: 'amber', unverified: 'amber', blocked: 'amber', stale: 'amber', deadline: 'amber', max_rounds: 'amber', dependency_unavailable: 'amber', offline: 'amber',
  refuted: 'red', wrong: 'red', error: 'red', failed: 'red', rejected: 'red', missing: 'red', revoked: 'red',
};
function statusInfo(value) {
  const key = lower(value);
  if (key in STATUS_COLORS) return [t('status.' + key), STATUS_COLORS[key]];
  return key.endsWith('_failed') ? [t('status.checkFailed'), 'red'] : [text(value) || t('status.unknown'), 'neutral'];
}
function badge(value, label) {
  const [title, color] = statusInfo(value);
  const result = node('span', 'badge ' + color, label || title);
  result.title = text(value) || t('status.none'); return result;
}
// One display vocabulary for recorded proof states, shared by every view.
const STATE_TONES = { documented: 'open', established: 'ok', completed: 'ok', attempted: 'active', open: 'open', blocked: 'warn', proposed: 'open', parked: 'muted', refuted: 'bad', stale: 'muted', failed: 'warn', unconfirmed: 'muted' };
const STATE_ORDER = ['documented', 'blocked', 'attempted', 'open', 'proposed', 'failed', 'unconfirmed', 'established', 'completed', 'parked', 'refuted', 'stale'];
const tone = (code) => STATE_TONES[code] || 'muted';
function stateLabel(code, kind) {
  if (code === 'established' && kind === 'fact') return t('state.factEstablished');
  if (code === 'established' && kind === 'evidence') return t('state.evidenceEstablished');
  if (code === 'completed' && kind !== 'route') return t('state.done');
  return code in STATE_TONES ? t('state.' + code) : t('state.unknown');
}
function stateTag(code, kind, label) {
  const result = node('span', 'state-tag tone-' + tone(code), label || stateLabel(code, kind));
  result.dataset.state = code || 'unknown'; return result;
}
function nodeState(item, blocked) {
  const kind = item.kind; const status = text(item.status).toUpperCase();
  if (item.validity && item.validity !== 'VALID') return 'stale';
  if (status === 'DOCUMENTED') return 'documented';
  if (kind === 'fact') return status === 'ACCEPTED' ? 'established' : status === 'REVOKED' ? 'refuted' : 'stale';
  if (kind === 'route') return { COMPLETED: 'completed', ACTIVE: 'attempted', PROPOSED: 'proposed', BLOCKED: 'blocked', PARKED: 'parked', REFUTED: 'refuted' }[status] || 'open';
  // Scope validity is registration only; FAIL is a failed check, not a refutation.
  if (kind === 'evidence') return { PASS: 'established', FAIL: 'failed' }[text(item.checker_result).toUpperCase()] || 'unconfirmed';
  if (status === 'DISCHARGED') return 'established';
  if ((status === 'OPEN' || status === 'IN_PROGRESS') && blocked?.has(item.id)) return 'blocked';
  return { IN_PROGRESS: 'attempted', REFUTED: 'refuted', PARKED: 'parked' }[status] || 'open';
}
function graphStates(graph) {
  if (!graph) return new Map();
  if (graph._states) return graph._states;
  const plain = new Map((graph.nodes || []).map((item) => [item.id, nodeState(item)]));
  const blocked = new Set((graph.edges || []).filter((edge) => edge.type === 'BLOCKED_BY' && (edge.validity || 'VALID') === 'VALID' && plain.get(edge.source) !== 'established').map((edge) => edge.target));
  graph._states = new Map((graph.nodes || []).map((item) => [item.id, nodeState(item, blocked)]));
  return graph._states;
}
const stateOf = (item) => graphStates(state.graph).get(item.id) || nodeState(item);
const checkName = (key) => i18nHas('check.' + key) ? t('check.' + key) : key;
const evidenceKind = (kind) => i18nHas('evkind.' + kind) ? t('evkind.' + kind) : text(kind);
const routeStatus = (key) => i18nHas('route.' + key) ? t('route.' + key) : text(key);
const eventLabel = (kind) => i18nHas('event.' + kind) ? t('event.' + kind) : text(kind).replace(/\./g, ' · ').replace(/_/g, ' ');
const signalLabel = (kind) => i18nHas('signal.' + kind) ? t('signal.' + kind) : text(kind);
const channelName = (kind) => i18nHas('channel.' + kind) ? t('channel.' + kind) : text(kind);
const isActive = (worker) => worker.alive === true;
const activeCount = (workers) => workers.some((worker) => worker.alive == null && ['running', 'active', 'idle', 'in_progress'].includes(lower(worker.status))) ? null : workers.filter(isActive).length;
const needsAttention = (value) => ['amber', 'red'].includes(statusInfo(value)[1]);
const params = new URLSearchParams(location.search);
const state = {
  view: 'overview', project: params.get('project') || '', projects: [], generation: 0, catalogSignature: '', catalog: null,
  workspace: null, architecture: null, architectureError: '', exploration: null, explorationError: '',
  graph: null, graphLayer: params.get('layer') === 'facts' ? 'facts' : 'research', graphSignature: '',
  selected: null, pendingNode: null, focus: 'all', cy: null, layout: null, live: true, refreshing: false, lastSync: null,
  teamFilter: 'all', recordView: 'activity', channels: [], channel: null, records: [],
  graphRequest: 0, detailRequest: 0, channelRequest: 0, guideRequest: 0, nodeLimit: 100, outlineLimit: 40, activityLimit: 80,
  guideData: null, guideTrail: [], proofStep: 0, cite: null, fullText: false, visibleNodes: [], outlineOpen: new Set(['path', 'open']), claimsOpen: new Set(),
  module: null, showParked: false, signalFilter: 'all', overviewSignature: '', exploreSignature: '', lang: 'en', restore: null,
  authPromise: null, authResolve: null, authReject: null, token: '',
  // Relation map: entry view, focus projection, stable relation/record selection and computed geometry.
  mapView: ['mainline', 'frontier', 'all'].includes(params.get('map')) ? params.get('map') : null, mapFocus: null, mapGuide: null,
  relation: params.get('rel') || null, mapRecord: null, sidePane: 'focus', showEvidence: false, showAlternatives: false,
  mapRoutes: null, mapMain: null, mapDetached: 0, needsFit: true, rendering: false, hoverKey: '', relationLimit: 200, visibleEdges: [],
  seen: { key: '', keys: new Set() }, freshKeys: new Set(), pendingCite: null,
};
const LANG_KEY = 'euler-dashboard-lang'; const UI_KEY = 'euler-dashboard-ui';
(() => {
  let stored = ''; try { stored = localStorage.getItem(LANG_KEY) || ''; } catch (_) { /* storage can be disabled */ }
  const requested = params.get('lang');
  state.lang = I18N_LANGS.includes(requested) ? requested : I18N_LANGS.includes(stored) ? stored : 'en';
  if (I18N_LANGS.includes(requested)) { try { localStorage.setItem(LANG_KEY, requested); } catch (_) { /* memory only */ } }
  setCurrentLang(state.lang);
})();
try { state.token = sessionStorage.getItem('euler-dashboard-token') || ''; } catch (_) { /* storage can be disabled */ }
function saveToken(value) {
  state.token = value;
  try { if (value) sessionStorage.setItem('euler-dashboard-token', value); else sessionStorage.removeItem('euler-dashboard-token'); } catch (_) { /* memory-only authentication remains available */ }
  $('#logout-button').hidden = !value;
}
saveToken(state.token);
function requestToken(failed) {
  if (state.authPromise) return state.authPromise;
  $('#auth-error').textContent = failed ? t('auth.invalid') : '';
  $('#access-token').value = '';
  state.authPromise = new Promise((resolve, reject) => { state.authResolve = resolve; state.authReject = reject; });
  $('#auth-dialog').showModal(); $('#access-token').focus();
  return state.authPromise;
}
function finishAuth(cancel) {
  const resolve = state.authResolve; const reject = state.authReject;
  state.authPromise = null; state.authResolve = null; state.authReject = null;
  $('#auth-dialog').close();
  if (cancel) { if (reject) reject(new Error(t('auth.required'))); }
  else { if (resolve) resolve(); }
}
$('#auth-form').addEventListener('submit', (event) => {
  event.preventDefault(); const value = $('#access-token').value.trim();
  if (!value) return; saveToken(value); finishAuth(false);
});
$('#auth-cancel').onclick = () => finishAuth(true);
$('#auth-dialog').addEventListener('cancel', (event) => { event.preventDefault(); finishAuth(true); });
$('#logout-button').onclick = () => { saveToken(''); location.reload(); };
async function api(path, attempt = 0) {
  const address = new URL(path, location.origin);
  if (state.project && address.pathname !== '/api/projects' && !address.searchParams.has('project')) address.searchParams.set('project', state.project);
  path = address.pathname + address.search;
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 20000);
  let response;
  try {
    response = await fetch(path, { headers: state.token ? { Authorization: 'Bearer ' + state.token } : {}, signal: controller.signal, cache: 'no-store' });
  } catch (error) { throw new Error(t(error.name === 'AbortError' ? 'api.timeout' : 'api.offline')); }
  finally { clearTimeout(timer); }
  if (response.status === 401 && attempt < 2) {
    const failed = !!state.token; saveToken(''); await requestToken(failed); return api(path, attempt + 1);
  }
  if (!response.ok) { const error = new Error(response.status === 401 ? t('api.unauthorized') : t('api.http', { status: String(response.status) })); error.status = response.status; throw error; }
  return response.json();
}
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4000); }
function connectionError(error) {
  const banner = $('#connection-banner'); banner.replaceChildren(icon('info'), node('span', '', t(state.lastSync ? 'conn.keep' : 'conn.check', { error: error.message })));
  const retry = node('button', '', t('conn.retry')); retry.onclick = () => refresh(true); banner.append(retry); banner.hidden = false;
  $('#refresh-note').textContent = t('sync.disconnected'); document.body.classList.add('disconnected');
}
function markdown(container, value) {
  container.classList.add('md'); markOriginal(container); const source = text(value);
  if (!window.markdownit) { container.textContent = source; return; }
  const md = markdown.engine || (markdown.engine = window.markdownit({ html: false, linkify: true, breaks: false }));
  // Never load remote images embedded in untrusted research text.
  md.renderer.rules.image = (tokens, index) => md.utils.escapeHtml(tokens[index].content || '[image]');
  const math = []; const stash = (tex, display) => { const key = 'EULERMATHPLACEHOLDER' + math.length + 'END'; math.push({ tex, display, key }); return key; };
  let protectedSource = source.replace(/\\\[([\s\S]+?)\\\]/g, (_, x) => stash(x, true)).replace(/\$\$([\s\S]+?)\$\$/g, (_, x) => stash(x, true)).replace(/\\\(([\s\S]+?)\\\)/g, (_, x) => stash(x, false)).replace(/\$([^\n$]+?)\$/g, (_, x) => stash(x, false));
  let html = md.render(protectedSource);
  for (const item of math) {
    const rendered = window.katex ? window.katex.renderToString(item.tex, { displayMode: item.display, throwOnError: false, trust: false, strict: 'ignore' }) : md.utils.escapeHtml(item.tex);
    html = html.replaceAll(item.key, () => rendered);
  }
  container.innerHTML = html;
  container.querySelectorAll('a').forEach((link) => { link.target = '_blank'; link.rel = 'noopener noreferrer'; });
}
function disclosure(label, value, key, cls = 'proof-source') {
  const details = node('details', cls); details.dataset.sourceKey = key; details.append(node('summary', '', label));
  const content = node('div', 'proof-source-content'); markdown(content, value); details.append(content); return details;
}
const openKeys = (root) => new Set([...root.querySelectorAll('details[open][data-source-key]')].map((d) => d.dataset.sourceKey));
const restoreKeys = (root, keys) => root.querySelectorAll('details[data-source-key]').forEach((d) => { if (keys.has(d.dataset.sourceKey)) d.open = true; });
function setPressed(selector, key, value) { $$(selector).forEach((button) => { const active = button.dataset[key] === value; button.classList.toggle('selected', active); button.setAttribute('aria-pressed', String(active)); }); }
async function copyText(value, label = t('copy.nodeId')) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      // A tailnet HTTP origin may lack Clipboard API; support the user's copy click.
      const input = node('textarea', 'copy-buffer', value); input.setAttribute('readonly', ''); document.body.append(input); input.select();
      const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error('Clipboard unavailable');
    }
    toast(t('copy.done', { label }));
  } catch (_) { toast(t('copy.fail', { label })); }
}
// Built-in display labels may be translated; every other title is source text.
function titleOf(item) {
  const bilingual = item.display_title_i18n || state.graph?.nodes.find(n => n.id === item.id)?.display_title_i18n;
  if (bilingual) return { text: bilingual[state.lang] || bilingual.en, original: false };
  const raw = text(item.title ?? item.display_title ?? item.label ?? item.statement ?? item.id);
  if (state.lang !== 'zh') { const label = displayLabel('node', state.project, item.id, raw); return { text: label.text, original: !label.translated }; }
  const record = state.graph?.nodes.find((candidate) => candidate.id === item.id);
  const curated = item.curated ?? (record ? !!record.display_title : !!item.display_title);
  return { text: raw, original: !curated };
}
function titleNode(tag, cls, item, max) { const title = titleOf(item); const result = node(tag, cls, max ? truncate(title.text, max) : title.text); return title.original ? markOriginal(result) : result; }
function labelNode(tag, cls, label) { const result = node(tag, cls, label.text); return label.translated ? result : markOriginal(result); }
const personLabel = (name) => displayLabel('person', state.project, null, name);
function teamName(team) {
  if (team.id === 'default' && team.name === 'Research team') return { text: t('team.default'), translated: true };
  if (team.id.startsWith('unassigned') && team.name === 'Unassigned workers') return { text: t('team.unassigned'), translated: true };
  return displayLabel('team', state.project, team.id, team.name || team.id);
}
const coordinatorLabel = (name) => name === 'Coordinator' ? { text: t('team.coordinatorDefault'), translated: true } : personLabel(name);
function taskSummaryNode(tag, cls, value, max) { const label = displayLabel('taskSummary', state.project, null, value); if (max) label.text = truncate(label.text, max); return labelNode(tag, cls, label); }

// Views: overview → reader → live research → relation map → diagnostics → team interaction.
const VIEWS = ['overview', 'reader', 'explore', 'map', 'ops', 'team'];
const LEGACY_VIEWS = { graph: 'map', teams: 'ops', activity: 'ops' };
function viewFromLocation() {
  const hash = location.hash.slice(1).split('?')[0];
  if (VIEWS.includes(hash)) return hash;
  if (LEGACY_VIEWS[hash]) return LEGACY_VIEWS[hash];
  return new URLSearchParams(location.search).get('node') ? 'reader' : 'overview';
}
function switchView(view, { updateHash = true, scroll = true } = {}) {
  if (!VIEWS.includes(view)) view = 'overview';
  const previous = state.view; state.view = view; document.body.dataset.activeView = view;
  $$('.view-tab').forEach((tab) => { const active = tab.dataset.view === view; tab.classList.toggle('active', active); if (active) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current'); });
  // Narrow screens scroll the tab strip; keep the current view's tab in sight.
  $('.view-tab[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  $$('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== view; });
  if (updateHash) selectionURL();
  if (previous !== view && scroll) window.scrollTo(0, 0);
  if (view === 'overview') renderOverview(true);
  if (view === 'explore') renderExploration(true);
  if (view === 'ops') { renderOps(); if (state.recordView === 'memory') loadChannels(); }
  if (view === 'reader') enterReader();
  if (view === 'map') enterMap();
  if (view === 'team') window.EulerTeam?.enter();
}
$$('.view-tab').forEach((tab) => tab.addEventListener('click', (event) => { event.preventDefault(); switchView(tab.dataset.view); }));
document.addEventListener('click', (event) => { const link = event.target.closest('[data-view-link]'); if (link) { event.preventDefault(); switchView(link.dataset.viewLink); } });
window.addEventListener('hashchange', () => switchView(viewFromLocation(), { updateHash: false }));
function selectionURL(push = false) {
  const url = new URL(location.href);
  if (state.project) url.searchParams.set('project', state.project);
  url.searchParams.set('layer', state.graphLayer);
  if (state.selected && state.view !== 'overview' && state.view !== 'team') url.searchParams.set('node', state.selected); else url.searchParams.delete('node');
  if (state.view === 'team' && state.teamMember) url.searchParams.set('member', state.teamMember); else url.searchParams.delete('member');
  const reading = state.selected && state.view === 'reader';
  if (reading && state.proofStep > 0) url.searchParams.set('part', String(state.proofStep)); else url.searchParams.delete('part');
  if (reading && state.cite) url.searchParams.set('cite', state.cite.start + '-' + state.cite.end); else url.searchParams.delete('cite');
  const mapping = state.view === 'map';
  if (mapping && state.mapView) url.searchParams.set('map', state.mapView); else url.searchParams.delete('map');
  if (mapping && state.relation) url.searchParams.set('rel', state.relation); else url.searchParams.delete('rel');
  if (state.lang !== 'zh') url.searchParams.set('lang', state.lang); else url.searchParams.delete('lang');
  url.hash = state.view;
  history[push ? 'pushState' : 'replaceState'](null, '', url);
}
function setLayer(layer) {
  if (layer === state.graphLayer && state.graph) return false;
  state.graphLayer = layer; setPressed('[data-layer]', 'layer', layer);
  state.guideRequest++; state.guideData = null; state.guideTrail = []; state.graph = null; state.graphSignature = ''; state.nodeLimit = 100;
  state.cy?.elements().remove();
  return true;
}
function openReader(id, layer = 'research') {
  setLayer(layer);
  state.selected = null; state.guideTrail = []; state.guideData = null; state.guideRequest++; state.pendingNode = id || null; state.proofStep = 0; state.cite = null; state.fullText = false;
  if (state.view === 'reader') enterReader(); else switchView('reader');
}
// Opening a record in the map selects it inside the current entry view; only an explicit
// upstream/downstream request narrows the map to that record's chain.
function openMap(id, layer = 'research', focus = 'neighbors') {
  setLayer(layer); resetFilters();
  state.selected = id || null; state.relation = null; state.mapRecord = null;
  state.focus = id && focus !== 'neighbors' ? focus : 'all';
  if (state.focus !== 'all') state.mapView = 'all';
  if (state.view === 'map') enterMap(); else switchView('map');
}
// A roadmap citation opens the reader at that exact, guide-validated source range.
function openReaderAt(id, layer, cite) { state.pendingCite = cite; openReader(id, layer); }

// Language: catalog-driven re-render that keeps the reader's place.
function captureAnchor() {
  const panel = $('[data-panel="' + state.view + '"]'); if (!panel) return null;
  const candidates = [...panel.querySelectorAll('[data-scroll-key]')].filter((element) => element.offsetParent !== null);
  const top = (document.querySelector('.masthead')?.offsetHeight || 0) + 4;
  const visible = candidates.filter((element) => element.getBoundingClientRect().bottom > top);
  const anchor = visible.find((element) => element.getBoundingClientRect().top >= top - 1) || visible[0];
  return anchor ? { key: anchor.dataset.scrollKey, offset: anchor.getBoundingClientRect().top, y: window.scrollY } : { key: null, offset: 0, y: window.scrollY };
}
function restoreAnchor(anchor) {
  if (!anchor) return;
  const target = anchor.key && $('[data-panel="' + state.view + '"] [data-scroll-key="' + CSS.escape(anchor.key) + '"]');
  if (target && anchor.y > 0) window.scrollTo(0, Math.max(0, target.getBoundingClientRect().top + window.scrollY - anchor.offset));
  else window.scrollTo(0, anchor.y);
}
function viewOpenState() {
  const panel = $('[data-panel="' + state.view + '"]');
  return { keys: panel ? [...openKeys(panel)] : [], ladder: $('#ladder-section').open };
}
function setLanguage(lang) {
  if (!I18N_LANGS.includes(lang) || lang === state.lang) return;
  const anchor = captureAnchor(); const open = viewOpenState();
  state.lang = lang; setCurrentLang(lang);
  try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* memory only */ }
  applyChrome();
  state.overviewSignature = ''; state.exploreSignature = '';
  if (state.view === 'overview') renderOverview(true);
  if (state.view === 'reader') { renderGuideOutline(); if (state.guideData) renderGuide(state.guideData, { keepScroll: true }); }
  if (state.view === 'explore') renderExploration(true);
  if (state.view === 'map' && state.graph) { graphFilters(); renderGraphChrome(state.graph); buildMap(state.graph); }
  if (state.view === 'ops') { $('#quota-banner').dataset.signature = ''; renderOps(); if (state.recordView === 'memory') loadChannels(); }
  if (state.view === 'team') window.EulerTeam?.render();
  const panel = $('[data-panel="' + state.view + '"]'); if (panel) restoreKeys(panel, new Set(open.keys));
  $('#ladder-section').open = open.ladder;
  requestAnimationFrame(() => { restoreAnchor(anchor); drawArchEdges(); });
  selectionURL(); saveUi();
}
function applyChrome() {
  applyStaticText(); setPressed('[data-lang]', 'lang', state.lang); updateLive();
  if (state.catalog) updateProjectOptions(state.catalog, true);
  if (state.lastSync) $('#refresh-note').textContent = t('sync.at', { time: fmtTime(new Date(state.lastSync)) }); else if (!document.body.classList.contains('disconnected')) $('#refresh-note').textContent = t('sync.waiting');
  $('#refresh-note').title = t('sync.atTitle');
  renderIdentity();
}
$$('[data-lang]').forEach((button) => button.addEventListener('click', () => setLanguage(button.dataset.lang)));
// A per-tab snapshot restores expansion and scroll after reload; it holds no research data.
function saveUi() {
  try {
    const open = viewOpenState(); const anchor = captureAnchor();
    sessionStorage.setItem(UI_KEY, JSON.stringify({ project: state.project, view: state.view, module: state.module, showParked: state.showParked, open: open.keys, ladder: open.ladder,
      claims: [...state.claimsOpen], fullText: state.fullText, outline: [...state.outlineOpen], anchor }));
  } catch (_) { /* best effort */ }
}
function loadUi() { try { return JSON.parse(sessionStorage.getItem(UI_KEY) || 'null'); } catch (_) { return null; } }
function consumeRestore(view) {
  const snapshot = state.restore;
  if (!snapshot || snapshot.view !== view || snapshot.project !== state.project) return;
  state.restore = null;
  const panel = $('[data-panel="' + view + '"]'); if (panel) restoreKeys(panel, new Set(snapshot.open || []));
  if (view === 'overview') $('#ladder-section').open = !!snapshot.ladder;
  requestAnimationFrame(() => { restoreAnchor(snapshot.anchor); drawArchEdges(); });
}
window.addEventListener('pagehide', saveUi);
document.addEventListener('visibilitychange', () => { if (document.hidden) saveUi(); });

// Overview: problem, recorded verdict, architecture, evidence levels and where to read next.
function catalogEntry() { return state.projects.find((item) => item.id === state.project) || {}; }
function projectLabels() {
  const project = state.workspace?.project || {}; const entry = catalogEntry();
  const name = project.name ? displayLabel('name', state.project, null, project.name) : entry.name ? displayLabel('catalog', state.project, null, entry.name) : { text: state.project || t('identity.fallback'), translated: !state.project };
  const rawDescription = project.description || entry.description || '';
  const description = rawDescription === '研究工作区 · 自动发现' ? { text: t('project.discovered'), translated: true }
    : project.description ? displayLabel('description', state.project, null, project.description) : displayLabel('catalogDescription', state.project, null, rawDescription);
  return { name, description };
}
function renderIdentity() {
  const { name, description } = projectLabels();
  const nameNode = $('#project-name'); nameNode.textContent = name.text; if (name.translated) { delete nameNode.dataset.original; nameNode.removeAttribute('translate'); } else markOriginal(nameNode);
  const descriptionNode = $('#project-description'); descriptionNode.textContent = description.text; if (description.translated) { delete descriptionNode.dataset.original; descriptionNode.removeAttribute('translate'); } else markOriginal(descriptionNode);
  document.title = t('doc.title', { name: name.text });
  const quota = quotaView(); const warnings = state.workspace?.warnings || [];
  const workers = (state.workspace?.teams || []).flatMap((team) => team.workers || []);
  const alerts = [quota?.available && quota.permit !== true ? t('alert.quota') : '', warnings.length ? t('alert.warnings', { count: warnings.length }) : '', workers.some((worker) => needsAttention(worker.status)) ? t('alert.members') : ''].filter(Boolean);
  $('#ops-alert').hidden = !alerts.length; $('#ops-alert').title = alerts.join(' · ');
  $('#explore-live-dot').hidden = !workers.some(isActive);
}
function renderOverview(force = false) {

  if (state.view !== 'overview') return;
  const arch = state.architecture; const ws = state.workspace; const ex = state.exploration;
  const signature = JSON.stringify([state.lang, state.project, arch && { ...arch, updated_at: null }, ws?.project, ws?.acceptance, ws?.quota?.permit, (ws?.teams || []).map((t) => (t.workers || []).map((w) => [w.status, w.alive, w.task_summary])), ex?.signals?.slice(0, 8), ex?.workers, ex?.coordinators, state.architectureError, state.module, state.showParked]);
  if (!force && signature === state.overviewSignature) { drawArchEdges(); return; }
  state.overviewSignature = signature;
  const scroll = window.scrollY; const view = $('#overview-view'); const keys = openKeys(view);
  renderHero(arch, ws); renderVerdict(arch, ws); renderArchitecture(arch); renderLadder(arch); renderRoutes(arch); renderLiveSummary(); renderReadingPath(arch); renderBasis(arch);
  restoreKeys(view, keys); window.scrollTo(0, scroll);
  requestAnimationFrame(drawArchEdges);
  if (arch) consumeRestore('overview');
}
function renderHero(arch, ws) {
  const target = arch?.target; const title = $('#hero-title');
  if (!arch && !ws) { title.textContent = t('hero.loadingTitle'); return; }
  const heading = target?.display_title_i18n ? { text: target.display_title_i18n[state.lang] || target.display_title_i18n.en, translated: true } : target?.curated ? displayLabel('node', state.project, target.id, target.title) : projectLabels().name;
  title.textContent = heading.text; if (heading.translated) { delete title.dataset.original; title.removeAttribute('translate'); } else markOriginal(title);
  const box = $('#hero-statement'); box.replaceChildren();
  const statement = target?.statement || arch?.goal || ws?.project?.goal || '';
  if (!statement) { box.append(node('p', 'statement-empty muted', t('hero.noGoal'))); return; }
  // The headline is an existing title; the recorded statement stays one click away, unabridged.
  const details = node('details', 'statement-details'); details.dataset.sourceKey = 'hero-statement';
  const hint = node('span', 'statement-hint'); hint.append(node('span', 'when-closed', t('hero.showOriginal')), node('span', 'when-open', t('hero.hideOriginal')));
  const summary = node('summary'); summary.append(node('span', 'statement-label', t(target ? 'hero.targetLabel' : 'hero.goalLabel')), src('span', 'statement-preview', statement.replace(/\s+/g, ' ').trim()), hint);
  const body = node('div', 'statement-text'); markdown(body, statement);
  details.append(summary, body);
  if (target?.curated) details.append(node('p', 'curated-note', t('hero.curated')));
  box.append(details);
}
function renderVerdict(arch, ws) {
  const box = $('#verdict'); box.replaceChildren();
  if (!arch) { box.className = 'verdict'; box.append(node('div', 'verdict-loading', state.architectureError ? t('verdict.unavailable', { error: state.architectureError }) : t('verdict.loading'))); return; }
  const v = arch.verdict || {}; const acceptance = ws?.acceptance || {}; const closure = arch.ladder?.closure || {};
  const controller = v.target_status ? t('verdict.controller', { label: statusInfo(v.target_status)[0], code: v.target_status }) : '';
  const kinds = {
    evidence_closed: ['ok', t('verdict.closed'), [t('verdict.evidenceClosed')]],
    historical: ['ok', ws?.historical_case?.result?.[state.lang] || t('historical.result'), [ws?.historical_case?.verification?.[state.lang] || '', t('historical.sourceNote')]],
    closed: ['ok', t('verdict.closed'), [t('verdict.closedLine', { outcome: v.outcome })]],
    refuted: ['bad', t('verdict.refuted'), [t('verdict.refutedLine', { outcome: v.outcome })]],
    formal_passed: ['ok', t('verdict.formal'), [t('verdict.formalLine', { audit: closure.formal_audit || t('audit.unrecorded'), controller }), acceptance.main_declaration ? t('verdict.mainDecl', { name: acceptance.main_declaration }) : '']],
    open: ['open', t('verdict.open'), [t('verdict.openLine', { controller, formal: closure.formal_available ? t('verdict.formalStatus', { status: statusInfo(closure.formal_status)[0] }) : t('verdict.noFormal') }), t('verdict.acceptanceNote')]],
    unregistered: ['muted', t('verdict.unregistered'), [t('verdict.unregisteredLine')]],
  };
  const [toneName, title, lines] = kinds[v.code] || kinds.unregistered;
  box.className = 'verdict tone-' + toneName; box.dataset.verdict = v.code || 'unregistered';
  const head = node('div', 'verdict-head'); const mark = node('span', 'verdict-mark'); mark.append(icon(toneName === 'ok' ? 'check' : 'info'));
  head.append(mark, node('div', 'verdict-kicker', t('verdict.kicker')));
  if (arch.target) { const read = node('button', 'text-button verdict-read', t('verdict.read')); read.id = 'verdict-read'; read.append(icon('arrow')); read.onclick = () => openReader(arch.target.id, 'research'); head.append(read); }
  else if (!arch.available) { const look = node('button', 'text-button verdict-read', t('verdict.explore')); look.append(icon('arrow')); look.onclick = () => switchView('explore'); head.append(look); }
  box.append(head, node('strong', 'verdict-title', title));
  lines.filter(Boolean).forEach((line) => { const row = node('p', 'verdict-line', line); row.title = line; box.append(row); });
  const workers = (state.exploration?.workers || []); const running = workers.filter(isActive).length;
  const leading = (state.exploration?.coordinators || []).some((lead) => ['active', 'running', 'in_progress'].includes(lower(lead.status)));
  const latest = (state.exploration?.signals || []).find((item) => item.timestamp);
  const live = node('div', 'verdict-live'); live.append(node('span', 'live-dot' + (running || leading ? ' on' : '')), node('span', '', (running ? t('verdict.running', { count: running }) : workers.length ? t('verdict.idle') : t('verdict.noWorkers')) + (leading ? t('verdict.leading') : '')));
  if (latest) { live.append(node('span', 'muted', t('verdict.latest'))); live.append(ageNode('span', 'muted', latest.timestamp)); }
  box.append(live);
}
function countLine(counts, kind, order = STATE_ORDER) {
  return order.filter((code) => counts?.[code]).map((code) => stateLabel(code, kind) + ' ' + number(counts[code])).join(' · ');
}
function renderLadder(arch) {
  const list = $('#evidence-ladder'); const brief = $('#ladder-summary'); list.replaceChildren(); brief.replaceChildren();
  if (!arch?.ladder) { brief.textContent = state.architectureError ? t('ladder.unavailable') : t('ladder.loading'); return; }
  const { routes, self_reports: reports, accepted_evidence: evidence, checked_components: components, closure } = arch.ladder;
  const results = reports.by_result || {}; const obligations = components.obligations || {}; const checks = components.checks || {};
  const passedChecks = Object.values(checks).reduce((a, b) => a + b, 0);
  const verdictTitle = t('ladder.v.' + (arch.verdict?.code in { evidence_closed: 1, historical: 1, closed: 1, refuted: 1, formal_passed: 1, open: 1, unregistered: 1 } ? arch.verdict.code : 'open'));
  for (const [key, value] of [['routes', number(routes.total)], ['attempts', number(reports.attempts)], ['passed', number(evidence.passed)], ['facts', number(evidence.facts_accepted)], ['closed', number(obligations.established || 0)], ['goal', verdictTitle]]) {
    const chip = node('span', 'ladder-chip'); chip.append(node('small', '', t('ladder.chip.' + key)), node('strong', '', value)); brief.append(chip);
  }
  const otherObligations = countLine(obligations, 'obligation', ['blocked', 'attempted', 'open', 'parked', 'refuted', 'stale']);
  const steps = [
    { key: 'routes', title: t('ladder.routes'), figures: [[routes.total, 'ladder.routesUnit']], lines: [Object.entries(routes.by_status || {}).sort((a, b) => b[1] - a[1]).map(([key, value]) => routeStatus(key) + ' ' + number(value)).join(' · ') || t('ladder.noRoutes')], note: t('ladder.routesNote') },
    { key: 'reports', title: t('ladder.reports'), figures: [[reports.attempts, 'ladder.attemptsUnit']], lines: [[results.success && t('ladder.success', { n: results.success }), results.partial && t('ladder.partial', { n: results.partial }), results.blocked && t('ladder.blockedN', { n: results.blocked }), results.abandoned && t('ladder.abandoned', { n: results.abandoned })].filter(Boolean).join(' · ') || t('ladder.noAttempts'), t('ladder.reportLine', { o: reports.obstacles, c: reports.counterexamples, r: reports.conclusions })], note: t('ladder.reportsNote') },
    { key: 'evidence', title: t('ladder.evidence'), figures: [[evidence.valid, 'ladder.validUnit'], [evidence.facts_accepted, 'ladder.factsUnit']], lines: [t('ladder.passedLine', { n: evidence.passed }) + (evidence.failed ? t('ladder.failedPart', { n: evidence.failed }) : '') + (evidence.unconfirmed ? t('ladder.unconfirmedPart', { n: evidence.unconfirmed }) : ''), [evidence.stale && t('ladder.stalePart', { n: evidence.stale }), evidence.facts_revoked && t('ladder.revokedPart', { n: evidence.facts_revoked }), evidence.facts_unavailable && t('ladder.unavailablePart', { n: evidence.facts_unavailable })].filter(Boolean).join(' · ')], note: t('ladder.evidenceNote') },
    { key: 'components', title: t('ladder.components'), figures: [[obligations.established || 0, 'ladder.closedUnit']], lines: [otherObligations ? t('ladder.others', { list: otherObligations }) : Object.keys(obligations).length ? '' : t('ladder.noOthers'), passedChecks ? t('ladder.passedChecks', { list: Object.entries(checks).map(([key, value]) => checkName(key) + ' ' + number(value)).join(' · ') }) : t('ladder.noPassed')], note: t('ladder.componentsNote') },
    { key: 'closure', title: t('ladder.closure'), final: true, text: verdictTitle, lines: [closure.target_status ? t('ladder.controller', { label: statusInfo(closure.target_status)[0] + (closure.outcome ? ' · ' + closure.outcome : '') }) : t('ladder.noController'), t('ladder.formal', { label: closure.formal_available ? statusInfo(closure.formal_status)[0] + (closure.formal_audit ? t('ladder.formalAudit', { v: closure.formal_audit }) : '') : t('ladder.notConfigured') })], note: t('ladder.closureNote') },
  ];
  steps.forEach((step, index) => {
    const item = node('li', 'ladder-step' + (step.final ? ' final tone-' + ({ closed: 'ok', formal_passed: 'ok', refuted: 'bad', open: 'open' }[arch.verdict?.code] || 'muted') : '')); item.dataset.step = step.key;
    const head = node('div', 'ladder-head'); head.append(node('span', 'ladder-index', String(index + 1)), node('h3', '', step.title)); item.append(head);
    const figures = node('div', 'ladder-figures');
    if (step.text) figures.append(node('strong', 'ladder-verdict', step.text));
    for (const [value, unit] of step.figures || []) { const figure = node('span', 'ladder-figure'); figure.append(node('strong', '', number(value)), node('small', '', t(unit, { count: Number(value) || 0 }))); figures.append(figure); }
    item.append(figures);
    step.lines.filter(Boolean).forEach((line) => item.append(node('p', 'ladder-line', line)));
    item.append(node('p', 'ladder-note', step.note)); list.append(item);
  });
}
function visibleArchIds(arch) {
  const nodes = arch.nodes || []; if (state.showParked) return new Set(nodes.map((item) => item.id));
  const hidden = new Set(nodes.filter((item) => item.role !== 'target' && item.state === 'parked').map((item) => item.id));
  let changed = true;
  while (changed) {
    changed = false;
    for (const item of nodes) {
      if (hidden.has(item.id) || item.role === 'target') continue;
      const out = (arch.edges || []).filter((edge) => edge.source === item.id);
      if (out.length && out.every((edge) => hidden.has(edge.target))) { hidden.add(item.id); changed = true; }
    }
  }
  return new Set(nodes.filter((item) => !hidden.has(item.id)).map((item) => item.id));
}
function belowCounts(item) {
  const merged = {}; for (const group of [item.closure?.obligations, item.closure?.facts]) for (const [key, value] of Object.entries(group || {})) merged[key] = (merged[key] || 0) + value;
  return merged;
}
function belowLine(item) {
  const counts = belowCounts(item); const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return item.direct_premises ? t('below.premises', { n: item.direct_premises }) : t('below.none');
  return t('below.count', { n: total, rest: item.open_count ? t('below.open', { n: item.open_count }) : t('below.noneOpen') });
}
const roleLabel = (item) => t(item.role === 'target' ? 'role.target' : item.role === 'route' ? 'role.route' : item.layer === 'facts' ? 'role.fact' : 'role.module');
const recent = (value) => { const when = date(value); return when && Date.now() - when.getTime() < 2 * 3600 * 1000; };
function archCard(item, arch, titles) {
  const card = node('button', 'arch-node role-' + item.role + ' tone-' + tone(item.state) + (item.curated ? '' : ' source-title')); card.type = 'button'; card.dataset.nodeId = item.id;
  card.setAttribute('aria-pressed', String(state.module === item.id));
  const top = node('span', 'arch-node-top'); top.append(node('span', 'arch-kind', roleLabel(item)), stateTag(item.state, item.kind));
  if (recent(item.last_activity_at)) { const pulse = node('span', 'arch-pulse'); pulse.title = t('arch.pulse', { time: fullTime(item.last_activity_at) }); top.append(pulse); }
  const title = titleOf(item);
  card.append(top, titleNode('strong', 'arch-title', item, item.role === 'target' ? 120 : 96), node('span', 'arch-meta', belowLine(item)));
  const feeds = (arch.edges || []).filter((edge) => edge.source === item.id && titles.has(edge.target)).map((edge) => titles.get(edge.target));
  if (feeds.length) {
    const line = node('span', 'arch-feeds'); line.append(document.createTextNode(t('arch.feeds', { list: '' })));
    feeds.forEach((entry, index) => { if (index) line.append(document.createTextNode(state.lang === 'en' ? ', ' : '、')); const part = node('span', '', truncate(entry.text, 22)); line.append(entry.original ? markOriginal(part) : part); });
    card.append(line);
  }
  card.setAttribute('aria-label', t('arch.cardAria', { role: roleLabel(item), title: title.text, state: stateLabel(item.state, item.kind), below: belowLine(item) }));
  card.onclick = () => { state.module = item.id; renderOverview(true); saveUi(); if (matchMedia('(max-width: 1100px)').matches) $('#module-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); };
  return card;
}
function renderArchitecture(arch) {
  const columns = $('#arch-columns'); const panel = $('#module-panel'); const tools = $('#arch-tools'); const legend = $('#arch-legend');
  columns.replaceChildren(); panel.replaceChildren(); tools.replaceChildren(); legend.replaceChildren();
  const caption = $('#architecture-caption');
  if (!arch) { caption.textContent = state.architectureError ? t('arch.unavailable', { error: state.architectureError }) : t('arch.loading'); $('#architecture').classList.add('is-empty'); return; }
  caption.textContent = arch.source ? t('arch.caption.' + arch.source) : t(arch.target ? 'arch.caption.targetOnly' : 'arch.caption.none');
  const nodes = arch.nodes || [];
  $('#architecture').classList.toggle('is-empty', !nodes.length);
  $('#architecture').classList.toggle('single-tier', (arch.tiers || 0) <= 1);
  if (!nodes.length) {
    const empty = node('div', 'arch-empty'); empty.append(node('strong', '', t('arch.emptyTitle')), node('p', '', t('arch.emptyText')));
    const go = node('button', 'button primary', t('arch.emptyGo')); go.onclick = () => switchView('explore'); empty.append(go); columns.append(empty); return;
  }
  const visible = visibleArchIds(arch); const hiddenCount = nodes.length - visible.size;
  if (hiddenCount || state.showParked) {
    const toggle = node('label', 'toggle'); const input = node('input'); input.type = 'checkbox'; input.checked = state.showParked; input.id = 'show-parked';
    input.onchange = () => { state.showParked = input.checked; if (!state.showParked && state.module && !visibleArchIds(arch).has(state.module)) state.module = null; renderOverview(true); saveUi(); };
    toggle.append(input, node('span', '', state.showParked ? t('arch.showParked') : t('arch.showParkedN', { n: hiddenCount }))); tools.append(toggle);
  }
  const shown = nodes.filter((item) => visible.has(item.id));
  const titles = new Map(shown.map((item) => [item.id, titleOf(item)]));
  const tiers = [...new Set(shown.map((item) => item.tier))].sort((a, b) => a - b);
  columns.style.setProperty('--tiers', String(tiers.length));
  tiers.forEach((tier, index) => {
    const column = node('div', 'arch-tier'); column.dataset.tier = String(index); column.dataset.flow = t('arch.flow');
    const members = shown.filter((item) => item.tier === tier).sort((a, b) => a.order - b.order);
    column.append(node('div', 'arch-tier-label', t(members.some((item) => item.role === 'target') ? 'arch.tier.target' : members.every((item) => item.role === 'route') ? 'arch.tier.routes' : index === 0 && tiers.length > 1 ? 'arch.tier.base' : tiers.length > 1 ? 'arch.tier.middle' : 'arch.tier.entry')));
    members.forEach((item) => column.append(archCard(item, arch, titles)));
    if (index === 0 && arch.omitted?.length) column.append(node('div', 'arch-more', t(arch.layer === 'facts' ? 'arch.moreFacts' : 'arch.morePremises', { n: arch.omitted.length })));
    columns.append(column);
  });
  if (!state.module || !visible.has(state.module)) state.module = (shown.find((item) => item.role === 'target') || shown[0]).id;
  highlightArch(arch);
  renderModulePanel(nodes.find((item) => item.id === state.module), arch);
  for (const code of (arch.historical_case ? ['documented', 'established', 'open', 'parked'] : ['established', 'attempted', 'open', 'blocked', 'parked'])) { const entry = node('span', 'legend-item'); entry.append(node('i', 'legend-swatch tone-' + tone(code)), document.createTextNode(stateLabel(code))); legend.append(entry); }
  const solid = node('span', 'legend-item'); solid.append(node('i', 'legend-line'), document.createTextNode(t('legend.direct'))); legend.append(solid);
  if ((arch.edges || []).some((edge) => !edge.direct)) { const chain = node('span', 'legend-item'); chain.append(node('i', 'legend-line chain'), document.createTextNode(t('legend.chain'))); legend.append(chain); }
  const pulse = node('span', 'legend-item'); pulse.append(node('i', 'arch-pulse'), document.createTextNode(t('legend.recent'))); legend.append(pulse);
}
function archRelated(arch, id) {
  const related = new Set([id]); const edges = arch.edges || [];
  for (const forward of [true, false]) {
    const queue = [id];
    while (queue.length) {
      const current = queue.shift();
      for (const edge of edges) {
        const [from, to] = forward ? [edge.source, edge.target] : [edge.target, edge.source];
        if (from === current && !related.has(to)) { related.add(to); queue.push(to); }
      }
    }
  }
  return related;
}
function highlightArch(arch) {
  const related = state.module && (arch.edges || []).length ? archRelated(arch, state.module) : null;
  $$('#arch-columns .arch-node').forEach((card) => {
    const id = card.dataset.nodeId; card.classList.toggle('is-selected', id === state.module);
    card.classList.toggle('is-related', !!related && related.has(id) && id !== state.module); card.classList.toggle('is-dimmed', !!related && !related.has(id));
    card.setAttribute('aria-pressed', String(id === state.module));
  });
}
function svgNode(tag, attrs) { const result = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attrs || {})) result.setAttribute(key, value); return result; }
function drawArchEdges() {
  const svg = $('#arch-edges'); const labels = $('#arch-labels'); const canvas = $('#arch-canvas');
  svg.replaceChildren(); labels.replaceChildren();
  const arch = state.architecture;
  if (!arch || state.view !== 'overview' || !canvas.offsetWidth || matchMedia('(max-width: 760px)').matches) return;
  const box = canvas.getBoundingClientRect(); svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  svg.style.width = box.width + 'px'; svg.style.height = box.height + 'px';
  const defs = svgNode('defs');
  for (const name of ['base', 'open', 'muted', 'focus']) { const marker = svgNode('marker', { id: 'arrow-' + name, viewBox: '0 0 10 10', refX: '9', refY: '5', markerWidth: '7', markerHeight: '7', orient: 'auto' }); marker.append(svgNode('path', { d: 'M0 1 L9 5 L0 9 z', class: 'arrow-' + name })); defs.append(marker); }
  svg.append(defs);
  const related = state.module ? archRelated(arch, state.module) : null;
  const cards = new Map($$('#arch-columns .arch-node').map((card) => [card.dataset.nodeId, card]));
  const states = new Map((arch.nodes || []).map((item) => [item.id, item.state]));
  for (const edge of arch.edges || []) {
    const a = cards.get(edge.source); const b = cards.get(edge.target); if (!a || !b) continue;
    const ra = a.getBoundingClientRect(); const rb = b.getBoundingClientRect();
    const x1 = ra.right - box.left; const y1 = ra.top + ra.height / 2 - box.top; const x2 = rb.left - box.left - 3; const y2 = rb.top + rb.height / 2 - box.top;
    if (x2 <= x1 + 4) continue;
    const dx = Math.max(20, (x2 - x1) * 0.5);
    const muted = states.get(edge.source) === 'parked' || states.get(edge.target) === 'parked';
    const focused = related && related.has(edge.source) && related.has(edge.target);
    const kind = edge.via_open && !edge.direct ? 'open' : muted ? 'muted' : focused ? 'focus' : 'base';
    svg.append(svgNode('path', { d: `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`, class: 'arch-edge edge-' + kind + (edge.direct ? '' : ' chain') + (related && !focused ? ' dimmed' : ''), 'marker-end': `url(#arrow-${kind})` }));
    if (!edge.direct && edge.via_open) {
      const label = node('span', 'arch-edge-label' + (related && !focused ? ' dimmed' : ''), t('arch.chainOpen', { n: edge.via_open }));
      label.style.left = ((x1 + x2) / 2) + 'px'; label.style.top = ((y1 + y2) / 2) + 'px'; labels.append(label);
    }
  }
}
if (window.ResizeObserver) { let frame; new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(drawArchEdges); }).observe($('#arch-canvas')); }
document.fonts?.ready?.then(() => drawArchEdges());
function itemButton(item, extra) {
  const button = node('button', 'item-link'); button.type = 'button'; button.dataset.nodeId = item.id;
  button.append(stateTag(item.state, item.kind), titleNode('span', 'item-title', item, 120));
  if (extra) button.append(node('span', 'item-note', extra));
  button.onclick = () => openReader(item.id, item.layer || 'research'); return button;
}
function renderModulePanel(item, arch) {
  const panel = $('#module-panel'); panel.replaceChildren(); if (!item) return;
  panel.dataset.nodeId = item.id; panel.dataset.scrollKey = 'module';
  const head = node('div', 'module-head'); head.append(node('span', 'module-kind', roleLabel(item)), stateTag(item.state, item.kind));
  panel.append(head, titleNode('h3', 'module-title', item));
  if (item.curated) panel.append(node('p', 'curated-note', t('module.curated')));
  if (item.excerpt) {
    // The server excerpt is length-bounded; never render a formula cut in half.
    let excerpt = item.excerpt; if ((excerpt.match(/\$/g) || []).length % 2) excerpt = excerpt.slice(0, excerpt.lastIndexOf('$')).trimEnd();
    const quote = node('blockquote', 'module-excerpt'); markdown(quote, excerpt + (item.excerpt.length >= 280 ? ' …' : '')); panel.append(quote);
  }
  const facts = node('dl', 'module-facts');
  const fact = (label, value) => { if (!value) return; const row = node('div'); row.append(node('dt', '', label), node('dd', '', value)); facts.append(row); };
  const counts = belowCounts(item); const total = Object.values(counts).reduce((a, b) => a + b, 0);
  fact(t('module.below'), total ? t('module.belowCount', { n: total, list: countLine(counts, item.layer === 'facts' ? 'fact' : 'obligation') }) : item.direct_premises ? t('module.belowPremises', { n: item.direct_premises }) : t('module.belowNone'));
  fact(t('module.checks'), Object.entries(item.closure?.evidence || {}).map(([key, value]) => checkName(key) + ' ' + number(value)).join(' · '));
  if (item.role === 'route') fact(t('module.routePremises'), t('module.routeCount', { n: item.requires }) + (countLine(item.requires_states) ? ' · ' + countLine(item.requires_states) : ''));
  fact(t('module.proof'), t(item.has_proof ? 'module.hasProof' : 'module.noProof'));
  fact(t('module.latest'), item.last_activity_at ? age(item.last_activity_at) + ' · ' + eventLabel(item.last_activity_kind) : t('module.noLatest'));
  panel.append(facts);
  // Act on the selected module before the longer lists below it, which can push past the sticky panel's fold.
  const actions = node('div', 'module-actions');
  const read = node('button', 'button primary', t('module.read')); read.id = 'module-read'; read.append(icon('arrow')); read.onclick = () => openReader(item.id, item.layer || 'research');
  const map = node('button', 'button secondary', t('module.map')); map.onclick = () => openMap(item.id, item.layer || 'research', 'upstream');
  actions.append(read, map); panel.append(actions);
  window.EulerHuman?.mount(panel, item, { layer: item.layer || 'research' });
  const chains = (arch.edges || []).filter((edge) => edge.source === item.id);
  const byId = new Map((arch.nodes || []).map((entry) => [entry.id, entry]));
  if (chains.length) {
    const block = node('section', 'module-block'); block.append(node('h4', '', t('module.supports')));
    for (const edge of chains) {
      const row = node('div', 'chain-row' + (edge.via_open ? ' has-open' : ''));
      const target = byId.get(edge.target); const heading = node('strong'); heading.append(document.createTextNode('→ ')); if (target) heading.append(titleNode('span', '', target, 60));
      row.append(heading, node('span', 'muted', edge.direct ? t('module.direct') : t('module.via', { n: edge.via }) + (edge.via_open ? t('module.viaOpen', { n: edge.via_open }) : t('module.viaClosed'))));
      block.append(row); (edge.via_items || []).forEach((entry) => block.append(itemButton(entry, entry.frontier ? t('module.frontier') : '')));
    }
    panel.append(block);
  }
  if (item.open_count) {
    const block = node('section', 'module-block'); block.append(node('h4', '', t('module.openBelow', { n: item.open_count })));
    if (item.state === 'established' || item.state === 'completed') block.append(node('p', 'muted small', t('module.openNote')));
    const shownItems = (item.open_items || []).slice(0, 3);
    shownItems.forEach((entry) => block.append(itemButton(entry, entry.frontier ? t('module.frontier') : '')));
    if (item.open_count > shownItems.length) block.append(node('p', 'muted small', t('module.more', { n: item.open_count - shownItems.length })));
    panel.append(block);
  }
}
function renderRoutes(arch) {
  const box = $('#route-list'); box.replaceChildren();
  const routes = arch?.routes || [];
  if (!arch) { box.append(node('div', 'empty-inline', t('routes.loading'))); return; }
  if (!routes.length) { box.append(node('div', 'empty-inline', t('routes.none'))); return; }
  const row = (route) => {
    const button = node('button', 'route-row tone-' + tone(route.state)); button.type = 'button'; button.dataset.nodeId = route.id;
    const head = node('span', 'route-head'); head.append(stateTag(route.state, 'route'));
    const target = node('span', 'route-target');
    if (route.targets_main) target.textContent = t('routes.toMain');
    else if (route.target_titles?.[0]) { target.append(document.createTextNode('→ ')); target.append(titleNode('span', '', { id: route.target_ids?.[0], title: route.target_titles[0] }, 36)); }
    else target.textContent = t('routes.noTarget');
    head.append(target);
    button.append(head, titleNode('strong', 'route-title', route, 150), node('span', 'route-meta', t('routes.needs', { count: route.requires }) + (countLine(route.requires_states) ? ' · ' + countLine(route.requires_states) : '') + (route.evidence ? t('routes.evidence', { n: route.evidence }) : '')));
    button.onclick = () => openReader(route.id, 'research'); return button;
  };
  const primary = routes.filter((route) => !['parked', 'refuted', 'stale'].includes(route.state));
  const alternatives = routes.filter((route) => ['parked', 'refuted', 'stale'].includes(route.state));
  primary.slice(0, 6).forEach((route) => box.append(row(route)));
  const rest = primary.slice(6);
  if (rest.length) { const more = node('details', 'route-more'); more.dataset.sourceKey = 'routes-more'; more.append(node('summary', '', t('routes.more', { n: rest.length }))); rest.forEach((route) => more.append(row(route))); box.append(more); }
  if (alternatives.length) { const alt = node('details', 'route-more alternatives'); alt.dataset.sourceKey = 'routes-alternatives'; alt.append(node('summary', '', t('routes.alternatives', { n: alternatives.length }))); alternatives.forEach((route) => alt.append(row(route))); box.append(alt); }
  if (!primary.length) box.prepend(node('p', 'muted small', t('routes.noActive')));
}
function quotaReason(quota) { return i18nHas('quota.reason.' + quota.reason_code) ? t('quota.reason.' + quota.reason_code) : text(quota.reason); }
function signalClaim(entry, max) {
  if (entry.claim_i18n) return node('span', '', entry.claim_i18n[state.lang] || entry.claim_i18n.en);

  const fragment = document.createDocumentFragment();
  if (entry.event && i18nHas('event.' + entry.event)) { fragment.append(document.createTextNode(eventLabel(entry.event) + (entry.subject ? ' · ' : ''))); if (entry.subject) fragment.append(titleNode('span', '', { id: entry.node_id, title: entry.subject }, max)); }
  else if (entry.kind === 'fact' && entry.node_id) fragment.append(titleNode('span', '', { id: entry.node_id, title: entry.claim }, max));
  else fragment.append(src('span', '', max ? truncate(entry.claim, max) : entry.claim));
  return fragment;
}
function renderLiveSummary() {
  const box = $('#live-summary'); box.replaceChildren(); const data = state.exploration;
  if (!data) { box.append(node('div', 'empty-inline', state.explorationError ? t('live.unavailable', { error: state.explorationError }) : t('live.loading'))); return; }
  const quota = quotaView();
  if (quota?.available && quota.permit !== true) { const warn = node('button', 'live-alert'); warn.append(icon('info'), node('span', '', t('live.quota', { reason: quotaReason(quota) }))); warn.onclick = () => switchView('ops'); box.append(warn); }
  const people = node('div', 'live-people');
  const members = [...(data.coordinators || []).map((lead) => ({ ...lead, lead: true })), ...(data.workers || [])];
  members.slice(0, 5).forEach((member) => {
    const row = node('div', 'live-person'); row.append(node('span', 'live-dot' + (member.alive === true || (member.lead && ['active', 'running'].includes(lower(member.status))) ? ' on' : '')));
    const body = node('div', 'live-person-body'); const title = node('div', 'live-person-head'); title.append(labelNode('strong', '', member.lead ? coordinatorLabel(member.name) : personLabel(member.name || t('live.member'))), badge(member.status)); if (member.lead) title.append(node('span', 'muted small', t('live.lead')));
    body.append(title, member.task_summary ? taskSummaryNode('p', '', member.task_summary, 110) : node('p', '', t(member.lead ? 'live.leadTask' : 'live.waiting'))); row.append(body); people.append(row);
  });
  if (!members.length) people.append(node('p', 'muted small', t('live.noMembers')));
  if (members.length > 5) people.append(node('p', 'muted small', t('live.moreMembers', { n: members.length - 5 })));
  const feed = node('ol', 'live-feed');
  const records = (data.signals || []).filter((entry) => entry.kind !== 'execution').slice(0, 5);
  records.forEach((entry) => {
    const item = node('li', 'live-record kind-' + entry.kind); item.dataset.scrollKey = 'signal-' + entry.id; const head = node('div', 'live-record-head');
    head.append(node('span', 'signal-kind', signalLabel(entry.kind)), (entry.date_label ? node('time', 'muted', entry.date_label) : ageNode('time', 'muted', entry.timestamp)));
    const claim = node('p'); claim.append(signalClaim(entry, 170));
    item.append(head, claim);
    const meta = node('small', 'muted'); meta.append(entry.author ? src('span', '', entry.author) : document.createTextNode(t('live.team')), document.createTextNode(' · ' + (entry.attempt_result ? t('attempt.' + entry.attempt_result) : entry.verification === 'fact_store' ? t('live.factStore') : t('live.public'))));
    item.append(meta); feed.append(item);
  });
  if (!records.length) feed.append(node('li', 'muted small', t('live.noRecords')));
  const control = (data.signals || []).find((entry) => entry.kind === 'execution');
  box.append(people, feed);
  if (control) { const line = node('p', 'live-control muted small'); line.append(document.createTextNode(t('live.controlPrefix')), signalClaim(control), document.createTextNode(' · ')); line.append(ageNode('span', '', control.timestamp)); box.append(line); }
}
function renderReadingPath(arch) {
  const list = $('#reading-path'); list.replaceChildren(); const caption = $('#path-caption');
  if (!arch) return;
  let items = arch.reading_path || [];
  if (items.length) caption.textContent = t('path.curated');
  else {
    items = [...(arch.nodes || [])].filter((item) => item.role !== 'route' || !['parked', 'refuted'].includes(item.state)).sort((a, b) => b.tier - a.tier || a.order - b.order);
    caption.textContent = t(items.length ? 'path.structural' : 'path.empty');
  }
  $('#overview-path').hidden = !items.length;
  items.slice(0, 8).forEach((item, index) => {
    const li = node('li'); const button = node('button', 'path-step tone-' + tone(item.state)); button.type = 'button'; button.dataset.nodeId = item.id;
    const head = node('span', 'path-head'); head.append(node('span', 'path-index', String(index + 1)), node('span', 'path-layer', t(item.layer === 'facts' ? 'path.layer.facts' : item.kind === 'route' ? 'path.layer.route' : 'path.layer.research')), stateTag(item.state, item.kind));
    button.append(head, titleNode('strong', '', item, 80), node('span', 'path-proof', t(item.has_proof ? 'path.hasProof' : 'path.noProof')));
    button.onclick = () => openReader(item.id, item.layer || 'research'); li.append(button); list.append(li);
  });
}
function renderBasis(arch) {
  const box = $('#overview-basis'); box.replaceChildren(); if (!arch) return;
  const list = node('ul'); ['basis.1', 'basis.2', 'basis.3', 'basis.4'].forEach((key) => list.append(node('li', '', t(key))));
  if (arch.historical_case) list.prepend(node('li', '', t('historical.sourceNote')));
  box.append(node('h2', '', t('basis.title')), list, node('p', 'muted small', t('basis.time', { time: fullTime(arch.updated_at) })));
}

// Diagnostics: quota guard, acceptance report, leads, teams and raw activity.
function quotaView() {
  const quota = state.workspace?.quota;
  if (!quota?.available) return quota;
  const checked = date(quota.checked_at);
  const maxAge = Number(quota.max_age_seconds) || 45;
  // Auto-refresh may be paused or the server unreachable. A previously healthy
  // snapshot must still expire in the browser, without issuing any mutation.
  if (!checked || checked.getTime() > Date.now() + 30000) return { ...quota, permit: false, status: 'unknown', reason_code: 'invalid_timestamp' };
  if (Date.now() - checked.getTime() > maxAge * 1000) return { ...quota, permit: false, status: 'stale', reason_code: 'stale' };
  return quota;
}
const percent = (value) => fmtPercent(value);
function renderQuota() {
  const panel = $('#quota-banner'); const quota = quotaView();
  panel.hidden = !quota?.available;
  if (!quota?.available) return;
  const oldDetailsOpen = panel.querySelector('details')?.open || false;
  const signature = JSON.stringify([state.lang, quota.permit, quota.status, quota.reason_code, quota.remaining_percent, quota.checked_at, quota.latched, quota.providers]);
  if (panel.dataset.signature === signature) return;
  panel.dataset.signature = signature;
  panel.classList.toggle('quota-paused', quota.permit !== true); panel.dataset.permit = String(quota.permit === true);
  panel.replaceChildren();
  const heading = node('div', 'quota-heading');
  const title = node('div'); title.append(node('span', 'section-kicker', 'QUOTA GUARD'), node('h2', '', t('quota.title')));
  const amount = node('div', 'quota-amount'); amount.append(node('strong', '', percent(quota.remaining_percent)), node('span', '', t(quota.cursor_pause_scope === 'cursor_roles' ? 'quota.codexRemaining' : 'quota.conservative')));
  heading.append(title, amount);
  const summary = node('div', 'quota-summary');
  summary.append(badge(quota.permit === true ? 'passed' : quota.status === 'unknown' || quota.status === 'stale' ? 'stale' : 'blocked', t(quota.permit === true ? 'quota.allowed' : 'quota.paused')), node('p', '', quotaReason(quota)));
  const rules = node('div', 'quota-rules');
  rules.append(node('span', '', t(quota.cursor_pause_scope === 'cursor_roles' ? 'quota.codexPause' : 'quota.autoPause', { p: percent(quota.threshold_percent) })));
  if (quota.cursor_pause_scope === 'cursor_roles') { rules.append(node('span', '', t('quota.cursorStop', { p: percent(quota.cursor_threshold_percent) }))); if (quota.permit && !quota.cursor_permit) rules.append(node('span', 'quota-latched', t('quota.cursorOff'))); }
  if (quota.latched) rules.append(node('span', 'quota-latched', t('quota.latched')));
  summary.append(rules);
  const controls = node('div', 'quota-controls');
  const updated = node('span', '', t('quota.checked', { time: fullTime(quota.checked_at) }));
  const button = node('button', 'text-button', t('quota.refresh')); button.id = 'quota-refresh'; button.append(icon('refresh')); button.onclick = () => refresh(true);
  controls.append(updated, button); panel.append(heading, summary, controls);
  const details = node('details', 'quota-details'); details.open = oldDetailsOpen;
  const providers = quota.providers || {};
  details.append(node('summary', '', t('quota.windows', { codex: percent(providers.codex?.remaining_percent), cursor: percent(providers.cursor?.remaining_percent) })));
  const grid = node('div', 'quota-provider-grid');
  for (const [key, label] of [['codex', 'Codex'], ['cursor', 'Cursor']]) {
    const provider = providers[key] || {}; const card = node('div', 'quota-provider');
    card.append(node('h3', '', label), node('strong', '', percent(provider.remaining_percent)));
    for (const [index, window] of (provider.windows || []).entries()) {
      const minutes = window.window_minutes;
      const windowLabel = minutes ? (minutes % 1440 === 0 ? t('quota.days', { n: minutes / 1440 }) : minutes % 60 === 0 ? t('quota.hours', { n: minutes / 60 }) : t('quota.minutes', { n: minutes })) : t('quota.window', { n: index + 1 });
      const row = node('p', '', t('quota.windowRemaining', { label: windowLabel, p: percent(window.remaining_percent) }));
      if (window.resets_at) row.append(node('span', '', t('quota.resets', { time: fullTime(window.resets_at) })));
      card.append(row);
    }
    if (key === 'cursor') card.append(node('p', '', t('quota.onDemand', { v: t(provider.on_demand_enabled === true ? 'quota.on' : provider.on_demand_enabled === false ? 'quota.off' : 'quota.unconfirmed') })));
    grid.append(card);
  }
  details.append(grid, node('p', 'quota-footnote', t('quota.footnote')));
  panel.append(details);
}
function renderAcceptance(acceptance) {
  const panel = $('#acceptance-panel'); const wasOpen = panel.querySelector('details')?.open || false; panel.replaceChildren(); panel.hidden = !acceptance?.available;
  if (!acceptance?.available) return;
  const mark = node('span', 'acceptance-mark'); mark.append(icon(acceptance.status === 'passed' ? 'check' : 'info'));
  const body = node('div', 'acceptance-body'); const title = node('h2', '', t(acceptance.status === 'passed' ? 'acc.passed' : 'acc.record'));
  title.append(node('small', '', fullTime(acceptance.checked_at))); body.append(title);
  const audit = acceptance.independent_audit || {};
  const line = node('p', '', (acceptance.mathematical_goal_achieved === true && acceptance.status === 'passed' ? t('acc.goal') : '') + (audit.verdict ? t('acc.audit', { v: audit.verdict }) : '') + (audit.checks != null ? t('acc.checks', { n: audit.checks }) : '') + (acceptance.source_count != null ? t('acc.sources', { n: acceptance.source_count }) : ''));
  if (acceptance.source_count == null && acceptance.source_label) line.append(src('span', '', acceptance.source_label));
  body.append(line);
  const details = node('details'); details.open = wasOpen; details.append(node('summary', '', t('acc.details')));
  const evidence = node('div', 'acceptance-evidence'); const dl = node('dl');
  const pairs = [['acc.main', acceptance.main_declaration, true], ['acc.source', acceptance.main_source, true], ['SHA-256', acceptance.main_sha256, true], ['acc.auditRow', audit.verdict ? t('acc.auditValue', { v: audit.verdict, c: audit.critical_errors, g: audit.gaps }) : null, false], ['acc.labels', acceptance.paper_labels != null ? number(acceptance.paper_labels) : null, false], ['acc.from', acceptance.source_label, true], ['acc.basis', acceptance.verification_basis ? t('acc.basisText') : null, false]];
  for (const [key, value, original] of pairs) if (value != null && value !== '') { dl.append(node('dt', '', key === 'SHA-256' ? key : t(key)), original ? src('dd', '', value) : node('dd', '', value)); }
  evidence.append(dl);
  const buildNames = { 'Whole project': 'acc.build.whole', 'All prerequisites': 'acc.build.prereq' };
  for (const build of acceptance.builds || []) evidence.append(node('p', '', t('acc.build', { label: t(buildNames[build.label] || 'acc.build.default'), code: number(build.exit_code) }) + (build.jobs != null ? t('acc.jobs', { n: build.jobs }) : '')));
  if (acceptance.fact_admission_claimed === false) evidence.append(node('p', '', t('acc.noFact')));
  details.append(evidence); body.append(details); panel.append(mark, body);
}
function verifierCard(verifier) {
  const card = node('article', 'lead-card verifier-card'); card.setAttribute('aria-label', t('verifier.title'));
  const mark = node('span', 'lead-avatar'); mark.append(icon('check'));
  const body = node('div', 'lead-body'); const header = node('div', 'lead-title');
  header.append(node('span', 'section-kicker', 'VERIFICATION SERVICE'), badge(verifier.status));
  body.append(header, node('h2', '', t('verifier.title')), node('p', 'lead-model', [verifier.model, verifier.effort ? t('lead.effort', { v: verifier.effort }) : null].filter(Boolean).join(' · ') || t('lead.noModel')));
  body.append(node('p', 'lead-task', t('verifier.text')));
  const meta = node('div', 'lead-meta');
  meta.append(node('span', '', verifier.backend ? t('verifier.backend', { v: verifier.backend }) : t('verifier.noBackend')), node('span', '', t('verifier.source')));
  if (verifier.checked_at) meta.append(node('span', '', t('lead.snapshot', { time: fullTime(verifier.checked_at) })));
  body.append(meta); card.append(mark, body); return card;
}
function renderLeads(data) {
  const panel = $('#lead-panel'); panel.replaceChildren();
  const leads = (data.teams || []).filter((team) => team.coordinator);
  for (const team of leads) {
    const lead = team.coordinator; const card = node('article', 'lead-card'); const mark = node('span', 'lead-avatar', text(lead.name).slice(0, 1).toUpperCase() || 'A');
    const body = node('div', 'lead-body'); const header = node('div', 'lead-title'); header.append(node('span', 'section-kicker', 'RESEARCH LEAD'), badge(lead.status));
    body.append(header, labelNode('h2', '', lead.name ? coordinatorLabel(lead.name) : { text: t('lead.default'), translated: true }), node('p', 'lead-model', [lead.model, lead.effort ? t('lead.effort', { v: lead.effort }) : null].filter(Boolean).join(' · ') || t('lead.noModel')));
    if (lead.task_summary) body.append(taskSummaryNode('p', 'lead-task', lead.task_summary));
    const meta = node('div', 'lead-meta'); meta.append(node('span', '', t('lead.coord')), node('span', '', t('lead.workers', { n: (team.workers || []).length })));
    if (lead.goal_status) meta.append(node('span', '', t('lead.goal', { v: statusInfo(lead.goal_status)[0] })));
    if (lead.checked_at) meta.append(node('span', '', t('lead.snapshot', { time: fullTime(lead.checked_at) })));
    body.append(meta); card.append(mark, body); panel.append(card);
  }
  if (data.verifier?.available) panel.append(verifierCard(data.verifier));
  panel.hidden = !panel.children.length;
}
function renderOps() {
  const data = state.workspace; if (!data) return;
  renderQuota(); renderAcceptance(data.acceptance); renderLeads(data);
  const warnings = $('#workspace-warnings'); warnings.replaceChildren();
  for (const warning of data.warnings || []) {
    const raw = typeof warning === 'string' ? warning : warning.message || JSON.stringify(warning); const localized = localizeWarning(raw);
    const line = node('div', 'notice warning'); line.append(icon('info'), localized === raw ? src('span', '', raw) : node('span', '', localized)); warnings.append(line);
  }
  renderTeams(); if (state.recordView === 'activity') renderActivity();
  consumeRestore('ops');
}
function renderTeams() {
  if (!state.workspace) return;
  const all = state.workspace.teams || [];
  const teams = all.filter((team) => state.teamFilter === 'all' || (state.teamFilter === 'active' ? ['running', 'active'].includes(lower(team.status)) || (team.workers || []).some(isActive) : needsAttention(team.status) || (team.workers || []).some((worker) => needsAttention(worker.status))));
  $('#teams-summary').textContent = t('teams.summary', { shown: teams.length, total: all.length, workers: state.workspace.metrics?.workers });
  const grid = $('#teams-grid'); grid.replaceChildren();
  if (!teams.length) { grid.append(node('div', 'large-empty', t(all.length ? 'teams.emptyFiltered' : 'teams.empty'))); return; }
  for (const team of teams) {
    const card = node('article', 'team-card'); card.dataset.teamId = team.id; card.dataset.scrollKey = 'team-' + team.id;
    const head = node('header', 'team-card-head'); const title = node('div');
    title.append(labelNode('h3', '', teamName(team)), team.description ? labelNode('p', '', displayLabel('teamDescription', state.project, team.id, team.description)) : node('p', '', t('teams.noDescription')));
    head.append(node('span', 'team-avatar', String(all.indexOf(team) + 1).padStart(2, '0')), title, badge(team.status)); card.append(head);
    const workers = team.workers || []; const stats = node('div', 'team-stats');
    const accepted = workers.some((worker) => worker.accepted_facts != null) ? workers.reduce((sum, worker) => sum + (Number(worker.accepted_facts) || 0), 0) : null;
    for (const [value, label] of [[workers.length, 'teams.members'], [activeCount(workers), 'teams.running'], [accepted, 'teams.facts']]) { const stat = node('div', 'team-stat'); stat.append(node('strong', '', number(value)), node('span', '', t(label))); stats.append(stat); } card.append(stats);
    if (team.coordinator) { const coord = team.coordinator; const line = node('div', 'coordinator'); line.append(node('span', 'label', t(state.workspace.project?.independent ? 'teams.lead' : 'teams.coordinator')), labelNode('strong', '', coordinatorLabel(coord.name)), node('span', '', coord.model || t('teams.noModel'))); if (coord.effort) line.append(node('span', '', t('lead.effort', { v: coord.effort }))); line.append(badge(coord.status)); if (coord.goal_status) line.append(node('span', 'label', t('lead.goal', { v: statusInfo(coord.goal_status)[0] }))); card.append(line); }
    if (!workers.length) card.append(node('div', 'empty-inline', t('teams.noWorkers')));
    for (const worker of workers) {
      const block = node('section', 'worker'); block.dataset.workerId = worker.id;
      const workerHead = node('div', 'worker-head'); workerHead.append(labelNode('h4', '', personLabel(worker.name || worker.id)), worker.role ? src('span', 'worker-role', worker.role) : node('span', 'worker-role', t('worker.defaultRole')), badge(worker.status));
      const model = node('div', 'worker-model', [worker.provider, worker.model, worker.effort ? t('lead.effort', { v: worker.effort }) : null].filter(Boolean).join(' · ') || t('worker.noModel'));
      block.append(workerHead, model, worker.task_summary ? src('p', 'worker-task', worker.task_summary) : node('p', 'worker-task', t('worker.noTask')));
      const info = node('div', 'worker-stats'); info.append(node('span', '', t('worker.round', { n: worker.round })), node('span', '', t('worker.facts', { n: worker.accepted_facts })));
      const last = node('span', '', t('worker.last', { age: age(worker.last_activity_at) })); last.title = fullTime(worker.last_activity_at);
      if (isActive(worker) && date(worker.last_activity_at) && Date.now() - date(worker.last_activity_at).getTime() > 1800000) { last.classList.add('stale'); last.textContent += t('worker.stale'); }
      info.append(last);
      if (worker.alive == null) info.append(node('span', 'stale', t('worker.aliveUnknown')));
      else info.append(node('span', '', t(worker.alive ? 'worker.alive' : 'worker.dead')));
      block.append(info); card.append(block);
    }
    grid.append(card);
  }
}
$$('[data-team-filter]').forEach((button) => button.onclick = () => { state.teamFilter = button.dataset.teamFilter; setPressed('[data-team-filter]', 'teamFilter', state.teamFilter); renderTeams(); });
const activityKind = (kind) => i18nHas('channel.' + kind) ? channelName(kind) : i18nHas('event.' + kind) ? eventLabel(kind) : kind ? eventLabel(kind) : t('event.other');
function renderActivity() {
  const parent = $('#activity-list'); const scroll = window.scrollY; const keys = openKeys(parent); parent.replaceChildren(); const query = lower($('#activity-search').value.trim());
  const source = state.recordView === 'activity' ? state.workspace?.activity || [] : state.records;
  const entries = source.filter((entry) => !query || lower([entry.title, entry.claim, entry.author, entry.kind, entry.detail, entry.evidence].join(' ')).includes(query));
  if (!entries.length) { parent.append(node('div', 'large-empty', t(query ? 'activity.empty' : 'activity.none'))); return; }
  for (const entry of entries.slice(0, state.activityLimit)) {
    const card = node('article', 'record-card'); card.dataset.scrollKey = 'record-' + (entry.id || entry.timestamp || entry.timestamp_utc); const header = node('div', 'record-head');
    header.append(entry.author ? src('span', 'record-author', entry.author) : node('span', 'record-author', t('activity.project')), node('span', 'count-pill', activityKind(entry.kind || state.channel)));
    if (entry.verdict) header.append(badge(entry.verdict)); if (entry.status) header.append(badge(entry.status));
    const timestamp = entry.timestamp || entry.timestamp_utc; const time = node('time', '', fullTime(timestamp)); if (date(timestamp)) time.dateTime = date(timestamp).toISOString(); header.append(time); card.append(header);
    // Research records keep their text; control events show the related record's title or their localized type.
    if (state.recordView === 'memory' || i18nHas('channel.' + entry.kind)) { const content = node('div'); markdown(content, entry.claim || entry.title || t('activity.record')); card.append(content); }
    else if (entry.node_id) card.append(titleNode('p', 'record-event', { id: entry.node_id, title: entry.title }));
    else card.append(node('p', 'record-event', activityKind(entry.kind)));
    if (entry.evidence || entry.detail) card.append(disclosure(t('activity.details'), entry.evidence || entry.detail, 'record|' + (entry.id || timestamp), 'record-detail'));
    if (entry.node_id) { const button = node('button', 'text-button', t('activity.openReader')); button.onclick = () => openReader(entry.node_id, 'research'); card.append(button); }
    if (entry.fact_id) { const button = node('button', 'text-button', t('activity.fact', { id: text(entry.fact_id).slice(0, 12) })); button.onclick = () => openReader(entry.fact_id, 'facts'); card.append(button); }
    parent.append(card);
  }
  if (entries.length > state.activityLimit) { const more = node('button', 'button secondary', t('activity.more', { n: entries.length - state.activityLimit })); more.onclick = () => { state.activityLimit += 80; renderActivity(); }; parent.append(more); }
  restoreKeys(parent, keys); if (scroll) window.scrollTo(0, scroll);
}
async function loadChannels() {
  const generation = state.generation;
  try {
    const data = await api('/api/channels'); if (generation !== state.generation) return; state.channels = data.channels || [];
    if (!state.channel || !state.channels.some((item) => item.kind === state.channel)) state.channel = (state.channels.find((item) => item.count > 0) || state.channels[0])?.kind || null;
    const tabs = $('#channel-tabs'); tabs.replaceChildren();
    for (const channel of state.channels) {
      const button = node('button', channel.kind === state.channel ? 'selected' : '', channelName(channel.kind)); button.dataset.channel = channel.kind; button.setAttribute('aria-pressed', String(channel.kind === state.channel)); button.append(node('span', 'count', number(channel.count)));
      button.onclick = () => { state.channel = channel.kind; state.activityLimit = 80; $$('#channel-tabs button').forEach((item) => { const active = item.dataset.channel === state.channel; item.classList.toggle('selected', active); item.setAttribute('aria-pressed', String(active)); }); loadChannel(); }; tabs.append(button);
    }
    if (state.channel) await loadChannel(); else { state.records = []; renderActivity(); }
  } catch (error) { if (generation !== state.generation) return; $('#activity-list').replaceChildren(node('div', 'large-empty', t('activity.failed', { error: error.message }))); }
}
async function loadChannel() {
  if (!state.channel) return; const request = ++state.channelRequest; const channel = state.channel; const generation = state.generation;
  if (!state.records.length) $('#activity-list').replaceChildren(node('div', 'large-empty', t('activity.loading')));
  try { const data = await api('/api/channel/' + encodeURIComponent(channel)); if (request !== state.channelRequest || channel !== state.channel || generation !== state.generation) return; state.records = data.entries || []; if (state.recordView === 'memory') renderActivity(); }
  catch (error) { if (request === state.channelRequest) $('#activity-list').replaceChildren(node('div', 'large-empty', t('activity.failed', { error: error.message }))); }
}
$$('[data-record-view]').forEach((button) => button.onclick = () => { state.recordView = button.dataset.recordView; state.activityLimit = 80; state.records = []; setPressed('[data-record-view]', 'recordView', state.recordView); $('#channel-tabs').hidden = state.recordView !== 'memory'; if (state.recordView === 'memory') loadChannels(); else renderActivity(); });
$('#activity-search').oninput = () => { state.activityLimit = 80; renderActivity(); };

// Live research: attempts, failures and alternatives, before any paper or Fact exists.
function renderExploration(force = false) {
  const box = $('#exploration-live'); const data = state.exploration;
  if (!data) { if (state.explorationError) box.replaceChildren(node('p', 'empty-inline', t('explore.unavailable', { error: state.explorationError }))); return; }
  const signature = JSON.stringify([state.lang, data.signals, data.workers, data.coordinators, data.branches, data.blockers, data.control, state.signalFilter]);
  if (!force && signature === state.exploreSignature) return;
  if (state.view !== 'explore' && !force) return;
  state.exploreSignature = signature;
  const expanded = openKeys(box); const scroll = window.scrollY;
  box.replaceChildren();
  const goal = node('header', 'exploration-goal'); goal.dataset.scrollKey = 'explore-goal';
  const title = node('div'); title.append(node('div', 'section-kicker', t('explore.kicker')), labelNode('h2', '', data.project.name ? displayLabel('name', state.project, null, data.project.name) : { text: t('explore.task'), translated: true }));
  goal.append(title, data.phase === 'complete' ? stateTag('established', 'obligation', t('explore.hasAcceptance')) : stateTag('open', 'obligation', t('explore.notDone')));
  const statement = data.control.goal || data.project.goal;
  if (statement) { goal.append(src('p', '', truncate(statement, 260))); if (statement.length > 260) goal.append(disclosure(t('explore.fullGoal'), statement, 'explore-goal')); }
  else goal.append(node('p', '', t('explore.noGoal')));
  goal.append(node('small', 'muted', t('explore.sync', { time: fullTime(data.updated_at) }))); box.append(goal);
  const team = node('section', 'exploration-workers'); team.setAttribute('aria-label', t('explore.members')); team.dataset.scrollKey = 'explore-team';
  (data.coordinators || []).forEach((lead) => { const card = node('article', 'exploration-worker coordinator-live'); const heading = node('div'); heading.append(labelNode('strong', '', lead.name ? coordinatorLabel(lead.name) : { text: t('explore.leadDefault'), translated: true }), badge(lead.status)); card.append(heading, lead.task_summary ? taskSummaryNode('p', '', lead.task_summary) : node('p', '', t('explore.leadTask')), node('small', 'muted', [lead.model, lead.effort, t('explore.leadTag')].filter(Boolean).join(' · '))); team.append(card); });
  data.workers.forEach((worker) => {
    const card = node('article', 'exploration-worker' + (isActive(worker) ? ' is-running' : '')); const head = node('div'); head.append(labelNode('strong', '', personLabel(worker.name)), badge(worker.status));
    const meta = node('small', 'muted', t('explore.lastActivity', { model: worker.model })); meta.append(ageNode('span', '', worker.last_activity_at));
    card.append(head, worker.task_summary ? src('p', '', worker.task_summary) : node('p', '', t('live.waiting')), meta); team.append(card);
  });
  if (!data.workers.length) team.append(node('p', 'empty-inline', t('explore.noWorkers')));
  box.append(team);
  const columns = node('div', 'exploration-columns'); const directions = node('section', 'exploration-directions'); directions.dataset.scrollKey = 'explore-routes';
  directions.append(node('h3', '', t('explore.routes')), node('p', 'muted', t('explore.routesNote')));
  if (!data.branches.length) directions.append(node('div', 'exploration-empty', t('explore.noRoutes')));
  data.branches.forEach((branch) => {
    const card = node('article', 'exploration-branch tone-' + tone(nodeState(branch))); card.append(stateTag(nodeState(branch), 'route'), titleNode('h4', '', branch, 130));
    if (branch.statement && branch.statement !== branch.label) card.append(src('p', '', truncate(branch.statement, 180)));
    const button = node('button', 'text-button', t('explore.readRoute')); button.onclick = () => openReader(branch.id, 'research'); card.append(button); directions.append(card);
  });
  const unresolved = node('details', 'exploration-open'); unresolved.dataset.sourceKey = 'explore-open'; unresolved.append(node('summary', '', t('explore.open', { n: data.open_count })));
  data.blockers.forEach((problem) => unresolved.append(itemButton({ id: problem.id, title: problem.display_title || problem.label, curated: !!problem.display_title, state: nodeState(problem), kind: problem.kind, layer: 'research' })));
  directions.append(unresolved);
  const signals = node('section', 'exploration-signals'); signals.dataset.scrollKey = 'explore-signals'; signals.append(node('h3', '', t('explore.signals')));
  const filters = node('div', 'exploration-filters');
  for (const key of ['all', 'attempt', 'obstacle', 'result']) {
    const button = node('button', state.signalFilter === key ? 'active' : '', t('explore.f.' + key)); button.dataset.filter = key; button.setAttribute('aria-pressed', String(state.signalFilter === key));
    button.onclick = () => { state.signalFilter = key; renderExploration(true); }; filters.append(button);
  }
  signals.append(filters);
  const types = { attempt: ['plan', 'direction', 'proof_attempt'], obstacle: ['obstacle', 'dead_end', 'counterexample'], result: ['conclusion', 'example', 'fact', 'evidence'] };
  const selected = data.signals.filter((entry) => !types[state.signalFilter] || types[state.signalFilter].includes(entry.kind) || (state.signalFilter === 'obstacle' && ['blocked', 'abandoned'].includes(entry.attempt_result)));
  if (!selected.length) signals.append(node('div', 'exploration-empty', t('explore.noSignals')));
  selected.slice(0, 30).forEach((entry) => {
    const item = node('article', 'exploration-signal ' + entry.kind); item.dataset.scrollKey = 'signal-' + entry.id; const header = node('div');
    header.append(node('span', 'signal-kind', signalLabel(entry.kind)), node('time', 'muted', entry.date_label || fullTime(entry.timestamp)));
    const claim = node('p'); claim.append(signalClaim(entry));
    const meta = node('small', 'muted'); meta.append(entry.author ? src('span', '', entry.author) : document.createTextNode(t('explore.team')), document.createTextNode(' · ' + t(entry.verification === 'fact_store' ? 'explore.factStore' : entry.verification === 'public_control_event' ? 'explore.controlEvent' : 'explore.publicRecord')));
    item.append(header, claim, meta);
    if (entry.attempt_result) item.append(node('p', 'proof-link-note', t('attempt.' + entry.attempt_result)));
    if (entry.reason) item.append(disclosure(t('explore.reason'), entry.reason, 'reason|' + entry.id));
    if (entry.detail) item.append(disclosure(t('explore.detail'), entry.detail_i18n?.[state.lang] || entry.detail, 'detail|' + entry.id));
    if (entry.node_id) { const read = node('button', 'text-button', t('explore.trace')); read.onclick = () => openReader(entry.node_id, entry.kind === 'fact' ? 'facts' : 'research'); item.append(read); }
    signals.append(item);
  });
  columns.append(directions, signals); box.append(columns, node('p', 'exploration-basis', t('explore.basis')));
  restoreKeys(box, expanded); if (state.view === 'explore') window.scrollTo(0, scroll);
  consumeRestore('explore');
}

// Relation map. Three entry points share one layered layout: the goal's recorded main line, the
// newest substantive frontier, and every record. Positions, ports, lanes and edge polylines are
// computed here, so Cytoscape only draws, and overlapping relations are hit-tested against the
// same geometry and offered one by one. Graph data stays separate from Cytoscape elements.
function graphNodeLabel(item) { return titleOf(item).text; }
const TONE_COLORS = { ok: '#709887', active: '#8b86ad', open: '#819bae', warn: '#b39160', muted: '#a7aaa6', bad: '#b27f7a' };
function graphColor(item) { return TONE_COLORS[tone(stateOf(item))] || '#d9dad6'; }
// A small state accent keeps the canvas quiet; labels and the inspector carry meaning.
function graphAccent(color, width, height) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect x="0" y="12" width="3" height="${Math.max(8, height - 24)}" rx="1.5" fill="${color}"/><circle cx="${width - 13}" cy="12" r="3" fill="${color}"/></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
const MAP_VIEWS = ['mainline', 'frontier', 'all'];
const SUPPORT_TYPES = new Set(['requires', 'depends_on', 'REQUIRES', 'targets']);
const relationName = (type) => i18nHas('relation.' + type) ? t('relation.' + type) : text(type);
const edgeKey = (edge) => edge.id || [edge.source, edge.type, edge.target].join('|');
const edgeById = (key) => (state.graph?.edges || []).find((edge) => edgeKey(edge) === key);
const recordKey = (record) => record.kind + ':' + (record.id || record.timestamp || text(record.claim).slice(0, 40));
const recordsById = () => !state.graph ? new Map() : state.graph._byId || (state.graph._byId = new Map((state.graph.nodes || []).map((item) => [item.id, item])));
const shortTime = (value) => { const when = date(value); return when ? new Intl.DateTimeFormat(I18N_LOCALES[state.lang], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(when) : t('time.unrecorded'); };
const layerName = (layer) => t(layer === 'facts' ? 'layer.factsLower' : 'layer.researchLower');
function currentFocus() { const focus = state.mapFocus; return focus && state.graph && focus.layer === state.graph._layer ? focus : null; }
function mapView() { return MAP_VIEWS.includes(state.mapView) ? state.mapView : currentFocus()?.default_view || 'all'; }
function filtersActive() { return !!($('#graph-search').value.trim() || $('#graph-team').value || $('#graph-author').value || $('#graph-status').value || $('#graph-depth').value !== ''); }
function findRecord(key) { return (currentFocus()?.frontier.records || []).find((record) => recordKey(record) === key); }
function trackFresh(focus, layer) {
  const key = state.project + '|' + layer;
  const current = [...focus.frontier.items.map((item) => 'item:' + item.kind + ':' + item.id), ...(focus.frontier.records || []).map((record) => 'record:' + recordKey(record))];
  if (state.seen.key !== key) { state.seen = { key, keys: new Set(current) }; state.freshKeys = new Set(); return; }
  for (const item of current) if (!state.seen.keys.has(item)) { state.seen.keys.add(item); state.freshKeys.add(item); }
}

// Layout: sink-aligned columns (each record sits just left of its nearest consumer), over-full
// columns hand movable records one column further left, long edges reserve a slot in every column
// they cross, and each node gives every edge its own port while each gap gives every vertical run
// its own lane. Disconnected records are packed below the component that holds the anchor.
const CARD_BASE = { anchor: [236, 92], evidence: [164, 52], card: [190, 74] };
function cardSize(kind, ports) { const [w, h] = CARD_BASE[kind]; return { w, h: Math.min(300, Math.max(h, 22 + 11 * ports)) }; }
function layeredLayout(items, edges, anchor) {
  const size = new Map(items.map((item) => [item.id, item.size])); const ids = items.map((item) => item.id);
  const links = edges.filter((edge) => size.has(edge.source) && size.has(edge.target) && edge.source !== edge.target);
  const out = new Map(ids.map((id) => [id, []]));
  links.forEach((edge) => out.get(edge.source).push(edge));
  // Recorded relations can contain cycles; DFS back edges are drawn as plain curves.
  const mark = new Map(); const back = new Set();
  for (const start of ids) {
    if (mark.get(start)) continue;
    mark.set(start, 1); const stack = [[start, 0]];
    while (stack.length) {
      const top = stack[stack.length - 1]; const list = out.get(top[0]);
      if (top[1] < list.length) { const edge = list[top[1]++]; const seen = mark.get(edge.target) || 0; if (seen === 1) back.add(edgeKey(edge)); else if (!seen) { mark.set(edge.target, 1); stack.push([edge.target, 0]); } }
      else { mark.set(top[0], 2); stack.pop(); }
    }
  }
  const dag = links.filter((edge) => !back.has(edgeKey(edge)));
  const succ = new Map(ids.map((id) => [id, []])), pred = new Map(ids.map((id) => [id, []]));
  dag.forEach((edge) => { succ.get(edge.source).push(edge.target); pred.get(edge.target).push(edge.source); });
  const parent = new Map(ids.map((id) => [id, id]));
  const find = (id) => { while (parent.get(id) !== id) { parent.set(id, parent.get(parent.get(id))); id = parent.get(id); } return id; };
  links.forEach((edge) => { const a = find(edge.source), b = find(edge.target); if (a !== b) parent.set(a, b); });
  const groups = new Map(); ids.forEach((id) => { const root = find(id); if (!groups.has(root)) groups.set(root, []); groups.get(root).push(id); });
  const mainRoot = anchor && size.has(anchor) ? find(anchor) : null;
  const comps = [...groups.values()].sort((a, b) => (find(b[0]) === mainRoot) - (find(a[0]) === mainRoot) || b.length - a.length || a[0].localeCompare(b[0]));
  const isDummy = (id) => id.charCodeAt(0) === 0;
  const component = (members) => {
    if (members.length === 1) { const s = size.get(members[0]); return { pos: new Map([[members[0], { x: 0, y: 0 }]]), routes: new Map(), x1: -s.w / 2, x2: s.w / 2, y1: -s.h / 2, y2: s.h / 2, single: true }; }
    const inside = new Set(members);
    const indegree = new Map(members.map((id) => [id, 0])); members.forEach((id) => succ.get(id).forEach((next) => indegree.set(next, indegree.get(next) + 1)));
    const order = []; const queue = members.filter((id) => !indegree.get(id)).sort();
    while (queue.length) { const id = queue.shift(); order.push(id); for (const next of succ.get(id)) { indegree.set(next, indegree.get(next) - 1); if (!indegree.get(next)) queue.push(next); } }
    const height = new Map();
    for (let i = order.length - 1; i >= 0; i--) { const id = order[i]; const next = succ.get(id); height.set(id, next.length ? 1 + Math.max(...next.map((x) => height.get(x))) : 0); }
    const top = Math.max(...height.values()); const col = new Map(members.map((id) => [id, top - height.get(id)]));
    const capacity = Math.max(7, Math.ceil(Math.sqrt(members.length) * 1.5)); let lowest = 0;
    for (let c = top; c >= lowest; c--) {
      const here = members.filter((id) => col.get(id) === c); if (here.length <= capacity) continue;
      const movable = here.filter((id) => id !== anchor && pred.get(id).every((p) => col.get(p) <= c - 2))
        .sort((a, b) => (pred.get(a).length > 0) - (pred.get(b).length > 0) || succ.get(a).length - succ.get(b).length || a.localeCompare(b));
      movable.slice(0, here.length - capacity).forEach((id) => col.set(id, c - 1));
      lowest = Math.min(lowest, c - 1);
    }
    const shift = Math.min(...col.values()); members.forEach((id) => col.set(id, col.get(id) - shift));
    const width = Math.max(...col.values()) + 1; const layers = Array.from({ length: width }, () => []);
    members.forEach((id) => layers[col.get(id)].push(id));
    const nextOf = new Map(), prevOf = new Map(); const chains = []; const dummyCol = new Map();
    const join = (a, b) => { if (!nextOf.has(a)) nextOf.set(a, []); nextOf.get(a).push(b); if (!prevOf.has(b)) prevOf.set(b, []); prevOf.get(b).push(a); };
    for (const edge of dag) {
      if (!inside.has(edge.source)) continue;
      const key = edgeKey(edge); const chain = [edge.source];
      for (let c = col.get(edge.source) + 1; c < col.get(edge.target); c++) { const dummy = '\u0000' + key + '\u0000' + c; layers[c].push(dummy); dummyCol.set(dummy, c); chain.push(dummy); }
      chain.push(edge.target); for (let i = 1; i < chain.length; i++) join(chain[i - 1], chain[i]);
      chains.push({ key, chain });
    }
    const columnOf = (id) => isDummy(id) ? dummyCol.get(id) : col.get(id);
    const index = new Map(); const reindex = (layer) => layer.forEach((id, i) => index.set(id, i));
    layers.forEach((layer) => { layer.sort((a, b) => (b === anchor) - (a === anchor) || a.localeCompare(b)); reindex(layer); });
    const bary = (id, near) => { const list = near.get(id) || []; return list.length ? list.reduce((sum, x) => sum + index.get(x), 0) / list.length : index.get(id); };
    for (let pass = 0; pass < 8; pass++) {
      const sweep = pass % 2 === 0 ? [...layers.keys()].reverse().slice(1) : [...layers.keys()].slice(1);
      for (const c of sweep) { const keyOf = new Map(layers[c].map((id) => [id, bary(id, pass % 2 === 0 ? nextOf : prevOf)])); layers[c].sort((a, b) => keyOf.get(a) - keyOf.get(b) || index.get(a) - index.get(b)); reindex(layers[c]); }
    }
    const h = (id) => isDummy(id) ? 2 : size.get(id).h;
    const gap = (a, b) => (h(a) + h(b)) / 2 + (isDummy(a) && isDummy(b) ? 10 : isDummy(a) || isDummy(b) ? 16 : 26);
    const y = new Map();
    layers.forEach((layer) => { let cursor = 0; layer.forEach((id, i) => { if (i) cursor += gap(layer[i - 1], id); y.set(id, cursor); }); layer.forEach((id) => y.set(id, y.get(id) - cursor / 2)); });
    // Least-squares placement towards neighbours that keeps the order and spacing (pool adjacent violators).
    const place = (layer, desired, weights) => {
      const offset = [0]; for (let i = 1; i < layer.length; i++) offset.push(offset[i - 1] + gap(layer[i - 1], layer[i]));
      const blocks = [];
      layer.forEach((id, i) => {
        blocks.push({ value: desired[i] - offset[i], weight: weights[i], count: 1 });
        while (blocks.length > 1 && blocks[blocks.length - 2].value > blocks[blocks.length - 1].value) {
          const b = blocks.pop(), a = blocks.pop(), weight = a.weight + b.weight;
          blocks.push({ value: (a.value * a.weight + b.value * b.weight) / weight, weight, count: a.count + b.count });
        }
      });
      let i = 0; for (const block of blocks) for (let k = 0; k < block.count; k++, i++) y.set(layer[i], block.value + offset[i]);
    };
    for (let pass = 0; pass < 10; pass++) {
      for (const c of pass % 2 ? [...layers.keys()] : [...layers.keys()].reverse()) {
        const layer = layers[c]; if (!layer.length) continue;
        place(layer, layer.map((id) => { const near = [...(prevOf.get(id) || []), ...(nextOf.get(id) || [])]; return near.length ? near.reduce((sum, x) => sum + y.get(x), 0) / near.length : y.get(id); }), layer.map((id) => isDummy(id) ? 0.6 : 1));
      }
    }
    const outPort = new Map(), inPort = new Map(); const leaving = new Map(), arriving = new Map();
    chains.forEach(({ key, chain }) => { const a = chain[0], b = chain[chain.length - 1]; if (!leaving.has(a)) leaving.set(a, []); leaving.get(a).push({ key, y: y.get(chain[1]) }); if (!arriving.has(b)) arriving.set(b, []); arriving.get(b).push({ key, y: y.get(chain[chain.length - 2]) }); });
    const spread = (id, list, target) => { list.sort((a, b) => a.y - b.y || a.key.localeCompare(b.key)); const n = list.length; const span = Math.min(h(id) - 22, (n - 1) * 12); list.forEach((entry, i) => target.set(entry.key, n > 1 ? -span / 2 + span * i / (n - 1) : 0)); };
    leaving.forEach((list, id) => spread(id, list, outPort)); arriving.forEach((list, id) => spread(id, list, inPort));
    const exitY = (id, key) => y.get(id) + (isDummy(id) ? 0 : outPort.get(key) || 0), entryY = (id, key) => y.get(id) + (isDummy(id) ? 0 : inPort.get(key) || 0);
    const hops = layers.map(() => []);
    chains.forEach(({ key, chain }) => { for (let i = 1; i < chain.length; i++) hops[columnOf(chain[i - 1])].push({ key, i, y1: exitY(chain[i - 1], key), y2: entryY(chain[i], key) }); });
    const lane = new Map(); const lanes = [];
    hops.forEach((list, c) => {
      const ends = [];
      list.filter((hop) => Math.abs(hop.y1 - hop.y2) >= 1).sort((a, b) => Math.min(a.y1, a.y2) - Math.min(b.y1, b.y2) || a.key.localeCompare(b.key)).forEach((hop) => {
        const lo = Math.min(hop.y1, hop.y2), hi = Math.max(hop.y1, hop.y2); let slot = ends.findIndex((end) => end < lo - 8);
        if (slot < 0) { slot = ends.length; ends.push(hi); } else ends[slot] = hi;
        lane.set(hop.key + '#' + hop.i, slot);
      });
      lanes[c] = ends.length;
    });
    const colWidth = layers.map((layer) => Math.max(28, ...layer.filter((id) => !isDummy(id)).map((id) => size.get(id).w)));
    const gapWidth = layers.map((_, c) => Math.max(62, 36 + (lanes[c] || 0) * 10));
    const colX = [0]; for (let c = 1; c < width; c++) colX.push(colX[c - 1] + colWidth[c - 1] + gapWidth[c - 1]);
    const laneX = (c, slot) => colX[c] + colWidth[c] + (gapWidth[c] - ((lanes[c] || 1) - 1) * 10) / 2 + slot * 10;
    const pos = new Map(); members.forEach((id) => pos.set(id, { x: colX[col.get(id)] + colWidth[col.get(id)] / 2, y: y.get(id) }));
    const routes = new Map();
    chains.forEach(({ key, chain }) => {
      const first = chain[0], last = chain[chain.length - 1];
      const points = [[pos.get(first).x + size.get(first).w / 2, exitY(first, key)]];
      for (let i = 1; i < chain.length; i++) {
        const c = columnOf(chain[i - 1]); const y1 = exitY(chain[i - 1], key), y2 = entryY(chain[i], key);
        if (Math.abs(y1 - y2) >= 1) { const x = laneX(c, lane.get(key + '#' + i)); points.push([x, y1], [x, y2]); }
      }
      points.push([pos.get(last).x - size.get(last).w / 2, entryY(last, key)]);
      routes.set(key, points.filter((point, i) => i === 0 || i === points.length - 1 || !((points[i - 1][0] === point[0] && points[i + 1][0] === point[0]) || (points[i - 1][1] === point[1] && points[i + 1][1] === point[1]))));
    });
    let y1 = Infinity, y2 = -Infinity;
    members.forEach((id) => { y1 = Math.min(y1, y.get(id) - size.get(id).h / 2); y2 = Math.max(y2, y.get(id) + size.get(id).h / 2); });
    routes.forEach((points) => points.forEach(([, py]) => { y1 = Math.min(y1, py); y2 = Math.max(y2, py); }));
    return { pos, routes, x1: 0, x2: colX[width - 1] + colWidth[width - 1], y1, y2 };
  };
  const pos = new Map(), routes = new Map(); let cursorX = 0, cursorY = 0, rowHeight = 0, rowLimit = 1100;
  comps.forEach((members, i) => {
    const block = component(members); const w = block.x2 - block.x1, hgt = block.y2 - block.y1; let dx, dy;
    if (i === 0) { dx = -block.x1; dy = -block.y1; cursorY = hgt + 120; rowLimit = Math.max(w, 1100); }
    else { if (cursorX > 0 && cursorX + w > rowLimit) { cursorX = 0; cursorY += rowHeight + 56; rowHeight = 0; } dx = cursorX - block.x1; dy = cursorY - block.y1; cursorX += w + (block.single ? 30 : 80); rowHeight = Math.max(rowHeight, hgt); }
    block.pos.forEach((p, id) => pos.set(id, { x: p.x + dx, y: p.y + dy }));
    block.routes.forEach((points, key) => routes.set(key, points.map(([x, py]) => [x + dx, py + dy])));
  });
  links.filter((edge) => back.has(edgeKey(edge))).forEach((edge) => { const a = pos.get(edge.source), b = pos.get(edge.target); routes.set(edgeKey(edge), [[a.x, a.y], [b.x, b.y]]); });
  return { pos, size, routes, back, main: new Set(comps[0] || []), detached: ids.length - (comps[0] || []).length };
}
// Cytoscape draws the computed polyline: explicit endpoints at the ports and segment points
// expressed relative to the line between the two node centres.
function routeEdges(layout) {
  state.cy.edges().forEach((edge) => {
    const key = edge.id(); const points = layout.routes.get(key);
    if (!points || layout.back.has(key)) { edge.style({ 'curve-style': 'bezier' }); return; }
    const s = layout.pos.get(edge.source().id()), e = layout.pos.get(edge.target().id());
    const first = points[0], last = points[points.length - 1], inner = points.slice(1, -1);
    const style = { 'source-endpoint': `${(first[0] - s.x).toFixed(1)}px ${(first[1] - s.y).toFixed(1)}px`, 'target-endpoint': `${(last[0] - e.x).toFixed(1)}px ${(last[1] - e.y).toFixed(1)}px` };
    if (!inner.length) style['curve-style'] = 'straight';
    else {
      const dx = e.x - s.x, dy = e.y - s.y, length2 = dx * dx + dy * dy || 1, length = Math.sqrt(length2);
      Object.assign(style, { 'curve-style': 'round-segments', 'segment-radius': 8, 'edge-distances': 'node-position',
        'segment-weights': inner.map(([x, y]) => (((x - s.x) * dx + (y - s.y) * dy) / length2).toFixed(5)).join(' '),
        'segment-distances': inner.map(([x, y]) => ((-(x - s.x) * dy + (y - s.y) * dx) / length).toFixed(2)).join(' ') });
    }
    edge.style(style);
  });
}
function straightRoutes() {
  const routes = new Map();
  state.cy.edges().forEach((edge) => { const a = edge.source().position(), b = edge.target().position(); routes.set(edge.id(), [[a.x, a.y], [b.x, b.y]]); });
  return routes;
}
function segmentDistance(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], length = dx * dx + dy * dy;
  const u = length ? Math.max(0, Math.min(1, ((p.x - a[0]) * dx + (p.y - a[1]) * dy) / length)) : 0;
  return Math.hypot(a[0] + u * dx - p.x, a[1] + u * dy - p.y);
}
// Every relation whose drawn polyline passes within the tolerance, nearest first.
function hitEdges(point, tolerance) {
  const cy = state.cy; if (!cy || !state.mapRoutes || !point) return [];
  const limit = tolerance / cy.zoom(); const hits = [];
  state.mapRoutes.forEach((points, key) => {
    let best = Infinity; for (let i = 1; i < points.length; i++) best = Math.min(best, segmentDistance(point, points[i - 1], points[i]));
    if (best <= limit) hits.push([best, key]);
  });
  return hits.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1])).map(([, key]) => key);
}

function mapRoles(focus, view, layer) {
  const roles = new Map(); const get = (id) => { if (!roles.has(id)) roles.set(id, {}); return roles.get(id); };
  if (focus?.anchor) get(focus.anchor).anchor = true;
  if (focus && view !== 'frontier') focus.mainline.nodes.forEach((id) => { get(id).spine = true; });
  if (focus && view !== 'mainline') {
    focus.frontier.items.forEach((item) => { get(item.id).front = item; if (state.freshKeys.has('item:' + item.kind + ':' + item.id)) get(item.id).fresh = true; });
    (focus.frontier.records || []).forEach((record) => record.links.forEach((link) => { if (link.layer === layer) { get(link.id).linked = true; if (state.freshKeys.has('record:' + recordKey(record))) get(link.id).fresh = true; } }));
  }
  (focus?.alternatives || []).forEach((id) => { get(id).alt = true; });
  if (!focus?.anchor && layer === 'facts') {
    const used = new Set((state.graph.edges || []).map((edge) => edge.source));
    (state.graph.nodes || []).forEach((item) => { if (!used.has(item.id) && lower(item.status) === 'accepted') get(item.id).top = true; });
  }
  return roles;
}
// A closed target can be certified directly while the planning ledger retains OPEN tasks.
// This is a display distinction only: no obligation or relation is rewritten.
function targetKernelEvidence() {
  const graph = state.graph, focus = currentFocus();
  if (!graph || graph._layer !== 'research' || !focus?.phase?.controller_closed) return [];
  const records = recordsById();
  return (graph.edges || []).filter(edge => edge.target === focus.anchor && edge.type === 'evidence' && (edge.validity || 'VALID') === 'VALID')
    .map(edge => records.get(edge.source)).filter(item => item && (item.validity || 'VALID') === 'VALID' && item.evidence_kind === 'KERNEL_CHECKED_PROOF' && item.checker_result === 'PASS');
}
function isLedgerOpen(item) {
  return item?.kind === 'obligation' && lower(item.status) === 'open' && targetKernelEvidence().length > 0;
}
function mapRecordTag(item) {
  return isLedgerOpen(item) ? badge('unknown', t('map.ledger.open')) : stateTag(stateOf(item), item.kind);
}
function dependencyTrace(id, graph = state.graph) {
  const allowed = new Set([...SUPPORT_TYPES, 'evidence', 'BLOCKED_BY']);
  const edges = (graph?.edges || []).filter(edge => allowed.has(edge.type || 'requires') && (edge.validity || 'VALID') === 'VALID');
  const walk = (reverse) => {
    const adjacency = new Map();
    for (const edge of edges) { const from = reverse ? edge.target : edge.source; if (!adjacency.has(from)) adjacency.set(from, []); adjacency.get(from).push(edge); }
    const nodes = new Set([id]), links = new Set(), queue = [id];
    for (let i = 0; i < queue.length; i++) for (const edge of adjacency.get(queue[i]) || []) {
      const next = reverse ? edge.source : edge.target; links.add(edgeKey(edge));
      if (!nodes.has(next)) { nodes.add(next); queue.push(next); }
    }
    return { nodes, edges: links };
  };
  const upstream = walk(true), downstream = walk(false);
  return { upstream, downstream, nodes: new Set([...upstream.nodes, ...downstream.nodes]), edges: new Set([...upstream.edges, ...downstream.edges]) };
}
function selectionTraceNote(item) {
  const trace = dependencyTrace(item.id), box = node('section', 'selection-trace-note');
  const shown = [...trace.nodes].filter(id => state.cy?.getElementById(id).length).length;
  box.append(node('strong', '', t('map.trace.title')),
    node('p', 'trace-upstream', t('map.trace.upstream', { n: trace.upstream.nodes.size - 1 })),
    node('p', 'trace-downstream', t('map.trace.downstream', { n: trace.downstream.nodes.size - 1 })),
    node('p', 'trace-scope', t('map.trace.scope', { shown, total: trace.nodes.size })));
  return box;
}

function mapLabel(item, role, layer) {
  const code = stateOf(item); let meta;
  if (role.anchor) meta = t(layer === 'facts' ? 'map.lab.conclusion' : 'map.lab.goal') + ' · ' + (currentFocus()?.phase?.awaiting_final_audit ? (state.lang === 'en' ? 'Audit pending' : '待终审') : stateLabel(code, item.kind));
  else if (role.front) meta = role.front.kind === 'ready' ? t('map.lab.ready') : t('map.lab.' + role.front.kind, { time: shortTime(role.front.at) });
  else if (item.kind === 'route') meta = t('map.lab.route') + ' · ' + stateLabel(code, 'route');
  else if (item.kind === 'evidence') meta = t('map.lab.evidence') + ' · ' + stateLabel(code, 'evidence');
  else meta = stateLabel(code, item.kind) + (role.top ? ' · ' + t('map.lab.top') : '');
  if (isLedgerOpen(item)) meta = t('map.ledger.open');
  if (targetKernelEvidence().some(evidence => evidence.id === item.id)) meta = t('map.ledger.kernel');
  if (role.linked) meta += ' · ' + t('map.lab.linked');
  return meta + '\n' + truncate(graphNodeLabel(item), item.kind === 'evidence' ? 30 : role.anchor ? 56 : 42);
}
function mapScope() {
  const graph = state.graph; const focus = currentFocus(); const view = mapView(); const exists = recordsById();
  let scope;
  if (state.focus !== 'all' && state.selected) scope = focusIds();
  else if (filtersActive()) scope = new Set(graph.nodes.map((item) => item.id));
  else if (view === 'mainline' && focus?.mainline.anchor) {
    scope = new Set(focus.mainline.nodes);
    if (state.showAlternatives) for (const id of focus.alternatives) { scope.add(id); for (const edge of graph.edges || []) if (edge.target === id && SUPPORT_TYPES.has(edge.type)) scope.add(edge.source); }
  } else if (view === 'frontier' && focus) {
    // Paths to the goal stay visible; the premises of an item appear when that item is selected.
    const items = focus.frontier.items; scope = new Set(focus.anchor ? [focus.anchor] : []);
    items.forEach((item) => { scope.add(item.id); item.path.forEach((id) => scope.add(id)); if (item.kind === 'ready' && item.id === state.selected) item.premises.forEach((id) => scope.add(id)); });
    (focus.frontier.records || []).forEach((record) => record.links.forEach((link) => { if (link.layer === graph._layer) scope.add(link.id); }));
  } else scope = new Set(graph.nodes.filter((item) => item.kind !== 'evidence' || state.showEvidence).map((item) => item.id));
  if (state.showEvidence) for (const edge of graph.edges || []) if (edge.type === 'evidence' && scope.has(edge.target)) scope.add(edge.source);
  if (state.selected) scope.add(state.selected);
  const relation = state.relation && edgeById(state.relation);
  if (relation) { scope.add(relation.source); scope.add(relation.target); }
  return new Set([...scope].filter((id) => exists.has(id)));
}

function populateSelect(id, values, firstLabel, labeler = (value) => value) {
  const select = $(id); const previous = select.value;
  select.replaceChildren(new Option(firstLabel, ''));
  for (const value of values) select.append(new Option(labeler(value), value));
  if (values.map(String).includes(previous)) select.value = previous;
}
function graphFilters() {
  const items = state.graph.nodes || []; const unique = (key) => [...new Set(items.map((item) => item[key]).filter((value) => value != null && value !== ''))].sort((a, b) => String(a).localeCompare(String(b)));
  const teams = new Map((state.workspace?.teams || []).map((team) => [team.id, teamName(team).text]));
  populateSelect('#graph-team', unique('team_id'), t('map.allTeams'), (value) => teams.get(value) || value);
  populateSelect('#graph-author', unique('author'), t('map.allAuthors'));
  populateSelect('#graph-status', unique('status'), t('map.allStatuses'), (value) => statusInfo(value)[0]);
  populateSelect('#graph-depth', unique('depth').sort((a, b) => Number(a) - Number(b)), t('map.allDepths'), (value) => t('map.depthN', { n: value }));
}
function graphEmpty(title, description) {
  const empty = $('#graph-empty'); empty.hidden = false; empty.querySelector('strong').textContent = title; empty.querySelector('span:last-child').textContent = description;
}
function renderGraphChrome(data) {
  const warnings = (data.warnings || []).map((item) => localizeWarning(typeof item === 'string' ? item : item.message || JSON.stringify(item)));
  $('#graph-notice').textContent = warnings.join(' · '); $('#graph-notice').hidden = !warnings.length;
  $('#graph-corner-label').textContent = t(data._layer === 'facts' ? 'map.cornerFacts' : 'map.cornerResearch');
  const legend = $('#graph-legend'); legend.replaceChildren();
  for (const code of ['established', 'attempted', 'open', 'blocked', 'parked']) { const entry = node('span', 'legend-item'); const swatch = node('i', 'legend-swatch'); swatch.style.backgroundColor = TONE_COLORS[tone(code)]; entry.append(swatch, document.createTextNode(stateLabel(code, data._layer === 'facts' ? 'fact' : 'obligation'))); legend.append(entry); }
  const lines = $('#graph-edge-legend'); lines.replaceChildren();
  for (const [cls, key] of [['main', 'map.legend.main'], ['path', 'map.legend.path'], ['', 'map.legend.dep'], ['evidence', 'map.legend.evidence'], ['blocked', 'map.legend.blocked'], ['invalid', 'map.legend.invalid']]) { const entry = node('span', 'legend-item'); entry.append(node('i', 'legend-line ' + cls), document.createTextNode(t(key))); lines.append(entry); }
}
async function loadGraph(force = false) {
  const requestId = ++state.graphRequest; const layer = state.graphLayer; const generation = state.generation;
  try {
    const [data, focus] = await Promise.all([api('/api/graph?layer=' + encodeURIComponent(layer)), api('/api/map-focus?layer=' + encodeURIComponent(layer)).catch(() => null)]);
    if (requestId !== state.graphRequest || layer !== state.graphLayer || generation !== state.generation) return;
    const signature = JSON.stringify([layer, data.nodes, data.edges, data.available, focus && [focus.phase, focus.default_view, focus.anchor, focus.mainline, focus.frontier, focus.alternatives]]);
    data._layer = layer; state.graph = data;
    if (focus) { focus._main = new Set(focus.mainline.edges); focus._path = new Set(focus.frontier.edges); trackFresh(focus, layer); }
    state.mapFocus = focus;
    renderGraphChrome(data);
    if (signature === state.graphSignature && !force) return;
    state.graphSignature = signature; graphFilters();
    if (state.mapGuide) state.mapGuide.stale = true;
    if (state.view === 'reader') { renderGuideOutline(); await syncGuide(); return; }
    if (state.view !== 'map') return;
    buildMap(data);
  } catch (error) {
    if (requestId !== state.graphRequest) return;
    $('#graph-notice').textContent = state.graph ? t('map.keepLast', { error: error.message }) : error.message; $('#graph-notice').hidden = false;
    if (!state.graph) { graphEmpty(t('map.unavailable'), t('map.unavailableDesc')); if (state.view === 'reader') $('#proof-reading').replaceChildren(node('p', 'empty-inline', t('reader.recordsUnavailable', { error: error.message }))); }
  }
}
const MAP_STYLE = [
  { selector: 'node', style: { shape: 'round-rectangle', 'corner-radius': 11, width: 'data(w)', height: 'data(h)', 'background-color': '#fffefd', 'background-image': 'data(accent)', 'background-width': '100%', 'background-height': '100%', 'border-width': 1, 'border-color': '#dce3df', label: 'data(label)', color: '#344843', 'font-family': '"Noto Sans SC","Noto Sans CJK SC","PingFang SC",system-ui,sans-serif', 'font-size': 12, 'font-weight': 500, 'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap', 'text-max-width': (element) => Math.max(120, element.data('w') - 30), 'line-height': 1.35, 'min-zoomed-font-size': 5, 'overlay-opacity': 0, 'underlay-color': '#344e45', 'underlay-opacity': .045, 'underlay-padding': 3 } },
  { selector: 'node[kind="route"]', style: { 'border-style': 'dashed', 'border-dash-pattern': [5, 4] } },
  { selector: 'node[kind="evidence"]', style: { 'background-color': '#f7faf8', 'font-size': 11 } },
  { selector: 'node.spine', style: { 'border-color': '#5f9180', 'border-width': 1.6 } },
  { selector: 'node.top', style: { 'border-color': '#86a598', 'border-width': 1.5 } },
  { selector: 'node.front', style: { 'border-color': '#c39a45', 'border-width': 2, 'background-color': '#fffaf0' } },
  { selector: 'node.front-refuted', style: { 'border-color': '#b27f7a', 'border-width': 2 } },
  { selector: 'node.front-ready', style: { 'border-color': '#c39a45', 'border-width': 1.6, 'border-style': 'dashed', 'border-dash-pattern': [6, 4] } },
  { selector: 'node.linked', style: { 'underlay-color': '#d4a64a', 'underlay-opacity': .2, 'underlay-padding': 5 } },
  { selector: 'node.fresh', style: { 'underlay-color': '#d39a2a', 'underlay-opacity': .38, 'underlay-padding': 7 } },
  { selector: 'node.alt', style: { opacity: .62 } },
  { selector: 'node.anchor', style: { 'background-color': '#f2f7f4', 'border-color': '#2c5a60', 'border-width': 2.2, 'font-size': 13.5, 'font-weight': 600, color: '#1c3a3f' } },
  { selector: 'node[status="missing"]', style: { 'border-style': 'dashed', 'border-color': '#bda39e' } },
  { selector: 'node[validity!="VALID"]', style: { 'border-style': 'dashed', 'border-color': '#b8bbb5', 'background-opacity': .65 } },
  { selector: 'node.neighbor', style: { 'border-color': '#93b4a5' } },
  { selector: 'node.selected-node', style: { 'border-color': '#2f6a5a', 'border-width': 2.4, 'underlay-color': '#86a99a', 'underlay-opacity': .22, 'underlay-padding': 7 } },
  { selector: 'node.rel-end', style: { 'border-color': '#1c3a3f', 'border-width': 2.2 } },
  { selector: 'node.record-focus', style: { 'underlay-color': '#d4a64a', 'underlay-opacity': .4, 'underlay-padding': 8 } },
  { selector: 'edge', style: { label: '', width: 1.3, 'line-color': '#bccac2', 'target-arrow-color': '#9fb1a7', 'target-arrow-shape': 'triangle', 'arrow-scale': .7, 'curve-style': 'bezier', opacity: .82, 'overlay-opacity': 0 } },
  { selector: 'edge[relationship="evidence"]', style: { 'line-style': 'dotted', 'line-color': '#9db8aa', 'target-arrow-color': '#9db8aa', 'target-arrow-shape': 'circle', 'arrow-scale': .55 } },
  { selector: 'edge[relationship="BLOCKED_BY"]', style: { 'line-color': '#b69b74', 'target-arrow-color': '#b69b74', 'line-style': 'dashed', 'line-dash-pattern': [5, 5] } },
  { selector: 'edge.alt-edge', style: { opacity: .45 } },
  { selector: 'edge.spine-edge', style: { width: 2.6, 'line-color': '#3f7666', 'target-arrow-color': '#3f7666', opacity: 1, 'z-index': 5 } },
  { selector: 'edge.path-edge', style: { width: 2.4, 'line-color': '#b5852b', 'target-arrow-color': '#b5852b', opacity: 1, 'z-index': 5 } },
  { selector: 'edge[validity!="VALID"]', style: { 'line-style': 'dashed', 'line-color': '#c3c6c1', 'target-arrow-color': '#c3c6c1', opacity: .6, width: 1.2 } },
  { selector: 'edge.highlight', style: { 'line-color': '#5f8f7c', 'target-arrow-color': '#5f8f7c', opacity: 1, width: 2.2, 'z-index': 8 } },
  { selector: 'edge.faded', style: { opacity: .26 } },
  { selector: 'node.ledger-note', style: { 'border-style': 'dashed', 'border-color': '#9ba9aa' } },
  { selector: 'edge.ledger-edge', style: { 'line-color': '#aab6b9', 'target-arrow-color': '#aab6b9', 'line-style': 'dashed', width: 1.3, opacity: .72 } },
  { selector: 'edge.kernel-edge', style: { 'line-color': '#256e5d', 'target-arrow-color': '#256e5d', width: 4, opacity: 1, 'z-index': 7 } },
  { selector: 'node.trace-upstream', style: { 'border-color': '#527da4', 'border-width': 2, 'underlay-color': '#9cb8d1', 'underlay-opacity': .16 } },
  { selector: 'node.trace-downstream', style: { 'border-color': '#277e70', 'border-width': 2, 'underlay-color': '#8ec6b9', 'underlay-opacity': .16 } },
  { selector: 'edge.trace-upstream', style: { 'line-color': '#527da4', 'target-arrow-color': '#527da4', width: 3.2, opacity: 1, 'z-index': 12 } },
  { selector: 'edge.trace-downstream', style: { 'line-color': '#277e70', 'target-arrow-color': '#277e70', width: 3.2, opacity: 1, 'z-index': 12 } },
  { selector: 'node.trace-muted', style: { opacity: .23 } },
  { selector: 'edge.trace-muted', style: { opacity: .10 } },
  { selector: 'edge.rel-hover', style: { 'line-color': '#2f6a5a', 'target-arrow-color': '#2f6a5a', opacity: 1, width: 3.2, 'z-index': 20 } },
  { selector: 'edge.rel-candidate', style: { 'line-color': '#c17d1c', 'target-arrow-color': '#c17d1c', opacity: 1, width: 3.6, 'z-index': 25 } },
  { selector: 'edge.rel-selected', style: { 'line-color': '#1c3a3f', 'target-arrow-color': '#1c3a3f', opacity: 1, width: 3.8, 'z-index': 30, 'underlay-color': '#7fb2a0', 'underlay-opacity': .3, 'underlay-padding': 4 } },
];
function createMap() {
  state.cy = window.cytoscape({ container: $('#graph-canvas'), elements: [], minZoom: .05, maxZoom: 5, wheelSensitivity: .18, boxSelectionEnabled: false, autounselectify: true, layout: { name: 'preset' }, style: MAP_STYLE });
  const cy = state.cy; let hoverFrame = 0;
  cy.on('tap', (event) => {
    const target = event.target;
    if (target !== cy && target.isNode()) { closeChooser(false); selectNode(target.id()); return; }
    const original = event.originalEvent; const touch = !!original && (original.pointerType === 'touch' || /^touch/.test(original.type || ''));
    let keys = hitEdges(event.position, touch ? 18 : 8);
    if (target !== cy && target.isEdge() && !keys.includes(target.id())) keys = [target.id(), ...keys];
    hideGraphRelation();
    if (!keys.length) { closeChooser(false); return; }
    if (keys.length === 1) selectRelation(keys[0]); else openChooser(keys, event.renderedPosition);
  });
  cy.on('mousemove', (event) => {
    if (event.target !== cy && event.target.isNode()) { hideGraphRelation(); return; }
    const position = event.position, rendered = event.renderedPosition;
    cancelAnimationFrame(hoverFrame); hoverFrame = requestAnimationFrame(() => hoverAt(position, rendered));
  });
  cy.on('mouseover', 'node', () => { $('#graph-canvas').style.cursor = 'pointer'; });
  cy.on('mouseout', 'node', () => { $('#graph-canvas').style.cursor = 'default'; });
  cy.on('pan zoom', () => { hideGraphRelation(); if (!state.rendering) closeChooser(false); });
  cy.on('dragfree', 'node', () => { if ($('#graph-layout').value !== 'layers') state.mapRoutes = straightRoutes(); });
  $('#graph-canvas').addEventListener('mouseleave', hideGraphRelation);
}
function hideGraphRelation() {
  $('#graph-edge-tooltip').hidden = true; state.hoverKey = '';
  state.cy?.elements('.rel-hover').removeClass('rel-hover');
}
function relationEnds(edge, max = 60) {
  const records = recordsById(); const source = records.get(edge.source), target = records.get(edge.target); const wrap = node('span', 'relation-item-ends');
  wrap.append(source ? titleNode('span', '', source, max) : src('span', '', edge.source), node('span', 'relation-arrow-inline', '→'), target ? titleNode('span', '', target, max) : src('span', '', edge.target));
  return wrap;
}
function relationTypeLine(edge) {
  const line = node('span', 'relation-item-type'); line.append(document.createTextNode(relationName(edge.type)));
  if ((edge.validity || 'VALID') !== 'VALID') line.append(badge(edge.validity, t('map.rel.invalid')));
  const focus = currentFocus(); const key = edgeKey(edge);
  if (focus?._main.has(key)) line.append(node('span', 'badge', t('map.legend.main')));
  else if (focus?._path.has(key)) line.append(node('span', 'badge amber', t('map.legend.path')));
  return line;
}
function hoverAt(position, rendered) {
  if (!state.cy || !$('#edge-chooser').hidden) return;
  const keys = hitEdges(position, 7); const signature = keys.join('\n');
  if (signature !== state.hoverKey) { state.cy.elements('.rel-hover').removeClass('rel-hover'); keys.forEach((key) => state.cy.getElementById(key).addClass('rel-hover')); state.hoverKey = signature; }
  $('#graph-canvas').style.cursor = keys.length ? 'pointer' : 'default';
  const tooltip = $('#graph-edge-tooltip');
  if (!keys.length) { tooltip.hidden = true; return; }
  tooltip.replaceChildren();
  if (keys.length === 1) {
    const edge = edgeById(keys[0]); if (!edge) return; const records = recordsById();
    tooltip.append(node('strong', 'edge-tooltip-kind', relationName(edge.type)));
    const ends = node('div', 'edge-tooltip-ends'); const source = records.get(edge.source), target = records.get(edge.target);
    if (source) ends.append(titleNode('span', '', source, 65)); ends.append(node('span', 'edge-tooltip-arrow', '↓')); if (target) ends.append(titleNode('span', '', target, 65));
    tooltip.append(ends);
    if ((edge.validity || 'VALID') !== 'VALID') tooltip.append(badge(edge.validity, t('map.rel.invalid')));
    tooltip.append(node('small', '', t('map.hover.one')));
  } else {
    tooltip.append(node('strong', 'edge-tooltip-kind', t('map.hover.many', { n: keys.length })));
    const kinds = node('div', 'edge-tooltip-ends'); keys.slice(0, 4).forEach((key) => { const edge = edgeById(key); if (edge) kinds.append(node('span', '', relationName(edge.type))); }); tooltip.append(kinds);
  }
  tooltip.hidden = false;
  tooltip.style.left = Math.max(12, Math.min(rendered.x + 18, state.cy.width() - tooltip.offsetWidth - 12)) + 'px';
  tooltip.style.top = Math.max(12, Math.min(rendered.y + 18, state.cy.height() - tooltip.offsetHeight - 12)) + 'px';
}
// Overlapping relations are never resolved silently: each candidate is listed and chosen explicitly.
function openChooser(keys, rendered) {
  const box = $('#edge-chooser'); box.replaceChildren();
  const head = node('div', 'chooser-head'); head.append(node('span', '', t('map.chooser.title', { n: keys.length })));
  const close = node('button', 'icon-button'); close.type = 'button'; close.setAttribute('aria-label', t('map.chooser.close')); close.append(icon('close')); close.onclick = () => closeChooser(true); head.append(close); box.append(head);
  for (const key of keys) {
    const edge = edgeById(key); if (!edge) continue;
    const option = node('button', 'chooser-option'); option.type = 'button'; option.dataset.relation = key;
    option.append(relationTypeLine(edge), relationEnds(edge, 70));
    // Same-endpoint REQUIRES records can differ only in their recorded rationale.
    option.append(src('span', 'chooser-rationale', edge.exact_claim ? truncate(edge.exact_claim, 150) : text(edge.relation_id || edge.id || key)));
    if (edge.exact_claim) option.title = edge.exact_claim;
    option.append(node('small', 'chooser-id', 'ID · ' + text(edge.relation_id || edge.id || key).slice(-12)));
    option.onclick = () => selectRelation(key);
    option.onmouseenter = option.onfocus = () => previewRelation(key); option.onmouseleave = () => previewRelation(null);
    box.append(option);
  }
  box.hidden = false; const stage = box.parentElement.getBoundingClientRect(); const point = rendered || { x: stage.width / 2, y: stage.height / 3 };
  box.style.left = Math.max(12, Math.min(point.x + 14, stage.width - box.offsetWidth - 12)) + 'px';
  box.style.top = Math.max(12, Math.min(point.y + 14, stage.height - box.offsetHeight - 12)) + 'px';
  box.querySelector('.chooser-option')?.focus({ preventScroll: true });
}
function closeChooser(restoreFocus) {
  const box = $('#edge-chooser'); if (box.hidden) return;
  box.hidden = true; box.replaceChildren(); previewRelation(null);
  if (restoreFocus) $('#graph-canvas').focus({ preventScroll: true });
}
function previewRelation(key) { state.cy?.elements('.rel-candidate').removeClass('rel-candidate'); if (key) state.cy?.getElementById(key).addClass('rel-candidate'); }
function arrowNavigation(container, selector) {
  container.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && container.id === 'edge-chooser') { event.preventDefault(); event.stopPropagation(); closeChooser(true); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const items = [...container.querySelectorAll(selector)]; const index = items.indexOf(document.activeElement); if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    items[next].focus();
  });
}
arrowNavigation($('#edge-chooser'), '.chooser-option'); arrowNavigation($('#relation-list-items'), '.relation-item'); arrowNavigation($('#node-list-items'), '.node-item');

async function buildMap(data) {
  hideGraphRelation(); closeChooser(false);
  if (!window.cytoscape) { graphEmpty(t('map.noLib'), t('map.noLibDesc')); applyFilters(); return; }
  if (!state.cy) createMap();
  let gone = null;
  if (state.selected && !recordsById().has(state.selected)) state.selected = null;
  if (state.relation && !edgeById(state.relation)) { state.relation = null; gone = 'map.rel.gone'; }
  if (state.mapRecord && !findRecord(state.mapRecord)) { state.mapRecord = null; gone = gone || 'map.rec.gone'; }
  const fit = state.needsFit; state.needsFit = false;
  applyFilters(fit);
  // A live update can change a selected record; keep its inspector current without moving the view.
  if (state.relation) { $('#graph-detail').hidden = false; renderRelation(edgeById(state.relation)); }
  else if (state.mapRecord) { $('#graph-detail').hidden = false; renderRecord(findRecord(state.mapRecord)); }
  else if (state.selected) { $('#graph-detail').hidden = false; await showDetail(recordsById().get(state.selected), data._layer); }
  else if (gone) { $('#graph-detail').hidden = false; $('#detail-kind').textContent = t('map.detail'); const body = $('#detail-body'); body.replaceChildren(node('p', 'node-status-note', t(gone))); delete body.dataset.nodeId; }
  else $('#graph-detail').hidden = true;
}
async function showDetail(item, layer) {
  const request = ++state.detailRequest; const hasProof = layer === 'facts' && lower(item.status) !== 'missing';
  renderDetail(item, hasProof);
  if (!hasProof) return;
  try { const detail = await api('/api/facts/' + encodeURIComponent(item.id)); if (request === state.detailRequest && state.selected === item.id) renderDetail({ ...item, ...detail }, false); }
  catch (error) { if (request === state.detailRequest) { renderDetail(item, false); $('#detail-body').append(node('p', 'node-status-note', t('detail.proofUnavailable', { error: error.message }))); } }
}
function enterMap() {
  $('#list-toggle').hidden = false;
  state.needsFit = true;
  if (state.graph && state.graph._layer === state.graphLayer) buildMap(state.graph);
  else { state.graphSignature = ''; graphEmpty(t('map.loadingLayer', { layer: t(state.graphLayer === 'facts' ? 'layer.factDeps' : 'layer.researchLower') }), t('map.loadingLayerDesc')); loadGraph(true); }
  selectionURL();
}
function focusIds() {
  if (state.focus === 'all' || !state.selected || !state.graph) return null;
  if (state.focus === 'path') return dependencyTrace(state.selected).nodes;
  const visible = new Set([state.selected]); const edges = state.graph.edges || [];
  if (state.focus === 'neighbors') { edges.forEach((edge) => { if (edge.source === state.selected) visible.add(edge.target); if (edge.target === state.selected) visible.add(edge.source); }); return visible; }
  const adjacency = new Map(); const upstream = state.focus === 'upstream';
  for (const edge of edges) { const source = upstream ? edge.target : edge.source; const target = upstream ? edge.source : edge.target; if (!adjacency.has(source)) adjacency.set(source, []); adjacency.get(source).push(target); }
  const queue = [state.selected]; for (let index = 0; index < queue.length; index++) for (const next of adjacency.get(queue[index]) || []) if (!visible.has(next)) { visible.add(next); queue.push(next); }
  return visible;
}
function applyFilters(fit = false) {
  if (!state.graph) return;
  const query = lower($('#graph-search').value.trim()); const team = $('#graph-team').value; const author = $('#graph-author').value; const status = $('#graph-status').value; const depth = $('#graph-depth').value; const scope = mapScope();
  state.visibleNodes = (state.graph.nodes || []).filter((item) => scope.has(item.id) && (item.id === state.selected || ((!query || lower([item.id, item.label, item.display_title, graphNodeLabel(item), item.statement, item.author].join(' ')).includes(query)) && (!team || item.team_id === team) && (!author || item.author === author) && (!status || item.status === status) && (depth === '' || String(item.depth) === depth))));
  const ids = new Set(state.visibleNodes.map((item) => item.id));
  state.visibleEdges = (state.graph.edges || []).filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  $('#clear-filters').hidden = !(filtersActive() || state.focus !== 'all');
  $('#graph-stat').textContent = t('map.stat', { shown: ids.size, total: state.graph.nodes?.length || 0, edges: state.visibleEdges.length, depth: state.graph.max_depth || 0 });
  if (!ids.size) graphEmpty(t(state.graph.available === false ? 'map.emptyNone' : state.graph.nodes?.length ? 'map.emptyNoMatch' : 'map.emptyGrowing'), t(state.graph.nodes?.length ? 'map.emptyNoMatchDesc' : state.graphLayer === 'research' ? 'map.emptyResearchDesc' : 'map.emptyFactsDesc'));
  else $('#graph-empty').hidden = !!window.cytoscape;
  ['#graph-export', '#graph-fit', '#zoom-in', '#zoom-out'].forEach((id) => { $(id).disabled = !ids.size || !state.cy; });
  renderMap({ fit });
  renderNodeList(); renderRelationList(); renderMapChrome();
}
function renderMap({ fit = false } = {}) {
  const cy = state.cy; if (!cy || !state.graph) return;
  hideGraphRelation(); closeChooser(false);
  const focus = currentFocus(); const view = mapView(); const layer = state.graph._layer;
  const keyId = !fit && (state.selected || focus?.anchor); const keyElement = keyId ? cy.getElementById(keyId) : null;
  const before = keyElement?.length ? { ...keyElement.renderedPosition() } : null;
  const roles = mapRoles(focus, view, layer); const nodes = state.visibleNodes, edges = state.visibleEdges;
  const anchor = focus?.anchor && nodes.some((item) => item.id === focus.anchor) ? focus.anchor : null;
  const ins = new Map(), outs = new Map(); edges.forEach((edge) => { ins.set(edge.target, (ins.get(edge.target) || 0) + 1); outs.set(edge.source, (outs.get(edge.source) || 0) + 1); });
  const items = nodes.map((item) => ({ id: item.id, size: cardSize(item.id === anchor ? 'anchor' : item.kind === 'evidence' ? 'evidence' : 'card', Math.max(ins.get(item.id) || 0, outs.get(item.id) || 0)) }));
  const choice = $('#graph-layout').value; const layout = choice === 'layers' ? layeredLayout(items, edges, anchor) : null;
  const sizes = new Map(items.map((item) => [item.id, item.size])); const old = new Map(); cy.nodes().forEach((element) => old.set(element.id(), { ...element.position() }));
  const elements = nodes.map((item, index) => {
    const size = sizes.get(item.id); const role = roles.get(item.id) || {}; const classes = [];
    if (role.anchor) classes.push('anchor'); if (role.spine && view === 'mainline') classes.push('spine'); if (role.top) classes.push('top');
    if (role.front) classes.push(role.front.kind === 'ready' ? 'front-ready' : role.front.kind === 'refuted' ? 'front-refuted' : 'front');
    if (isLedgerOpen(item)) classes.push('ledger-note');
    if (role.linked) classes.push('linked'); if (role.fresh) classes.push('fresh'); if (role.alt) classes.push('alt');
    return { group: 'nodes', data: { id: item.id, label: mapLabel(item, role, layer), w: size.w, h: size.h, accent: graphAccent(graphColor(item), size.w, size.h), kind: item.kind || 'fact', status: item.status, state: stateOf(item), validity: item.validity || 'VALID' }, classes: classes.join(' '), position: layout ? layout.pos.get(item.id) : old.get(item.id) || { x: Math.cos(index * 2.4) * Math.sqrt(index + 1) * 60, y: Math.sin(index * 2.4) * Math.sqrt(index + 1) * 60 } };
  });
  // In All records only a completed derivation keeps its emphasis; an open goal's structure stays neutral.
  const spine = view === 'mainline' || (view === 'all' && focus?.complete) ? focus?._main : null, paths = view === 'frontier' ? focus?._path : null;
  for (const edge of edges) {
    const key = edgeKey(edge); const classes = [];
    if (spine?.has(key)) classes.push('spine-edge'); if (paths?.has(key)) classes.push('path-edge'); if (roles.get(edge.source)?.alt) classes.push('alt-edge');
    const records = recordsById();
    if (isLedgerOpen(records.get(edge.source)) || isLedgerOpen(records.get(edge.target))) classes.push('ledger-edge');
    if (targetKernelEvidence().some(item => item.id === edge.source) && edge.target === focus?.anchor) classes.push('kernel-edge');
    elements.push({ group: 'edges', data: { id: key, source: edge.source, target: edge.target, relationship: edge.type || 'requires', validity: edge.validity || 'VALID' }, classes: classes.join(' ') });
  }
  state.rendering = true;
  cy.batch(() => { cy.elements().remove(); cy.add(elements); if (layout) routeEdges(layout); });
  cy.autoungrabify(!!layout);
  if (layout) { state.mapRoutes = layout.routes; state.mapMain = layout.main; state.mapDetached = layout.detached; }
  else { runForceLayout(choice); state.mapRoutes = straightRoutes(); state.mapMain = null; state.mapDetached = 0; }
  markSelection();
  const after = keyId && cy.getElementById(keyId);
  if (before && after?.length) { const now = after.renderedPosition(); cy.panBy({ x: before.x - now.x, y: before.y - now.y }); }
  else fitMap(state.focus === 'path');
  state.rendering = false;
}
function runForceLayout(choice) {
  const cy = state.cy; const elements = cy.elements(); if (!elements.nodes().length) return;
  if (choice === 'circle') cy.layout({ name: 'circle', fit: false, padding: 55, spacingFactor: 1.25 }).run();
  else cy.layout({ name: 'cose', animate: false, randomize: false, fit: false, padding: 55, nodeRepulsion: () => 9500, idealEdgeLength: () => 58, edgeElasticity: () => 100, gravity: .3, numIter: Math.min(900, 350 + elements.nodes().length * 2), componentSpacing: 100, initialTemp: 100, coolingFactor: .95, minTemp: 1 }).run();
  if (choice !== 'network') return;
  // A network has no privileged axis; rotate a tall cluster to suit the canvas, then separate cards.
  const box = elements.nodes().boundingBox({ includeLabels: false });
  const width = cy.width() - 110, height = cy.height() - 110;
  if (width > 0 && height > 0 && Math.min(width / box.h, height / box.w) > Math.min(width / box.w, height / box.h) * 1.12) {
    const cx = (box.x1 + box.x2) / 2, cyy = (box.y1 + box.y2) / 2;
    elements.nodes().positions((item) => ({ x: cx - (item.position('y') - cyy), y: cyy + (item.position('x') - cx) }));
  }
  const cards = elements.nodes().map((item) => ({ node: item, x: item.position('x'), y: item.position('y'), w: item.outerWidth(), h: item.outerHeight() }));
  for (let pass = 0; pass < 60; pass++) {
    let moved = false;
    for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
      const a = cards[i], b = cards[j], dx = b.x - a.x, dy = b.y - a.y;
      const ox = (a.w + b.w) / 2 + 22 - Math.abs(dx), oy = (a.h + b.h) / 2 + 22 - Math.abs(dy);
      if (ox <= 0 || oy <= 0) continue;
      if (ox < oy) { const shift = (ox + .5) / 2 * (dx < 0 ? -1 : 1); a.x -= shift; b.x += shift; } else { const shift = (oy + .5) / 2 * (dy < 0 ? -1 : 1); a.y -= shift; b.y += shift; }
      moved = true;
    }
    if (!moved) break;
  }
  cy.batch(() => cards.forEach((card) => card.node.position({ x: card.x, y: card.y })));
}
// Focused views fit their records; a large view opens readable at the main goal instead of as a speck.
function fitMap(full = false) {
  const cy = state.cy; if (!cy || !cy.nodes().length) return;
  cy.resize(); if (!cy.width() || !cy.height()) { state.needsFit = true; return; }
  const main = !full && state.mapMain ? cy.nodes().filter((item) => state.mapMain.has(item.id())) : cy.nodes();
  cy.fit(main.length ? main : cy.nodes(), 44);
  const center = { x: cy.width() / 2, y: cy.height() / 2 };
  if (cy.zoom() > 1.15) cy.zoom({ level: 1.15, renderedPosition: center });
  if (!full && cy.zoom() < .55) {
    const anchor = cy.getElementById(currentFocus()?.anchor || '');
    cy.zoom(.55);
    if (anchor.length) { const p = anchor.position(); cy.pan({ x: cy.width() * .78 - p.x * .55, y: cy.height() / 2 - p.y * .55 }); } else cy.center(main.length ? main : cy.nodes());
  }
}
function revealNodes(ids) {
  const cy = state.cy; if (!cy) return;
  const nodes = cy.collection(ids.map((id) => cy.getElementById(id)).filter((element) => element.length)); if (!nodes.length) return;
  const outside = nodes.some((element) => { const p = element.renderedPosition(); return p.x < 40 || p.y < 40 || p.x > cy.width() - 40 || p.y > cy.height() - 40; });
  if (outside) cy.center(nodes);
}
function markSelection() {
  const cy = state.cy; if (!cy) return;
  cy.batch(() => {
    cy.elements().removeClass('selected-node neighbor highlight rel-selected rel-end faded record-focus trace-upstream trace-downstream trace-muted');
    if (state.relation) { const edge = cy.getElementById(state.relation); if (edge.length) { edge.addClass('rel-selected'); edge.connectedNodes().addClass('rel-end'); cy.edges().difference(edge).addClass('faded'); } }
    else if (state.selected) {
      const item = cy.getElementById(state.selected);
      if (item.length) {
        const trace = dependencyTrace(state.selected);
        cy.nodes().filter(element => !trace.nodes.has(element.id())).addClass('trace-muted');
        cy.edges().filter(element => !trace.edges.has(element.id())).addClass('trace-muted');
        for (const [direction, cone] of [['upstream', trace.upstream], ['downstream', trace.downstream]]) {
          for (const id of cone.nodes) if (id !== state.selected) cy.getElementById(id).addClass('trace-' + direction);
          for (const id of cone.edges) cy.getElementById(id).addClass('trace-' + direction);
        }
        item.addClass('selected-node');
      }
    }
    else if (state.mapRecord) (findRecord(state.mapRecord)?.links || []).forEach((link) => cy.getElementById(link.id).addClass('record-focus'));
  });
}
function renderNodeList() {
  const list = $('#node-list-items'); const scroll = $('#node-list').scrollTop; list.replaceChildren();
  $('#node-list-count').textContent = number(state.visibleNodes.length);
  if (!state.visibleNodes.length) { list.append(node('div', 'empty-inline', t('map.noMatchList'))); return; }
  for (const item of state.visibleNodes.slice(0, state.nodeLimit)) {
    const button = node('button', 'node-item' + (state.selected === item.id ? ' selected' : '')); button.dataset.nodeId = item.id;
    button.setAttribute('aria-pressed', String(state.selected === item.id)); button.append(titleNode('strong', '', item), node('small', '', item.id.slice(0, 12) + ' · ' + (isLedgerOpen(item) ? t('map.ledger.open') : stateLabel(stateOf(item), item.kind))));
    button.title = graphNodeLabel(item); button.onclick = () => selectNode(item.id, { reveal: true }); list.append(button);
  }
  if (state.visibleNodes.length > state.nodeLimit) { const more = node('button', 'node-more', t('map.more100')); more.onclick = () => { state.nodeLimit += 100; renderNodeList(); }; list.append(more); }
  $('#node-list').scrollTop = scroll;
}
function renderRelationList() {
  const list = $('#relation-list-items'); const box = $('#relation-list'); const scroll = box.scrollTop; list.replaceChildren();
  const focus = currentFocus(); const edges = state.visibleEdges || []; $('#relation-list-count').textContent = number(edges.length);
  if (!edges.length) { list.append(node('div', 'empty-inline', t('map.relList.empty'))); return; }
  const rank = (edge) => focus?._main.has(edgeKey(edge)) ? 0 : focus?._path.has(edgeKey(edge)) ? 1 : 2;
  const sorted = [...edges].sort((a, b) => rank(a) - rank(b) || a.target.localeCompare(b.target) || a.source.localeCompare(b.source) || edgeKey(a).localeCompare(edgeKey(b)));
  const duplicateCounts = new Map();
  const pairKey = (edge) => JSON.stringify([edge.source, edge.type, edge.target]);
  sorted.forEach((edge) => duplicateCounts.set(pairKey(edge), (duplicateCounts.get(pairKey(edge)) || 0) + 1));
  for (const edge of sorted.slice(0, state.relationLimit)) {
    const key = edgeKey(edge); const button = node('button', 'relation-item' + (state.relation === key ? ' selected' : '')); button.type = 'button'; button.dataset.relation = key;
    button.setAttribute('aria-pressed', String(state.relation === key)); button.append(relationTypeLine(edge), relationEnds(edge, 56));
    if (duplicateCounts.get(pairKey(edge)) > 1) {
      if (edge.exact_claim) button.append(src('span', 'chooser-rationale', truncate(edge.exact_claim, 120)));
      button.append(node('small', 'chooser-id', 'ID · ' + text(edge.relation_id || edge.id || key).slice(-12)));
    }
    button.onfocus = () => previewRelation(key); button.onblur = () => previewRelation(null);
    button.onclick = () => selectRelation(key, { reveal: true }); list.append(button);
  }
  if (sorted.length > state.relationLimit) { const more = node('button', 'node-more', t('map.relList.more')); more.onclick = () => { state.relationLimit += 100; renderRelationList(); }; list.append(more); }
  box.scrollTop = scroll;
}
function setSidePane(pane) {
  state.sidePane = pane;
  $$('[data-side]').forEach((tab) => tab.setAttribute('aria-selected', String(tab.dataset.side === pane)));
  $$('#map-side [data-pane]').forEach((element) => { element.hidden = element.dataset.pane !== pane; });
}
$$('[data-side]').forEach((tab) => tab.addEventListener('click', () => setSidePane(tab.dataset.side)));

function renderMapChrome() {
  const view = mapView(); const focus = currentFocus(); setPressed('[data-map-view]', 'mapView', view);
  $('#show-alternatives-wrap').hidden = !(view === 'mainline' && focus?.alternatives.length);
  $('#show-alternatives').checked = state.showAlternatives; $('#show-evidence').checked = state.showEvidence;
  renderPhase(focus); renderMapSide();
}
// Controller closure and formal acceptance are separate records; both are stated, never merged.
function renderPhase(focus) {
  const box = $('#map-phase'); box.replaceChildren(); box.className = 'map-phase'; if (!focus) return;
  if (focus.phase?.archive_closed) { box.append(node('span', '', t('map.phase.evidenceClosed'))); return; }
  const phase = focus.phase || {}; const label = phase.target_state ? stateLabel(phase.target_state, 'obligation') : t('state.unknown');
  const current = state.lang === 'en' ? label.toLowerCase() : label;
  let main;
  if (phase.awaiting_final_audit) main = state.lang === 'en' ? 'Root proof checked · Independent final audit pending.' : '主目标证明已检查 · 独立最终审计尚未完成。';
  else if (phase.controller_closed) main = t(phase.target_state === 'refuted' ? 'map.phase.completeRefuted' : 'map.phase.complete');
  else if (phase.formal_current) main = t('map.phase.formalCurrent', { state: current });
  else if (phase.target_state) main = phase.recorded_outcome ? t('map.phase.reopened', { outcome: phase.recorded_outcome, state: current }) : t('map.phase.progress', { state: current });
  else main = t('map.phase.noTarget');
  if (!(phase.controller_closed || phase.formal_current)) box.classList.add('progress');
  box.append(node('span', 'phase-main', main));
  const formal = phase.formal_acceptance;
  if (!phase.formal_available) box.append(node('span', 'phase-aside', t(targetKernelEvidence().length ? 'map.ledger.linked' : 'map.phase.formalNone')));
  else box.append(node('span', 'phase-aside', t('map.phase.formal', { status: formal === 'passed' ? t('map.formal.passed') : formal ? statusInfo(formal)[0] : t('map.formal.none') })));
  if (phase.formal_passed && !phase.formal_current && !phase.controller_closed) box.append(node('span', 'phase-aside', t('map.phase.formalOld')));
  const certificates = targetKernelEvidence();
  if (certificates.length) {
    const pending = state.graph.nodes.filter(item => item.kind === 'obligation' && lower(item.status) === 'open').length;
    const note = node('div', 'map-ledger-note');
    note.append(node('strong', '', t('map.ledger.title')), node('span', '', t('map.ledger.explain', { n: pending })));
    const open = node('button', 'text-button', t('map.ledger.certificate')); open.type = 'button';
    open.onclick = () => selectNode(certificates[0].id, { reveal: true }); note.append(open); box.append(note);
  }
  if (state.graph?._layer === 'facts') box.append(node('span', 'phase-aside', t(focus.anchor ? 'map.phase.factsAnchor' : 'map.phase.noAnchorFacts')));
  if (filtersActive()) box.append(node('span', 'phase-aside', t('map.all.filtered')));
}
function renderMapSide() {
  const pane = $('#map-focus'); const scroll = pane.scrollTop; const open = openKeys(pane); pane.replaceChildren();
  if (!state.graph) return;
  const focus = currentFocus(); const view = mapView();
  if (view === 'mainline') mainlinePane(pane, focus); else if (view === 'frontier') frontierPane(pane, focus); else allPane(pane, focus);
  restoreKeys(pane, open); pane.scrollTop = scroll;
}
function sideRow(id, label) {
  const item = recordsById().get(id); const button = node('button', 'side-row' + (state.selected === id ? ' selected' : '')); button.type = 'button'; button.dataset.nodeId = id;
  if (!item) { button.append(src('span', '', id)); button.disabled = true; return button; }
  const body = node('span', 'side-row-body'); if (label) body.append(node('span', 'side-row-label', label)); body.append(titleNode('span', 'side-row-title', item, 140));
  button.append(mapRecordTag(item), body); button.onclick = () => selectNode(id, { reveal: true }); return button;
}
function sideRows(group, ids, label, key) {
  if (!ids.length) return;
  group.append(node('div', 'side-row-label', label)); ids.slice(0, 6).forEach((id) => group.append(sideRow(id)));
  if (ids.length > 6) { const more = node('details', 'side-more'); more.dataset.sourceKey = key; more.append(node('summary', '', t('map.spine.more', { n: ids.length - 6 }))); ids.slice(6).forEach((id) => more.append(sideRow(id))); group.append(more); }
}
// Explanations of how a view was chosen stay one click away; findings come first.
function sideHow(pane, label, key, notes) {
  const how = node('details', 'side-how'); how.dataset.sourceKey = key; how.append(node('summary', '', label));
  notes.filter(Boolean).forEach((note) => how.append(node('p', 'side-note', note))); pane.append(how);
}
function mainlinePane(pane, focus) {
  pane.append(node('h3', 'side-title', t('map.main.title')));
  const line = focus?.mainline; const layer = state.graph._layer;
  if (!line?.anchor) { pane.append(node('p', 'side-note', t(layer === 'facts' ? 'map.phase.noAnchorFacts' : 'map.main.basis.none'))); return; }
  if (line.nodes.length <= 1 && layer === 'research') { pane.append(node('p', 'side-note', t('map.main.tryFacts'))); const open = node('button', 'text-button side-toggle', t('map.main.openFacts')); open.onclick = () => $('[data-layer="facts"]').click(); pane.append(open); }
  pane.append(roadmap(line.anchor));
  const group = node('section', 'side-group'); group.append(node('h4', '', t('map.spine.title')));
  const certificates = targetKernelEvidence();
  if (certificates.length) sideRows(group, certificates.map(item => item.id), t('map.ledger.certificate'), 'target-kernel');
  group.append(sideRow(line.anchor, t(layer === 'facts' ? 'map.spine.conclusion' : 'map.spine.anchor')));
  line.routes.forEach((id) => group.append(sideRow(id, t('map.spine.route'))));
  const premises = line.nodes.filter((id) => id !== line.anchor && !line.routes.includes(id) && !line.evidence.includes(id));
  sideRows(group, premises, t('map.spine.premises', { n: premises.length }), 'spine-premises');
  sideRows(group, line.evidence, t('map.spine.evidence', { n: line.evidence.length }), 'spine-evidence');
  pane.append(group);
  if (focus.alternatives.length) {
    pane.append(node('p', 'side-note', t('map.alt.note', { n: focus.alternatives.length })));
    const toggle = node('button', 'text-button side-toggle', t(state.showAlternatives ? 'map.alt.hide' : 'map.alt.show')); toggle.onclick = () => { state.showAlternatives = !state.showAlternatives; applyFilters(true); }; pane.append(toggle);
  }
  sideHow(pane, t('map.main.how'), 'how-mainline', [t('map.main.basis.' + line.basis), line.limited ? t('map.main.limited', { n: line.nodes.length }) : '']);
}
// A compact mathematical spine: short editorial headings, details and source jumps on demand.
// It reads the pinned source beside the graph and is never drawn as recorded edges.
function roadmap(anchor) {
  const box = node('section', 'roadmap'); box.dataset.scrollKey = 'roadmap';
  const head = node('div', 'roadmap-head'); head.append(node('h4', '', t('map.road.title'))); box.append(head);
  const guide = state.mapGuide;
  if (!guide || guide.anchor !== anchor || guide.layer !== state.graphLayer || (guide.stale && !guide.loading)) loadMapGuide(anchor);
  const current = state.mapGuide;
  if (!current?.data) { box.append(node('p', 'side-note', current?.error ? t('map.road.error', { error: current.error }) : t('map.road.loading'))); return box; }
  const data = current.data; const process = data.process;
  const read = () => { const button = node('button', 'button secondary roadmap-read', t('map.road.read')); button.type = 'button'; button.onclick = () => openReader(anchor, state.graphLayer); return button; };
  if (!data.sections.length) { box.append(node('p', 'side-note', t('map.road.noProof'))); return box; }
  if (process?.status === 'editorial' && process.source_sha256 === data.proof_sha256) {
    head.append(node('span', 'editorial-tag', t('map.road.tag')));
    const steps = process.steps.map((step, index) => ({ step, index }));
    const line = steps.filter(({ step }) => (step.role || 'argument') === 'argument'), aside = steps.filter(({ step }) => (step.role || 'argument') !== 'argument');
    const list = node('ol', 'roadmap-steps'); line.forEach(({ step, index }, k) => list.append(roadmapStep(step, index, k + 1, data, anchor))); box.append(list);
    if (aside.length) { const more = node('details', 'roadmap-side'); more.dataset.sourceKey = 'roadmap-side'; more.append(node('summary', '', t('map.road.aside', { n: aside.length }))); const rest = node('ul', 'roadmap-steps'); aside.forEach(({ step, index }) => rest.append(roadmapStep(step, index, null, data, anchor))); more.append(rest); box.append(more); }
    box.append(node('p', 'roadmap-note', t('map.road.note', { sha: text(process.source_sha256).slice(0, 12) })), read());
  } else box.append(node('p', 'side-note', t(process?.status === 'withdrawn' ? (process.reason === 'source_changed' ? 'map.road.withdrawn' : 'map.road.withdrawnAnchor') : 'map.road.none')), node('p', 'side-note', t('map.road.sections', { n: data.sections.length })), read());
  return box;
}
function roadmapStep(step, index, numberLabel, data, anchor) {
  const item = node('li', 'roadmap-step' + (numberLabel ? '' : ' aside')); item.dataset.step = String(index + 1); item.dataset.role = step.role || 'argument';
  const details = node('details', 'roadmap-detail'); details.dataset.sourceKey = 'road-step-' + (index + 1);
  const summary = node('summary', 'roadmap-summary');
  if (numberLabel) summary.append(node('span', 'roadmap-index', String(numberLabel)));
  const heading = node('span', 'roadmap-title', step.title?.[state.lang] || truncate(step.text[state.lang] || step.text.zh, 40)); heading.dataset.editorial = ''; summary.append(heading);
  const first = step.cites[0];
  if (first && data.sections[first.section]) {
    const jump = node('button', 'roadmap-source', t('map.road.source')); jump.type = 'button'; jump.title = t('map.road.sourceTitle', { n: first.section + 1 }); jump.setAttribute('aria-label', jump.title);
    jump.onclick = (event) => { event.preventDefault(); event.stopPropagation(); openReaderAt(anchor, state.graphLayer, { start: first.start, end: first.end, section: first.section, step: index + 1 }); };
    summary.append(jump);
  }
  details.append(summary);
  const body = node('div', 'roadmap-body'); const paragraph = node('p', 'roadmap-text', step.text[state.lang] || step.text.zh); paragraph.dataset.editorial = ''; body.append(paragraph);
  const cites = node('div', 'roadmap-cites');
  step.cites.forEach((cite) => {
    const section = data.sections[cite.section]; if (!section) return;
    const button = node('button', 'cite-link'); button.type = 'button'; button.dataset.cite = cite.start + '-' + cite.end;
    button.append(document.createTextNode(t('process.cite', { n: cite.section + 1 })));
    if (section.heading != null) { button.append(document.createTextNode(' · ')); button.append(src('span', 'cite-heading', truncate(section.heading, 34))); }
    button.title = t('process.citeTitle', { start: cite.start, end: cite.end, quote: cite.quote.replace(/\s+/g, ' ').slice(0, 60) });
    button.onclick = () => openReaderAt(anchor, state.graphLayer, { start: cite.start, end: cite.end, section: cite.section, step: index + 1 });
    cites.append(button);
  });
  body.append(cites); details.append(body); item.append(details); return item;
}
async function loadMapGuide(anchor) {
  const layer = state.graphLayer, generation = state.generation; const keep = state.mapGuide?.anchor === anchor && state.mapGuide.layer === layer ? state.mapGuide.data : null;
  state.mapGuide = { anchor, layer, data: keep, loading: true };
  try {
    const data = await api('/api/derivation?layer=' + encodeURIComponent(layer) + '&node_id=' + encodeURIComponent(anchor));
    if (generation !== state.generation || state.mapGuide?.anchor !== anchor || layer !== state.graphLayer) return;
    state.mapGuide = { anchor, layer, data };
  } catch (error) {
    if (generation !== state.generation || state.mapGuide?.anchor !== anchor) return;
    state.mapGuide = { anchor, layer, data: keep, error: error.message };
  }
  if (state.view === 'map' && mapView() === 'mainline') renderMapSide();
}
function frontierPane(pane, focus) {
  pane.append(node('h3', 'side-title', t('map.front.title')));
  if (!focus) return;
  const group = (title, list, empty, render) => { const box = node('section', 'side-group'); box.append(node('h4', '', title)); if (!list.length) box.append(node('p', 'side-note', empty)); list.forEach((entry) => box.append(render(entry))); pane.append(box); };
  const items = focus.frontier.items;
  group(t('map.front.closed'), items.filter((item) => item.kind !== 'ready'), t('map.front.closedEmpty'), frontCard);
  group(t('map.front.ready'), items.filter((item) => item.kind === 'ready'), t('map.front.readyEmpty'), frontCard);
  group(t('map.front.records'), focus.frontier.records || [], t('map.front.recordsEmpty'), recordCard);
  sideHow(pane, t('map.front.how'), 'how-frontier', [t('map.front.basis')]);
}
function frontCard(item) {
  const record = recordsById().get(item.id); const card = node('button', 'front-card ' + item.kind + (state.selected === item.id ? ' selected' : '')); card.type = 'button'; card.dataset.nodeId = item.id; card.dataset.frontier = item.kind;
  const head = node('span', 'front-card-head'); head.append(node('span', 'front-kind', t('map.front.kind.' + item.kind)));
  if (state.freshKeys.has('item:' + item.kind + ':' + item.id)) head.append(node('span', 'fresh-badge', t('map.front.fresh')));
  head.append(node('span', '', t('map.front.time.' + item.time_source, { time: item.at ? fullTime(item.at) : t('time.unrecorded') })));
  if (item.at) head.append(ageNode('span', '', item.at));
  card.append(head, record ? titleNode('span', 'front-title', record, 160) : src('span', 'front-title', item.id));
  if (item.kind === 'ready') card.append(node('span', 'front-meta', t('map.front.readyNote', { n: item.premises.length })));
  card.append(node('span', 'front-meta', item.connected ? t('map.front.connected', { n: Math.max(0, item.path.length - 1) }) : t('map.front.unconnected')));
  card.onclick = () => selectNode(item.id, { reveal: true }); return card;
}
function recordCard(record) {
  const key = recordKey(record); const card = node('button', 'front-card record' + (state.mapRecord === key ? ' selected' : '')); card.type = 'button'; card.dataset.recordKey = key;
  const head = node('span', 'front-card-head'); head.append(node('span', 'front-kind', signalLabel(record.kind)), node('span', 'unverified-badge', t('map.front.unverified')));
  if (state.freshKeys.has('record:' + key)) head.append(node('span', 'fresh-badge', t('map.front.fresh')));
  card.append(head, src('span', 'front-title', truncate(record.claim, 220)));
  const meta = node('span', 'front-meta'); meta.append(record.author ? src('span', '', record.author) : node('span', '', t('explore.team')), document.createTextNode(' · '), node('span', '', fullTime(record.timestamp)));
  if (record.result) meta.append(document.createTextNode(' · '), node('span', '', t('attempt.' + record.result)));
  card.append(meta);
  const link = node('span', 'front-link' + (record.links.length ? '' : ' none'));
  if (!record.links.length) link.textContent = t('map.front.unlinked');
  else {
    link.append(document.createTextNode(t('map.front.linked')));
    record.links.slice(0, 2).forEach((entry, i) => { const item = entry.layer === state.graphLayer ? recordsById().get(entry.id) : null; if (i) link.append(document.createTextNode(' · ')); link.append(item ? titleNode('span', '', item, 50) : src('span', '', entry.id.slice(0, 12))); if (entry.layer !== state.graphLayer) link.append(node('span', '', t('map.front.otherLayer', { layer: layerName(entry.layer) }))); });
  }
  card.append(link); card.onclick = () => selectRecord(key); return card;
}
function allPane(pane, focus) {
  pane.append(node('h3', 'side-title', t('map.all.title')));
  const nodes = state.graph.nodes || []; const layer = state.graph._layer; const counts = new Map();
  nodes.filter((item) => item.kind !== 'evidence').forEach((item) => { const code = stateOf(item); counts.set(code, (counts.get(code) || 0) + 1); });
  const list = STATE_ORDER.filter((code) => counts.get(code)).map((code) => stateLabel(code, layer === 'facts' ? 'fact' : 'obligation') + ' ' + number(counts.get(code)));
  pane.append(node('p', 'side-note', t('map.all.counts', { n: number(nodes.length), list: fmtList(list) })));
  if (state.mapDetached) pane.append(node('p', 'side-note', t('map.all.detached', { n: number(state.mapDetached) })));
  const evidence = nodes.filter((item) => item.kind === 'evidence').length;
  if (evidence && !state.showEvidence) pane.append(node('p', 'side-note', t('map.all.evidenceHidden', { n: number(evidence) })));
  if (!focus?.anchor && layer === 'facts') { const used = new Set((state.graph.edges || []).map((edge) => edge.source)); const tops = nodes.filter((item) => !used.has(item.id) && lower(item.status) === 'accepted').length; if (tops) pane.append(node('p', 'side-note', t('map.all.tops', { n: number(tops) }))); }
  if (focus?.anchor) { const go = node('button', 'button secondary side-toggle', t('map.all.goto')); go.type = 'button'; go.onclick = () => selectNode(focus.anchor, { reveal: true }); pane.append(go); }
}

async function selectNode(id, { reveal = false } = {}) {
  const item = recordsById().get(id); if (!item) return;
  closeChooser(false);
  const previous = state.selected; const ready = new Set((currentFocus()?.frontier.items || []).filter((entry) => entry.kind === 'ready').map((entry) => entry.id));
  state.selected = id; state.relation = null; state.mapRecord = null;
  if (state.view !== 'map') { switchView('map'); return; }
  $('#graph-detail').hidden = false;
  if (window.innerWidth < 1000 || state.graph?.projection === 'historical_source') { $('#map-side').hidden = true; $('#list-toggle').setAttribute('aria-pressed', 'false'); }
  if (state.focus !== 'all' || !state.cy?.getElementById(id).length || (mapView() === 'frontier' && (ready.has(id) || ready.has(previous)))) { applyFilters(false); reveal = true; }
  else { markSelection(); renderNodeList(); renderRelationList(); renderMapSide(); }
  if (reveal) revealNodes([id]);
  selectionURL();
  await showDetail(item, state.graphLayer);
  if (state.graph?.projection === 'historical_source') setTimeout(() => { if (state.view === 'map') fitMap(true); }, 300);
}
function selectRelation(key, { reveal = false } = {}) {
  const edge = edgeById(key); if (!edge) return;
  closeChooser(false); hideGraphRelation();
  const shown = state.cy?.getElementById(key).length;
  state.relation = key; state.selected = null; state.mapRecord = null; state.detailRequest++;
  $('#graph-detail').hidden = false; renderRelation(edge);
  if (!shown) { applyFilters(false); reveal = true; } else { markSelection(); renderRelationList(); renderNodeList(); renderMapSide(); }
  if (reveal) revealNodes([edge.source, edge.target]);
  selectionURL();
}
function selectRecord(key) {
  const record = findRecord(key); if (!record) return;
  closeChooser(false); state.mapRecord = key; state.selected = null; state.relation = null; state.detailRequest++;
  $('#graph-detail').hidden = false; renderRecord(record); markSelection(); renderMapSide(); renderRelationList(); renderNodeList();
  const linked = record.links.filter((link) => link.layer === state.graphLayer).map((link) => link.id); if (linked.length) revealNodes(linked);
  selectionURL();
}
function detailFocus(key) {
  resetFilters(); state.focus = key; if (key === 'all') state.mapView = 'all';
  $$('#detail-body [data-focus]').forEach((action) => { action.classList.toggle('active', action.dataset.focus === key); action.setAttribute('aria-pressed', String(action.dataset.focus === key)); });
  applyFilters(true); selectionURL();
  const item = recordsById().get(state.selected);
  if (item) $('#detail-body .selection-trace-note')?.replaceWith(selectionTraceNote(item));
}
function renderDetail(item, loading) {
  const parent = $('#detail-body'); parent.replaceChildren(); parent.dataset.nodeId = item.id; delete parent.dataset.relationId; delete parent.dataset.recordKey;
  $('#detail-kind').textContent = t('map.detail');
  parent.append(node('div', 'detail-id', item.id), titleNode('h2', 'detail-title', item, 150));
  const meta = node('div', 'detail-meta'); meta.append(mapRecordTag(item), item.author ? src('span', '', item.author) : node('span', '', t('detail.noAuthor')), node('span', '', t('detail.depth', { n: item.depth }))); parent.append(meta);
  parent.append(selectionTraceNote(item));
  if (targetKernelEvidence().some(evidence => evidence.id === item.id)) parent.append(node('p', 'ledger-detail-note', t('map.ledger.selectedCertificate')));
  if (isLedgerOpen(item)) parent.append(node('p', 'ledger-detail-note', t('map.ledger.detail')));
  const actions = node('div', 'detail-actions');
  for (const key of ['path', 'neighbors', 'upstream', 'downstream', 'all']) { const button = node('button', key === state.focus ? 'active' : '', t('detail.focus.' + key)); button.dataset.focus = key; button.setAttribute('aria-pressed', String(key === state.focus)); button.onclick = () => detailFocus(key); actions.append(button); }
  const copy = node('button', '', t('detail.copyId')); copy.setAttribute('aria-label', t('detail.copyIdAria')); copy.onclick = () => copyText(item.id); actions.append(copy); parent.append(actions);
  const section = (label, content, collapsible = false) => { if (!content) return; const block = node(collapsible ? 'details' : 'section', 'detail-section'); block.append(node(collapsible ? 'summary' : 'h3', '', label)); const body = node('div'); markdown(body, content); block.append(body); parent.append(block); };
  parent.append(src('p', 'node-status-note', truncate(item.statement || item.exact_statement, 230)));
  const reading = node('div', 'detail-read'); const read = node('button', 'button primary', t('module.read')); read.type = 'button'; read.append(icon('arrow')); read.onclick = () => openReader(item.id, state.graphLayer); reading.append(read); parent.append(reading);
  window.EulerHuman?.mount(parent, item, { layer: state.graphLayer });
  section(t('detail.statement'), item.statement || item.exact_statement, true);
  section(t('detail.proof'), item.proof, true); section(t('detail.intuition'), item.intuition || item.detail, true);
  if (loading) parent.append(node('p', 'node-status-note', t('detail.loadingProof')));
  const relations = (state.graph?.edges || []).filter((edge) => edge.source === item.id || edge.target === item.id);
  if (relations.length) {
    const block = node('details', 'detail-section detail-relations'); block.open = true;
    block.append(node('summary', '', t('map.recordedRelations', { n: relations.length })));
    relations.forEach((edge, index) => {
      let host = block;
      if (index >= 5) { host = block.querySelector('.more-relations'); if (!host) { host = node('details', 'more-relations'); host.append(node('summary', '', t('map.moreRelations', { n: relations.length - 5 }))); block.append(host); } }
      const incoming = edge.target === item.id, other = recordsById().get(incoming ? edge.source : edge.target);
      const row = node('div', 'detail-relation-row');
      const caption = node('button', 'detail-relation-caption', (incoming ? '← ' : '→ ') + relationName(edge.type)); caption.type = 'button'; caption.dataset.relation = edgeKey(edge); caption.onclick = () => selectRelation(edgeKey(edge), { reveal: true }); row.append(caption);
      if (other) { const link = node('button', 'detail-relation-node'); link.append(titleNode('span', '', other, 100)); link.onclick = () => selectNode(other.id, { reveal: true }); row.append(link); }
      if (edge.validity && edge.validity !== 'VALID') row.append(badge(edge.validity, t('reader.relationInvalid')));
      host.append(row);
    });
    parent.append(block);
  }
  const predecessors = item.predecessors || [];
  if (predecessors.length) { const block = node('section', 'detail-section'); block.append(node('h3', '', t(state.graphLayer === 'facts' ? 'detail.directPremises' : 'detail.upstreamRelations', { n: predecessors.length }))); for (const predecessor of predecessors) { const record = recordsById().get(predecessor); const button = node('button', 'predecessor'); button.append(record ? titleNode('span', '', record, 100) : src('span', '', predecessor)); button.title = predecessor; button.onclick = () => { if (record) selectNode(predecessor, { reveal: true }); else toast(t('detail.missingPredecessor')); }; block.append(button); } parent.append(block); }
  if (item.created_at) parent.append(node('p', 'node-status-note', t('detail.recordedAt', { time: fullTime(item.created_at) })));
  parent.append(node('p', 'node-status-note', t(state.graphLayer === 'facts' ? 'detail.noteFacts' : 'detail.noteResearch')));
}
function renderRelation(edge) {
  const parent = $('#detail-body'); parent.replaceChildren(); delete parent.dataset.nodeId; delete parent.dataset.recordKey; parent.dataset.relationId = edgeKey(edge);
  $('#detail-kind').textContent = t('map.rel.title');
  const records = recordsById(); const source = records.get(edge.source), target = records.get(edge.target); const key = edgeKey(edge); const focus = currentFocus();
  const head = node('div', 'relation-head'); head.append(node('h2', 'relation-type', relationName(edge.type)), node('code', 'relation-code', edge.type));
  if ((edge.validity || 'VALID') !== 'VALID') head.append(badge(edge.validity, t('map.rel.invalid')));
  parent.append(head);
  if (focus?._main.has(key)) parent.append(node('p', 'relation-flag main', t('map.rel.onMain')));
  else if (focus?._path.has(key)) parent.append(node('p', 'relation-flag path', t('map.rel.onPath')));
  const end = (role, item, id) => {
    const box = node('div', 'relation-end'); const label = `map.rel.${role}.${edge.type}`; box.append(node('div', 'relation-end-label', t(i18nHas(label) ? label : 'map.rel.' + role)));
    const button = node('button', 'relation-end-node'); button.type = 'button'; button.dataset.end = role;
    if (item) { button.append(mapRecordTag(item), titleNode('span', '', item, 180)); button.onclick = () => selectNode(item.id, { reveal: true }); } else { button.append(src('span', '', id)); button.disabled = true; }
    box.append(button); return box;
  };
  parent.append(end('from', source, edge.source), node('div', 'relation-arrow', '↓'), end('to', target, edge.target), node('p', 'relation-note', t('map.rel.direction')));
  if (edge.recorded_source && edge.recorded_target) {
    const line = node('p', 'relation-note'); const a = records.get(edge.recorded_source), b = records.get(edge.recorded_target);
    line.append(node('span', '', t('map.rel.recorded')), a ? titleNode('span', '', a, 50) : src('span', '', edge.recorded_source), document.createTextNode(' '), node('code', '', edge.type), document.createTextNode(' '), b ? titleNode('span', '', b, 50) : src('span', '', edge.recorded_target));
    parent.append(line);
  }
  const why = node('section', 'detail-section'); why.append(node('h3', '', t('map.rel.rationale')));
  if (edge.exact_claim) { const body = node('div'); markdown(body, edge.exact_claim); why.append(body); } else why.append(node('p', 'relation-note', t('map.rel.noRationale')));
  parent.append(why);
  const actions = node('div', 'relation-actions');
  const readTarget = node('button', 'button primary', t('map.rel.readTarget')); readTarget.id = 'relation-read-target'; readTarget.type = 'button'; readTarget.onclick = () => openReader(edge.target, state.graphLayer);
  const readSource = node('button', 'button secondary', t('map.rel.readSource')); readSource.id = 'relation-read-source'; readSource.type = 'button'; readSource.onclick = () => openReader(edge.source, state.graphLayer);
  actions.append(readTarget, readSource); parent.append(actions);
  const siblings = (state.graph.edges || []).filter((other) => other !== edge && ((other.source === edge.source && other.target === edge.target) || (other.source === edge.target && other.target === edge.source)));
  if (siblings.length) {
    const block = node('section', 'detail-section relation-siblings'); block.append(node('h3', '', t('map.rel.siblings', { n: siblings.length })));
    siblings.forEach((other) => { const button = node('button', 'detail-relation-node'); button.type = 'button'; button.dataset.relation = edgeKey(other); button.append(relationTypeLine(other)); if (other.exact_claim) button.append(src('span', 'relation-item-ends', truncate(other.exact_claim, 90))); button.onclick = () => selectRelation(edgeKey(other)); block.append(button); });
    parent.append(block);
  }
  const identity = node('p', 'relation-note');
  if (edge.relation_id) identity.append(document.createTextNode(t('map.rel.identity')), node('code', '', edge.relation_id)); else identity.textContent = t('map.rel.identityContent');
  parent.append(identity);
}
function renderRecord(record) {
  const parent = $('#detail-body'); parent.replaceChildren(); delete parent.dataset.nodeId; delete parent.dataset.relationId; parent.dataset.recordKey = recordKey(record);
  $('#detail-kind').textContent = t('map.rec.title');
  const head = node('div', 'record-detail-head'); head.append(node('span', 'front-kind', signalLabel(record.kind)), node('span', 'unverified-badge', t('map.front.unverified')));
  if (record.result) head.append(node('span', 'badge neutral', t('attempt.' + record.result)));
  parent.append(head);
  const claim = node('div', 'detail-title'); markdown(claim, record.claim); parent.append(claim);
  const meta = node('div', 'detail-meta'); meta.append(record.author ? src('span', '', record.author) : node('span', '', t('detail.noAuthor')), node('span', '', fullTime(record.timestamp)));
  if (record.reported_status) { const reported = node('span'); reported.append(document.createTextNode(t('map.front.reported')), src('span', '', record.reported_status)); meta.append(reported); }
  parent.append(meta, node('p', 'node-status-note', t('map.rec.note')));
  if (record.reason) parent.append(disclosure(t('explore.reason'), record.reason, 'record-reason|' + recordKey(record), 'detail-section proof-source'));
  if (record.detail) parent.append(disclosure(t('explore.detail'), record.detail, 'record-detail|' + recordKey(record), 'detail-section proof-source'));
  const links = node('section', 'detail-section'); links.append(node('h3', '', t('map.rec.links')));
  if (!record.links.length) links.append(node('p', 'relation-note', t('map.rec.noLinks')));
  record.links.forEach((link) => {
    const button = node('button', 'predecessor'); button.type = 'button'; button.dataset.nodeId = link.id;
    if (link.layer === state.graphLayer) { const item = recordsById().get(link.id); button.append(item ? titleNode('span', '', item, 100) : src('span', '', link.id)); button.onclick = () => selectNode(link.id, { reveal: true }); }
    else { button.append(src('span', '', link.id.slice(0, 16)), node('span', 'muted', t('map.front.otherLayer', { layer: layerName(link.layer) }))); button.onclick = () => openMap(link.id, link.layer, 'neighbors'); }
    links.append(button);
  });
  parent.append(links);
  const more = node('button', 'text-button', t('map.rec.explore')); more.type = 'button'; more.onclick = () => switchView('explore'); parent.append(more);
}
function closeDetail(focusButton = true) {
  state.selected = null; state.relation = null; state.mapRecord = null; state.focus = 'all'; state.detailRequest += 1; $('#graph-detail').hidden = true;
  closeChooser(false); applyFilters(false); selectionURL(); if (focusButton) $('#list-toggle').focus();
}
function setMapView(view) {
  state.mapView = view; resetFilters(); setSidePane('focus'); applyFilters(true); selectionURL();
}
$$('[data-map-view]').forEach((button) => button.addEventListener('click', () => setMapView(button.dataset.mapView)));
$('#show-evidence').onchange = (event) => { state.showEvidence = event.target.checked; applyFilters(false); };
$('#show-alternatives').onchange = (event) => { state.showAlternatives = event.target.checked; applyFilters(true); };
$('#close-detail').onclick = () => closeDetail();
$('#list-toggle').onclick = () => { const open = $('#map-side').hidden; $('#map-side').hidden = !open; $('#list-toggle').setAttribute('aria-pressed', String(open)); };
$$('[data-layer]').forEach((button) => button.onclick = () => {
  if (!setLayer(button.dataset.layer)) return;
  state.selected = null; state.relation = null; state.mapRecord = null; state.mapView = null; state.mapGuide = null; state.pendingNode = null; state.cite = null; state.needsFit = true;
  $('#graph-detail').hidden = true; resetFilters();
  if (state.view === 'reader') { $('#proof-reading').replaceChildren(node('div', 'empty-inline', t('reader.loadingLayer', { layer: t(state.graphLayer === 'facts' ? 'layer.factsLower' : 'layer.researchLower') }))); $('#proof-outline-items').replaceChildren(); }
  else graphEmpty(t('map.loadingLayer', { layer: t(state.graphLayer === 'facts' ? 'layer.factDeps' : 'layer.researchLower') }), t('map.loadingLayerDesc'));
  loadGraph(true); selectionURL();
});
let searchTimer;
const filterChanged = () => { state.nodeLimit = 100; if (filtersActive()) state.mapView = 'all'; applyFilters(true); };
$('#graph-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(filterChanged, 120); });
['#graph-team', '#graph-author', '#graph-status', '#graph-depth'].forEach((id) => $(id).onchange = filterChanged);
function resetFilters() { $('#graph-search').value = ''; ['#graph-team', '#graph-author', '#graph-status', '#graph-depth'].forEach((id) => { $(id).value = ''; }); state.focus = 'all'; }
$('#clear-filters').onclick = () => { resetFilters(); applyFilters(true); };
$('#graph-layout').onchange = () => applyFilters(true); $('#graph-fit').onclick = () => fitMap(true);
function zoom(factor) { if (!state.cy) return; state.cy.zoom({ level: Math.max(state.cy.minZoom(), Math.min(state.cy.maxZoom(), state.cy.zoom() * factor)), renderedPosition: { x: state.cy.width() / 2, y: state.cy.height() / 2 } }); }
$('#zoom-in').onclick = () => zoom(1.35); $('#zoom-out').onclick = () => zoom(1 / 1.35);
$('#graph-export').onclick = () => {
  if (!state.cy) return;
  const link = node('a'); link.download = 'euler-' + state.graphLayer + '-' + new Date().toISOString().slice(0, 10) + '.png'; link.href = state.cy.png({ bg: '#fbfaf7', full: true, maxWidth: 3000, maxHeight: 2400, scale: 2 }); link.click(); toast(t('map.exported'));
};
$('#graph-help').onclick = () => { const open = $('#graph-help-text').hidden; $('#graph-help-text').hidden = !open; $('#graph-help').setAttribute('aria-expanded', String(open)); };
// Resizing keeps the reader's view centred; only a pending first view is fitted.
if (window.ResizeObserver) {
  let previous = null; let resizeFrame;
  new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect; if (width <= 0 || height <= 0) return;
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      const cy = state.cy; if (!cy) { previous = { width, height }; return; }
      cy.resize();
      if (state.needsFit || !previous) { state.needsFit = false; fitMap(); }
      else if (previous.width !== width || previous.height !== height) cy.panBy({ x: (width - previous.width) / 2, y: (height - previous.height) / 2 });
      previous = { width, height };
    });
  }).observe($('#graph-canvas'));
}

// Source-grounded proof reader: one conclusion, its premises and recorded justification.
function readerMainTarget() {
  const id = state.graph?.target_id || (state.mapFocus?.layer === state.graphLayer ? state.mapFocus.anchor : null);
  const current = id && state.graph?.nodes.find(n => n.id === id);
  if (current) return { ...current, layer: state.graphLayer };
  // A checked formal target need not itself be an admitted Fact.
  return state.architecture?.target ? { ...state.architecture.target, layer: state.architecture.target.layer || 'research' } : null;
}
function guideRootId() {
  const declared = state.graph?.target_id || (state.mapFocus?.layer === state.graphLayer ? state.mapFocus.anchor : null);
  if (declared && state.graph?.nodes.some(n => n.id === declared)) return declared;
  return state.graph?.nodes.filter((n) => !['missing', 'revoked'].includes(lower(n.status))).sort((a, b) => (b.depth || 0) - (a.depth || 0))[0]?.id;
}
$('#guide-root').onclick = () => { const target = readerMainTarget(); if (!target) return; state.guideTrail = []; if (target.layer !== state.graphLayer) openReader(target.id, target.layer); else guideSelect(target.id); };
$('#reader-to-map').onclick = () => openMap(state.selected, state.graphLayer, 'neighbors');
$('#outline-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderGuideOutline, 120); });
function enterReader() {
  $('#list-toggle').hidden = true;
  if (state.graph && state.graph._layer === state.graphLayer) { renderGuideOutline(); syncGuide(); }
  else { $('#proof-reading').replaceChildren(node('div', 'empty-inline', t('reader.loadingRecords'))); loadGraph(true); }
}
function renderGuideOutline() {
  const box = $('#proof-outline-items'); const scroll = box.scrollTop; box.replaceChildren(); if (!state.graph) return;
  const query = lower($('#outline-search').value.trim()); const nodes = state.graph.nodes || []; const byId = new Map(nodes.map((item) => [item.id, item]));
  const matches = (item) => !query || lower([item.id, item.display_title, graphNodeLabel(item), item.label, item.statement].join(' ')).includes(query);
  const pool = nodes.filter((item) => (state.graphLayer !== 'research' || item.kind !== 'evidence' || item.id === state.selected) && matches(item));
  const mainTarget = readerMainTarget();
  $('#guide-root').disabled = !mainTarget;
  const path = (state.graph.reading_order || []).filter((id, index, list) => id && id !== mainTarget?.id && byId.has(id) && list.indexOf(id) === index && matches(byId.get(id))).map((id) => byId.get(id));
  const used = new Set(path.map((item) => item.id));
  if (mainTarget) used.add(mainTarget.id);
  const rest = pool.filter((item) => !used.has(item.id)).sort((a, b) => (a.depth || 0) - (b.depth || 0) || graphNodeLabel(a).localeCompare(graphNodeLabel(b)));
  const groups = [
    ['target', t('outline.target'), mainTarget ? [mainTarget] : []],
    ['path', t('outline.path'), path],
    ['open', t('outline.open'), rest.filter((item) => item.kind !== 'route' && ['open', 'blocked', 'attempted'].includes(stateOf(item)))],
    ['routes', t('outline.routes'), rest.filter((item) => item.kind === 'route')],
    ['done', t(state.graphLayer === 'facts' ? 'outline.doneFacts' : 'outline.done'), rest.filter((item) => item.kind !== 'route' && stateOf(item) === 'established')],
    ['other', t('outline.other'), rest.filter((item) => item.kind !== 'route' && !['open', 'blocked', 'attempted', 'established'].includes(stateOf(item)))],
  ];
  let count = 0;
  for (const [key, label, items] of groups) {
    if (!items.length) continue; count += items.length;
    const group = node('details', 'outline-group'); group.dataset.group = key;
    group.open = key === 'target' || !!query || state.outlineOpen.has(key) || items.some((item) => item.id === state.selected);
    group.addEventListener('toggle', () => { if (group.open) state.outlineOpen.add(key); else state.outlineOpen.delete(key); });
    group.append(node('summary', '', label + ' · ' + number(items.length)));
    for (const item of items.slice(0, key === 'path' ? 20 : state.outlineLimit)) {
      const button = node('button', 'proof-outline-item' + (item.id === state.selected ? ' current' : '')); button.dataset.nodeId = item.id;
      button.setAttribute('aria-current', item.id === state.selected ? 'step' : 'false');
      const title = titleOf(item); const label2 = node('strong', '', truncate(title.text, 90)); if (title.original) markOriginal(label2);
      button.append(node('span', 'state-dot tone-' + tone(stateOf(item))), label2);
      button.title = title.text + ' · ' + stateLabel(stateOf(item), item.kind);
      if (key === 'target') button.append(node('small', 'outline-target-note', t(item.layer !== state.graphLayer ? 'outline.targetOtherLayer' : 'outline.targetAcceptance')));
      button.onclick = () => item.layer && item.layer !== state.graphLayer ? openReader(item.id, item.layer) : guideSelect(item.id);
      group.append(button);
    }
    if (items.length > state.outlineLimit && key !== 'path') { const more = node('button', 'node-more', t('outline.more')); more.onclick = () => { state.outlineLimit += 40; renderGuideOutline(); }; group.append(more); }
    box.append(group);
  }
  $('#proof-outline-count').textContent = number(count);
  if (!count) box.append(node('p', 'empty-inline', t(query ? 'outline.noMatch' : 'outline.empty')));
  box.scrollTop = scroll;
}
async function syncGuide() {
  if (!state.graph) return;
  const exists = (id) => id && state.graph.nodes.some((n) => n.id === id);
  const linkId = new URLSearchParams(location.search).get('node');
  const pending = state.pendingNode; state.pendingNode = null;
  if (pending && !exists(pending)) { $('#proof-reading').replaceChildren(node('p', 'proof-gap', t('reader.notInLayer'))); delete $('#proof-reading').dataset.nodeId; return; }
  if (!pending && !state.selected && linkId && !exists(linkId)) { $('#proof-reading').replaceChildren(node('p', 'proof-gap', t('reader.linkMissing'))); delete $('#proof-reading').dataset.nodeId; return; }
  const id = [pending, state.selected, linkId, guideRootId()].find(exists);
  if (!id) { $('#proof-reading').replaceChildren(node('div', 'empty-inline', t('reader.layerEmpty'))); return; }
  await guideSelect(id, false);
}
async function guideSelect(id, remember = true) {
  if (!id || !state.graph?.nodes.some((n) => n.id === id)) return;
  const old = state.selected;
  if (remember && old && old !== id) { state.guideTrail.push(old); if (state.guideTrail.length > 20) state.guideTrail.shift(); }
  if (old !== id) {
    const query = new URLSearchParams(location.search); const part = Number(query.get('part')); const linked = !remember && !old && query.get('node') === id;
    state.proofStep = linked && Number.isInteger(part) && part >= 0 && part < 1000 ? part : 0;
    const cite = linked ? /^(\d+)-(\d+)$/.exec(query.get('cite') || '') : null;
    state.cite = cite ? { start: Number(cite[1]), end: Number(cite[2]) } : null; state.fullText = false;
  }
  if (state.pendingCite && !remember) { state.cite = { start: state.pendingCite.start, end: state.pendingCite.end }; state.proofStep = state.pendingCite.section; }
  state.pendingCite = null;
  state.selected = id; selectionURL(); renderGuideOutline();
  const request = ++state.guideRequest; const generation = state.generation; const layer = state.graphLayer;
  if (!state.guideData || state.guideData.node.id !== id) $('#proof-reading').replaceChildren(node('div', 'empty-inline', t('reader.loadingStep')));
  try {
    const data = await api('/api/derivation?layer=' + encodeURIComponent(layer) + '&node_id=' + encodeURIComponent(id));
    if (request !== state.guideRequest || generation !== state.generation || layer !== state.graphLayer) return;
    state.guideData = data; renderGuide(data, { focusCite: !!state.cite });
  } catch (error) {
    if (request === state.guideRequest && generation === state.generation) $('#proof-reading').replaceChildren(node('p', 'empty-inline', t('reader.stepFailed', { error: error.message })));
  }
}
function cardState(item) { const record = state.graph?.nodes.find((n) => n.id === item.id); return record ? stateOf(record) : nodeState(item); }
function relationLabel(item) { return item.relation && i18nHas('relation.' + item.relation) ? t('relation.' + item.relation) : ''; }
function proofNodeButton(item, suffix = '') {
  const button = node('button', 'proof-node-link'); button.dataset.nodeId = item.id;
  button.append(stateTag(cardState(item), item.kind), titleNode('strong', '', item, 150));
  if (suffix) button.append(node('span', 'proof-link-note', suffix));
  button.onclick = () => guideSelect(item.id); return button;
}
function guideGroup(parent, key, title, items, empty, { open = true, sort = false, kindNote = false } = {}) {
  const section = node('section', 'proof-section'); section.dataset.section = key; section.dataset.scrollKey = 'section-' + key;
  const unresolved = items.filter((item) => ['open', 'blocked', 'attempted'].includes(cardState(item))).length;
  const heading = node('h3'); heading.append(document.createTextNode(title)); if (items.length) heading.append(node('span', 'count-pill', unresolved ? t('reader.countOpen', { n: items.length, open: unresolved }) : number(items.length)));
  section.append(heading);
  if (!items.length) { section.append(node('p', 'muted', empty)); parent.append(section); return; }
  const ordered = sort ? [...items].sort((a, b) => STATE_ORDER.indexOf(cardState(a)) - STATE_ORDER.indexOf(cardState(b))) : items;
  if (sort && unresolved) section.append(node('p', 'muted small', t('reader.sortedNote')));
  const host = open ? section : (() => { const wrap = node('details', 'proof-more'); wrap.dataset.sourceKey = 'section|' + key; wrap.append(node('summary', '', t('reader.expandN', { n: items.length }))); section.append(wrap); return wrap; })();
  ordered.forEach((item, i) => {
    const content = i >= 6 && open ? (section.querySelector('details.proof-more') || (() => { const extra = node('details', 'proof-more'); extra.dataset.sourceKey = 'more|' + key; extra.append(node('summary', '', t('reader.expandRest', { n: items.length - 6 }))); section.append(extra); return extra; })()) : host;
    const card = node('div', 'proof-premise'); card.append(proofNodeButton(item, kindNote && item.evidence_kind ? evidenceKind(item.evidence_kind) : relationLabel(item)));
    if (item.rationale) card.append(disclosure(t('reader.rationale'), item.rationale, 'rationale|' + key + '|' + item.id));
    else if (item.relation) card.append(node('p', 'proof-link-note', t('reader.noRationale')));
    if (item.relation_validity && item.relation_validity !== 'VALID') card.append(badge(item.relation_validity, t('reader.relationInvalid')));
    content.append(card);
  });
  parent.append(section);
}
const sectionTitle = (section) => section.heading != null ? { text: section.heading, original: true } : { text: t('source.paragraph', { n: section.ordinal || section.index + 1 }), original: false };
function scrollToSection(key) { const target = $('#proof-reading [data-section="' + key + '"]'); if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
function jumpToCite(stepNumber, cite) {
  state.proofStep = cite.section; state.cite = { start: cite.start, end: cite.end, step: stepNumber }; state.fullText = false;
  renderGuide(state.guideData, { focusCite: true }); saveUi();
}
function renderProcess(data) {
  const block = node('div', 'process'); block.dataset.scrollKey = 'process';
  const process = data.process;
  if (process?.status === 'editorial') {
    block.classList.add('editorial');
    const head = node('div', 'process-head'); head.append(node('h4', '', t('process.title')), node('span', 'editorial-tag', t('process.editorialTag'))); block.append(head);
    if (process.summary) { const summary = node('p', 'process-summary', process.summary[state.lang] || process.summary.zh); summary.dataset.editorial = ''; block.append(summary); }
    const steps = node('ol', 'process-steps');
    process.steps.forEach((step, index) => {
      const active = state.cite && step.cites.some((cite) => cite.start === state.cite.start && cite.end === state.cite.end);
      if (active && !state.cite.step) state.cite.step = index + 1;
      const item = node('li', 'process-step' + (active ? ' active' : '')); item.dataset.step = String(index + 1); item.dataset.scrollKey = 'step-' + (index + 1);
      item.append(node('span', 'step-index', String(index + 1)));
      const body = node('div', 'step-body'); const textNode = node('p', 'step-text', step.text[state.lang] || step.text.zh); textNode.dataset.editorial = ''; body.append(textNode);
      const cites = node('div', 'step-cites');
      step.cites.forEach((cite) => {
        const section = data.sections[cite.section]; if (!section) return;
        const button = node('button', 'cite-link'); button.type = 'button'; button.dataset.cite = cite.start + '-' + cite.end; button.dataset.section = String(cite.section);
        button.append(document.createTextNode(t('process.cite', { n: cite.section + 1 })));
        // Only a real source heading is shown; untitled parts are identified by their number alone.
        if (section.heading != null) { button.append(document.createTextNode(' · ')); button.append(src('span', 'cite-heading', truncate(section.heading, 60))); }
        if (!cite.whole) button.append(node('span', 'cite-excerpt', ' · ' + t('process.excerpt')));
        button.title = t('process.citeTitle', { start: cite.start, end: cite.end, quote: cite.quote.replace(/\s+/g, ' ').slice(0, 60) });
        button.onclick = () => jumpToCite(index + 1, cite); cites.append(button);
      });
      body.append(cites);
      const refs = (step.refs || []).filter((ref) => state.graph?.nodes.some((candidate) => candidate.id === ref));
      if (refs.length) { const line = node('p', 'step-refs', t('process.refs')); refs.forEach((ref) => { const link = node('button', 'text-button ref-link'); link.append(titleNode('span', '', state.graph.nodes.find((candidate) => candidate.id === ref), 48)); link.onclick = () => guideSelect(ref); line.append(link); }); body.append(line); }
      item.append(body); steps.append(item);
    });
    block.append(steps, node('p', 'process-disclaimer', t('process.disclaimer', { sha: text(process.source_sha256).slice(0, 12) })));
    return block;
  }
  if (process?.status === 'withdrawn') block.append(node('p', 'proof-gap process-withdrawn', t(process.reason === 'source_changed' ? 'process.withdrawn' : 'process.withdrawnAnchor')));
  const headings = data.sections.filter((section) => section.heading != null);
  if (headings.length >= 2) {
    block.append(node('h4', '', t('process.headings')), node('p', 'muted small', t('process.headingsNote')));
    const list = node('ol', 'source-outline');
    headings.forEach((section) => {
      const level = (section.text.match(/^\s*(#{1,6})/) || ['', '#'])[1].length;
      const item = node('li', 'level-' + Math.min(level, 4)); const button = node('button', 'cite-link'); button.type = 'button'; button.dataset.section = String(section.index);
      button.append(document.createTextNode('§' + (section.index + 1) + ' '), src('span', 'cite-heading', section.heading));
      button.onclick = () => { state.proofStep = section.index; state.cite = null; state.fullText = false; renderGuide(state.guideData, { focusSource: true }); };
      item.append(button); list.append(item);
    });
    block.append(list);
  } else block.append(node('p', 'muted', t('process.paragraphs', { n: data.sections.length })));
  return block;
}
function renderAbsent(data) {
  const block = node('div', 'process absent'); block.dataset.scrollKey = 'process';
  block.append(node('h4', '', t('process.absentTitle')), node('p', '', t('process.absentText')));
  const list = node('ul', 'absent-list');
  const routes = data.routes.length; const premises = data.premises.length + data.routes.reduce((sum, route) => sum + route.premises.length, 0);
  for (const [key, count, target] of [['process.absentPremises', premises, data.premises.length ? 'premises' : 'argument'], ['process.absentRoutes', routes, 'argument'], ['process.absentEvidence', data.evidence.length, 'evidence'], ['process.absentNext', data.next.length, 'next']]) {
    const item = node('li'); const button = node('button', 'text-button', t(key, { n: count })); button.disabled = !count; button.onclick = () => scrollToSection(target); item.append(button); list.append(item);
  }
  block.append(list, node('p', 'muted small', t('process.absentVerify')));
  return block;
}
function sectionBody(section, cite) {
  const container = node('div', 'proof-step-text'); container.dataset.section = String(section.index);
  const inside = cite && cite.start >= section.start && cite.end <= section.end && cite.start < cite.end;
  if (!inside) { const body = node('div'); markdown(body, section.text); container.append(body); return container; }
  // Backend offsets count Unicode code points; JavaScript string indices count UTF-16 units.
  const points = Array.from(section.text);
  const before = points.slice(0, cite.start - section.start).join(''); const cited = points.slice(cite.start - section.start, cite.end - section.start).join(''); const after = points.slice(cite.end - section.start).join('');
  if (before.trim()) { const part = node('div'); markdown(part, before); container.append(part); }
  const wrap = node('div', 'cited-source'); wrap.dataset.start = String(cite.start); wrap.dataset.end = String(cite.end);
  if (cite.step) wrap.append(node('div', 'cited-label', t('process.citedLabel', { n: cite.step })));
  const body = node('div'); markdown(body, cited); wrap.append(body); container.append(wrap);
  if (after.trim()) { const part = node('div'); markdown(part, after); container.append(part); }
  return container;
}
function renderSource(data) {
  const wrap = node('div', 'source-reader'); wrap.dataset.scrollKey = 'source';
  const head = node('div', 'source-head'); head.append(node('h4', '', t('source.title')));
  const toggle = node('button', 'text-button source-toggle', t(state.fullText ? 'source.paged' : 'source.full')); toggle.onclick = () => { state.fullText = !state.fullText; renderGuide(data, { keepScroll: true }); saveUi(); }; head.append(toggle);
  wrap.append(head);
  if (data.proof_source_fact_id && data.proof_source_fact_id !== data.node.id) { const original = node('button', 'text-button', t('source.from')); original.onclick = () => openReader(data.proof_source_fact_id, 'facts'); wrap.append(original); }
  if (state.fullText) {
    data.sections.forEach((section) => { const block = node('div', 'source-section'); block.id = 'source-section-' + section.index; const label = sectionTitle(section); const caption = node('div', 'source-section-label', '§' + (section.index + 1) + (label.original ? '' : ' · ' + label.text)); block.append(caption, sectionBody(section, state.cite)); wrap.append(block); });
    return wrap;
  }
  state.proofStep = Math.min(state.proofStep, data.sections.length - 1);
  const current = data.sections[state.proofStep]; const controls = node('div', 'proof-step-controls');
  const select = node('select'); select.setAttribute('aria-label', t('source.selectAria'));
  data.sections.forEach((step, i) => { const label = sectionTitle(step); const option = new Option((i + 1) + '. ' + label.text, String(i)); if (label.original) markOriginal(option); select.append(option); }); select.value = String(state.proofStep);
  select.onchange = () => { state.proofStep = Number(select.value); state.cite = null; renderGuide(data, { focusSource: true }); };
  const prev = node('button', 'button secondary', t('source.prev')); prev.disabled = state.proofStep === 0; prev.onclick = () => { state.proofStep--; state.cite = null; renderGuide(data, { focusSource: true }); };
  const next = node('button', 'button secondary', t('source.next')); next.disabled = state.proofStep === data.sections.length - 1; next.onclick = () => { state.proofStep++; state.cite = null; renderGuide(data, { focusSource: true }); };
  controls.append(select, prev, next); wrap.append(controls, node('p', 'proof-link-note', t('source.position', { n: state.proofStep + 1, total: data.sections.length })));
  wrap.append(sectionBody(current, state.cite));
  return wrap;
}
function renderGuide(data, { focusCite = false, focusSource = false, keepScroll = false } = {}) {
  // A saved/shared highlight must still belong to the current, source-validated guide.
  if (state.cite) {
    let matched = null;
    if (data.process?.status === 'editorial' && data.process.source_sha256 === data.proof_sha256) {
      data.process.steps.forEach((step, i) => {
        const cite = step.cites.find((item) => item.start === state.cite.start && item.end === state.cite.end);
        if (cite) matched = { start: cite.start, end: cite.end, section: cite.section, step: i + 1 };
      });
    }
    state.cite = matched;
    if (matched) state.proofStep = matched.section;
  }
  const box = $('#proof-reading'); const same = box.dataset.nodeId === data.node.id; const scroll = window.scrollY; const expanded = same ? openKeys(box) : new Set();
  box.replaceChildren(); box.dataset.nodeId = data.node.id;
  const trail = $('#proof-breadcrumb'); trail.replaceChildren();
  const back = node('button', 'text-button', t('reader.back')); back.disabled = !state.guideTrail.length;
  back.onclick = () => { const id = state.guideTrail.pop(); if (id) guideSelect(id, false); };
  const previous = state.guideTrail.length ? state.graph?.nodes.find((n) => n.id === state.guideTrail[state.guideTrail.length - 1]) : null;
  const crumb = node('span', 'muted');
  if (previous) { crumb.append(document.createTextNode(t('reader.prevStep', { title: '' }))); crumb.append(titleNode('span', '', previous, 40)); }
  else crumb.textContent = t(state.graphLayer === 'facts' ? 'reader.factsMode' : 'reader.researchMode');
  trail.append(back, crumb);
  const record = state.graph?.nodes.find((n) => n.id === data.node.id) || data.node; const code = stateOf(record);
  const header = node('header', 'proof-reading-header'); header.dataset.scrollKey = 'reader-header';
  const kicker = t(data.node.kind === 'route' ? 'reader.kind.route' : data.node.kind === 'evidence' ? 'reader.kind.evidence' : data.node.kind === 'fact' ? 'reader.kind.fact' : data.node.id === state.graph?.target_id ? 'reader.kind.target' : 'reader.kind.obligation');
  header.append(node('div', 'section-kicker', kicker), titleNode('h2', '', { ...data.node, curated: !!record.display_title }));
  const meta = node('div', 'proof-reading-meta'); meta.append(stateTag(code, data.node.kind));
  if (data.node.kind === 'evidence') { const key = text(data.node.checker_result).toUpperCase(); const result = node('span', 'badge ' + ({ PASS: '', FAIL: 'amber' }[key] ?? 'neutral'), i18nHas('checker.' + key) ? t('checker.' + key) : t('checker.none')); result.title = t('checker.title', { value: data.node.checker_result || t('time.unrecorded') }); meta.append(result); }
  if (data.node.evidence_kind) meta.append(node('span', 'badge blue', evidenceKind(data.node.evidence_kind)));
  if (record.display_title) meta.append(node('span', 'curated-note', t('reader.curated')));
  const share = node('button', 'text-button', t('reader.copyLink')); share.append(icon('copy')); share.onclick = () => copyText(location.href, t('copy.stepLink')); meta.append(share);
  if (data.projection === 'historical_source') meta.append(node('span', 'curated-note', t('historical.sourceNote')));
  header.append(meta); box.append(header);
  if (data.node.state_basis?.review_note) box.append(disclosure(t('reader.acceptanceBasis'), data.node.state_basis.review_note, 'state-basis|' + data.node.id));
  const claim = node('div', 'proof-claim'); claim.dataset.scrollKey = 'claim'; claim.append(node('div', 'statement-label', t('reader.statementLabel')));
  const statement = text(data.node.statement || data.node.summary); const summary = node('div', 'proof-claim-text'); markdown(summary, statement); claim.append(summary);
  if (statement.length > 600) {
    const id = data.node.id; const toggle = node('button', 'text-button statement-toggle');
    const apply = () => { const open = state.claimsOpen.has(id); summary.classList.toggle('clamped', !open); toggle.textContent = t(open ? 'reader.collapseStatement' : 'reader.expandStatement'); };
    toggle.onclick = () => { if (state.claimsOpen.has(id)) state.claimsOpen.delete(id); else state.claimsOpen.add(id); apply(); saveUi(); };
    apply(); claim.append(toggle);
  }
  box.append(claim);
  const premiseCount = data.premises.length + data.routes.reduce((sum, route) => sum + route.premises.length, 0);
  box.append(node('p', 'proof-position', [t('reader.positionPremises', { count: premiseCount }), t('reader.positionSupports', { count: data.next.length }), data.sections.length ? t('reader.withProof', { n: data.sections.length }) : t('reader.withoutProof')].join(' · ')));
  window.EulerHuman?.mount(box, data.node, { layer: state.graphLayer });
  guideGroup(box, 'premises', t('reader.premises'), data.premises, t(data.routes.length ? 'reader.premisesRoutes' : 'reader.noPremises'), { sort: true });
  const inference = node('section', 'proof-section inference-section'); inference.dataset.section = 'argument'; inference.dataset.scrollKey = 'section-argument'; inference.append(node('h3', '', t('reader.argument')));
  if (data.intuition) {
    const idea = node('div', 'key-idea'); idea.dataset.scrollKey = 'key-idea'; idea.append(node('div', 'key-idea-label', t('process.keyIdea')));
    const body = node('div', 'key-idea-text'); markdown(body, data.intuition); idea.append(body, node('p', 'key-idea-note', t('process.keyIdeaNote'))); inference.append(idea);
  }
  inference.append(data.sections.length ? renderProcess(data) : renderAbsent(data));
  if (data.routes.length) {
    const routeContainer = data.sections.length ? node('details', 'proof-source') : node('div', 'recorded-routes');
    if (data.sections.length) { routeContainer.dataset.sourceKey = 'routes'; routeContainer.append(node('summary', '', t('reader.routesShow', { n: data.routes.length }))); }
    else routeContainer.append(node('h4', '', t('reader.routesRecorded')));
    routeContainer.append(node('p', 'muted', t('reader.routesNote')));
    data.routes.forEach((route, i) => {
      const card = node('article', 'proof-route'); const heading = node('div', 'proof-route-heading'); heading.append(node('strong', '', t('reader.route', { n: i + 1 })), stateTag(cardState(route), 'route')); card.append(heading);
      card.append(proofNodeButton(route, t('reader.enterRoute')));
      if (route.premises.length) {
        const deps = node('div', 'route-premises'); [...route.premises].sort((a, b) => STATE_ORDER.indexOf(cardState(a)) - STATE_ORDER.indexOf(cardState(b))).forEach((p) => deps.append(proofNodeButton(p))); card.append(deps);
      } else card.append(node('p', 'muted', t('reader.routeNoPremises')));
      if (route.mechanism) card.append(disclosure(t('reader.routeMethod'), route.mechanism, 'mechanism|' + route.id)); routeContainer.append(card);
    });
    inference.append(routeContainer);
  }
  if (data.mechanism) { const method = node('div', 'proof-method-block'); method.append(node('div', 'statement-label', t('reader.methodLabel'))); const textBox = node('div', 'proof-method'); markdown(textBox, data.mechanism); method.append(textBox); inference.append(method); }
  if (data.sections.length) inference.append(renderSource(data));
  box.append(inference);
  guideGroup(box, 'evidence', t('reader.evidence'), data.evidence, t(state.graphLayer === 'facts' ? 'reader.noEvidenceFacts' : 'reader.noEvidence'), { open: data.evidence.length <= 3, kindNote: true });
  guideGroup(box, 'next', t('reader.next'), data.next, t('reader.noNext'));
  if (data.related.length) guideGroup(box, 'related', t('reader.related'), data.related, '', { open: false });
  box.append(node('p', 'proof-source-id', t('source.id', { id: data.node.id })));
  restoreKeys(box, expanded);
  const cited = focusCite && box.querySelector('.cited-source');
  if (cited) requestAnimationFrame(() => cited.scrollIntoView({ block: 'center' }));
  else if (focusSource) requestAnimationFrame(() => box.querySelector('.source-reader')?.scrollIntoView({ block: 'start' }));
  else if (same || keepScroll) window.scrollTo(0, scroll);
  else { const top = $('#reader-view').getBoundingClientRect().top + window.scrollY - 8; if (window.scrollY > top) window.scrollTo(0, top); }
  selectionURL();
  consumeRestore('reader');
}

// Data loading: every response is checked against the project generation that requested it.
function updateLive() { $('#live-toggle').setAttribute('aria-pressed', String(state.live)); $('#live-toggle span:last-child').textContent = t(state.live ? 'sync.live' : 'sync.paused'); }
$('#live-toggle').onclick = () => { state.live = !state.live; updateLive(); if (state.live) refresh(); };
$('#refresh-button').onclick = () => refresh(true);
async function refresh(manual = false) {
  if (state.refreshing) return; const generation = state.generation; state.refreshing = true; $('#refresh-button').classList.add('spinning'); $('#refresh-button').disabled = true;
  try {
    const needGraph = ['reader', 'map'].includes(state.view) || !!state.graph;
    const results = await Promise.allSettled([api('/api/workspace'), api('/api/architecture'), api('/api/exploration'), api('/api/projects'), needGraph ? loadGraph() : Promise.resolve()]);
    if (generation !== state.generation) return;
    const [workspace, architecture, exploration, projects] = results;
    if (projects.status === 'fulfilled') updateProjectOptions(projects.value);
    if (exploration.status === 'fulfilled') { state.exploration = exploration.value; state.explorationError = ''; } else state.explorationError = exploration.reason.message;
    if (architecture.status === 'fulfilled') { state.architecture = architecture.value; state.architectureError = ''; } else state.architectureError = architecture.reason.message;
    if (workspace.status === 'fulfilled') {
      state.workspace = workspace.value; state.lastSync = Date.now();
      $('#connection-banner').hidden = true; document.body.classList.remove('disconnected');
      $('#refresh-note').textContent = t('sync.at', { time: fmtTime(new Date()) });
      $('#refresh-note').title = t('sync.atTitle');
      if (state.graph) graphFilters();
      if (manual) toast(t('toast.refreshed'));
    } else connectionError(workspace.reason);
    renderIdentity(); renderOverview(); renderExploration();
    window.EulerHuman?.update();
    if (state.view === 'reader' && state.graph) renderGuideOutline();
    if (state.view === 'ops') { renderOps(); if (state.recordView === 'memory') await loadChannels(); }
  } finally { if (generation === state.generation) { document.body.classList.toggle('project-loading', !state.workspace); $('#project-loading').hidden = true; state.refreshing = false; $('#refresh-button').classList.remove('spinning'); $('#refresh-button').disabled = false; } }
}
function updateProjectOptions(data, force = false) {
  const signature = JSON.stringify([data.projects, state.lang]);
  if (signature === state.catalogSignature && !force) return;
  state.catalogSignature = signature; state.catalog = data; state.projects = data.projects || [];
  const selector = $('#project-selector'); selector.replaceChildren();
  state.projects.forEach((p) => {
    const label = displayLabel('catalog', p.id, null, p.name); const name = p.available ? label.text : t('nav.unavailable', { name: label.text });
    const option = new Option(name, p.id); option.disabled = !p.available; if (!label.translated) markOriginal(option); selector.append(option);
  });
  if (state.project) selector.value = state.project;
}
async function initializeProjects() {
  try {
    const data = await api('/api/projects'); state.projects = data.projects || [];
    updateProjectOptions(data);
    const requested = state.project;
    state.project = state.projects.some((p) => p.id === requested) ? requested : data.default_project;
    $('#project-selector').value = state.project;
    setPressed('[data-layer]', 'layer', state.graphLayer);
    const snapshot = loadUi();
    const view = viewFromLocation();
    if (view === 'map' && params.get('node')) state.selected = params.get('node');
    if (snapshot && snapshot.project === state.project && snapshot.view === view) {
      state.restore = snapshot; state.module = snapshot.module || null; state.showParked = !!snapshot.showParked; state.fullText = !!snapshot.fullText;
      (snapshot.claims || []).forEach((id) => state.claimsOpen.add(id)); if (snapshot.outline) state.outlineOpen = new Set(snapshot.outline);
    }
    renderIdentity();
    // Keep the deep-linked node/part in the URL until the reader has consumed it.
    switchView(view, { scroll: false, updateHash: false });
    await refresh();
  } catch (error) { connectionError(error); }
}
async function changeProject(id, push = true) {
  if (!state.projects.some((p) => p.id === id && p.available)) return;
  if (document.querySelector('#human-dialog')?.open) document.querySelector('#human-dialog').close();
  if (document.querySelector('#milestone-dialog')?.open) document.querySelector('#milestone-dialog').close();
  state.project = id; state.generation++; state.graphRequest++; state.detailRequest++; state.channelRequest++; state.guideRequest++;
  state.workspace = null; state.architecture = null; state.architectureError = ''; state.exploration = null; state.explorationError = '';
  state.graph = null; state.guideData = null; state.guideTrail = []; state.selected = null; state.pendingNode = null; state.module = null; state.showParked = false; state.cite = null; state.fullText = false; state.restore = null;
  state.records = []; state.channels = []; state.channel = null; state.graphSignature = ''; state.visibleNodes = []; state.overviewSignature = ''; state.exploreSignature = '';
  state.refreshing = false; state.lastSync = null; state.focus = 'all'; state.nodeLimit = 100; state.outlineLimit = 40; state.proofStep = 0;
  state.mapFocus = null; state.mapView = null; state.mapGuide = null; state.relation = null; state.mapRecord = null; state.mapRoutes = null; state.mapMain = null;
  state.visibleEdges = []; state.relationLimit = 200; state.needsFit = true; state.seen = { key: '', keys: new Set() }; state.freshKeys = new Set(); state.pendingCite = null;
  $('#edge-chooser').hidden = true; $('#graph-edge-tooltip').hidden = true;
  state.cy?.destroy(); state.cy = null; state.layout = null;
  $('#project-selector').value = id; $('#graph-detail').hidden = true;
  $('#exploration-live').replaceChildren(node('div', 'empty-inline', t('explore.loading')));
  $('#proof-reading').replaceChildren(node('div', 'empty-inline', t('reader.projectLoading'))); delete $('#proof-reading').dataset.nodeId;
  $('#proof-outline-items').replaceChildren(); $('#proof-breadcrumb').replaceChildren();
  resetFilters(); $('#activity-search').value = ''; $('#outline-search').value = ''; $('#project-loading').hidden = false; document.body.classList.add('project-loading');
  renderIdentity(); renderOverview(true); window.EulerTeam?.projectChanged();
  if (push) { state.graphLayer = 'research'; setPressed('[data-layer]', 'layer', 'research'); selectionURL(true); }
  if (state.view === 'map') graphEmpty(t('map.loadingLayer', { layer: t('layer.researchLower') }), t('map.loadingLayerDesc'));
  await refresh();
}
$('#project-selector').onchange = (event) => changeProject(event.target.value);
window.addEventListener('popstate', async () => {
  const query = new URLSearchParams(location.search); const project = query.get('project');
  const layer = query.get('layer'); const focus = query.get('node'); const lang = query.get('lang') || 'zh';
  if (I18N_LANGS.includes(lang) && lang !== state.lang) setLanguage(lang);
  if (['facts', 'research'].includes(layer) && layer !== state.graphLayer) setLayer(layer);
  setPressed('[data-layer]', 'layer', state.graphLayer);
  if (project && project !== state.project) await changeProject(project, false);
  if (project && state.project !== project) return;
  state.selected = null; state.guideData = null; state.pendingNode = focus || null;
  state.mapView = ['mainline', 'frontier', 'all'].includes(query.get('map')) ? query.get('map') : null; state.relation = query.get('rel') || null; state.mapRecord = null;
  if (viewFromLocation() === 'map') { state.selected = focus || null; state.pendingNode = null; }
  switchView(viewFromLocation(), { updateHash: false });
});
document.addEventListener('keydown', (event) => {
  // Native dialogs own Escape and keyboard input while open. Page shortcuts
  // must not dismiss the selected proof step behind a research dialog.
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape' && !$('#auth-dialog').open && state.view === 'map') { if (!$('#edge-chooser').hidden) closeChooser(true); else if (!$('#graph-detail').hidden) closeDetail(); }
  if (event.key === '/' && state.view !== 'team' && !event.ctrlKey && !event.metaKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) && !$('#auth-dialog').open) {
    event.preventDefault();
    if (state.view === 'reader') $('#outline-search').focus(); else { if (state.view !== 'map') switchView('map'); $('#graph-search').focus(); }
  }
});
applyChrome();
initializeProjects();
setInterval(() => { if (state.live && !document.hidden && !$('#auth-dialog').open) refresh(); }, 15000);
setInterval(() => { if (document.hidden) return; renderQuota(); $$('[data-age]').forEach((item) => { item.textContent = age(item.dataset.age); }); }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.live && (!state.lastSync || Date.now() - state.lastSync > 15000)) refresh(); });
window.addEventListener('resize', () => requestAnimationFrame(drawArchEdges));
