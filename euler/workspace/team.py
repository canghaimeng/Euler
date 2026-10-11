# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Persistent whole-team identity, bounded mailbox and native session snapshots."""
from __future__ import annotations
from contextlib import contextmanager, nullcontext
import copy
from datetime import datetime,timezone
import hashlib,json,os,re,shutil,time,uuid
from pathlib import Path

from .milestones import _json,_lock,_hash,MilestoneError

TEAM='.euler-team'
PROVIDERS={'codex','grok','claude-code','gemini','cursor'}
RECOVERED={'NATIVE_FORK','NEW_SESSION','NATIVE_RESUME','ISOLATED_RESUME','CLI_READY','FORK_READY'}


def validate_turn_limit(value):
    if value is not None and (type(value) is not int or value < 1):
        raise TeamError('max_turns must be a positive integer or null')
    return value


def limit_reached(used, maximum):
    return maximum is not None and used >= maximum


def quota_threshold(state):
    """Local opt-in guard, including policies saved by older deployments."""
    policy=state.get('policy',{})
    if policy.get('quota_monitor')=='provider_managed':return None
    return policy.get('codex_stop_percent')


def ready_for_conversation(state):
    return all(m['recovery'] in RECOVERED or m['recovery']=='NOT_STARTED' for m in state['members'])


def validate_member(member):
    validate_turn_limit(member.get('max_turns'))
    member['provider']={'claude':'claude-code','cursor-agent':'cursor'}.get(member.get('provider'),member.get('provider'))
    if member.get('provider') not in PROVIDERS or not member.get('model') or not member.get('effort'):
        raise TeamError('exact supported provider/model/effort required: codex, claude-code, gemini, grok, cursor')
    if any(not isinstance(member[k],str) or len(member[k])>200 for k in ['model','effort']):raise TeamError('model and effort must be short strings')
    if member['provider'] in {'gemini','cursor'} and member['effort'] not in {'default','provider-default','model-defined'}:
        raise TeamError('Gemini/Cursor effort is selected through the exact provider model; use effort=model-defined')
    return member


class TeamError(ValueError):pass


def now():return datetime.now(timezone.utc).isoformat()


def load(project):
    p=Path(project)/TEAM/'state.json'
    return json.loads(p.read_text()) if p.is_file() and not p.is_symlink() else None


@contextmanager
def edit(project):
    root=Path(project)/TEAM
    with _lock(root):
        state=load(project)
        if state is None:raise TeamError('No persistent team is registered in this workspace')
        yield state
        _json(root/'state.json',state)


