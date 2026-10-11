/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
/* Copyright 2026 Qiuyu Chen (canghaimeng). */
(() => {
  const tr=(en,zh)=>state.lang==='zh'?zh:en;
  const make=(tag,cls,text)=>node(tag,cls||'',text||'');
  const uid=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  const names={codex:'Codex','claude-code':'Claude Code',gemini:'Gemini CLI',grok:'Grok',cursor:'Cursor Agent'};
  const launch=make('button','button secondary masthead-action');launch.id='create-project-button';launch.type='button';
  document.querySelector('#milestones-button').after(launch);
  function label(){launch.replaceChildren(make('span','creation-plus','＋'),make('span','',tr('Create project','创建项目')))}
  label();new MutationObserver(label).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  const dialog=make('dialog','human-dialog creation-dialog');dialog.id='creation-dialog';dialog.setAttribute('aria-labelledby','creation-title');document.body.append(dialog);
  let generation=0;
  dialog.addEventListener('close',()=>generation++);
  async function call(path,body){
    const r=await fetch(path,{method:body===undefined?'GET':'POST',cache:'no-store',
      headers:{...(state.token?{Authorization:'Bearer '+state.token}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:tr('The request failed.','请求未成功。'));return d;
  }
  function field(label,input){const box=make('label','human-field');box.append(make('span','',label),input);return box;}
  function textInput(id,placeholder,max=120){const input=make('input');input.id=id;input.placeholder=placeholder;input.maxLength=max;input.required=true;return input;}
  async function open(mode,project=null){
    const ticket=++generation;dialog.replaceChildren();
    const title=tr(mode==='project'?'Start a research project':'Add a team member',mode==='project'?'开始一个研究项目':'添加团队成员');
    const header=make('header','human-dialog-head'),close=make('button','text-button',tr('Close','关闭'));close.type='button';close.onclick=()=>dialog.close();
    const heading=make('h2','',title);heading.id='creation-title';header.append(heading,close);dialog.append(header);
    dialog.append(make('p','creation-intro',mode==='project'
      ?tr('Name your project, choose a lead, and tell it what you want to investigate. Its reply will appear in the new project’s Team interaction.','给项目命名，选择主 Agent，再告诉它你想研究什么。创建后会进入新项目的团队交互，直接看到回复。')
      :tr('This member joins the currently selected project branch. You and the lead can send it work; its conversations will be saved with the team.','新成员将加入当前项目的这个分支。你和主 Agent 都可以向它分配工作，对话会随团队一起保存。')));
    const error=make('p','human-error');error.setAttribute('role','alert');dialog.append(error);
    const loading=make('p','muted',tr('Reading available CLIs…','正在读取可用 CLI…'));dialog.append(loading);dialog.showModal();
    let caps;
    try{caps=await call('/api/projects/create-capabilities')}catch(e){if(ticket===generation){loading.remove();error.textContent=e.message}return}
    if(ticket!==generation||!dialog.open)return;loading.remove();
    if(mode==='project'&&!caps.enabled){error.textContent=tr('Project creation has not been enabled for this workbench. See the setup guide.','当前工作台尚未启用页面创建项目，请按使用指南配置工作区。');return}
    const form=make('form','creation-form');
    const name=textInput('creation-name',tr(mode==='project'?'e.g. A graph theory question':'e.g. Proof reviewer',mode==='project'?'例如：图论研究':'例如：证明审查助手'));
    form.append(field(tr(mode==='project'?'Project name':'Member name',mode==='project'?'项目名称':'成员名称'),name));
    const row=make('div','creation-grid'),provider=make('select');provider.id='creation-provider';
    const offered=(caps.providers||[]).filter(p=>p.enabled!==false);
    for(const p of offered){const option=new Option((names[p.id]||p.id)+(p.available?'':tr(' · not installed',' · 未安装')),p.id);option.disabled=!p.available;provider.append(option)}
    const first=offered.find(p=>p.available);if(first)provider.value=first.id;
    row.append(field(tr(mode==='project'?'Lead agent':'Agent CLI',mode==='project'?'主 Agent':'Agent CLI'),provider));
    const model=textInput('creation-model',tr('Model available to your account','填写账户可用的模型名'),180);row.append(field(tr('Model','模型'),model));form.append(row);
    const options=make('div','creation-grid'),effort=make('select');effort.id='creation-effort';
    const role=make('select');role.id='creation-role';role.append(new Option(tr('Assistant','助手'),'assistant'),new Option(tr('Reviewer','审查员'),'verifier'));
    const target=make('select'); target.id='creation-target';
    for(const [value,en,zh] of [['PROOF','Proof','证明'],['COUNTEREXAMPLE','Counterexample','寻找反例'],['CLASSIFICATION','Classification','分类研究'],['FORMALIZATION','Formalization','形式化']])target.append(new Option(tr(en,zh),value));
    options.append(field(tr('Reasoning effort','推理强度'),effort));options.append(mode==='member'?field(tr('Role','分工角色'),role):field(tr('Research goal','研究目标'),target));form.append(options);
    function providerChanged(){const p=offered.find(x=>x.id===provider.value);model.value=p?.model||'';effort.replaceChildren();const values=['gemini','cursor'].includes(provider.value)?['model-defined']:provider.value==='claude-code'?['low','medium','high','max']:['low','medium','high','xhigh','ultra'];for(const value of values)effort.append(new Option(value==='model-defined'?tr('Set by the selected model','由所选模型决定'):value,value));effort.value=values.includes(p?.effort)?p.effort:values.includes('high')?'high':values[0]}
    provider.onchange=providerChanged;providerChanged();
    const question=make('textarea');question.id='creation-question';question.rows=mode==='project'?5:3;question.maxLength=mode==='project'?16000:4000;question.required=true;
    question.placeholder=mode==='project'?tr('Describe the question, known results, or the direction you want to explore…','描述你想研究的问题、已有结果，或希望探索的方向……'):tr('What should this member help with?','希望这位成员负责什么？');
    form.append(field(tr(mode==='project'?'Your first message':'Assignment',mode==='project'?'给主 Agent 的第一条消息':'具体分工'),question));
    const footer=make('div','creation-footer'),note=make('p','muted',mode==='project'?tr('Creating starts the first conversation using the CLI account configured on this machine.','创建后将使用本机已配置的 CLI 账户开始首轮对话。'):tr('The member’s native session connects when it receives its first task.','成员收到第一条消息或任务时会自动连接原生会话。'));
    const submit=make('button','button primary',tr(mode==='project'?'Create and start conversation':'Add member',mode==='project'?'创建并开始对话':'添加成员'));submit.type='submit';submit.disabled=!first;footer.append(note,submit);form.append(footer);dialog.append(form);name.focus();
    let requestKey=uid(),lastPayload=null;
    form.onsubmit=async event=>{
      event.preventDefault();if(!form.reportValidity())return;
      if(mode==='member'&&project!==state.project){error.textContent=tr('The selected project changed. Reopen Add member in the intended branch.','当前项目已切换，请在目标分支重新打开添加成员。');return}
      const member={provider:provider.value,model:model.value.trim(),effort:effort.value};
      const payload=mode==='project'?{name:name.value.trim(),question:question.value.trim(),lead:member,target_kind:target.value}:{member:{...member,name:name.value.trim(),role:role.value,assignment:question.value.trim()}};
      const serialized=JSON.stringify(payload);if(lastPayload&&lastPayload!==serialized)requestKey=uid();lastPayload=serialized;
      submit.disabled=true;error.textContent='';submit.textContent=tr('Creating…','正在创建…');
      try{
        const result=await call(mode==='project'?'/api/projects':'/api/team/members?project='+encodeURIComponent(project),{...payload,idempotency_key:requestKey});
        if(ticket!==generation||!dialog.open){if(mode==='member')window.EulerTeam?.update(project);return}
        dialog.close();
        if(mode==='project'){const u=new URL(result.url,location.origin);u.searchParams.set('lang',state.lang);location.assign(u.href)}
        else await window.EulerTeam?.focusMember(project,result.id);
      }catch(e){if(ticket===generation&&dialog.open){error.textContent=e.message;submit.disabled=false;submit.textContent=tr('Retry','重试')}}
    };
  }
  launch.onclick=()=>open('project');
  window.EulerCreation={openProject:()=>open('project'),openMember:p=>open('member',p)};
  window.EulerTeam?.render();
})();
