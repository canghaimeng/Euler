/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
/* Copyright 2026 Qiuyu Chen (canghaimeng). */
/* Git milestones keep the complete graph and team workspace together. */
(() => {
  let current = null, generation = 0, dialogVersion = 0;
  const tr = (en, zh) => state.lang === 'zh' ? zh : en;
  const make = (tag, cls, text) => node(tag, cls || '', text || '');
  const id = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2,'0')).join('');
  const button = (text, fn, cls='button secondary') => { const b=make('button',cls,text);b.type='button';b.onclick=fn;return b; };
  const when = value => { const d=new Date(value); return Number.isNaN(d.getTime()) ? String(value) : fmtDateTime(d); };
  const note = (el,text,tone='') => { el.textContent=text; el.dataset.tone=tone; };
  const picker = make('button','button secondary masthead-action'); picker.type='button'; picker.id='milestones-button'; picker.onclick=()=>open();
  document.querySelector('.project-picker').after(picker);
  const banner = make('section','milestone-banner'); banner.id='milestone-banner';banner.hidden=true;
  document.querySelector('#main').prepend(banner);
  const dialog=make('dialog','human-dialog');dialog.id='milestone-dialog';document.body.append(dialog);
  dialog.addEventListener('close',()=>{if(!dialog.open)dialogVersion++;});
  async function call(path,body,project=state.project){
    const u=new URL(path,location.origin);if(project)u.searchParams.set('project',project);
    const r=await fetch(u,{method:body===undefined?'GET':'POST',cache:'no-store',headers:{
      ...(state.token?{Authorization:'Bearer '+state.token}:{}),...(body===undefined?{}:{'Content-Type':'application/json'})},
      ...(body===undefined?{}:{body:JSON.stringify(body)})});
    const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);return d;
  }
  function field(label,input){const wrap=make('label','human-field');wrap.append(make('span','',label),input);return wrap;}
  function label(count){picker.replaceChildren(make('span','label-long',tr('Research milestones','研究里程碑')),make('span','label-short',tr('Milestones','里程碑')));if(count)picker.append(make('span','','\u00a0· '+count));}
  function objectTitle(objectId){const n=state.graph?.nodes?.find(x=>x.id===objectId)||state.architecture?.nodes?.find(x=>x.id===objectId);return n&&window.titleOf?titleOf(n).text:objectId;}
  async function update(){
    const serial=++generation,project=state.project;
    if(current?.project!==project){banner.hidden=true;delete banner.dataset.signature;}
    label(current?.project===project?current.data?.items.length:0);
    try{const d=await call('/api/milestones');if(serial!==generation||project!==state.project)return;current={project,data:d};label(d.items.length);renderBanner(d.restored_from);}
    catch(e){if(serial!==generation||project!==state.project)return;current={project,error:e.message};label(0);}
    document.dispatchEvent(new Event('euler:workbench-status'));
  }
  function renderBanner(s){
    banner.hidden=!s;if(!s)return;
    // Periodic refreshes keep the same buttons so keyboard focus is not lost.
    const signature=JSON.stringify([state.lang,s]);if(banner.dataset.signature===signature)return;banner.dataset.signature=signature;
    const objectId=s.request.object_id,selected=objectId?{id:objectId,kind:s.selected_object_kind,layer:s.request.layer,title:objectTitle(objectId)}:null;
    const mark=make('span','milestone-banner-mark');mark.append(icon('history'));
    const body=make('div','milestone-banner-body'),title=make('p','milestone-banner-title');
    title.append(make('strong','',tr('Restored workspace','已恢复的工作区')),make('span','',' · '+s.source_label+' · '+s.source_commit.slice(0,8)+' · '+when(s.created_at)));
    body.append(title,make('p','muted',s.team?.available?tr('Files and team conversations from this milestone were restored here as an independent workspace; the original campaign is unchanged.','此里程碑的文件与团队会话已恢复到这个独立工作区；原项目保持不变。'):tr('Files from this milestone were restored here as an independent workspace. It saved no team conversations, so none can be resumed; the original campaign is unchanged.','此里程碑的文件已恢复到这个独立工作区。它没有保存团队会话，因此无法接续；原项目保持不变。')));
    if(s.request.question)body.append(make('p','milestone-question',tr('Your question: ','你的追问：')+s.request.question));
    const actions=make('div','milestone-banner-actions');
    if(s.team?.available)actions.append(button(tr('Continue in Team interaction','在团队交互中继续'),()=>window.EulerTeam?.open()));
    if(selected&&!objectId.startsWith('history:'))actions.append(button(tr('Review this object','查看此对象'),()=>window.EulerHuman?.inspect(s.selected_object_kind||(s.request.layer==='facts'?'fact':'obligation'),objectId)));
    banner.replaceChildren(mark,body);if(actions.childElementCount)banner.append(actions);
  }
  async function open(selected=null){
    const project=state.project,version=++dialogVersion;dialog.replaceChildren();
    const head=make('header','human-dialog-head');head.append(make('h2','',tr('Continue from a research milestone','从研究里程碑继续')),button(tr('Close','关闭'),()=>dialog.close()));dialog.append(head);
    const error=make('p','human-error');error.setAttribute('role','alert');const loading=make('p','muted',tr('Reading saved milestones…','正在读取已保存的里程碑…'));dialog.append(error,loading);if(!dialog.open)dialog.showModal();
    try{
      const d=await call('/api/milestones');if(project!==state.project||version!==dialogVersion||!dialog.open)return;loading.remove();
      dialog.append(make('p','',tr('A milestone saves the team’s files, proof dependencies, tasks and evidence together in a local Git commit. Restoring creates a separate workspace with the full graph; this workspace stays as it is.','里程碑用本地 Git 一起保存团队文件、证明依赖、任务和证据；恢复会建立独立工作区并保留完整关系图，当前工作区保持不变。')));
      if(selected){const about=make('p','milestone-about',tr('Selected proof object: ','选定证明对象：'));about.append(src('span','',selected.title||selected.id));dialog.append(about);}
      if(!d.items.length)dialog.append(make('p','notice',tr('No recorded Git milestones yet. Earlier research dates cannot be reconstructed from a later snapshot.','尚未保存 Git 里程碑。不能用今天的快照冒充过去某一时刻的完整状态。')));
      if(d.items.length){
        const list=make('select');list.id='milestone-select';for(const m of d.items)list.append(new Option(when(m.created_at)+' · '+m.label+' · '+m.commit.slice(0,8)+(m.team?.complete?tr(' · team state',' · 含团队状态'):tr(' · files only',' · 仅文件')),m.commit));
        const summary=make('p','muted'),team=make('p','milestone-team');let key=id();
        const describe=()=>{const m=d.items.find(x=>x.commit===list.value);const g=m.graph_counts.research;summary.textContent=tr(`${m.file_count} files · ${g?.nodes||0} research nodes · ${g?.edges||0} edges · ${m.excluded_count} excluded entries (credentials, processes and dependency caches)`,`${m.file_count} 个文件 · ${g?.nodes||0} 个研究节点 · ${g?.edges||0} 条边 · ${m.excluded_count} 项凭证、进程或依赖缓存排除项`);
          note(team,m.team?.complete?tr('Includes the members’ native conversations. The new branch opens in Team interaction, where you recover its member sessions and continue; your question waits there unsent.','包含成员的原生会话。新分支会在“团队交互”中打开：先恢复成员会话再继续，你的问题会预先填好但不会自动发送。'):tr('Files only: this milestone saved no team conversations, so restoring cannot resume the members.','仅文件：这个里程碑没有保存团队会话，恢复后无法接续成员对话。'),m.team?.complete?'ok':'');};
        list.onchange=()=>{key=id();describe();};describe();
        dialog.append(field(tr('Milestone','里程碑'),list),summary,team);
        const q=make('textarea');q.id='milestone-question';q.rows=4;q.maxLength=12000;dialog.append(field(tr('Question to investigate from this point (optional)','从这个时间点发起的追问（可选）'),q));
        if(d.can_restore){
          const status=make('p','milestone-status');status.setAttribute('role','status');const row=make('div','human-action-row');
          const submit=button(tr('Restore full workspace and prepare question','恢复完整工作区并准备追问'),async()=>{submit.disabled=true;note(status,tr('Restoring and checking file hashes…','正在恢复并核对文件哈希…'));
            try{const r=await call('/api/milestones/'+list.value+'/restore',{idempotency_key:key,question:q.value,object_id:selected?.id||null,layer:selected?.layer||(selected?.kind==='fact'?'facts':'research')},project);note(status,tr('Restored. Opening the new branch in Team interaction…','已恢复，正在团队交互中打开新分支…'),'ok');
              const next=new URL(r.url,location.origin);if(!next.hash)next.hash='team';location.assign(next.pathname+next.search+next.hash);}
            catch(e){note(status,e.message,'error');submit.disabled=false;}},'button primary');
          row.append(submit,status);dialog.append(row);
        } else dialog.append(make('p','human-readonly',tr('Restoring is not enabled in this deployment; milestones can only be browsed.','当前部署未启用恢复，只能浏览里程碑。')));
      }
      if(d.can_capture){
        const captureKey=id();let saving=false;const section=make('details','human-record');section.append(make('summary','',tr('Save the current team state','保存当前团队状态')));
        const name=make('input');name.id='milestone-label';name.placeholder=tr('e.g. Draft after peer review','例如：同行审阅后的草稿');
        const status=make('p','milestone-status');status.setAttribute('role','status');
        const save=button(tr('Create Git milestone','创建 Git 里程碑'),async()=>{if(saving)return;saving=true;save.disabled=true;note(status,tr('Saving; the team must be idle…','正在保存；团队必须处于静止状态…'));
          try{await call('/api/milestones',{label:name.value,idempotency_key:captureKey},project);await update();open(selected);}catch(e){note(status,e.message,'error');}finally{saving=false;save.disabled=false;}});
        section.append(make('p','muted',tr('Save a named checkpoint before changing direction or asking a new question. This records the configured project files, proof graph and any registered team conversations; it does not launch research or publish to GitHub.','在转向新路线或提出新问题前保存一个具名检查点。它记录配置范围内的项目文件、证明图和已注册的团队会话，不会启动研究或发布到 GitHub。')),field(tr('Milestone label','里程碑名称'),name),make('p','muted',tr('The team must be idle. An active lease or files changing during capture cause a retry request.','团队必须处于静止边界；有活动租约或保存时文件仍变化，系统会要求稍后重试。')));
        const row=make('div','human-action-row');row.append(save,status);section.append(row);dialog.append(section);
      }
    }catch(e){if(project!==state.project||version!==dialogVersion||!dialog.open)return;loading.remove();error.textContent=e.message;dialog.append(button(tr('Try again','重试'),()=>open(selected)));}
  }
  window.EulerMilestones={update,open,status:()=>current};setTimeout(update,0);
})();