def initialize(project, members, *, objective, max_turns=None, turn_seconds=180, quota_policy=None):
    project=Path(project).resolve(strict=True)
    if load(project):raise TeamError('team already registered; existing sessions cannot be overwritten')
    validate_turn_limit(max_turns)
    if not objective or not 10<=turn_seconds<=3600:raise TeamError('invalid bounded team policy')
    quota_policy=quota_policy or {}
    if set(quota_policy)-{'codex_stop_percent'}:raise TeamError('unknown account quota policy')
    threshold=quota_policy.get('codex_stop_percent')
    if threshold is not None and (type(threshold) not in {int,float} or not 0<=threshold<=100):
        raise TeamError('codex_stop_percent must be between 0 and 100')
    if threshold is not None and not any(m.get('provider')=='codex' for m in members):
        raise TeamError('included-account monitoring requires a Codex member')
    names=set();prepared=[]
    for item in members:
        m=validate_member(dict(item));name=m.get('id','')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',name) or name in names:raise TeamError('member IDs must be unique')
        names.add(name)
        if m.get('provider') not in PROVIDERS or not m.get('model') or not m.get('effort'):raise TeamError('exact supported provider/model/effort required')
        if m.get('role') not in {'lead','assistant','verifier'}:raise TeamError('explicit team role required')
        wd=project/'workers'/name;wd.mkdir(parents=True,exist_ok=True)
        m.update(workdir='workers/'+name,status='NOT_STARTED',session_id=None,archive=None,session_materialized=False,turns_used=0,
                 goal=m.get('goal') or {'objective':objective if m['role']=='lead' else m.get('assignment','Assist the team'), 'status':'paused'},
                 restored_from=None,recovery='NOT_STARTED')
        if m.get('assignment'):(wd/'TASK.md').write_text(m['assignment']+'\n')
        prepared.append(m)
    leads=[m['id'] for m in prepared if m['role']=='lead']
    if len(leads)!=1:raise TeamError('exactly one lead agent is required')
    state={'schema':1,'instance_id':str(uuid.uuid4()),'objective':objective,'lead':leads[0],
        'members':prepared,'state':'PAUSED','messages':[],'node_participants':{},
        'policy':{'max_turns':max_turns,'turn_seconds':turn_seconds,'grok_turn_timeout_seconds':None,
                  'quota_monitor':'codex_included' if threshold is not None else 'provider_managed',
                  **({'codex_stop_percent':threshold} if threshold is not None else {})},
        'turns_used':0,'created_at':now(),'source':None}
    _json(project/TEAM/'state.json',state)
    meta_path=project/'project.json'
    meta=json.loads(meta_path.read_text()) if meta_path.is_file() else {'name':project.name}
    meta.update(workers=[m['id'] for m in prepared],coordinator=leads[0],worker_configs=[{'name':m['id'],**{k:m[k] for k in ['provider','model','effort','role']}} for m in prepared])
    _json(meta_path,meta)
    for m in prepared:
        (project/m['workdir']/'.role').write_text('PROVIDER='+m['provider']+'\nMODEL='+m['model']+'\nREASONING_EFFORT='+m['effort']+'\nROLE='+m['role']+'\n')
    from .milestones import configured,enable
    if not configured(project):enable(project)
    return public_state(project)


def public_state(project):
    s=load(project)
    if not s:return {'available':False,'state':'UNREGISTERED','reason':'No team sessions were captured; file restoration alone cannot recreate them.'}
    members=[{k:m.get(k) for k in ['id','name','role','provider','model','effort','status','session_id','recovery','restored_from','goal','captured_goal','provider_options','assignment','max_turns','turns_used']} for m in s['members']]
    inherited = inherited_message_ids(project, s)
    return {'available':True,'instance_id':s['instance_id'],'objective':s['objective'],'state':'RUNNING' if s['state']=='READY' and any(m['status'] in {'RUNNING','CONNECTING'} for m in s['members']) else s['state'],
        'lead':s['lead'],'members':members,'messages':[{**m,'inherited':m['id'] in inherited} for m in s['messages']],'policy':s['policy'],
        'turns_used':s['turns_used'],'source':{k:v for k,v in s['source'].items() if k!='roots'} if s['source'] else None,'node_participants':s['node_participants'],
        'roster_events':s.get('roster_events',[]),'ready_for_conversation':ready_for_conversation(s),
        'complete_native_recovery':all(m['recovery'] in RECOVERED for m in s['members'])}


