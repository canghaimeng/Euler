/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Qiuyu Chen (canghaimeng). */
/* Object-bound human workbench. Existing read views remain the entry point. */
(() => {
  const H = { capability: null, campaign: null, project: null, selected: null, preview: null, serial: 0, dialogVersion: 0 };
  // getRandomValues is available on private HTTP deployments as well as HTTPS;
  // randomUUID is restricted to secure contexts in several browsers.
  const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const tr = (en, zh) => state.lang === 'zh' ? zh : en;
  const el = (tag, cls, text) => node(tag, cls || '', text || '');
  const button = (text, action, cls = 'button secondary') => { const b = el('button', cls, text); b.type = 'button'; b.onclick = action; return b; };
  const details = (title, data) => { const d = el('details', 'human-record'); d.append(el('summary', '', title), el('pre', '', typeof data === 'string' ? data : JSON.stringify(data, null, 2))); return d; };
  const errorBox = el('p', 'human-error'); errorBox.setAttribute('role', 'alert');
  const dialog = el('dialog', 'human-dialog'); dialog.id = 'human-dialog'; document.body.append(dialog);
  dialog.addEventListener('close', () => { if (!dialog.open) { H.dialogVersion++; H.preview = null; } });
  const bar = el('section', 'human-runbar'); bar.id = 'human-runbar'; bar.hidden = true;
  document.querySelector('#main').prepend(bar);
  const changesView = el('details','human-changes'); changesView.hidden = true; bar.after(changesView);
  const investigations = el('section', 'overview-card human-investigations'); investigations.id = 'human-investigations'; investigations.hidden = true;
  document.querySelector('#exploration-live').after(investigations);
  async function call(path, body, project = state.project) {
    const u = new URL(path, location.origin); if (project) u.searchParams.set('project', project);
    const headers = { ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}) };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; }
    const r = await fetch(u, { method: body === undefined ? 'GET' : 'POST', headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }), cache: 'no-store' });
    const data = await r.json(); if (!r.ok) throw new Error(data.detail || r.statusText); return data;
  }
  function field(label, element) { const wrap = el('label', 'human-field'); wrap.append(el('span', '', label), element); return wrap; }
  function modal(title) {
    dialog.replaceChildren(); errorBox.textContent = '';
    const head = el('header', 'human-dialog-head'); head.append(el('h2', '', title), button(tr('Close', '关闭'), () => dialog.close())); dialog.append(head, errorBox);
    if (state.projects?.some(p => p.id === state.project && p.name.includes('(MOCK)'))) dialog.append(el('p','notice',tr('MOCK worker and verifier · protocol demonstration only, not a mathematical result', '模拟 worker 与验证裁决 · 仅为流程演示，不是数学成果')));
    if (!dialog.open) dialog.showModal();
  }
  async function runCommand(command) {
    modal(tr('Research execution control', '研究运行控制'));
    const mode = el('select'); mode.id = 'human-pause-mode';
    mode.append(new Option(tr('Let current tasks finish', '等待当前任务结束'), 'drain'), new Option(tr('Request local worker cancellation', '请求取消本地 worker'), 'cancel'));
    dialog.append(el('p', '', tr('Pause prevents ordinary dispatch. Focused investigations require their own explicit start. Stop waits for actual lease settlement; remote provider cancellation is not guaranteed.', '暂停会禁止普通派发；聚焦研究需要单独明确启动。终止状态取决于实际租约结算，不能保证远程提供商立即停止。')));
    if (command === 'pause') dialog.append(field(tr('Pause mode', '暂停方式'), mode));
    const labels = { pause: tr('Confirm pause', '确认暂停'), resume: tr('Confirm resume', '确认继续'), stop: tr('Confirm stop', '确认终止') };
    const revision = H.campaign; const project = state.project;
    dialog.append(button(labels[command], async () => {
      try { await call('/api/human/control', { command, mode: command === 'stop' ? 'cancel' : mode.value, idempotency_key: requestId(), revision: revision.revision, contract_id: revision.contract_id }, project); dialog.close(); await refresh(); await update(); }
      catch (e) { errorBox.textContent = e.message; }
    }, 'button primary'));
  }
  async function update() {
    window.EulerMilestones?.update();
    window.EulerTeam?.update();
    const project = state.project, serial = ++H.serial;
    try {
      const caps = await call('/api/human/capabilities'); if (project !== state.project || serial !== H.serial) return;
      H.capability = caps; H.project = project; bar.hidden = !caps.enabled; investigations.hidden = !caps.enabled; repaint();
      if (!caps.enabled) { changesView.hidden = true; return; }
      const [campaign, items] = await Promise.all([call('/api/human/state'), call('/api/human/interventions')]);
      if (project !== state.project || serial !== H.serial) return;
      H.campaign = campaign; bar.replaceChildren();
      const stateLabel = { RUNNING: tr('Campaign running', '项目运行中'), PAUSING: tr('Waiting for tasks to settle', '正在等待任务结算'), PAUSED: tr('Campaign paused', '原项目已暂停'), STOP_REQUESTED: tr('Stop requested · awaiting settlement', '已请求终止 · 等待结算'), STOPPED: tr('Execution stopped', '研究执行已停止') }[campaign.state];
      const body = el('div'); body.append(el('strong', '', stateLabel), el('small', '', tr('Focused work is authorized separately. Mathematical acceptance is unchanged.', '聚焦任务单独授权；数学验收标准保持原样。'))); bar.append(body);
      let pending = null; try { pending = JSON.parse(sessionStorage.getItem('euler-pending-intervention:' + project) || 'null'); } catch (_) {}
      if (pending) body.append(button(tr('Recover interrupted submission','恢复中断的提交'),()=>{
        modal(tr('Recover the original request','恢复原来的请求'));
        dialog.append(el('p','',pending.request.user_question),el('p','muted',tr('Resubmit the same operation identity. If already recorded, the server returns that investigation without creating another task.','使用同一个操作标识重试；如果此前已记录，服务端返回原研究，不创建重复任务。')),button(tr('Recover request','恢复请求'),async()=>{try{const result=await call('/api/human/interventions',pending,project);sessionStorage.removeItem('euler-pending-intervention:'+project);await update();await showInvestigation(result.intervention_id);}catch(e){errorBox.textContent=e.message;}}));
      }));
      const controls = el('div', 'human-buttons');
      controls.append(button(tr('Pause', '暂停'), () => runCommand('pause')), button(tr('Resume', '继续'), () => runCommand('resume')), button(tr('Stop', '终止'), () => runCommand('stop'))); bar.append(controls);
      const parentFocused = campaign.active_leases.filter(l => l.focused).length;
      if (parentFocused) body.append(el('small', '', tr(`${parentFocused} focused task(s) active`, `${parentFocused} 个聚焦任务活动中`)));
      renderInvestigations(items);
      const currentInvestigation = items.find(i => i.intervention_id === H.investigationId);
      if (dialog.open && currentInvestigation && currentInvestigation.state !== H.investigationState) showInvestigation(H.investigationId);
      const seenKey = 'euler-reviewed-sequence:' + project;
      let seen = 0; try { seen = Number(localStorage.getItem(seenKey)) || 0; } catch (_) {}
      const changes = await call('/api/human/changes?since=' + seen).catch(() => null);
      if (changes && project === state.project) {
        changesView.hidden = false; changesView.replaceChildren(el('summary','',tr('What changed since your review', '自上次阅读后的变化') + ' · ' + changes.items.length));
        changesView.append(el('p','muted',tr('These are recorded state changes. Human requests and task settlement do not imply a new mathematical result.', '以下为记录中的状态变化；人类追问与任务结算不代表新的数学结论。')));
        for (const change of changes.items) {
          const row = el('div','human-change'); row.append(el('strong','',change.kind), el('small','',change.timestamp), details(tr('Source event','来源事件'),change.payload)); changesView.append(row);
        }
        changesView.append(button(tr('Mark these records as reviewed','标记以上记录已读'),()=>{try{localStorage.setItem(seenKey,String(changes.latest_sequence));}catch(_){}update();}));
      }
    } catch (e) { if (project === state.project) { bar.hidden = true; investigations.hidden = true; } }
  }
  function renderInvestigations(items) {
    investigations.replaceChildren(el('h2', '', tr('Focused investigations', '人类发起的聚焦研究')), el('p', 'muted', tr('Each request retains the selected object version, derived scope, dispatched task and checked outcome.', '每次追问保留原对象版本、派生范围、执行任务与检查结果。')));
    if (!items.length) investigations.append(el('p', '', tr('Select a mathematical object in the reader or relation map to begin.', '从证明阅读或关系图中选择一个数学对象开始。')));
    for (const item of items.slice().reverse()) {
      const card = el('article', 'human-investigation'); card.dataset.interventionId = item.intervention_id;
      card.append(el('strong', '', item.request.user_question), el('p', '', `${item.state} · ${tr('Mathematical obligation', '数学义务')}: ${item.mathematical_status}`));
      card.append(button(tr('Request → task → evidence', '追问 → 任务 → 证据'), () => showInvestigation(item.intervention_id)));
      investigations.append(card);
    }
  }
  async function showInvestigation(id) {
    const project = state.project, version = ++H.dialogVersion;
    try {
      const item = await call('/api/human/interventions/' + encodeURIComponent(id)); if (project !== state.project || version !== H.dialogVersion) return;
      H.investigationId = id; H.investigationState = item.state; dialog.dataset.investigationId = id;
      modal(tr('Investigation and outcome', '聚焦研究与结果'));
      dialog.append(el('h3', '', item.request.user_question), el('p', '', `${item.state} · ${tr('Mathematical status', '数学状态')}: ${item.mathematical_status}`), el('p', '', tr('Task settlement alone does not close the mathematical obligation. Original parent acceptance is unchanged.', '任务结算本身不代表数学义务闭合；原始主目标验收不因此改变。')));
      if(item.runtime_observation) dialog.append(el('p','muted',tr('Observed worker state: ', '观测到的 worker 状态：') + item.runtime_observation.state));
      dialog.append(button(tr('Return to source object', '回到被追问的对象'), () => { dialog.close(); openReader(item.target.object_id, item.target.object_type === 'fact' ? 'facts' : 'research'); }), button(tr('Open derived obligation', '打开派生义务'), () => { dialog.close(); openReader(item.obligation_id, 'research'); }));
      dialog.append(details(tr('Reviewed statement and scope', '已确认的命题与作用范围'), { statement: item.request.requested_statement, scope: item.request.requested_scope, target: item.target }), details(tr('Evidence identifiers', '验收证据标识'), item.evidence_refs));
      for (const ref of item.evidence_refs) dialog.append(button(tr('Open verification evidence ', '打开验收证据 ') + ref.slice(0,12),()=>{dialog.close();openReader(ref,'research');}));
      const stageNames = {
        'euler.human_requested': tr('Research question recorded','研究问题已记录'),
        'euler.human_validated': tr('Object version and scope checked','对象版本与范围已核对'),
        'euler.focused_created': tr('Independent scoped investigation created','已创建独立的范围明确的研究'),
        'euler.human_dispatched': tr('Task dispatched with its source context','已携带来源上下文派发任务'),
        'euler.human_outcome_reconciled': tr('Outcome returned to this question','结果已关联回这次追问')
      };
      const timeline = el('ol', 'human-timeline');
      for (const event of item.timeline.filter(e=>stageNames[e.type])) { const row = el('li'); row.append(el('strong', '', stageNames[event.type]), el('small', '', `${event.timestamp} · ${event.actor}`), details(tr('Source record', '来源记录'), event.payload)); timeline.append(row); }
      dialog.append(timeline,details(tr('Complete execution and verification trace','完整执行与验证轨迹'),item.timeline));
      if (['AWAITING_START', 'AUTHORIZED', 'READY'].includes(item.state)) {
        dialog.append(button(H.capability.allow_launch ? tr('Start this focused investigation', '启动这一个聚焦研究') : tr('Prepare bounded dispatch', '准备受限派发'), async () => {
          try { await call('/api/human/interventions/' + id + '/start', { idempotency_key: 'start:' + id, launch: H.capability.allow_launch }, project); await update(); await showInvestigation(id); }
          catch(e) { errorBox.textContent = e.message; }
        }, 'button primary'));
      }
      dialog.append(button(tr('Refresh outcome', '刷新结果'), () => showInvestigation(id)), button(tr('Export event trace', '导出事件轨迹'), () => {
        const u = URL.createObjectURL(new Blob([JSON.stringify(item, null, 2)], {type:'application/json'})); const a = el('a'); a.href = u; a.download = 'euler-investigation-' + id.slice(0,12) + '.json'; a.click(); URL.revokeObjectURL(u);
      }));
    } catch(e) { errorBox.textContent = e.message; }
  }
  async function inspect(kind, id) {
    const project = state.project, version = ++H.dialogVersion;
    H.investigationId = null;
    modal(tr('Object record', '对象记录')); dialog.append(el('p', 'muted', tr('Loading original identity and evidence…', '正在读取原始对象与证据…')));
    try {
      const obj = await call('/api/human/object?object_type=' + encodeURIComponent(kind) + '&object_id=' + encodeURIComponent(id)); if (project !== state.project || version !== H.dialogVersion) return;
      H.selected = obj; H.preview = null; modal(obj.writable && H.capability?.enabled ? tr('Inspect object · single task', '查看对象 · 单项任务') : tr('Object record', '对象记录'));
      dialog.append(el('p', 'human-statement', obj.statement), el('p', 'muted', `${obj.target.object_type} · ${obj.status}`), details(tr('Identity, version and provenance', '身份、版本与来源'), { ...obj.target, source: obj.source }), details(tr('Scope, assumptions and quantifiers', '范围、假设与量词'), obj.scope || tr('Unknown', '未知')), details(tr('Dependencies and alternatives', '依赖与替代路线'), { premises: obj.premises, relations: obj.relations, routes: obj.alternative_routes }), details(tr('Evidence and verification level', '证据与验证等级'), obj.evidence));
      if (kind === 'route' && obj.alternative_routes.length > 1) {
        const alternatives = el('select'); alternatives.append(new Option(tr('Compare an alternative route…','选择一条路线比较…'),''));
        obj.alternative_routes.filter(r=>r.route_id!==id).forEach(r=>alternatives.append(new Option(r.mechanism,r.route_id)));
        alternatives.onchange = async()=>{if(!alternatives.value)return;try{const compared=await call('/api/human/compare?left='+encodeURIComponent(id)+'&right='+encodeURIComponent(alternatives.value));modal(tr('Compare recorded routes','比较记录中的路线'));for(const side of ['left','right']){const route=compared[side];dialog.append(el('h3','',route.statement),details(tr('Scope, premises and evidence','范围、前提与证据'),{scope:route.scope,route:route.record,evidence:route.evidence}));}dialog.append(el('p','muted',tr('These are recorded relationships; no equivalence or new implication is inferred.','这里只比较已记录的关系，不推断等价性或新的蕴含。')));}catch(e){errorBox.textContent=e.message;}};
        dialog.append(field(tr('Compare routes','比较路线'),alternatives));
      }
      if (obj.unknown_scope_fields?.length) dialog.append(el('p','muted',tr('Scope fields not recorded: ', '未记录的范围字段：') + obj.unknown_scope_fields.join(', ')));
      for (const a of obj.assumptions) dialog.append(button(tr('Inspect assumption: ', '查看假设：') + a.statement, () => inspect('assumption', a.object_id), 'text-button'));
      if (!obj.writable || !H.capability?.enabled) { dialog.append(el('p', 'human-readonly', obj.readonly_reason || tr('Read-only deployment. Research controls have not been enabled.', '当前部署为只读，尚未启用研究控制。'))); return; }
      const intent = el('select'); intent.id = 'human-intent';
      for (const [v,en,zh] of [['VERIFY_OR_AUDIT','Verify or audit','核查或审计'],['CHALLENGE_OR_COUNTEREXAMPLE','Challenge / seek counterexample','质疑／寻找反例'],['EXPLORE_ASSUMPTION','Explore assumptions or generalize','研究假设／尝试推广'],['EXPLORE_ALTERNATIVE','Explore an alternative route','探索替代路线']]) intent.append(new Option(tr(en,zh),v));
      const question = el('textarea'); question.id = 'human-question'; question.rows = 3; question.maxLength = 6000;
      const restoredQuestion = await call('/api/milestones').catch(()=>null);
      if (project !== state.project || version !== H.dialogVersion || !dialog.open) return;
      if (restoredQuestion?.restored_from?.request?.object_id === id) question.value = restoredQuestion.restored_from.request.question || '';
      const statement = el('textarea'); statement.id = 'human-statement'; statement.rows = 3; statement.value = obj.statement; statement.maxLength = 12000;
      const scope = el('textarea'); scope.id = 'human-scope'; scope.rows = 7; scope.value = JSON.stringify(obj.scope,null,2); scope.disabled = true;
      const domain = el('textarea'); domain.id = 'human-domain'; domain.rows = 2; domain.value = obj.scope.parameter_domain; domain.disabled = true;
      const assumptions = el('textarea'); assumptions.id = 'human-assumptions'; assumptions.rows = 3; assumptions.value = obj.scope.assumptions.join('\n'); assumptions.disabled = true;
      scope.oninput = ()=>{try{const parsed=JSON.parse(scope.value);domain.value=parsed.parameter_domain || '';assumptions.value=(parsed.assumptions || []).join('\n');}catch(_){}};
      const syncScope = ()=>{try{const parsed=JSON.parse(scope.value);parsed.parameter_domain=domain.value;parsed.assumptions=assumptions.value.split('\n').filter(x=>x.trim());scope.value=JSON.stringify(parsed,null,2);}catch(_){}};
      domain.oninput=syncScope; assumptions.oninput=syncScope;
      intent.onchange = () => { scope.disabled = !['EXPLORE_ASSUMPTION','CHALLENGE_OR_COUNTEREXAMPLE'].includes(intent.value);if(intent.value==='CHALLENGE_OR_COUNTEREXAMPLE'&&statement.value===obj.statement)statement.value=tr('There exists a counterexample to the claim: ', '存在以下命题的反例：')+obj.statement;domain.disabled=scope.disabled;assumptions.disabled=scope.disabled;if(scope.disabled){scope.value=JSON.stringify(obj.scope,null,2);domain.value=obj.scope.parameter_domain;assumptions.value=obj.scope.assumptions.join('\n');} invalidate(); };
      const allowance = el('input'); allowance.id = 'human-seconds'; allowance.type = 'number'; allowance.min = '1'; allowance.max = String(H.capability.maximum.wall_ms / 1000); allowance.value = allowance.max;
      const scopeEditor = el('details', 'human-record'); scopeEditor.append(el('summary', '', tr('Advanced: quantifiers, objects and definition version', '高级：量词、对象与定义版本')), scope);
      const preview = el('div', 'human-preview'); preview.id = 'human-preview';
      const confirm = button(tr('Confirm scoped request', '确认这个范围内的追问'), async () => {
        if (!H.preview || project !== state.project) return;
        confirm.disabled = true;
        try { const submission = { request:H.preview.request, preview_hash:H.preview.preview_hash, idempotency_key:H.preview.key }; try {sessionStorage.setItem('euler-pending-intervention:'+project,JSON.stringify(submission));} catch(_) {} const result = await call('/api/human/interventions', submission, project); try {sessionStorage.removeItem('euler-pending-intervention:'+project);} catch(_) {} await update(); await showInvestigation(result.intervention_id); }
        catch(e) { errorBox.textContent = e.message; confirm.disabled = false; }
      }, 'button primary'); confirm.hidden = true;
      let formVersion = 0;
      function invalidate() { formVersion++; H.preview = null; preview.replaceChildren(); confirm.hidden = true; }
      [question,statement,scope,domain,assumptions,allowance,intent].forEach(x => x.addEventListener('input',invalidate));
      dialog.append(el('h3', '', tr('Continue from this object', '从这个对象继续研究')), field(tr('Research intent', '研究意图'), intent), field(tr('Your mathematical question', '你的数学问题'), question), field(tr('Exact statement to investigate', '准备研究的精确命题'), statement),field(tr('Parameter / object domain','参数／对象范围'),domain),field(tr('Assumptions (one per line)','假设（每行一项）'),assumptions), scopeEditor, field(tr('Maximum execution time (seconds)', '执行时间上限（秒）'), allowance));
      dialog.append(button(tr('Preview target, scope and allowance', '预览目标、作用范围与资源'), async () => {
        try {
          const currentForm = formVersion;
          const req = {target:obj.target,intent:intent.value,user_question:question.value,requested_statement:statement.value,requested_scope:JSON.parse(scope.value),budget:{...H.capability.maximum,wall_ms:Number(allowance.value)*1000}};
          const v = await call('/api/human/preview',req,project); if (currentForm !== formVersion || project !== state.project || version !== H.dialogVersion) return; H.preview = {...v,key:requestId()};
          const scopeList=el('ul');v.request.requested_scope.assumptions.forEach(a=>scopeList.append(el('li','',a)));
          preview.replaceChildren(el('h3','',tr('Review before creating work','创建任务前核对')),el('p','',v.request.requested_statement),el('strong','',v.request.requested_scope.parameter_domain),scopeList,el('p','',tr('Execution allowance: ','执行额度：')+(v.request.budget.wall_ms/1000)+tr(' seconds; at most ',' 秒；最多 ')+v.request.budget.output_tokens+tr(' output tokens reserved.',' 输出 token 预约。')),details(tr('Exact scope and resource record','精确范围与资源记录'),{scope:v.request.requested_scope,budget:v.request.budget}),el('p','',tr('Creates one independent task. Explicit start follows; a paused parent remains paused.','创建一个独立任务，之后需明确启动；已暂停的原项目保持暂停。')));confirm.hidden=false;errorBox.textContent='';
        } catch(e) { invalidate(); errorBox.textContent=e.message; }
      }),preview,confirm);
      const previous = await call('/api/human/interventions?object_id='+encodeURIComponent(id));
      if (project === state.project && version === H.dialogVersion && dialog.open && previous.length) { dialog.append(el('h3','',tr('Earlier questions on this object','对此对象的已有追问'))); previous.forEach(i=>dialog.append(button(i.request.user_question,()=>showInvestigation(i.intervention_id)))); }
    } catch(e) {
      if (project !== state.project || version !== H.dialogVersion || !dialog.open) return;
      const unbound = e.message.includes('no canonical backend identity') || e.message.includes('Historical/read-only project has no native control identity');
      // Replace the loading view on failure; a read request never starts work.
      modal(unbound ? tr('This record is read-only here', '这条记录在此只读') : tr('Unable to load this object', '暂时无法读取这个对象'));
      if (unbound) {
        dialog.append(el('p', 'human-readonly', tr(
          'This displayed record has no linked native research object, so no single task can start from it. You can read its proof and dependencies. This click has not started any work.',
          '这个展示节点没有关联原生研究对象，因此不能从这里发起单项任务；你可以阅读它的证明与依赖。此次点击没有启动任何工作。')));
        const layer = kind === 'fact' ? 'facts' : 'research', actions = el('div', 'human-action-row');
        actions.append(button(tr('Read the proof and dependencies', '查看证明与依赖'), () => { dialog.close(); openReader(id, layer); }, 'button primary'));
        if (window.EulerMilestones) actions.append(button(tr('Ask from a milestone', '从里程碑追问'), () => { dialog.close(); window.EulerMilestones.open({ id, kind, layer }); }));
        dialog.append(actions, el('p', 'muted', tr(
          'A milestone restores into a separate workspace. If it saved the team’s conversations, the members can be resumed there; file-only milestones cannot recreate them.',
          '里程碑会恢复为独立工作区。如果它保存了团队会话，可在那里接续成员对话；仅含文件的里程碑无法重建会话。')));
      } else {
        errorBox.textContent = tr('The object could not be loaded. No task was started.', '对象读取失败，此次操作没有启动任务。');
        dialog.append(button(tr('Retry', '重试'), () => inspect(kind,id)));
      }
      dialog.append(details(tr('Technical details', '技术详情'), e.message));
    }
  }
  // Ways to continue from a selected step. Reading stays primary in app.js; these are optional.
  let actionCount = 0;
  const historical = () => !!state.architecture?.historical_case || state.graph?.projection === 'historical_source';
  const nativeless = () => H.project === state.project && H.capability?.native_identity === false;
  function option(action, iconName) {
    const key = 'node-action-' + (++actionCount), b = el('button', 'node-option'); b.type = 'button'; b.dataset.nodeAction = action;
    const mark = el('span', 'node-option-icon'); mark.append(icon(iconName));
    const title = el('strong', 'node-option-title'), desc = el('span', 'node-option-desc'), meta = el('span', 'node-option-meta');
    title.id = key + '-title'; desc.id = key + '-desc'; meta.id = key + '-meta';
    const body = el('span', 'node-option-body'); body.append(title, desc, meta); b.append(mark, body, icon('arrow'));
    b.setAttribute('aria-labelledby', title.id); b.setAttribute('aria-describedby', desc.id + ' ' + meta.id);
    return b;
  }
  function describe(b, title, desc, status) {
    b.querySelector('.node-option-title').textContent = title;
    b.querySelector('.node-option-desc').textContent = status.desc || desc;
    const meta = b.querySelector('.node-option-meta'); meta.textContent = status.meta || ''; meta.dataset.tone = status.tone || '';
    if (status.off) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
  }
  function teamStatus() {
    const s = window.EulerTeam?.status();
    if (!s || s.project !== state.project) return { meta: tr('Checking the team…', '正在读取团队…'), tone: 'wait' };
    if (s.error) return { meta: tr('Team status unavailable · open to retry', '团队状态暂不可读 · 可打开重试'), tone: 'warn' };
    const d = s.data;
    if (!d.available) return { off: true, desc: tr('This workspace saved no team conversations.', '此工作区没有保存团队会话。') };
    const members = tr(`${d.members.length} members`, `${d.members.length} 位成员`);
    if (!d.complete_native_recovery) return { meta: tr('Sessions need recovery first', '需先恢复成员会话') + ' · ' + members, tone: 'warn' };
    return { meta: window.EulerTeam.stateName(d.state) + ' · ' + members + (d.can_launch ? '' : tr(' · view only', ' · 仅查看')), tone: d.state === 'READY' ? 'ok' : '' };
  }
  function milestoneStatus() {
    const s = window.EulerMilestones?.status();
    if (!s || s.project !== state.project) return { meta: tr('Checking milestones…', '正在读取里程碑…'), tone: 'wait' };
    if (s.error) return { meta: tr('Milestones unavailable · open to retry', '里程碑暂不可读 · 可打开重试'), tone: 'warn' };
    const d = s.data, n = d.items.length, team = d.items.filter(m => m.team?.complete).length;
    if (!d.enabled) return { off: true, desc: tr('Milestones are not enabled for this project.', '此项目未启用里程碑。') };
    if (!n) return { meta: tr('No milestone saved yet', '尚未保存里程碑') };
    const kind = team === n ? tr('with team conversations', '含团队会话') : team ? tr(`${team} with team conversations`, `${team} 个含团队会话`) : tr('files only', '仅文件');
    return { meta: tr(n === 1 ? '1 milestone' : `${n} milestones`, `${n} 个里程碑`) + ' · ' + kind + (d.can_restore ? '' : tr(' · browse only', ' · 仅浏览')) };
  }
  function paint(section) {
    const { item, team, milestone, minor } = section._actions;
    describe(team, tr('Ask the current team', '向当前团队追问'), tr('Open Team interaction with this step attached; nothing runs until you send.', '带着这一步打开团队交互；发送前不会运行。'), teamStatus());
    describe(milestone, tr('Ask from a milestone', '从里程碑追问'), tr('Restore a saved point as a separate workspace, then ask there.', '将保存的时间点恢复为独立工作区，再在其中追问。'), milestoneStatus());
    const unbound = item.id.startsWith('history:') || historical() ? 'historical' : nativeless() ? 'nativeless' : '';
    const single = H.project === state.project && !!H.capability?.enabled, signature = [state.lang, unbound, single].join();
    // Status refreshes must not replace a focused control.
    if (minor.dataset.signature === signature) return;
    minor.dataset.signature = signature; minor.replaceChildren(); minor.classList.toggle('is-note', !!unbound);
    if (unbound) { minor.append(icon('info'), el('span', '', unbound === 'historical' ? tr('Historical record · read-only here; no single task', '历史记录 · 此处只读，不能发起单项任务') : tr('No native research object · read-only here', '没有原生研究对象 · 此处只读'))); return; }
    const kind = item.kind === 'failure' ? 'blocker' : item.kind || 'obligation';
    const b = button(single ? tr('Inspect object · single task', '查看对象 · 单项任务') : tr('Inspect object record', '查看对象记录'), () => { if (section.dataset.project === state.project) inspect(kind, item.id); }, 'human-object-button');
    b.prepend(icon('search')); b.dataset.objectId = item.id;
    minor.append(b, el('small', '', single ? tr('One bounded task, separate from the team', '独立于团队的单项受限任务') : tr('Version, scope and evidence', '版本、范围与证据')));
  }
  const repaint = () => document.querySelectorAll('.node-continue').forEach(s => { if (s._actions && s.dataset.project === state.project) try { paint(s); } catch (e) { console.error('Euler node actions unavailable', e); } });
  document.addEventListener('euler:workbench-status', repaint);
  function mount(container, item, { layer = item?.layer || 'research' } = {}) {
    if (!item?.id) return;
    const target = { id: item.id, kind: item.kind, layer, title: window.titleOf ? titleOf(item).text : item.id };
    const section = el('section', 'node-continue'); section.dataset.nodeId = item.id; section.dataset.project = state.project;
    const heading = el('h4', 'node-continue-title', tr('Continue from this step', '从这一步继续研究')); heading.id = 'node-continue-' + (++actionCount);
    section.setAttribute('aria-labelledby', heading.id);
    const team = option('team', 'chat'), milestone = option('milestone', 'history'), minor = el('div', 'node-minor');
    const run = (b, action) => { b.onclick = () => { if (section.dataset.project === state.project && b.getAttribute('aria-disabled') !== 'true') action(); }; };
    run(team, () => window.EulerTeam?.open(target)); run(milestone, () => window.EulerMilestones?.open(target));
    const options = el('div', 'node-options'); options.append(team, milestone);
    section.append(heading, options, minor); section._actions = { item, team, milestone, minor };
    // The optional workbench must never prevent the proof itself from rendering.
    try { paint(section); container.append(section); } catch (e) { console.error('Euler node actions unavailable', e); }
  }
  window.EulerHuman = { update, mount, inspect, showInvestigation };
  new MutationObserver(() => update()).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
  setTimeout(update, 0);
})();
