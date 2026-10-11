# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Native CLI sessions. Never synthesize replacement conversations."""
from __future__ import annotations
from contextlib import contextmanager
import json,os,shutil,sys,time,uuid,re
import tempfile
from pathlib import Path
from urllib.parse import quote
from .session_rpc import SessionRpc,RpcError
from .team import TeamError,TEAM,quota_threshold
from .milestones import _json


def _home(project,member):
    root=Path(project).resolve()
    home=root/'.euler-provider'/member['id']
    if not home.resolve().is_relative_to(root):raise TeamError('provider home escapes the current workspace')
    return home


def team_environment(project, member, home, state=None):
    root=Path(project).resolve()
    if not home.resolve().is_relative_to(root) or home.is_symlink():
        raise TeamError('provider home escapes the current workspace')
    for relative in ['tmp','cache','node-cache','claude/.credentials.json','cursor/acp-sessions',
                     'config/cursor/auth.json','.gemini/oauth_creds.json','cursor-data']:
        if not (home/relative).resolve().is_relative_to(home.resolve()):
            raise TeamError('provider runtime path redirects outside its private directory')
    temp=home/'tmp';temp.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy()
    from euler.providers.base import SECRET_KEYS_BY_PROVIDER, CONSULT_SECRET_KEYS
    keys={key for values in SECRET_KEYS_BY_PROVIDER.values() for key in values}
    keys.update(CONSULT_SECRET_KEYS)
    keys.update(['EULER_CONTROL_TOKEN','EULER_HUMAN_CONFIG','EULER_CONTROL_ACTOR',
                 'AZURE_OPENAI_KEY','AZURE_OPENAI_API_KEY','GOOGLE_API_KEY','GEMINI_API_KEY'])
    if member['provider']=='codex' and quota_threshold(state or {}) is None:
        keys.difference_update({'OPENAI_API_KEY','CODEX_API_KEY'})
    if member['provider']!='claude-code':keys.add('CLAUDE_CODE_OAUTH_TOKEN')
    for key in keys:env.pop(key,None)
    env.update(TMPDIR=str(temp),XDG_CACHE_HOME=str(home/'cache'),NODE_COMPILE_CACHE=str(home/'node-cache'),
               EULER_TEAM_PROJECT=str(root),EULER_TEAM_MEMBER=member['id'])
    env['PYTHONPATH']=os.pathsep.join(filter(None,[str(Path(__file__).resolve().parents[2]),env.get('PYTHONPATH','')]))
    return env


def _mcp(project,member):
    env={'EULER_TEAM_PROJECT':str(project),'EULER_TEAM_MEMBER':member['id'],
         'PYTHONPATH':str(Path(__file__).resolve().parents[2])}
    return {'command':sys.executable,'args':['-m','euler.workspace.team_mcp'],'env':env}


def _instructions(project,member,state):
    return (f"You are {member['id']}, the {member['role']} in this persistent Euler team. "
        f"The project workspace is {project}. Team objective: {state['objective']}. "
        "Preserve the mathematical scope, evidence gates and original proof dependencies. "
        "Your prior native conversation belongs to this member; it has not been summarized by another agent. "
        "Use the euler_team tools to consult the other restored team members. "
        "The lead may use euler_team_create_member to add a registered teammate with an explicit provider, model, effort and assignment, then consult it through euler_team_ask. "
        "Team replies are research discussion, not admitted Facts; verified results must still use the normal Euler truth gateway. "
        "The old campaign's paths are unavailable. All files and assignments to use are in this workspace. "
        "Do not launch independent model CLIs, spawn replacement agents or access the original campaign. "
        "Keep user-visible replies concise; write proof/code artifacts inside this workspace when useful.")