def add_member(project, definition, *, key, actor='human:operator'):
    """Register a new member in this branch; prepare its native session on first use."""
    if not isinstance(key,str) or not 1<=len(key)<=160:raise TeamError('request identity required')
    allowed={'id','name','provider','model','effort','role','assignment','max_turns'}
    if not isinstance(definition,dict) or set(definition)-allowed:raise TeamError('unexpected member configuration fields')
    member=validate_member(dict(definition))
    member.setdefault('id','member-'+hashlib.sha256(key.encode()).hexdigest()[:12])
    member.setdefault('name',member['id']);member.setdefault('role','assistant');member.setdefault('assignment','Assist with the current research objective.')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',member['id']):raise TeamError('invalid member ID')
    if member['role'] not in {'assistant','verifier'}:raise TeamError('new members must be assistants or reviewers; the lead is unchanged')
    if not isinstance(member['name'],str) or not 1<=len(member['name'].strip())<=100:raise TeamError('member name required (up to 100 characters)')
    if not isinstance(member['assignment'],str) or len(member['assignment'])>4000:raise TeamError('member assignment is too long')
    root=Path(project).resolve();fingerprint=hashlib.sha256(json.dumps(member,sort_keys=True).encode()).hexdigest()
    with edit(root) as state:
        if actor.startswith('agent:'):
            sender=actor[6:]
            if sender!=state['lead'] or not any(m['state']=='RUNNING' and m['request']['recipient']==sender for m in state['messages']):
                raise TeamError('only the active lead may add a team member')
        elif not actor.startswith('human:'):raise TeamError('invalid member creation actor')
        requests=state.setdefault('member_requests',{})
        if key in requests:
            if requests[key]['fingerprint']!=fingerprint:raise TeamError('member request identity has different content')
            return requests[key]['result']
        if any(m['id']==member['id'] or m.get('name',m['id'])==member['name'] for m in state['members']):raise TeamError('member name or ID already exists in this branch')
        wd=root/'workers'/member['id']
        if not wd.resolve().is_relative_to(root):raise TeamError('member workspace escapes the current branch')
        if wd.exists():raise TeamError('member workspace already exists')
        wd.mkdir(parents=True)
        (wd/'TASK.md').write_text(member['assignment']+'\n')
        (wd/'.role').write_text('\n'.join(f'{k}={member[v]}' for k,v in [('PROVIDER','provider'),('MODEL','model'),('REASONING_EFFORT','effort'),('ROLE','role')])+'\n')
        member.update(workdir='workers/'+member['id'],status='NOT_STARTED',session_id=None,archive=None,
            goal={'objective':member['assignment'],'status':'paused'},restored_from=None,recovery='NOT_STARTED',turns_used=0,session_materialized=False)
        state['members'].append(member)
        result={'id':member['id'],'name':member['name'],'state':'NOT_STARTED','preparation':'Native session starts on the first message or lead consultation.'}
        requests[key]={'fingerprint':fingerprint,'result':result}
        parent=next((m['id'] for m in state['messages'] if m['state']=='RUNNING' and m['request']['recipient']==state['lead']),None) if actor.startswith('agent:') else None
        state.setdefault('roster_events',[]).append({'event_type':'member_added','member_id':member['id'],'actor':actor,
            'parent_message_id':parent,'created_at':now(),'idempotency_key':key})
        meta_path=root/'project.json';meta=json.loads(meta_path.read_text())
        meta['workers']=[m['id'] for m in state['members']]
        meta['worker_configs']=[{'name':m['id'],**{k:m[k] for k in ['provider','model','effort','role']}} for m in state['members']]
        _json(meta_path,meta)
        return result


def inherited_message_ids(project, state):
    """Identify checkpoint history from exact saved IDs, never from timestamps."""
    source = state.get('source') or {}
    if not source:
        return set()
    if 'inherited_message_ids' in source:
        return set(source['inherited_message_ids'])
    # Older restored workspaces retain their immutable captured team record.
    root = Path(project).resolve()
    frozen = root.parents[1] / 'team/state.json'
    if (root / 'RESTORE_ORIGIN.json').is_file() and frozen.is_file() and not frozen.is_symlink():
        saved = json.loads(frozen.read_text())
        if saved.get('instance_id') == source.get('instance_id'):
            return {m['id'] for m in saved.get('messages', [])}
    return set()


