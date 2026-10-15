/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
/* Copyright 2026 Qiuyu Chen (canghaimeng). */
/* Team interaction: project → branch → member conversations on the native team host.
   Each catalog workspace is one branch with its own members and native sessions. Reading
   never starts work; only Send, Recover and the run controls post, always to the branch
   that was selected when the button was pressed. */
(() => {
  const tr = (en, zh) => state.lang === 'zh' ? zh : en;
  const make = (tag, cls, text) => node(tag, cls || '', text == null ? '' : text);
  const button = (text, fn, cls = 'button secondary') => { const b = make('button', cls, text); b.type = 'button'; b.onclick = fn; return b; };
  const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const when = value => { const d = value ? new Date(value) : null; return d && !Number.isNaN(d.getTime()) ? fmtDateTime(d) : ''; };
  const stamp = m => m.completed_at || m.started_at || m.created_at || '';
  const ACTIVE = new Set(['QUEUED', 'RUNNING']);
  const RECOVERED = new Set(['NATIVE_FORK', 'NEW_SESSION', 'NATIVE_RESUME', 'ISOLATED_RESUME', 'CLI_READY', 'FORK_READY']);
  const PAGE = 40, LONG = 1400;
  const turnsText = d => d.policy?.max_turns == null ? tr(`${d.turns_used} turns used`, `已用 ${d.turns_used} 轮`) : tr(`${d.turns_used} / ${d.policy.max_turns} turns used`, `已用 ${d.turns_used} / ${d.policy.max_turns} 轮`);
  const coarse = () => matchMedia('(pointer: coarse)').matches;

  function names(s) {
    return { PAUSED: tr('Paused', '已暂停'), PAUSING: tr('Pausing', '正在暂停'), RESTORED_PAUSED: tr('Sessions awaiting recovery', '会话待恢复'), READY: tr('Team ready', '团队就绪'),
      CONNECTING: tr('Connecting', '正在连接'), NOT_STARTED: tr('Ready for a first task', '等待首个任务'), RUNNING: tr('Working', '工作中'), FORK_PENDING: tr('Native fork pending', '等待原生会话分叉'), MISSING_SESSION: tr('No native session', '没有原生会话'), RECOVERY_FAILED: tr('Recovery failed', '恢复失败'),
      RECOVERY_INCOMPLETE: tr('Recovery incomplete', '恢复不完整'), QUOTA_PAUSED: tr('Paused by quota', '额度暂停'), BUDGET_PAUSED: tr('Turn allowance exhausted', '本次轮次额度已用尽'),
      STOP_REQUESTED: tr('Stopping', '正在终止'), STOPPED: tr('Stopped', '已停止'), UNREGISTERED: tr('No team', '没有团队') }[s] || s;
  }
  function messageState(s) { return { QUEUED: tr('Queued', '排队中'), RUNNING: tr('Working', '进行中'), SUSPENDED: tr('Held', '已挂起'), ANSWERED: tr('Answered', '已回复'), FAILED: tr('Failed', '失败'), CANCELLED: tr('Cancelled', '已取消') }[s] || s; }
  const chatReady = d => d?.ready_for_conversation ?? d?.complete_native_recovery;
  const roleName = r => ({ lead: tr('Lead', '主 Agent'), assistant: tr('Assistant', '助手') }[r] || r || '');
  const providerName = p => ({ codex: 'Codex', grok: 'Grok', claude: 'Claude', 'claude-code': 'Claude', gemini: 'Gemini', cursor: 'Cursor' }[p] || p || '');
  const kindName = k => ({ route: tr('Route', '路线'), obligation: tr('Step', '步骤'), fact: tr('Fact', '事实'), failure: tr('Obstacle', '障碍'), evidence: tr('Evidence', '证据'), problem: tr('Problem', '问题') }[k] || k || tr('Step', '步骤'));
  const initial = m => (String(m?.name || m?.id || '?')[0] || '?').toUpperCase();
  const busy = d => !!(d?.available && (d.members?.some(m => m.status === 'RUNNING') || d.messages?.some(m => ACTIVE.has(m.state))));

  // Drafts, attachments, unsent requests and the chosen member live per branch for this tab.
  const STORE = 'euler-team-workspace', PREFILL = 'euler-team-prefilled';
  let saved = {}; try { saved = JSON.parse(sessionStorage.getItem(STORE) || '{}') || {}; } catch (_) { saved = {}; }
  function slot(p) { const s = saved[p] || (saved[p] = {}); s.drafts ||= {}; s.pending ||= []; s.seen ||= {}; return s; }
  // A send still in flight when the page unloaded is unconfirmed; it is offered for retry, never resent silently.
  Object.values(saved).forEach(s => (s.pending || []).forEach(x => { if (x.status === 'sending') x.status = 'unconfirmed'; }));
  let persistTimer = 0;
  function persist(now = false) {
    clearTimeout(persistTimer);
    const write = () => { try { sessionStorage.setItem(STORE, JSON.stringify(saved)); } catch (_) { /* memory only */ } };
    if (now) write(); else persistTimer = setTimeout(write, 250);
  }
  window.addEventListener('pagehide', () => persist(true));
  const prefilled = () => { try { return JSON.parse(localStorage.getItem(PREFILL) || '[]'); } catch (_) { return []; } };
  const markPrefilled = sig => { try { localStorage.setItem(PREFILL, JSON.stringify([...prefilled().filter(x => x !== sig), sig].slice(-50))); } catch (_) { /* memory only */ } };

  async function call(path, body, project) {
    const u = new URL(path, location.origin); if (project) u.searchParams.set('project', project);
    const controller = new AbortController();
    // Native session recovery may take longer than a normal HTTP read. Polling
    // shows its progress; a client deadline must not misreport it as failed.
    const timer = path === '/api/team/prepare' ? null : setTimeout(() => controller.abort(), 20000);
    let r;
    try {
      r = await fetch(u, { method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal: controller.signal,
        headers: { ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    } catch (e) { const error = Error(e.name === 'AbortError' ? tr('The server did not answer in time.', '服务器未及时响应。') : tr('Cannot reach the workbench server.', '无法连接工作台服务器。')); error.unconfirmed = true; throw error; }
    finally { clearTimeout(timer); }
    let d = null; try { d = await r.json(); } catch (_) { /* non-JSON error page */ }
    if (!r.ok) throw Error(d?.detail ? (typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail)) : tr(`Request failed (HTTP ${r.status})`, `请求失败（HTTP ${r.status}）`));
    return d;
  }

  // One reading per branch. An older response never replaces a newer one, and only the
  // selected branch is ever drawn.
  const cache = new Map(), branchIndex = { items: new Map(), at: 0, loading: false };
  let serial = 0;
  async function update(p = state.project) {
    if (!p) return;
    const n = ++serial; let entry;
    try { entry = { data: await call('/api/team', undefined, p) }; } catch (e) { entry = { error: e.message }; }
    accept(p, n, entry);
  }
  function accept(p, n, entry) {
    const old = cache.get(p);
    if (old && old.serial > n) return;
    cache.set(p, entry.data ? { data: entry.data, serial: n, at: Date.now() } : { ...(old || {}), error: entry.error, serial: n });
    if (entry.data) { reconcilePending(p, entry.data); branchIndex.items.set(p, summary(entry.data)); }
    if (p !== state.project) return;
    document.dispatchEvent(new Event('euler:workbench-status'));
    indicate();
    if (state.view === 'team') render();
  }
  function status() { const c = cache.get(state.project); return !c ? null : c.data ? { project: state.project, data: c.data } : { project: state.project, error: c.error }; }
  function reconcilePending(p, d) {
    const s = saved[p]; if (!s?.pending?.length) return;
    const keys = new Set((d.messages || []).map(m => m.key));
    const left = s.pending.filter(x => !keys.has(x.key));
    if (left.length !== s.pending.length) { s.pending = left; persist(); }
  }
  const summary = d => ({ available: !!d.available, state: d.state, complete: !!chatReady(d), branch: d.branch || null, members: d.members?.length || 0, turns: d.turns_used, max: d.policy?.max_turns });
  async function loadBranches(force = false) {
    if (branchIndex.loading || (!force && Date.now() - branchIndex.at < 30000)) return;
    branchIndex.loading = true;
    const ids = (state.projects || []).filter(x => x.available && x.id !== state.project).map(x => x.id);
    let next = 0;
    const worker = async () => { while (next < ids.length) { const id = ids[next++], n = ++serial; try { accept(id, n, { data: await call('/api/team', undefined, id) }); } catch (_) { branchIndex.items.delete(id); } } };
    try { await Promise.all([worker(), worker(), worker()]); branchIndex.at = Date.now(); } finally { branchIndex.loading = false; }
    if (state.view === 'team') render();
  }
  function siblings(p, d) {
    const me = d?.branch, list = [];
    if (me?.project_id) for (const [id, s] of branchIndex.items) if (s.branch?.project_id === me.project_id) list.push([id, s]);
    if (d && !list.some(([id]) => id === p)) list.push([p, summary(d)]);
    const order = s => s.branch?.kind === 'restored' ? '1' + (s.branch.restored_at || '') : '0';
    return list.sort((a, b) => order(a[1]).localeCompare(order(b[1])));
  }
  const catalogName = id => { const c = (state.projects || []).find(x => x.id === id); return c ? displayLabel('catalog', c.id, null, c.name) : { text: id, translated: false }; };
  const nameNode = (tag, cls, id, fallback) => { const l = catalogName(id); const text = l.text === id && fallback ? fallback : l.text; return l.translated ? make(tag, cls, text) : src(tag, cls, text); };

  // Proof objects for attachment titles and the attachment picker (research layer).
  const graphs = new Map();
  function graphFor(p) {
    if (!graphs.has(p)) graphs.set(p, call('/api/graph?layer=research', undefined, p).catch(e => { graphs.delete(p); throw e; }));
    return graphs.get(p);
  }
  const known = new Map();
  function titleFor(a) {
    if (!a?.id) return '';
    const n = state.graph?.nodes?.find(x => x.id === a.id) || state.architecture?.nodes?.find(x => x.id === a.id) || known.get(state.project + '\u0000' + a.id);
    if (n) return titleOf(n).text;
    if (a.title) return a.title;
    const p = state.project;
    graphFor(p).then(g => { (g.nodes || []).forEach(x => known.set(p + '\u0000' + x.id, x)); if (p === state.project && state.view === 'team') render(); }).catch(() => {});
    return a.id.length > 14 ? a.id.slice(0, 12) + '…' : a.id;
  }

  // Page skeleton: rail (branch and members) + conversation with a bottom composer.
  const layout = document.querySelector('#team-layout');
  const rail = make('aside', 'team-rail'), branchBox = make('section', 'team-branches'), memberBox = make('nav', 'team-members'), controlsBox = make('section', 'team-run');
  rail.append(branchBox, memberBox, controlsBox);
  const chat = make('section', 'team-chat'), head = make('header', 'team-chat-head'), notices = make('div', 'team-notices'), earlier = make('div', 'team-earlier');
  const log = make('ol', 'team-log'), end = make('div', 'team-log-end'), empty = make('section', 'team-empty');
  const composer = make('form', 'team-composer'); composer.noValidate = true;
  const pill = button('', () => scrollToLatest(true), 'team-new-pill'); pill.hidden = true;
  const announcer = make('p', 'sr-only'); announcer.setAttribute('role', 'status');
  const recoverBox = make('div', 'team-recover'), savedBox = make('div', 'team-saved'), attachBox = make('div', 'team-attach'), pickerBox = make('div', 'team-picker');
  const field = make('div', 'team-field'), label = make('label', 'sr-only'), input = make('textarea'), sendButton = make('button', 'button primary team-send');
  const hint = make('p', 'team-hint');
  input.id = 'team-input'; input.rows = 1; input.maxLength = 16000; label.htmlFor = 'team-input'; hint.id = 'team-hint'; hint.setAttribute('role', 'status'); input.setAttribute('aria-describedby', 'team-hint');
  sendButton.type = 'submit'; pickerBox.hidden = true;
  field.append(label, input, sendButton);
  composer.append(pill, recoverBox, savedBox, attachBox, pickerBox, field, hint);
  chat.append(head, notices, earlier, log, end, composer, announcer);
  layout?.append(rail, chat, empty);
  const view = { project: null, member: null, logKey: '', limits: new Map(), expanded: new Set(), urlMember: new URLSearchParams(location.search).get('member'),
    focusComposer: false, focusKey: null, saved: null, flash: null, recovering: new Set(), controlBusy: false, confirmStop: 0, branchesOpen: null };

  let composing = false, composedAt = 0;
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; composedAt = performance.now(); });
  input.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.shiftKey || e.altKey) return;
    // The Enter that confirms an IME candidate must never send.
    if (e.isComposing || composing || e.keyCode === 229 || performance.now() - composedAt < 30) return;
    if (coarse() && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault(); send();
  });
  input.addEventListener('input', () => {
    if (view.project && view.member) { slot(view.project).drafts[view.member] = input.value; persist(); }
    autosize(); view.flash = null; renderSaved(); renderHint();
  });
  composer.addEventListener('submit', e => { e.preventDefault(); send(); });
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight + 2, Math.max(132, innerHeight * 0.38)) + 'px'; }
  window.addEventListener('scroll', () => {
    if (state.view !== 'team' || !isAtLatest()) return;
    pill.hidden = true; markSeen();
  }, { passive: true });

  function isAtLatest() { return end.getBoundingClientRect().top <= composer.getBoundingClientRect().top + 72; }
  function scrollToLatest(smooth = false) {
    const delta = end.getBoundingClientRect().top - (composer.getBoundingClientRect().top - 12);
    if (delta > 0) window.scrollBy({ top: delta, behavior: smooth && !matchMedia('(prefers-reduced-motion: reduce)').matches ? 'smooth' : 'auto' });
    pill.hidden = true; markSeen();
  }

  function render() {
    if (!layout) return;
    const p = state.project; if (!p) return;
    const c = cache.get(p), d = c?.data;
    const mode = !c ? 'loading' : !d ? 'error' : !d.available ? 'empty' : 'chat';
    if (view.project !== p) { view.project = p; view.member = null; view.logKey = ''; view.flash = null; view.confirmStop = 0; log.replaceChildren(); closePicker(); }
    layout.dataset.mode = mode;
    renderBranches(p, c, d);
    if (mode !== 'chat') {
      memberBox.replaceChildren(); delete memberBox.dataset.sig; controlsBox.replaceChildren(); delete controlsBox.dataset.sig;
      chat.hidden = true; empty.hidden = false; renderEmpty(mode, c, d); return;
    }
    chat.hidden = false; empty.hidden = true; delete empty.dataset.sig;
    prefill(p, d); chooseMember(p, d);
    renderMembers(p, d); renderControls(d); renderHead(d); renderNotices(c, d); renderComposer(p, d); renderLog(p, d);
    if (view.focusComposer) { view.focusComposer = false; requestAnimationFrame(() => input.focus({ preventScroll: true })); }
  }

  function chooseMember(p, d) {
    const ids = d.members.map(m => m.id);
    if (ids.includes(view.member)) return;
    const pick = [view.urlMember, slot(p).member, d.lead, ids[0]].find(x => ids.includes(x));
    view.urlMember = null; selectMember(pick, false);
  }
  function selectMember(member, user = true) {
    const p = state.project, s = slot(p);
    view.member = member; s.member = member; state.teamMember = member; persist();
    input.value = s.drafts[member] || ''; autosize();
    view.logKey = ''; view.flash = null; log.replaceChildren(); closePicker();
    if (user) { render(); selectionURL(); }
  }

  function renderBranches(p, c, d) {
    const group = siblings(p, d);
    const sig = JSON.stringify([state.lang, p, group.map(([id, s]) => [id, s.state, s.complete, s.available, s.turns, s.max, s.branch]), d?.state, d?.turns_used, d?.branch, !!c, !!c?.error]);
    if (branchBox.dataset.sig === sig) return;
    const open = branchBox.querySelector('details')?.open;
    branchBox.dataset.sig = sig; branchBox.replaceChildren();
    const b = d?.branch;
    branchBox.append(make('p', 'team-kicker', tr('Project', '项目')), b?.project_name ? src('h2', 'team-project-name', b.project_name) : nameNode('h2', 'team-project-name', p));
    const picker = make('details', 'team-branch-picker');
    picker.open = open ?? view.branchesOpen ?? false;
    picker.addEventListener('toggle', () => { view.branchesOpen = picker.open; });
    const sum = make('summary'); sum.append(make('span', 'team-kicker', tr('Branch', '分支')), nameNode('strong', 'team-branch-current', p, b?.workspace_name), make('span', 'team-branch-count', group.length > 1 ? tr(`${group.length} branches`, `${group.length} 个分支`) : ''));
    const list = make('ul', 'team-branch-list'); list.setAttribute('aria-label', tr('Branches of this project', '此项目的分支'));
    for (const [id, s] of group.length ? group : [[p, null]]) {
      const li = make('li'), item = make('button', 'team-branch'); item.type = 'button'; item.dataset.project = id;
      if (id === p) item.setAttribute('aria-current', 'true');
      const kind = s?.branch?.kind === 'restored' ? tr('Restored branch', '恢复分支') : s?.branch ? tr('Original workspace', '原工作区') : tr('Workspace', '工作区');
      const meta = [kind, s?.branch?.source_commit ? s.branch.source_commit.slice(0, 8) : '', s?.branch?.restored_at ? when(s.branch.restored_at) : ''].filter(Boolean).join(' · ');
      const tone = !s ? '' : !s.available ? 'mute' : !s.complete ? 'warn' : s.state === 'READY' || s.state === 'RUNNING' ? 'ok' : 'mute';
      const chip = make('span', 'team-dot-label', !s ? '' : !s.available ? tr('No team', '没有团队') : !s.complete ? tr('Sessions awaiting recovery', '会话待恢复') : names(s.state)); chip.dataset.tone = tone;
      item.append(make('span', 'team-branch-kind', meta), nameNode('strong', 'team-branch-name', id, s?.branch?.workspace_name), chip);
      item.onclick = () => { if (id !== state.project) changeProject(id); };
      li.append(item); list.append(li);
    }
    picker.append(sum, list); branchBox.append(picker);
    if (branchIndex.loading && group.length < 2) picker.append(make('p', 'team-rail-note', tr('Looking for other branches of this project…', '正在查找此项目的其他分支…')));
    const facts = make('div', 'team-branch-facts');
    if (b?.kind === 'restored') {
      const from = make('p'); from.append(make('span', '', tr('Restored from milestone ', '恢复自里程碑 ')), src('strong', '', b.source_label || (b.source_commit || '').slice(0, 8)), make('span', '', (b.source_commit ? ' · ' + b.source_commit.slice(0, 8) : '') + (b.restored_at ? ' · ' + when(b.restored_at) : '')));
      facts.append(from);
      if (b.parent_workspace_id) { const parent = make('p'); parent.append(make('span', '', tr('Parent: ', '父工作区：')), button('', () => changeProject(b.parent_workspace_id), 'text-button')); parent.lastChild.append(nameNode('span', '', b.parent_workspace_id)); facts.append(parent); }
      else if (!b.lineage_complete) facts.append(make('p', 'muted', tr('Its parent workspace is not in this catalog, so the lineage is not recorded here.', '父工作区不在此目录中，此处无法记录完整谱系。')));
      facts.append(make('p', 'muted', tr('Messages sent here stay in this branch. The parent and other branches are unchanged.', '在此发送的消息只属于这个分支；父工作区和其他分支不受影响。')));
    } else if (d?.available) facts.append(make('p', 'muted', tr('Messages sent here continue this workspace’s own member sessions.', '在此发送的消息会接续本工作区自己的成员会话。')));
    if (d?.available) {
      const line = make('p', 'team-branch-state'); const chip = make('span', 'team-dot-label', chatReady(d) ? names(d.state) : tr('Sessions awaiting recovery', '会话待恢复'));
      chip.dataset.tone = !chatReady(d) ? 'warn' : ['READY', 'RUNNING'].includes(d.state) ? 'ok' : 'mute';
      line.append(chip, make('span', '', turnsText(d)));
      facts.append(line);
    }
    if (facts.childElementCount) branchBox.append(facts);
  }

  function memberInfo(p, d, m) {
    const turns = d.messages.filter(x => x.request?.recipient === m.id);
    const latest = turns.filter(x => !ACTIVE.has(x.state)).map(stamp).sort().at(-1) || '';
    const s = slot(p); if (!(m.id in s.seen)) { s.seen[m.id] = latest; persist(); }
    return { turns: turns.length, active: turns.some(x => ACTIVE.has(x.state)), unseen: view.member !== m.id && latest > (s.seen[m.id] || ''), latest };
  }
  function memberStatus(m, info) {
    if (m.status === 'CONNECTING') return [tr('Connecting', '正在连接'), 'live'];
    if (m.recovery === 'NOT_STARTED') return [tr('Connects on first task', '首个任务时自动连接'), 'mute'];
    if (!RECOVERED.has(m.recovery)) return [tr('Session not recovered', '会话未恢复'), 'warn'];
    if (m.status === 'RUNNING') return [tr('Working', '工作中'), 'live'];
    if (info.active) return [tr('Queued', '排队中'), 'live'];
    if (m.status === 'READY') return [tr('Ready', '就绪'), 'ok'];
    return [names(m.status), 'mute'];
  }
  function renderMembers(p, d) {
    if (memberBox.dataset.lang !== state.lang || !memberBox.firstChild) {
      memberBox.dataset.lang = state.lang; memberBox.setAttribute('aria-label', tr('Members of this branch', '本分支成员'));
      memberBox.replaceChildren(make('p', 'team-kicker', tr('Members', '成员')), make('ul', 'team-member-list'));
    }
    const list = memberBox.querySelector('ul'), existing = new Map([...list.children].map(li => [li.dataset.key, li]));
    let prev = null;
    for (const m of d.members) {
      const info = memberInfo(p, d, m), [label, tone] = memberStatus(m, info);
      const sig = JSON.stringify([state.lang, m.id, m.role, m.provider, m.model, m.effort, label, info.unseen, view.member === m.id]);
      let li = existing.get(m.id);
      if (!li || li.dataset.sig !== sig) {
        const fresh = make('li'), b = make('button', 'team-member'); b.type = 'button'; b.dataset.member = m.id;
        if (view.member === m.id) b.setAttribute('aria-current', 'true');
        const avatar = make('span', 'team-avatar', initial(m)); avatar.dataset.provider = m.provider || ''; avatar.setAttribute('aria-hidden', 'true');
        const body = make('span', 'team-member-body'), name = make('span', 'team-member-name');
        name.append(src('strong', '', m.name || m.id), make('span', 'team-member-role', roleName(m.role)));
        const status = make('span', 'team-dot-label', label); status.dataset.tone = tone;
        body.append(name, make('span', 'team-member-model', [providerName(m.provider), m.model, m.effort].filter(Boolean).join(' · ')), status);
        b.append(avatar, body);
        if (info.unseen) { const dot = make('span', 'team-unseen'); dot.title = tr('New reply', '新回复'); b.append(dot); }
        b.onclick = () => { if (view.member !== m.id) selectMember(m.id); };
        fresh.append(b); fresh.dataset.key = m.id; fresh.dataset.sig = sig;
        if (li) { const focused = li.contains(document.activeElement); li.replaceWith(fresh); if (focused) b.focus({ preventScroll: true }); }
        li = fresh;
      }
      existing.delete(m.id);
      const expected = prev ? prev.nextElementSibling : list.firstElementChild; if (li !== expected) list.insertBefore(li, expected); prev = li;
    }
    existing.forEach(li => li.remove());
    let add = memberBox.querySelector('.team-add-member');
    if (d.can_add_members && window.EulerCreation) {
      if (!add) { add = button('', () => window.EulerCreation.openMember(state.project), 'team-add-member'); add.id='add-member-button'; memberBox.append(add); }
      add.textContent=tr('＋ Add member', '＋ 添加成员');
    } else add?.remove();
  }

  function renderControls(d) {
    const ready = !!(chatReady(d) && d.can_launch);
    const sig = JSON.stringify([state.lang, ready, d.state, view.controlBusy, view.confirmStop > Date.now()]);
    if (controlsBox.dataset.sig === sig) return;
    const open = controlsBox.querySelector('details')?.open, note = controlsBox.querySelector('.team-status')?.textContent || '', tone = controlsBox.querySelector('.team-status')?.dataset.tone || '';
    controlsBox.dataset.sig = sig; controlsBox.replaceChildren(); if (!ready) return;
    const box = make('details', 'team-run-controls'); box.open = !!open;
    box.append(make('summary', '', tr('Run controls', '运行控制')), make('p', 'muted', tr('Pause interrupts the current turn and holds queued messages. Resume continues them. Stop blocks new messages until you resume.', '暂停会中断当前一轮并保留排队消息；继续会接着执行；终止会阻止新消息，直到你再次继续。')));
    const row = make('div', 'team-run-row'), status = make('p', 'team-status', note); status.setAttribute('role', 'status'); status.dataset.tone = tone;
    const confirming = view.confirmStop > Date.now();
    for (const [command, en, zh] of [['pause', 'Pause', '暂停'], ['resume', 'Resume', '继续'], ['stop', confirming ? 'Confirm stop' : 'Stop', confirming ? '确认终止' : '终止']]) {
      const b = button(tr(en, zh), () => control(command, d), command === 'stop' && confirming ? 'button team-danger' : 'button secondary'); b.disabled = view.controlBusy; row.append(b);
    }
    box.append(row, status); controlsBox.append(box);
  }
  async function control(command, d) {
    const p = state.project, status = () => controlsBox.querySelector('.team-status');
    if (command === 'stop' && view.confirmStop <= Date.now()) { view.confirmStop = Date.now() + 6000; renderControls(d); setTimeout(() => { if (state.project === p && cache.get(p)?.data) renderControls(cache.get(p).data); }, 6100); return; }
    view.confirmStop = 0; view.controlBusy = true; renderControls(d);
    const say = (t, tone = '') => { const s = status(); if (s && state.project === p) { s.textContent = t; s.dataset.tone = tone; } };
    say(tr('Sending the command…', '正在发送指令…'));
    try { await call('/api/team/control', { command }, p); say(''); } catch (e) { say(e.message, 'error'); }
    finally { view.controlBusy = false; if (p === state.project) { const fresh = cache.get(p)?.data; if (fresh) renderControls(fresh); } update(p); }
  }

  function renderHead(d) {
    const m = d.members.find(x => x.id === view.member); if (!m) return;
    const sig = JSON.stringify([state.lang, view.project, m, d.lead]);
    if (head.dataset.sig === sig) return;
    const open = head.querySelector('details')?.open; head.dataset.sig = sig; head.replaceChildren();
    const avatar = make('span', 'team-avatar large', initial(m)); avatar.dataset.provider = m.provider || ''; avatar.setAttribute('aria-hidden', 'true');
    const title = make('div', 'team-chat-title'), h = make('h2'); h.append(src('span', '', m.name || m.id), make('span', 'team-member-role', roleName(m.role)));
    const session = m.recovery === 'NOT_STARTED' ? tr('A native session will connect on the first task', '首个任务时将自动连接原生会话') : !RECOVERED.has(m.recovery) ? tr('Native session not recovered in this branch yet', '本分支尚未恢复原生会话')
      : m.recovery === 'ISOLATED_RESUME' ? tr('Native conversation restored in this branch’s independent store', '原生会话已在本分支独立存储中恢复') : m.recovery === 'FORK_READY' ? tr('Saved conversation ready; native fork starts on Send', '历史会话已就绪，发送时创建原生分叉') : m.recovery === 'CLI_READY' ? tr('CLI ready; native conversation starts on Send', 'CLI 已就绪，发送时创建原生会话') : m.recovery === 'NATIVE_FORK' ? tr('Native session forked from the saved checkpoint', '原生会话从保存的检查点分叉') : m.recovery === 'NATIVE_RESUME' ? tr('Native session resumed', '原生会话已接续') : tr('This workspace’s own native session', '本工作区自己的原生会话');
    title.append(h, make('p', 'team-chat-sub', [providerName(m.provider), m.model, m.effort].filter(Boolean).join(' · ') + ' · ' + session));
    head.append(avatar, title);
    const more = make('details', 'team-chat-more'); more.open = !!open;
    more.append(make('summary', '', tr('Assignment and session', '分工与会话')));
    if (m.assignment) { const a = make('div', 'team-assignment'); markdown(a, String(m.assignment)); more.append(make('p', 'team-kicker', tr('Assignment', '分工')), a); }
    const ids = make('dl', 'team-ids');
    for (const [k, v] of [[tr('Session in this branch', '本分支会话'), m.session_id], [tr('Forked from session', '分叉自会话'), m.restored_from], [tr('Recovery', '恢复方式'), m.recovery]]) if (v) ids.append(make('dt', '', k), src('dd', '', v));
    more.append(ids, make('p', 'muted', tr('This page shows the public exchanges recorded for this member in this branch, not its private reasoning or tool transcripts.', '这里显示本分支为该成员记录的公开交流，不包含其私有推理或工具记录。')));
    head.append(more);
  }

  function renderNotices(c, d) {
    const items = [];
    if (d.research_campaign?.status === 'NEEDS_INPUT') {
      const reasons = { unchanged_frontier: tr('Three lead turns left the frontier unchanged. Review the obstacles or try another approach.', '主 Agent 连续三轮未改变研究前沿，请检查障碍或调整路线。'), provider_failure: tr('A provider call failed. Check the failed message before resuming.', '服务商调用失败，请查看失败消息后再继续。'), missing_target: tr('The research target is no longer available.', '当前研究目标已不可用。') };
      items.push(['warn', tr('Research is paused for review: ', '研究已暂停，等待调整：') + (reasons[d.research_campaign.reason_code] || d.research_campaign.reason || '')]);
    }
    if (d.research_campaign?.enabled && d.state === 'RUNNING') items.push(['info', tr('The team is continuing from the research frontier. Proofs close only after verification.', '团队正根据研究前沿继续推进；证明步骤经验证后才会闭合。')]);
    if (c.error) items.push(['error', tr(`Could not refresh: ${c.error}. Showing the reading from ${c.at ? fmtTime(new Date(c.at)) : '—'}.`, `刷新失败：${c.error}。显示 ${c.at ? fmtTime(new Date(c.at)) : '—'} 的读取结果。`), true]);
    if (d.complete_native_recovery) {
      if (d.state === 'PAUSED' || d.state === 'PAUSING') items.push(['warn', tr('The team is paused. Queued messages wait until you resume it under Run controls.', '团队已暂停。排队的消息会等到你在“运行控制”中继续。')]);
      if (d.state === 'QUOTA_PAUSED') items.push(['warn', tr('Paused by the model quota guard. Resume under Run controls once quota has recovered.', '模型额度保护已暂停团队。额度恢复后可在“运行控制”中继续。')]);
    }
    const sig = JSON.stringify([state.lang, items]);
    if (notices.dataset.sig === sig) return;
    notices.dataset.sig = sig; notices.replaceChildren();
    for (const [tone, text, retry] of items) { const n = make('div', 'notice ' + (tone === 'error' ? 'error' : 'warning')); n.append(icon('info'), make('span', '', text)); if (retry) n.append(button(tr('Retry', '重试'), () => update(), 'text-button')); notices.append(n); }
  }

  function sendable(d, member) {
    if (!chatReady(d)) return { ok: false, reason: tr('Recover this branch’s member sessions before sending.', '请先恢复本分支的成员会话，再发送。') };
    if (!d.can_launch) return { ok: false, reason: tr('Sending is not enabled in this deployment; conversations are view-only.', '当前部署未启用发送，对话仅供查看。') };
    if (['STOPPED', 'STOP_REQUESTED'].includes(d.state)) return { ok: false, reason: tr('The team is stopped. Resume it under Run controls to send.', '团队已终止。请在“运行控制”中继续后再发送。') };
    if (d.state === 'QUOTA_PAUSED') return { ok: false, reason: tr('Paused by the model quota guard.', '模型额度保护已暂停团队。') };
    if (d.policy?.max_turns != null && d.turns_used >= d.policy.max_turns) return { ok: false, reason: tr(`This branch has used all ${d.policy.max_turns} turns.`, `本分支已用完全部 ${d.policy.max_turns} 轮。`) };
    if (!d.members.some(m => m.id === member)) return { ok: false, reason: tr('Choose a member first.', '请先选择成员。') };
    const selected = d.members.find(m => m.id === member);
    if (selected?.max_turns != null && (selected.turns_used || 0) >= selected.max_turns) return { ok: false, reason: tr(`${member} has reached its configured turn limit.`, `${member} 已达到你设置的轮次上限。`) };
    return { ok: true };
  }

  function renderComposer(p, d) {
    const member = view.member;
    const display = d.members.find(m => m.id === member)?.name || member;
    label.textContent = tr(`Message to ${display}`, `发给 ${display} 的消息`);
    input.placeholder = tr(`Message ${display} in this branch…`, `在此分支给 ${display} 发消息…`);
    const hasSavedSessions = d.members.some(m => m.restored_from);
    const sig = JSON.stringify([state.lang, p, d.complete_native_recovery, d.ready_for_conversation, d.can_prepare, hasSavedSessions, view.recovering.has(p)]);
    if (recoverBox.dataset.sig !== sig) {
      const status = recoverBox.querySelector('.team-status'), keep = status && [status.textContent, status.dataset.tone];
      recoverBox.dataset.sig = sig; recoverBox.replaceChildren(); recoverBox.hidden = !!chatReady(d);
      if (!chatReady(d)) {
        const mark = make('span', 'team-recover-mark'); mark.append(icon('history'));
        const body = make('div', 'team-recover-body');
        body.append(make('strong', '', hasSavedSessions ? tr('This branch’s member sessions are not recovered yet', '本分支的成员会话尚未恢复') : tr('Connect this branch’s member sessions', '连接本分支的成员会话')),
          make('p', '', hasSavedSessions ? tr('Recovering forks each member’s saved native conversation into this branch so you can continue it here. No model turn runs and nothing is sent.', '恢复会把每位成员保存的原生会话分叉到本分支，之后即可在此继续。恢复不运行模型，也不会发送任何内容。') : tr('Connect the configured members before sending your first question. Connecting sessions starts no research turn.', '先连接已配置的成员，再发送第一个问题。连接会话本身不会启动研究。')));
        const row = make('div', 'team-recover-row'), line = make('p', 'team-status'); line.setAttribute('role', 'status');
        if (keep) { line.textContent = keep[0]; line.dataset.tone = keep[1]; }
        if (d.can_prepare) {
          const go = button(view.recovering.has(p) ? tr('Connecting sessions…', '正在连接会话…') : hasSavedSessions ? tr('Recover this branch’s member sessions', '恢复本分支的成员会话') : tr('Connect member sessions', '连接成员会话'), () => recover(p), 'button primary');
          go.disabled = view.recovering.has(p); go.id = 'team-recover-button'; row.append(go);
        } else line.textContent = tr('Recovery is not enabled in this deployment. On the server, run euler-team prepare for this workspace.', '当前部署未启用恢复。请在服务器上对此工作区运行 euler-team prepare。');
        row.append(line); body.append(row); recoverBox.append(mark, body);
      }
    }
    renderSaved(); renderAttachment(p);
    const gate = sendable(d, member);
    if (sendButton.dataset.lang !== state.lang) { sendButton.dataset.lang = state.lang; sendButton.setAttribute('aria-label', tr('Send', '发送')); sendButton.replaceChildren(make('span', 'team-send-label', tr('Send', '发送')), icon('arrow')); }
    sendButton.disabled = !gate.ok;
    sendButton.title = gate.ok ? tr(`Send one turn to ${member}`, `向 ${member} 发送一轮`) : gate.reason;
    renderHint();
  }
  function renderHint() {
    const d = cache.get(state.project)?.data; if (!d?.available) return;
    const gate = sendable(d, view.member);
    let text, tone = '';
    if (view.flash && view.flash.until > Date.now()) ({ text, tone } = view.flash);
    else if (!gate.ok) { text = gate.reason; tone = 'warn'; }
    else {
      const turns = d.policy ? ' · ' + turnsText(d) : '';
      const keys = coarse() ? '' : tr(' · Enter sends, Shift+Enter adds a line', ' · Enter 发送，Shift+Enter 换行');
      const display = d.members.find(m => m.id === view.member)?.name || view.member;
      text = tr(`Sends a real turn to ${display} in this branch`, `会在本分支向 ${display} 发起真实的一轮`) + turns + keys;
    }
    if (hint.textContent !== text) hint.textContent = text;
    hint.dataset.tone = tone;
  }
  function flash(text, tone) { view.flash = { text, tone, until: Date.now() + 6000 }; renderHint(); }

  async function recover(p) {
    if (view.recovering.has(p)) return;
    view.recovering.add(p); const d = cache.get(p)?.data; if (d) renderComposer(p, d);
    const say = (t, tone = '') => { const s = recoverBox.querySelector('.team-status'); if (s && state.project === p) { s.textContent = t; s.dataset.tone = tone; } };
    say(tr('Connecting this branch’s native member sessions; no research turn runs…', '正在连接本分支的成员原生会话；尚未启动研究…'));
    try { await call('/api/team/prepare', {}, p); say(tr('Sessions recovered. You can send now.', '会话已恢复，现在可以发送。'), 'ok'); }
    catch (e) { say(e.message, 'error'); }
    finally { view.recovering.delete(p); delete recoverBox.dataset.sig; await update(p); branchIndex.at = 0; loadBranches(); }
  }

  // The question saved with a milestone restore is offered once in the lead's composer.
  function prefill(p, d) {
    view.saved = null;
    const ms = window.EulerMilestones?.status(), r = ms?.project === p ? ms.data?.restored_from : null;
    const q = r?.request?.question;
    if (!q || !q.trim()) return;
    const untouched = !d.messages.some(m => !m.inherited && m.request?.sender === 'human') && !slot(p).pending.length;
    if (!untouched) return;
    const lead = d.members.some(m => m.id === d.lead) ? d.lead : d.members[0]?.id;
    const objectId = r.request.object_id || null;
    const attachment = objectId ? { id: objectId, kind: r.selected_object_kind || null, layer: r.request.layer || 'research', title: null } : null;
    view.saved = { question: q, attachment, lead, label: r.source_label, commit: r.source_commit };
    const sig = [p, r.source_commit, r.created_at, r.request.key || ''].join('|');
    if (prefilled().includes(sig)) return;
    markPrefilled(sig);
    const s = slot(p);
    if (!(s.drafts[lead] || '').trim()) s.drafts[lead] = q;
    if (attachment && !s.attachment) s.attachment = attachment;
    persist(true);
    // A late milestone response must not switch away from a member the user chose.
    if (view.member === lead && !input.value.trim()) { input.value = s.drafts[lead]; autosize(); }
  }
  function renderSaved() {
    const v = view.saved, p = state.project;
    const inComposer = v && view.member === v.lead && input.value.trim() === v.question.trim();
    const sig = JSON.stringify([state.lang, p, v, inComposer]);
    if (savedBox.dataset.sig === sig) return;
    savedBox.dataset.sig = sig; savedBox.replaceChildren(); savedBox.hidden = !v;
    if (!v) return;
    savedBox.append(icon('history'));
    const text = make('p');
    if (inComposer) text.append(make('span', '', tr('Saved question from the milestone restore — not sent. Review it and press Send when you are ready.', '恢复里程碑时保存的问题——尚未发送。核对后再点击发送。')));
    else {
      text.append(make('span', '', tr('This branch was restored with a saved question: ', '此分支恢复时附带了一个问题：')), src('q', '', v.question.length > 160 ? v.question.slice(0, 160) + '…' : v.question));
      const use = button(tr('Put it in the composer', '放入输入框'), () => {
        if (view.member !== v.lead) selectMember(v.lead);
        input.value = input.value.trim() ? v.question + '\n\n' + input.value : v.question;
        slot(p).drafts[v.lead] = input.value; if (v.attachment && !slot(p).attachment) slot(p).attachment = v.attachment;
        persist(true); autosize(); render(); input.focus();
      }, 'text-button');
      text.append(use);
    }
    savedBox.append(text);
  }

  function renderAttachment(p) {
    const a = slot(p).attachment;
    const sig = JSON.stringify([state.lang, p, a, a && titleFor(a)]);
    if (attachBox.dataset.sig === sig) return;
    attachBox.dataset.sig = sig; attachBox.replaceChildren();
    if (!a) { const add = button(tr('Attach a proof step', '附加证明步骤'), () => togglePicker(), 'text-button team-attach-add'); add.prepend(icon('book')); attachBox.append(add); return; }
    const chip = make('div', 'team-chip'); chip.title = a.id;
    chip.append(icon('book'), make('span', 'team-chip-kind', kindName(a.kind)), src('span', 'team-chip-title', titleFor(a)));
    const change = button(tr('Change', '更换'), () => togglePicker(), 'text-button');
    const remove = make('button', 'icon-button team-chip-remove'); remove.type = 'button'; remove.setAttribute('aria-label', tr('Remove the attached step', '移除附加的步骤')); remove.title = remove.getAttribute('aria-label'); remove.append(icon('close'));
    remove.onclick = () => { slot(p).attachment = null; persist(true); closePicker(); renderAttachment(p); input.focus(); };
    chip.append(change, remove);
    attachBox.append(chip, make('p', 'team-attach-note', tr('Its recorded statement, scope and ID are sent with your message.', '它记录的命题、范围与 ID 会随消息一起发送。')));
  }
  function closePicker() { pickerBox.hidden = true; pickerBox.replaceChildren(); }
  function togglePicker() { if (!pickerBox.hidden) { closePicker(); return; } openPicker(); }
  async function openPicker() {
    const p = state.project; pickerBox.hidden = false; pickerBox.replaceChildren(make('p', 'muted', tr('Loading proof steps…', '正在读取证明步骤…')));
    let g; try { g = await graphFor(p); } catch (e) { if (p === state.project && !pickerBox.hidden) pickerBox.replaceChildren(make('p', 'human-error', e.message), button(tr('Close', '关闭'), closePicker, 'text-button')); return; }
    if (p !== state.project || pickerBox.hidden) return;
    (g.nodes || []).forEach(x => known.set(p + '\u0000' + x.id, x));
    const order = new Map((g.reading_order || []).map((id, i) => [id, i]));
    const nodes = (g.nodes || []).filter(n => n.kind !== 'evidence').sort((a, b) => (order.get(a.id) ?? 1e9) - (order.get(b.id) ?? 1e9));
    const search = make('input'); search.type = 'search'; search.placeholder = tr('Search steps by title or ID', '按标题或 ID 搜索步骤'); search.setAttribute('aria-label', search.placeholder);
    const list = make('ul', 'team-picker-list');
    const draw = () => {
      const q = search.value.trim().toLowerCase(); list.replaceChildren();
      const hits = nodes.filter(n => !q || [n.id, titleOf(n).text, n.statement].join(' ').toLowerCase().includes(q)).slice(0, 8);
      for (const n of hits) {
        const li = make('li'), b = make('button', 'team-picker-item'); b.type = 'button';
        b.append(src('strong', '', titleOf(n).text), make('small', '', kindName(n.kind) + ' · ' + n.id.slice(0, 10)));
        b.onclick = () => { slot(p).attachment = { id: n.id, kind: n.kind, layer: 'research', title: titleOf(n).text }; persist(true); closePicker(); renderAttachment(p); input.focus(); };
        li.append(b); list.append(li);
      }
      if (!hits.length) list.append(make('li', 'muted', tr('No matching step.', '没有匹配的步骤。')));
    };
    search.oninput = draw;
    search.onkeydown = e => { if (e.key === 'Escape') { e.preventDefault(); closePicker(); input.focus(); } };
    const top = make('div', 'team-picker-head'); top.append(make('strong', '', tr('Attach a step from this branch’s research graph', '从本分支的研究图附加一个步骤')), button(tr('Close', '关闭'), () => { closePicker(); input.focus(); }, 'text-button'));
    pickerBox.replaceChildren(top, search, list); draw(); search.focus();
  }

  // Timeline: this member's turns in this branch, their delegations, and unsent requests.
  function entries(p, d, member) {
    const list = [];
    for (const m of d.messages || []) {
      const r = m.request || {};
      if (r.recipient === member) list.push({ key: 'm:' + m.id, at: m.created_at || '', m, inherited: m.inherited === true, kind: 'turn' });
      else if (r.sender === member) list.push({ key: 'd:' + m.id, at: m.created_at || '', m, inherited: m.inherited === true, kind: 'delegation' });
    }
    list.sort((a, b) => a.at.localeCompare(b.at));
    for (const x of slot(p).pending) if (x.member === member) list.push({ key: 'p:' + x.key, at: x.created_at, pending: x, kind: 'pending' });
    return list;
  }
  function renderLog(p, d) {
    const member = view.member, key = p + '\u0000' + member, fresh = view.logKey !== key;
    const atLatest = !fresh && isAtLatest();
    const all = entries(p, d, member);
    if (fresh && view.focusKey) { const i = all.findIndex(x => x.key === view.focusKey); if (i >= 0 && all.length - i > (view.limits.get(key) || PAGE)) view.limits.set(key, all.length - i + 5); }
    const limit = view.limits.get(key) || PAGE, hidden = Math.max(0, all.length - limit), shown = all.slice(hidden);
    const restored = d.branch?.kind === 'restored' || all.some(x => x.inherited);
    const want = [];
    const firstSaved = shown.findIndex(x => x.inherited), lastSaved = shown.map(x => !!x.inherited).lastIndexOf(true);
    const savedCount = all.filter(x => x.inherited).length, untouched = !all.some(x => !x.inherited);
    if (restored && lastSaved < 0) want.push(divider('start', untouched, d));
    shown.forEach((x, i) => {
      if (restored && i === firstSaved) want.push(divider('saved', savedCount, d));
      want.push(entry(x, d, member));
      if (restored && i === lastSaved) want.push(divider('start', untouched, d));
    });
    if (!all.length && !restored) want.push({ key: 'empty', sig: state.lang + member, build: () => make('li', 'team-log-empty', tr(`No messages with ${member} in this workspace yet. Write below to start.`, `本工作区还没有与 ${member} 的对话。在下方输入即可开始。`)) });
    const arrivals = [];
    const existing = new Map([...log.children].map(el => [el.dataset.key, el]));
    let prev = null;
    for (const w of want) {
      let el = existing.get(w.key);
      if (!el || el.dataset.sig !== w.sig) {
        const built = w.build(); built.dataset.key = w.key; built.dataset.sig = w.sig;
        // New messages and turns that just finished are arrivals; status ticks and dividers are not.
        if (!fresh && w.message && (!el || w.finished?.(el))) arrivals.push(w);
        if (el) { el.querySelectorAll('details[open][data-fold]').forEach(x => built.querySelector(`details[data-fold="${x.dataset.fold}"]`)?.setAttribute('open', '')); el.replaceWith(built); }
        el = built;
      }
      existing.delete(w.key);
      const expected = prev ? prev.nextElementSibling : log.firstElementChild; if (el !== expected) log.insertBefore(el, expected); prev = el;
    }
    existing.forEach(el => el.remove());
    log.setAttribute('aria-label', tr(`Conversation with ${member}`, `与 ${member} 的对话`));
    earlier.replaceChildren();
    if (hidden) {
      const more = button(tr(`Show ${Math.min(PAGE, hidden)} earlier messages`, `显示更早的 ${Math.min(PAGE, hidden)} 条`) + ' · ' + tr(`${hidden} not shown`, `尚有 ${hidden} 条`), () => showEarlier(key), 'button secondary team-earlier-button');
      earlier.append(more);
    }
    view.logKey = key;
    if (fresh) {
      const focus = view.focusKey && log.querySelector(`[data-key="${CSS.escape(view.focusKey)}"]`); view.focusKey = null;
      requestAnimationFrame(() => { if (focus) { focus.scrollIntoView({ block: 'center' }); focus.classList.add('is-focus'); setTimeout(() => focus.classList.remove('is-focus'), 2400); } else scrollToLatest(); });
      return;
    }
    if (!arrivals.length) return;
    if (atLatest) requestAnimationFrame(() => scrollToLatest());
    else {
      const reply = arrivals.find(w => w.reply);
      pill.replaceChildren(make('span', '', reply ? tr(`New reply from ${member}`, `${member} 有新回复`) : tr('New activity below', '下方有新动态')), icon('arrow'));
      pill.hidden = false; announcer.textContent = pill.textContent;
    }
  }
  function markSeen() {
    const p = state.project, d = cache.get(p)?.data; if (!d?.available || !view.member) return;
    const latest = d.messages.filter(x => x.request?.recipient === view.member && !ACTIVE.has(x.state)).map(stamp).sort().at(-1) || '';
    const s = slot(p); if (s.seen[view.member] !== latest) { s.seen[view.member] = latest; persist(); renderMembers(p, d); }
  }
  function showEarlier(key) {
    const first = [...log.children].find(el => el.getBoundingClientRect().bottom > 0), top = first?.getBoundingClientRect().top;
    view.limits.set(key, (view.limits.get(key) || PAGE) + PAGE);
    const d = cache.get(state.project)?.data; if (d) renderLog(state.project, d);
    if (first?.isConnected) window.scrollBy(0, first.getBoundingClientRect().top - top);
  }

  function divider(kind, info, d) {
    const b = d.branch || {};
    const sig = JSON.stringify([state.lang, kind, info, b.source_label, b.source_commit, b.restored_at]);
    return { key: 'div:' + kind, sig, build: () => {
      const li = make('li', 'team-divider is-' + kind), text = make('span', 'team-divider-text');
      if (kind === 'saved') {
        text.append(icon('history'), make('strong', '', tr('Saved history', '已保存的历史')), make('span', '', tr(' · inherited from milestone ', ' · 继承自里程碑 ')), src('span', '', b.source_label || (b.source_commit || '').slice(0, 8) || tr('checkpoint', '检查点')));
        li.append(text, make('p', 'team-divider-note', tr(`${info} recorded messages from before this branch was restored. They are part of the members’ forked memory; nothing here was sent from this branch.`, `本分支恢复之前记录的 ${info} 条消息。它们属于成员分叉后的记忆，并非从本分支发出。`)));
      } else {
        text.append(make('strong', '', tr('This branch starts here', '本分支从这里开始')), make('span', '', b.restored_at ? ' · ' + tr('restored ', '恢复于 ') + when(b.restored_at) : ''));
        li.append(text);
        if (info) li.append(make('p', 'team-divider-note', tr('Nothing has been sent in this branch yet.', '本分支还没有发送过任何消息。')));
      }
      return li;
    } };
  }
  function entry(x, d, member) {
    if (x.kind === 'pending') {
      const it = x.pending;
      return { key: x.key, sig: JSON.stringify([state.lang, it.status, it.error, it.question.length, it.attachment && titleFor(it.attachment)]), build: () => pendingEl(it) };
    }
    const m = x.m;
    const sig = JSON.stringify([state.lang, x.kind, m.state, m.reply ? m.reply.length : 0, m.error, m.inherited, m.started_at, m.completed_at, (m.request?.question || '').length, view.expanded.has(m.id), m.request?.object_id && titleFor({ id: m.request.object_id })]);
    const done = s => ['ANSWERED', 'FAILED', 'CANCELLED'].includes(s);
    return { key: x.key, sig, message: true, reply: x.kind === 'turn' && m.state === 'ANSWERED',
      finished: old => !done(old.dataset.state) && done(m.state),
      build: () => x.kind === 'turn' ? turnEl(m, d, member) : delegationEl(m) };
  }

  function timeNode(value) { const t = make('time', 'team-time', when(value)); if (value) t.dateTime = value; return t; }
  function stateChip(s, text) { const c = make('span', 'team-state-chip', text || messageState(s)); c.dataset.state = String(s).toLowerCase(); return c; }
  function bubble(text, id) {
    const b = make('div', 'team-bubble'); markdown(b, text);
    if (text.length <= LONG) return [b];
    const open = view.expanded.has(id); b.classList.toggle('is-clamped', !open);
    return [b, button(open ? tr('Show less', '收起') : tr('Show the full message', '显示完整消息'), () => { if (view.expanded.has(id)) view.expanded.delete(id); else view.expanded.add(id); const d = cache.get(state.project)?.data; if (d) renderLog(state.project, d); }, 'text-button team-more')];
  }
  function objectChip(m) {
    const id = m.request?.object_id; if (!id) return null;
    const rec = m.selected_object?.record, chip = make('div', 'team-ask-object');
    chip.append(icon('book'), make('span', 'team-chip-kind', kindName(rec?.kind)), src('span', '', titleFor({ id })));
    chip.title = id;
    if (!m.selected_object) return chip;
    const box = make('details', 'team-fold'); box.dataset.fold = 'o';
    box.append(make('summary', '', tr('Recorded object sent with this message', '随消息发送的对象记录')));
    const text = rec?.exact_statement || rec?.statement || rec?.summary;
    if (text) { const s = make('div', 'team-object-statement'); markdown(s, text); box.append(s); }
    const facts = make('dl', 'team-ids');
    for (const [k, v] of [[tr('ID', 'ID'), m.selected_object.object_id], [tr('Layer', '层'), m.selected_object.layer], [tr('Status', '状态'), rec?.status || rec?.state], [tr('Relations', '关系'), (m.selected_object.relations || []).length], [tr('Record hash', '记录哈希'), (m.selected_object.record_hash || '').slice(0, 16)]]) if (v !== undefined && v !== null && v !== '') facts.append(make('dt', '', k), src('dd', '', String(v)));
    box.append(facts);
    return [chip, box];
  }
  function turnEl(m, d, member) {
    const li = make('li', 'team-turn'); li.dataset.state = m.state; if (m.inherited) li.classList.add('is-inherited');
    const r = m.request || {}, ask = make('div', 'team-ask'), meta = make('div', 'team-meta');
    if (r.sender === 'human') { ask.classList.add('from-you'); meta.append(make('strong', '', tr('You', '你'))); }
    else if (r.sender === 'team-delivery') {
      ask.classList.add('from-delivery'); const origin = d.messages.find(x => x.id === r.parent);
      meta.append(make('strong', '', origin ? tr(`${origin.request.recipient}’s reply, delivered to ${member}`, `${origin.request.recipient} 的回复，已转交给 ${member}`) : tr('Team delivery', '团队转交')));
    } else {
      ask.classList.add('from-member'); const who = d.members.find(x => x.id === r.sender);
      meta.append(make('strong', '', tr(`From ${who?.name || r.sender}`, `来自 ${who?.name || r.sender}`))); if (who) meta.append(make('span', 'team-member-role', roleName(who.role)));
    }
    meta.append(timeNode(m.created_at)); if (m.inherited) meta.append(make('span', 'team-saved-tag', tr('Saved', '已保存')));
    ask.append(meta);
    if (r.sender === 'team-delivery') { const fold = make('details', 'team-fold'); fold.dataset.fold = 'q'; fold.append(make('summary', '', tr('Show the delivered reply', '查看转交的回复'))); const b = make('div', 'team-bubble'); markdown(b, r.question || ''); fold.append(b); ask.append(fold); }
    else ask.append(...bubble(r.question || '', m.id));
    const obj = objectChip(m); if (obj) ask.append(...[].concat(obj));
    li.append(ask, answerEl(m, d, member));
    return li;
  }
  function answerEl(m, d, member) {
    const who = d.members.find(x => x.id === member), box = make('div', 'team-answer'), meta = make('div', 'team-meta');
    const avatar = make('span', 'team-avatar small', initial(who || { id: member })); avatar.dataset.provider = who?.provider || ''; avatar.setAttribute('aria-hidden', 'true');
    meta.append(avatar, src('strong', '', who?.name || member), stateChip(m.state));
    if (m.completed_at) meta.append(timeNode(m.completed_at));
    if (m.started_at && m.completed_at) {
      const s = Math.max(0, (new Date(m.completed_at) - new Date(m.started_at)) / 1000);
      if (Number.isFinite(s)) meta.append(make('span', 'team-time', s < 90 ? tr(`${Math.round(s)} s`, `${Math.round(s)} 秒`) : tr(`${Math.round(s / 60)} min`, `${Math.round(s / 60)} 分钟`)));
    }
    box.append(meta);
    if (m.state === 'ANSWERED') { const b = make('div', 'team-reply'); if (m.reply) markdown(b, m.reply); else b.textContent = tr('Answered without a public reply.', '已完成，但没有公开回复。'); box.append(b); }
    else if (m.state === 'RUNNING') { const p = make('p', 'team-working'); p.append(make('span', 'team-typing'), make('span', '', tr('Working on it · started ', '正在处理 · 开始于 ')), ageNode('span', '', m.started_at)); box.append(p); }
    else if (m.state === 'QUEUED') box.append(make('p', 'team-waiting', tr('Waiting for its turn in this branch’s queue.', '正在本分支队列中等待。')));
    else if (m.state === 'SUSPENDED') box.append(make('p', 'team-waiting', tr('Held while the team is paused. Resume the team to continue it.', '团队暂停期间挂起。继续团队后会接着执行。')));
    else if (m.state === 'FAILED') box.append(make('p', 'team-failed', (m.inherited ? tr('Failed before the milestone was saved: ', '在保存里程碑之前已失败：') : tr('Failed: ', '失败：')) + (m.error || tr('no reason recorded', '未记录原因'))));
    else if (m.state === 'CANCELLED') box.append(make('p', 'team-waiting', tr('Cancelled.', '已取消。')));
    return box;
  }
  function delegationEl(m) {
    const li = make('li', 'team-delegation'); li.dataset.state = m.state; if (m.inherited) li.classList.add('is-inherited');
    const r = m.request || {}, line = make('div', 'team-delegation-line');
    line.append(icon('arrow'), make('span', '', tr('Asked ', '询问了 ')), src('strong', '', r.recipient), stateChip(m.state), timeNode(m.created_at));
    if (m.inherited) line.append(make('span', 'team-saved-tag', tr('Saved', '已保存')));
    line.append(button(tr(`Open in ${r.recipient}’s conversation`, `在 ${r.recipient} 的对话中查看`), () => { view.focusKey = 'm:' + m.id; selectMember(r.recipient); }, 'text-button'));
    const fold = make('details', 'team-fold'); fold.dataset.fold = 'q'; fold.append(make('summary', '', tr('Show the request', '查看请求'))); const b = make('div', 'team-bubble'); markdown(b, r.question || ''); fold.append(b);
    li.append(line, fold); return li;
  }
  function pendingEl(it) {
    const li = make('li', 'team-turn is-pending'); li.dataset.state = it.status;
    const ask = make('div', 'team-ask from-you'), meta = make('div', 'team-meta');
    meta.append(make('strong', '', tr('You', '你')), timeNode(it.created_at)); ask.append(meta, ...bubble(it.question, 'p:' + it.key));
    if (it.attachment) { const c = make('div', 'team-ask-object'); c.append(icon('book'), make('span', 'team-chip-kind', kindName(it.attachment.kind)), src('span', '', titleFor(it.attachment))); ask.append(c); }
    const box = make('div', 'team-answer');
    if (it.status === 'sending') box.append(make('p', 'team-waiting', tr('Sending…', '正在发送…')));
    else if (it.status === 'sent') box.append(make('p', 'team-waiting', tr('Sent · waiting for the team record…', '已发送 · 等待团队记录…')));
    else {
      box.append(make('p', 'team-failed', it.status === 'unconfirmed' ? tr('Delivery is not confirmed yet. Retry keeps the same request identity, so an already received message is not created twice.', '尚未确认服务器是否收到消息。重试会沿用同一请求标识，不会重复创建已接收的消息。') : tr('Send encountered a problem: ', '发送遇到问题：') + (it.error || '')));
      const row = make('div', 'team-pending-actions'), p = state.project;
      row.append(button(tr('Retry', '重试'), () => post(p, it), 'button secondary'), button(tr('Edit', '编辑'), () => editPending(p, it), 'text-button'), button(tr('Discard', '放弃'), () => dropPending(p, it), 'text-button subtle'));
      box.append(row);
    }
    li.append(ask, box); return li;
  }

  async function send() {
    const p = state.project, d = cache.get(p)?.data, member = view.member;
    if (!d?.available || p !== view.project) return;
    const gate = sendable(d, member);
    if (!gate.ok) { flash(gate.reason, 'warn'); return; }
    const question = input.value;
    if (!question.trim()) { flash(tr('Write a message first.', '请先写下消息。'), 'error'); input.focus(); return; }
    const s = slot(p), attachment = s.attachment || null;
    const item = { key: uid(), member, question, object_id: attachment?.id || null, attachment, created_at: new Date().toISOString(), status: 'sending' };
    s.pending.push(item); s.drafts[member] = ''; s.attachment = null;
    input.value = ''; autosize(); persist(true); closePicker();
    render(); requestAnimationFrame(() => scrollToLatest());
    await post(p, item);
  }
  // Retrying reuses the identical payload and request identity, so the server can deduplicate it.
  async function post(p, item) {
    item.status = 'sending'; item.error = null; persist(true);
    if (p === state.project && state.view === 'team') render();
    try {
      const r = await call('/api/team/ask', { member: item.member, question: item.question, object_id: item.object_id, idempotency_key: item.key }, p);
      const s = slot(p);
      if (r?.id && r.request) {
        s.pending = s.pending.filter(x => x.key !== item.key);
        const d = cache.get(p)?.data; if (d?.messages && !d.messages.some(x => x.id === r.id)) d.messages.push({ inherited: false, ...r });
      } else item.status = 'sent';
    } catch (e) { item.status = e.unconfirmed ? 'unconfirmed' : 'failed'; item.error = e.message; }
    persist(true);
    if (p === state.project && state.view === 'team') render();
    update(p);
  }
  function editPending(p, it) {
    const s = slot(p); s.pending = s.pending.filter(x => x.key !== it.key);
    if (view.member !== it.member) selectMember(it.member);
    input.value = input.value.trim() ? it.question + '\n\n' + input.value : it.question;
    s.drafts[it.member] = input.value; if (it.attachment && !s.attachment) s.attachment = it.attachment;
    persist(true); autosize(); render(); input.focus();
  }
  function dropPending(p, it) { const s = slot(p); s.pending = s.pending.filter(x => x.key !== it.key); persist(true); render(); }

  function renderEmpty(mode, c, d) {
    const sig = JSON.stringify([state.lang, state.project, mode, c?.error, d?.reason, d?.branch, [...branchIndex.items].filter(([, s]) => s.available).map(([id]) => id)]);
    if (empty.dataset.sig === sig) return;
    empty.dataset.sig = sig; empty.replaceChildren();
    if (mode === 'loading') { empty.append(make('p', 'team-empty-loading', tr('Reading this branch’s team…', '正在读取本分支的团队…'))); return; }
    if (mode === 'error') {
      empty.append(make('h2', '', tr('The team could not be read', '暂时无法读取团队')), make('p', 'human-error', c.error), button(tr('Try again', '重试'), () => update(), 'button secondary'));
      return;
    }
    const b = d?.branch, mark = make('span', 'team-empty-mark'); mark.append(icon('chat'));
    empty.append(mark, make('h2', '', tr('No team conversations in this workspace', '此工作区没有团队对话')));
    empty.append(make('p', '', b?.kind === 'restored'
      ? tr('This branch was restored from a milestone that saved files only. Its files and proof graph are complete, but no member sessions were saved, so there is nobody to talk to here.', '此分支恢复自只保存了文件的里程碑。文件与证明图完整，但没有保存成员会话，因此这里没有可对话的成员。')
      : tr('This workspace has no registered member conversations that this workbench can continue. Its files and proof graph remain available for reading.', '此工作区尚未登记可由工作台接续的成员会话。已有文件和证明图仍可阅读。')));
    empty.append(make('p', 'muted', tr('To talk with a team, switch to a workspace that has one, or restore a milestone that saved the team’s conversations.', '如需与团队对话，请切换到有团队的工作区，或恢复一个保存了团队会话的里程碑。')));
    const withTeam = [...branchIndex.items].filter(([id, s]) => s.available && id !== state.project).slice(0, 4);
    if (withTeam.length) { const row = make('div', 'team-empty-links'); for (const [id] of withTeam) { const go = button('', () => changeProject(id), 'button secondary'); go.append(nameNode('span', '', id)); row.append(go); } empty.append(make('p', 'team-kicker', tr('Workspaces with a team', '有团队的工作区')), row); }
  }

  function indicate() {
    const dot = document.querySelector('#team-tab-dot'); if (!dot) return;
    const d = cache.get(state.project)?.data, tone = !d?.available ? '' : !chatReady(d) ? 'warn' : busy(d) ? 'live' : '';
    dot.hidden = !tone; dot.dataset.tone = tone;
    dot.title = tone === 'warn' ? tr('Member sessions need recovery', '成员会话需要恢复') : tone === 'live' ? tr('A member is working', '有成员正在工作') : '';
  }

  function enter() { view.logKey = ''; pill.hidden = true; render(); update(); loadBranches(); }
  function projectChanged() { closePicker(); indicate(); if (state.view === 'team') render(); update(); }
  function open(selected = null) {
    if (selected?.id) { slot(state.project).attachment = { id: selected.id, kind: selected.kind || null, layer: selected.layer || 'research', title: selected.title || null }; persist(true); }
    view.focusComposer = true;
    if (state.view === 'team') render(); else switchView('team');
  }
  document.addEventListener('euler:workbench-status', () => { if (state.view === 'team' && window.EulerMilestones?.status()?.project === state.project) { const d = cache.get(state.project)?.data; if (d?.available) { prefill(state.project, d); renderSaved(); } } });
  let tick = 0;
  setInterval(() => {
    if (state.view !== 'team' || document.hidden || !state.project) return;
    tick++;
    const d = cache.get(state.project)?.data, s = slot(state.project);
    const active = busy(d) || s.pending.some(x => x.status === 'sending' || x.status === 'sent') || view.recovering.has(state.project);
    if (active || (state.live && tick % 3 === 0)) update();
    if (tick % 10 === 0) loadBranches();
    if (view.confirmStop && view.confirmStop < Date.now() && d) { view.confirmStop = 0; renderControls(d); }
  }, 3000);
  window.EulerTeam = { update, open, enter, focusMember: async (p,id) => { await update(p); if (p===state.project) { switchView('team'); view.focusComposer=true; selectMember(id); } }, render: () => { if (state.view === 'team') render(); }, projectChanged, status, stateName: names };
  // app.js may have routed to #team before this script ran.
  setTimeout(() => { if (state.view === 'team') enter(); else update(); }, 0);
})();