def isolation_policy(project,state):
    root=Path(project).resolve()
    if state.get('source'):
        # This manifest sits outside every writable captured root. A resumed
        # agent cannot broaden its grants by editing its project configuration.
        manifest_path=root.parents[1]/'manifest.json'
        manifest=json.loads(manifest_path.read_text())
        base=root.parents[1]
        writable=[str((base/'files'/alias).resolve()) for alias in manifest['roots']]
        archived=json.loads((base/'team/state.json').read_text())
        hidden=sorted(set(list(manifest['roots'].values())+(archived.get('source') or {}).get('roots',[])))
    else:
        writable=[str(root)];hidden=[]
    return {'writable':writable,'hidden':hidden}


@contextmanager
def runtime_launch(project,member,state,home,env,command):
    # Unix-domain sockets have short pathname limits. Cursor otherwise falls
    # back to a shared /tmp/.cursor path when restored workspace paths are long.
    # Give each process a short alias to its OWN persistent data and private tmp.
    with tempfile.TemporaryDirectory(prefix='euler-cli-',dir='/tmp') as temporary:
        temp=Path(temporary).resolve()
        env=dict(env);env['TMPDIR']=str(temp)
        if member['provider']=='cursor':
            data=home/'cursor-data';data.mkdir(exist_ok=True)
            (temp/'data').symlink_to(data,target_is_directory=True)
            env['CURSOR_DATA_DIR']=str(temp/'data')
        policy=isolation_policy(project,state)
        policy['writable'].append(str(temp))
        isolation=home/'isolation.json';_json(isolation,policy)
        yield [sys.executable,'-m','euler.workspace.isolation','--policy',str(isolation),'--',*command],env