def selected_object_context(project, object_id):
    """Pin the public source record that a member is being asked to inspect."""
    from euler.observability.monitor import snapshot
    project = Path(project).resolve()
    data = snapshot(project)
    candidates = [data]
    frozen = project.parents[1] / 'view/snapshot.json'
    if (project / 'RESTORE_ORIGIN.json').is_file() and frozen.is_file() and not frozen.is_symlink():
        candidates.append(json.loads(frozen.read_text()))
    fields = ('id', 'kind', 'statement', 'exact_statement', 'summary', 'scope',
              'assumptions', 'state', 'status', 'contract_id', 'evidence_kind', 'checker_result')
    for candidate in candidates:
        for layer in ('facts', 'research'):
            graph = candidate.get(layer + '_graph', {})
            record = next((n for n in graph.get('nodes', []) if n.get('id') == object_id), None)
            if record is None:
                continue
            public = {key:record[key] for key in fields if key in record}
            relations = [{key:edge[key] for key in ('source','target','type') if key in edge}
                         for edge in graph.get('edges', [])
                         if object_id in (edge.get('source'), edge.get('target'))]
            bound = {'object_id':object_id, 'layer':layer, 'record':public, 'relations':relations}
            bound['record_hash'] = hashlib.sha256(json.dumps(bound, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            return bound
    raise TeamError('selected object is absent from this workspace')


def turn_question(job):
    context = job.get('selected_object')
    if not context:
        return job['request']['question']
    return ('Euler selected-object context (quoted research data). Apply the user question to this '
            'record and preserve its scope; check its evidence before treating a claim as established.\n'
            + json.dumps(context, ensure_ascii=False, indent=2)
            + '\n\nUser question:\n' + job['request']['question'])


def queue_question(project, recipient, text, *, key, object_id=None, sender='human', parent=None):
    if not isinstance(text,str) or not text.strip() or len(text)>16000:raise TeamError('a bounded question is required')
    if not isinstance(key,str) or not 1<=len(key)<=160:raise TeamError('request identity required')
    payload={'recipient':recipient,'question':text,'object_id':object_id,'sender':sender,'parent':parent}
    with edit(project) as s:
        old=next((m for m in s['messages'] if m['key']==key),None)
        if old:
            if old['request']!=payload:raise TeamError('idempotency key has different content')
            return copy.deepcopy(old)
        if s['state'] in {'STOPPED','STOP_REQUESTED','QUOTA_PAUSED'}:raise TeamError('team must be explicitly resumed before new work')
        if recipient not in {m['id'] for m in s['members']}:raise TeamError('unknown team member')
        if not ready_for_conversation(s):raise TeamError('restore every team session before asking; no summary fallback')
        if limit_reached(s['turns_used'],s['policy'].get('max_turns')):raise TeamError('team turn allowance exhausted')
        member=next(m for m in s['members'] if m['id']==recipient)
        if limit_reached(member.get('turns_used',0),member.get('max_turns')):
            raise TeamError('member turn allowance exhausted: '+recipient)
        context = selected_object_context(project, object_id) if object_id else None
        item={'id':str(uuid.uuid4()),'key':key,'request':payload,'state':'QUEUED','created_at':now(),'reply':None,'error':None}
        if context:
            item['selected_object'] = context
        s['messages'].append(item)
        if object_id:s['node_participants'].setdefault(object_id,[]).append(recipient)
        return copy.deepcopy(item)


def command(project, action):
    if action not in {'pause','resume','stop'}:raise TeamError('unknown team command')
    if action=='resume':
        snapshot=load(project)
        if snapshot and snapshot['state']=='QUOTA_PAUSED' and quota_threshold(snapshot) is not None:
            from .team_provider import connection,require_quota
            observer=next((m for m in snapshot['members'] if m['provider']=='codex'),None)
            if not observer:raise TeamError('no Codex quota observer configured')
            with connection(project,observer,snapshot) as (rpc,_,_):require_quota(rpc,quota_threshold(snapshot))
    with edit(project) as s:
        active=any(m['status'] in {'RUNNING','CONNECTING'} for m in s['members'])
        if action=='resume':
            if not ready_for_conversation(s):raise TeamError('restore all member sessions before resuming')
            s['state']='READY'
            for message in s['messages']:
                if message['state']=='SUSPENDED':message['state']='QUEUED'
        else:
            s['state']=('PAUSING' if active else 'PAUSED') if action=='pause' else ('STOP_REQUESTED' if active else 'STOPPED')
            for message in s['messages']:
                if message['state']=='QUEUED':message['state']='SUSPENDED'
    return public_state(project)


def prepare(project):
    """Native session fork/reconnect for every member. No inference is started."""
    from .team_provider import prepare_member
    project=Path(project).resolve()
    with edit(project) as s:
        if s['state'] in {'QUOTA_PAUSED','STOPPED','STOP_REQUESTED','BUDGET_PAUSED'}:raise TeamError('explicit team resume is required')
        if any(m['status'] in {'RUNNING','CONNECTING'} for m in s['members']):raise TeamError('team is busy')
        for member in s['members']:
            if member['recovery'] in RECOVERED:continue
            try:
                member.update(prepare_member(project,member,s))
                member['session_materialized']=bool(member.get('archive'))
                member['status']='READY'
            except Exception as exc:
                member['status']='RECOVERY_FAILED';member['error']=str(exc)
                s['state']='RECOVERY_INCOMPLETE'
                _json(project/TEAM/'state.json',s)
                raise TeamError('Session recovery failed for '+member['id']+': '+str(exc)) from None
            _json(project/TEAM/'state.json',s)
        s['state']='READY'
    from .milestones import checkpoint_if_enabled
    checkpoint_if_enabled(project)
    return public_state(project)


def capture_team(project, stage):
    """Copy provider persistence opaquely into the same Git tree as all files."""
    s=load(project)
    if not s:return {'available':False,'reason':'No registered native team state at capture'}
    if any(m['status'] in {'RUNNING','CONNECTING'} for m in s['members']):raise MilestoneError('team agent turn still running')
    meta=copy.deepcopy(s);records={}
    for m in meta['members']:
        source=Path(m['archive']) if m.get('archive') else None
        if not source or not source.exists():
            m['checkpoint_session']=None
            never_started=(source is None and not m.get('restored_from') and not m.get('turns_used',0)
                and (m['status']=='NOT_STARTED' or m['recovery'] in {'NOT_STARTED','CLI_READY'}))
            m['checkpoint_status']='NOT_STARTED' if never_started else 'MISSING_SESSION'
            if m['checkpoint_status']=='NOT_STARTED':m['session_id']=None
            continue
        if m.get('recovery')=='FORK_READY':
            # The saved conversation is still the captured parent until Claude
            # materializes its fork on the first explicit user turn.
            m['session_id']=m['restored_from']
        prefix='team/sessions/'+m['id']
        sources=[source] if source.is_file() else sorted(p for p in source.rglob('*') if p.is_file())
        for src in sources:
            if src.is_symlink():raise MilestoneError('session archive must contain regular local files')
            rel=prefix+'/'+(src.name if source.is_file() else str(src.relative_to(source)))
            # These are provider-generated session files, not account homes.
            if src.name in {'auth.json','credentials.json','config.toml'}:raise MilestoneError('credentials cannot be a session archive')
            before=(src.stat().st_size,src.stat().st_mtime_ns)
            dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
            if before!=(src.stat().st_size,src.stat().st_mtime_ns) or _hash(src)!=_hash(dest):raise MilestoneError('provider session changed during checkpoint')
            records[rel]={'sha256':_hash(dest),'bytes':dest.stat().st_size}
        m['checkpoint_session']=prefix+('/'+source.name if source.is_file() else '')
        m['checkpoint_status']='CAPTURED'
        m['archive']=None
    _json(stage/'team/state.json',meta)
    records['team/state.json']={'sha256':_hash(stage/'team/state.json'),'bytes':(stage/'team/state.json').stat().st_size}
    return {'available':True,'members':[{k:m[k] for k in ['id','provider','role','checkpoint_status']} for m in meta['members']],
            'complete':all(m['checkpoint_status'] in {'CAPTURED','NOT_STARTED'} for m in meta['members']), 'files':records}


def restore_team(stage, destination, root, commit, manifest):
    checkpoint=stage/'team/state.json'
    if not checkpoint.exists():return {'available':False,'complete':False}
    for rel,record in manifest['team']['files'].items():
        if _hash(stage/rel)!=record['sha256']:raise MilestoneError('team session archive hash mismatch')
    s=json.loads(checkpoint.read_text());source_instance=s['instance_id'];s['instance_id']=str(uuid.uuid4())
    s['source']={'commit':commit,'instance_id':source_instance,'captured_turns_used':s['turns_used'],
        'inherited_message_ids':[m['id'] for m in s['messages']],
        'roots':sorted(set(list(manifest['roots'].values())+(s.get('source') or {}).get('roots',[])))}
    s['turns_used']=0;s['state']='RESTORED_PAUSED'
    for m in s['members']:
        m['turns_used']=0
        m['restored_from']=m['session_id'];m['session_id']=None;m['status']='FORK_PENDING';m['recovery']='PENDING'
        m['archive']=str(destination/m['checkpoint_session']) if m.get('checkpoint_session') else None
        if m.get('checkpoint_status')=='MISSING_SESSION':m['status']='MISSING_SESSION'
    for message in s['messages']:
        if message['state'] in {'QUEUED','RUNNING'}:message['state']='SUSPENDED'
    _json(root/TEAM/'state.json',s)
    return {'available':True,'complete':manifest['team']['complete'],'members':len(s['members']),'state':'RESTORED_PAUSED'}


def _drain(project):
    """Run the durable mailbox; each member retains its own native conversation."""
    from .team_provider import run_turn, connection, require_quota, prepare_member
    project=Path(project).resolve()
    # Only one runner may own a team's queue. Different teams remain independent.
    with _lock(project/TEAM/'runner'):
        while True:
            with edit(project) as s:
                if s['state'] in {'PAUSED','PAUSING','STOPPED','STOP_REQUESTED','QUOTA_PAUSED'}:return
                queued=next((m for m in s['messages'] if m['state']=='QUEUED'),None)
                if not queued:return
                if limit_reached(s['turns_used'],s['policy'].get('max_turns')):
                    s['state']='BUDGET_PAUSED';return
                member=next(m for m in s['members'] if m['id']==queued['request']['recipient'])
                if limit_reached(member.get('turns_used',0),member.get('max_turns')):
                    queued.update(state='SUSPENDED',error='member turn allowance exhausted: '+member['id'])
                    continue
                if member['status']=='RUNNING':raise TeamError('unsettled member turn requires recovery')
                queued['state']='RUNNING';queued['started_at']=now();member['status']='RUNNING';s['turns_used']+=1
                member['turns_used']=member.get('turns_used',0)+1
                job=copy.deepcopy(queued);actor=copy.deepcopy(member);snapshot=copy.deepcopy(s)
            try:
                threshold=quota_threshold(snapshot)
                codex=next((m for m in snapshot['members'] if m['provider']=='codex'),None) if threshold is not None else None
                if threshold is not None and not codex:
                    raise TeamError('a Codex quota observer must be configured')
                # A deployment can opt into an account-level guard for all members.
                with (connection(project,codex,snapshot) if codex else nullcontext((None,None,None))) as (quota_rpc,_,_):
                    remaining=require_quota(quota_rpc,threshold) if codex else None;next_check=[time.monotonic()+15]
                    def cancelled():
                        state=load(project)
                        if state['state'] in {'PAUSING','PAUSED','STOP_REQUESTED','STOPPED','QUOTA_PAUSED'}:return True
                        if codex and time.monotonic()>=next_check[0]:
                            try:require_quota(quota_rpc,threshold)
                            except Exception:
                                with edit(project) as current:current['state']='QUOTA_PAUSED'
                                return True
                            next_check[0]=time.monotonic()+15
                        return False
                    if actor['recovery']=='NOT_STARTED':
                        with edit(project) as live:next(m for m in live['members'] if m['id']==actor['id'])['status']='CONNECTING'
                        if cancelled():raise TeamError('team interruption requested')
                        prepared=prepare_member(project,actor,snapshot);prepared.setdefault('session_materialized',bool(prepared.get('archive')));actor.update(prepared)
                        with edit(project) as live:
                            current=next(m for m in live['members'] if m['id']==actor['id']);current.update(prepared);current['status']='RUNNING'
                    if cancelled():raise TeamError('team interruption requested')
                    response=run_turn(project,actor,snapshot,turn_question(job),cancelled)
                if not response['reply'].strip():raise TeamError('provider returned no public reply')
                error=None
            except Exception as exc:
                response=None;error=str(exc)
            with edit(project) as s:
                member=next(m for m in s['members'] if m['id']==actor['id'])
                message=next(m for m in s['messages'] if m['id']==job['id'])
                if member['provider']=='claude-code':
                    from .team_claude import persistence
                    member.update(persistence(project,member))
                elif member['provider'] in {'cursor','gemini'} and member.get('session_id'):
                    from .team_provider import _home
                    from .team_acp import session_archive
                    try:archive=session_archive(_home(project,member),member,member['session_id'],allow_empty=True)
                    except TeamError:archive=None
                    if archive:member.update(archive=str(archive),session_materialized=True,
                        recovery='ISOLATED_RESUME' if member.get('restored_from') else 'NEW_SESSION')
                member['status']='READY';message['completed_at']=now()
                if error:
                    message.update(state='CANCELLED' if s['state'] in {'PAUSING','PAUSED','STOP_REQUESTED','STOPPED','QUOTA_PAUSED'} else 'FAILED',error=error)
                    if 'QUOTA' in error:s['state']='QUOTA_PAUSED'
                else:
                    message.update(state='ANSWERED',reply=response['reply'],native_session_id=response['native_session_id'])
                    member['goal']=response.get('goal') or member['goal']
                    if response.get('provider_options'):member['provider_options']=response['provider_options']
                    for key in ['archive','session_id','session_materialized','recovery','resume_path']:
                        if key in response:member[key]=response[key]
                if s['state']=='PAUSING':s['state']='PAUSED'
                if s['state']=='STOP_REQUESTED':s['state']='STOPPED'
                # A delegated reply returns to the lead in the same branch and
                # conversation. It remains discussion, never a verifier PASS.
                if job['request']['sender'] in {m['id'] for m in s['members']}:
                    sender=job['request']['sender']
                    follow={'recipient':sender,'question':actor['id']+' replied to your team request:\n'+(response['reply'] if response else 'Operational failure: '+error+'. No mathematical verdict was produced.')+
                            '\nUse this result to continue coordinating the team. State any remaining mathematical uncertainty.',
                            'object_id':job['request']['object_id'],'sender':'team-delivery','parent':job['id']}
                    s['messages'].append({'id':str(uuid.uuid4()),'key':'delivery:'+job['id'],'request':follow,
                        'state':'QUEUED','created_at':now(),'reply':None,'error':None,
                        **({'selected_object':copy.deepcopy(job['selected_object'])} if job.get('selected_object') else {})})


def drain(project):
    try:
        _drain(project)
    finally:
        from .milestones import checkpoint_if_enabled
        checkpoint_if_enabled(project)


def start_runner(project):
    import subprocess,sys
    project=Path(project).resolve()
    with edit(project) as s:
        if s['state'] in {'STOPPED','STOP_REQUESTED','QUOTA_PAUSED','BUDGET_PAUSED'}:raise TeamError('team is paused by stop, quota or budget')
        if not ready_for_conversation(s):raise TeamError('restore every member session before launching')
        s['state']='READY'
    log=project/TEAM/'runner.log'
    with log.open('ab') as out:
        p=subprocess.Popen([sys.executable,'-m','euler.workspace.team','run','--project',str(project)],
            stdout=out,stderr=out,start_new_session=True,env=os.environ.copy())
    return {'state':'QUEUED','runner_pid':p.pid}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['init','prepare','status','ask','run','pause','resume','stop','doctor'])
    parser.add_argument('--project',type=Path);parser.add_argument('--definition',type=Path)
    parser.add_argument('--member');parser.add_argument('--question');parser.add_argument('--key');parser.add_argument('--object')
    args=parser.parse_args()
    if args.command=='doctor':
        from .isolation import host_support
        from .team_acp import BINARIES,binary_for
        providers={}
        for name in BINARIES:
            try:providers[name]={'available':True,'binary':binary_for(name)}
            except ValueError as exc:providers[name]={'available':False,'reason':str(exc)}
        print(json.dumps({'isolation':host_support(),'providers':providers,'inference_started':False},indent=2));return
    if args.project is None:parser.error('--project is required for this command')
    if args.command=='init':
        definition=json.loads(args.definition.read_text());result=initialize(args.project,definition['members'],objective=definition['objective'],
            max_turns=definition.get('max_turns'),turn_seconds=definition.get('turn_seconds',180),
            quota_policy=definition.get('quota_policy'))
    elif args.command=='prepare':result=prepare(args.project)
    elif args.command=='status':result=public_state(args.project)
    elif args.command=='ask':
        result=queue_question(args.project,args.member,args.question,key=args.key or str(uuid.uuid4()),object_id=args.object)
        start_runner(args.project)
    elif args.command=='run':drain(args.project);return
    else:result=command(args.project,args.command)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