@contextmanager
def connection(project,member,state,callback=None):
    root=Path(project).resolve();home=_home(root,member)
    try:home.resolve().relative_to(root)
    except ValueError:raise TeamError('provider home escapes the current workspace') from None
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    env=team_environment(root,member,home,state)
    mcp=_mcp(root,member)
    auth=None
    if member['provider']=='codex':
        auth=Path(os.environ.get('EULER_TEAM_CODEX_AUTH',str(Path.home()/'.codex/auth.json')))
        env['CODEX_HOME']=str(home)
        binary=os.environ.get('EULER_TEAM_CODEX_BIN') or shutil.which('codex')
        config={'mcp_servers':{'euler_team':mcp},'model_reasoning_effort':member['effort'],
                'features.goals':True,'features.multi_agent':False,'features.multi_agent_v2':False}
        if quota_threshold(state) is not None:config['forced_login_method']='chatgpt'
        command=[binary,'app-server','--stdio']
    elif member['provider']=='grok':
        auth=Path(os.environ.get('EULER_TEAM_GROK_AUTH',str(Path.home()/'.grok/auth.json')))
        env['GROK_HOME']=str(home)
        binary=os.environ.get('EULER_TEAM_GROK_BIN') or shutil.which('grok')
        command=[binary,'--no-subagents','agent','--no-leader','--model',member['model'],'--reasoning-effort',member['effort'],'--always-approve','stdio']
        config={}
    elif member['provider'] in {'gemini','cursor'}:
        from .team_acp import configure
        command=configure(root,member,home,env);binary=command[0];config={}
    else:raise TeamError('provider has no RPC adapter')
    if not binary:raise TeamError('native provider binary unavailable')
    if home.is_symlink() or (home/'auth.json').is_symlink():raise TeamError('provider runtime home must not redirect to another workspace')
    if auth and auth.is_file() and (not (home/'auth.json').exists() or auth.stat().st_mtime_ns>(home/'auth.json').stat().st_mtime_ns):
        # Current credentials are provisioned only into ignored runtime homes.
        # They never enter the Git checkpoint or the browser response.
        shutil.copyfile(auth,home/'auth.json');(home/'auth.json').chmod(0o600)
    with runtime_launch(root,member,state,home,env,command) as (wrapped,child_env), SessionRpc(wrapped,cwd=str(root),env=child_env,callback=callback) as rpc:
        if member['provider']=='codex':
            rpc.request('initialize',{'clientInfo':{'name':'euler_team','version':'0.3'},'capabilities':{'experimentalApi':True}})
            rpc.notify('initialized')
        elif member['provider']=='grok':
            rpc.request('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'euler_team','version':'0.3'}})
            rpc.request('authenticate',{'methodId':'grok.com'})
        else:
            from .team_acp import authenticate
            authenticate(rpc,member['provider'])
        try:
            yield rpc,home,config
        finally:
            _json(home/'transport-counts.json',rpc.counts)


def require_quota(rpc, stop_percent):
    try:
        account=rpc.request('account/read',{'refreshToken':False})
        data=rpc.request('account/rateLimits/read')
    except Exception:raise TeamError('QUOTA: current included-usage permission is unavailable') from None
    if (account.get('account') or {}).get('type')!='chatgpt':raise TeamError('QUOTA: ChatGPT included-account login required; no API-key fallback')
    if data.get('ordinaryUsageAllowed') is not True:raise TeamError('QUOTA: ordinary included usage not confirmed')
    left=[]
    for bucket in [data.get('rateLimits'),*(data.get('rateLimitsByLimitId') or {}).values()]:
        if not bucket:continue
        if bucket.get('spendControlReached') or bucket.get('rateLimitReachedType'):raise TeamError('QUOTA: account limit reached')
        for key in ['primary','secondary']:
            window=bucket.get(key)
            if window:
                used=window.get('usedPercent')
                if not isinstance(used,(int,float)) or not 0<=used<=100:raise TeamError('QUOTA: unknown percentage')
                left.append(100-used)
        individual=bucket.get('individualLimit')
        if individual and isinstance(individual.get('remainingPercent'),(int,float)):left.append(individual['remainingPercent'])
    if not left or min(left)<=stop_percent:raise TeamError(f'QUOTA: remaining allowance is at or below {stop_percent}%, or unknown')
    return min(left)


def _codex_params(project,member,state,config):
    return {'cwd':str(Path(project)/member['workdir']),'runtimeWorkspaceRoots':[str(project)],
        'model':member['model'],'modelProvider':'openai','serviceTier':'default',
        'approvalPolicy':'never','sandbox':'danger-full-access','config':config,
        'developerInstructions':_instructions(project,member,state)}


def _grok_mcp(project,member):
    m=_mcp(project,member)
    return [{'name':'euler_team','command':m['command'],'args':m['args'],
             'env':[{'name':k,'value':v} for k,v in m['env'].items()]}]


def _grok_options(rpc,sid,member,loaded=None):
    actual={x['id']:x.get('currentValue') for x in (loaded or {}).get('configOptions',[])}
    wanted={**member.get('provider_options',{}),'model':member['model'],'reasoning_effort':member['effort']}
    for option,value in wanted.items():
        if actual.get(option)==value:continue
        result=rpc.request('session/set_config_option',{'sessionId':sid,'configId':option,'value':value})
        actual.update({x['id']:x.get('currentValue') for x in result.get('configOptions',[])})
        if actual.get(option)!=value:raise TeamError('Grok configuration did not match the saved member options')
    return actual


def prepare_member(project,member,state):
    if member.get('checkpoint_status')=='MISSING_SESSION':raise TeamError('native session was not captured; no reconstructed substitute is permitted')
    if member['provider']=='claude-code':
        from .team_claude import prepare_member as prepare_claude
        return prepare_claude(project,member,state)
    if member['provider'] in {'gemini','cursor'}:
        from .team_acp import prepare_member as prepare_acp
        return prepare_acp(project,member,state)
    archive=Path(member['archive']) if member.get('archive') else None
    source=member.get('restored_from')
    if source and (not archive or not archive.exists()):raise TeamError('captured native session is missing')
    with connection(project,member,state) as (rpc,home,config):
        if member['provider']=='codex':
            params=_codex_params(project,member,state,config)
            if source:
                # CLI 0.159 also resolves the parent ID in its local sessions tree.
                # Index an opaque frozen COPY; never resume or read the live parent.
                date=re.match(r'rollout-(\d{4})-(\d{2})-(\d{2})',archive.name)
                if not date:raise TeamError('unsupported Codex rollout filename')
                shadow=home/'sessions'/date.group(1)/date.group(2)/date.group(3)/archive.name
                shadow.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(archive,shadow)
                result=rpc.request('thread/fork',{**params,'threadId':source,'path':str(shadow),'excludeTurns':True,'deferGoalContinuation':True})
            else:result=rpc.request('thread/start',{**params,'ephemeral':False,'allowProviderModelFallback':False})
            thread=result['thread'];sid=thread['id']
            if source and sid==source:raise TeamError('provider reused source thread identity')
            if result['model']!=member['model']:raise TeamError('model drift while restoring')
            goal=member.get('goal') or {};native_goal=goal
            if goal.get('objective'):
                params={'threadId':sid,'objective':goal['objective'],'status':'complete' if goal.get('status')=='complete' else 'paused'}
                if goal.get('tokenBudget') is not None:params['tokenBudget']=max(1,goal['tokenBudget']-goal.get('tokensUsed',0))
                native_goal=rpc.request('thread/goal/set',params).get('goal') or goal
            return {'session_id':sid,'archive':thread['path'],'session_cwd':str(Path(project)/member['workdir']),
                    'recovery':'NATIVE_FORK' if source else 'NEW_SESSION','captured_goal':goal,'goal':native_goal}
        cwd=str(Path(project)/member['workdir']);servers=_grok_mcp(project,member)
        if source:
            group=member.get('session_group') or quote(member['session_cwd'],safe='')
            stored=home/'sessions'/group/source
            if not stored.exists():shutil.copytree(archive,stored)
            # ACP extensions use the underscore-prefixed wire method.
            fork=rpc.request('_x.ai/session/fork',{'sourceSessionId':source,'sourceCwd':member['session_cwd'],
                        'newCwd':cwd})
            sid=fork.get('sessionId') or fork.get('newSessionId')
            if not sid or sid==source:raise TeamError('Grok did not return a distinct native session fork')
            result=rpc.request('session/load',{'sessionId':sid,'cwd':cwd,'mcpServers':servers})
        else:
            result=rpc.request('session/new',{'cwd':cwd,'mcpServers':servers});sid=result['sessionId']
        options=_grok_options(rpc,sid,member,result)
        paths=list((home/'sessions').glob('*/'+sid))
        if len(paths)!=1:raise TeamError('Grok session persistence location is ambiguous')
        return {'session_id':sid,'archive':str(paths[0]),'session_group':paths[0].parent.name,
                'session_cwd':cwd,'provider_options':options,'recovery':'NATIVE_FORK' if source else 'NEW_SESSION'}


def run_turn(project,member,state,question,cancelled):
    if member['provider']=='claude-code':
        from .team_claude import run_turn as claude_turn
        return claude_turn(project,member,state,question,cancelled)
    answer=[];done=[];turn_id=[None];accept=[False]
    def event(message):
        method=message.get('method');params=message.get('params',{})
        if not accept[0]:return  # Loading a session may replay old history.
        if member['provider']=='codex':
            if method=='turn/started':turn_id[0]=params.get('turn',{}).get('id')
            if method=='item/completed':
                item=params.get('item',{})
                if item.get('type')=='agentMessage' and item.get('phase')!='commentary':answer.append(item.get('text',''))
            if method=='turn/completed':done.append(params.get('turn',{}))
        elif method=='session/update':
            update=params.get('update',{})
            if update.get('sessionUpdate')=='agent_message_chunk':
                content=update.get('content',{})
                if content.get('type')=='text':answer.append(content.get('text',''))
        # No thought/reasoning/tool payload is copied into the public timeline.
    with connection(project,member,state,callback=event) as (rpc,home,config):
        sid=member['session_id'];deadline=time.monotonic()+state['policy']['turn_seconds']
        if member['provider']=='codex':
            threshold=quota_threshold(state)
            quota=require_quota(rpc,threshold) if threshold is not None else None
            params=_codex_params(project,member,state,config)
            rpc.request('thread/resume',{**params,'threadId':sid,'excludeTurns':True})
            # Goals remain recorded, paused during a bounded researcher turn.
            if (member.get('goal') or {}).get('objective') and member['goal'].get('status')!='complete':rpc.request('thread/goal/set',{'threadId':sid,'status':'paused'})
            accept[0]=True
            result=rpc.request('turn/start',{'threadId':sid,'input':[{'type':'text','text':question}],
                'model':member['model'],'effort':member['effort'],'serviceTierForTurn':'default'})
            turn_id[0]=result['turn']['id'];next_quota=time.monotonic()+15
            while not done:
                reason=None
                if cancelled():reason='team interruption requested'
                elif time.monotonic()>deadline:reason='bounded turn deadline reached'
                elif threshold is not None and time.monotonic()>=next_quota:
                    try:quota=require_quota(rpc,threshold)
                    except Exception as exc:reason=str(exc)
                    next_quota=time.monotonic()+15
                if reason:
                    rpc.request('turn/interrupt',{'threadId':sid,'turnId':turn_id[0]},timeout=10)
                    raise TeamError(reason)
                time.sleep(.1)
            if done[-1].get('status')!='completed':raise TeamError('native Codex turn did not complete')
            goal=rpc.request('thread/goal/get',{'threadId':sid}).get('goal')
            return {'reply':'\n'.join(answer),'goal':goal or member.get('goal'), 'remaining_percent':quota,'native_session_id':sid}
        session_args={'cwd':str(Path(project)/member['workdir']),'mcpServers':_grok_mcp(project,member)}
        if sid:
            loaded=rpc.request('session/load',{'sessionId':sid,**session_args})
        else:
            loaded=rpc.request('session/new',session_args);sid=loaded['sessionId']
            # Keep the native identity even if the first turn is interrupted.
            # The private store may contain useful partial history at that point.
            from .team import edit
            with edit(project) as active:
                next(m for m in active['members'] if m['id']==member['id'])['session_id']=sid
        if member['provider']=='grok':options=_grok_options(rpc,sid,member,loaded)
        else:
            from .team_acp import apply_model
            options=apply_model(rpc,sid,member,loaded)
        accept[0]=True
        import threading
        output=[]
        def prompt():
            try:output.append(rpc.request('session/prompt',{'sessionId':sid,'prompt':[{'type':'text','text':_instructions(project,member,state)+'\n\n'+question}]},timeout=None))
            except Exception as exc:output.append(exc)
        thread=threading.Thread(target=prompt,daemon=True);thread.start()
        while thread.is_alive():
            if cancelled():
                rpc.notify('session/cancel',{'sessionId':sid});thread.join(timeout=10);raise TeamError(member['provider']+' turn interrupted by team control or quota guard')
            if member['provider']!='grok' and time.monotonic()>deadline:
                rpc.notify('session/cancel',{'sessionId':sid});thread.join(timeout=10);raise TeamError('bounded native turn deadline reached')
            thread.join(timeout=.2)
        if not output or isinstance(output[0],Exception):raise TeamError('native '+member['provider']+' turn failed')
        if output[0].get('stopReason') not in {'end_turn','stop'}:raise TeamError('native '+member['provider']+' turn not completed')
        saved={}
        if member['provider'] in {'cursor','gemini'}:
            from .team_acp import session_archive
            saved={'archive':str(session_archive(home,member,sid)),'session_id':sid,'session_materialized':True,
                   'recovery':'ISOLATED_RESUME' if member.get('restored_from') else 'NEW_SESSION'}
        return {'reply':''.join(answer),'goal':member.get('goal'),'native_session_id':sid,'provider_options':options,**saved}
