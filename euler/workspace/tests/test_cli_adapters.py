"""Native protocol boundaries with local executable doubles; no provider inference."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

from euler.workspace import team
from euler.workspace.isolation import macos_profile, IsolationError
from euler.workspace.milestones import Milestones, public_projection


@pytest.fixture
def native_binaries(tmp_path, monkeypatch):
    for provider in ('gemini', 'cursor', 'claude-code'):
        binary = tmp_path / (provider + '-cli')
        binary.write_text('#!' + sys.executable + '\n' + 'PROVIDER=' + repr(provider) + '\n' + r'''
import json, os, sys, uuid
from pathlib import Path
args=sys.argv[1:]
if PROVIDER=='claude-code':
    config=Path(os.environ['CLAUDE_CONFIG_DIR']); question=sys.stdin.read()
    sid=args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]
    previous=''
    if '--resume' in args:
        source=args[args.index('--resume')+1]
        old=Path(source) if '--fork-session' in args else config/'projects/fixture'/f'{source}.jsonl'
        previous=old.read_text()
    file=config/'projects/fixture'/f'{sid}.jsonl';file.parent.mkdir(parents=True,exist_ok=True)
    file.write_text(previous+'OPAQUE-NATIVE-HISTORY:'+question)
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'PRIVATE'}]}}))
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':sid,'result':'Native public answer.'}))
    sys.exit()
model=args[args.index('--model')+1]
def archive(sid):
    if PROVIDER=='cursor':return Path(os.environ['CURSOR_CONFIG_DIR'])/'acp-sessions'/sid/'store.db'
    return Path(os.environ['GEMINI_CLI_HOME'])/'.gemini/tmp/euler-session/chats'/('session-test-'+sid[:8]+'.jsonl')
def send(obj):print(json.dumps(obj),flush=True)
for line in sys.stdin:
    message=json.loads(line);method=message['method'];p=message.get('params',{});result={}
    if method=='initialize':result={'agentCapabilities':{'loadSession':True},'protocolVersion':1}
    elif method=='authenticate':pass
    elif method=='session/new':
        sid=str(uuid.uuid4());file=archive(sid);file.parent.mkdir(parents=True,exist_ok=True);file.write_text('OPAQUE-NATIVE-HISTORY')
        if PROVIDER=='cursor':(file.parent/'meta.json').write_text(json.dumps({'cwd':p['cwd'],'schemaVersion':1}))
        result={'sessionId':sid,'models':{'currentModelId':model}}
    elif method=='session/load':
        assert archive(p['sessionId']).read_text().startswith('OPAQUE-NATIVE-HISTORY')
        assert p['mcpServers'][0]['name']=='euler_team'
        send({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'OLD REPLAY'}}}})
        result={'models':{'currentModelId':model}}
    elif method=='session/prompt':
        file=archive(p['sessionId']);file.write_text(file.read_text()+'\nNEW TURN')
        send({'method':'session/update','params':{'update':{'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'PRIVATE'}}}})
        send({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Native public answer.'}}}})
        result={'stopReason':'end_turn'}
    elif method=='session/cancel':continue
    else:raise RuntimeError(method)
    if 'id' in message:send({'id':message['id'],'result':result})
''')
        binary.chmod(0o755)
        key = 'CLAUDE' if provider == 'claude-code' else provider.upper()
        monkeypatch.setenv('EULER_TEAM_' + key + '_BIN', str(binary))
    credentials = tmp_path / 'credentials'
    credentials.mkdir()
    for filename in ['oauth_creds.json', 'auth.json', '.credentials.json']:
        (credentials / filename).write_text('{"test_credentials":"never-public"}')
    monkeypatch.setenv('EULER_TEAM_GEMINI_AUTH_DIR', str(credentials))
    monkeypatch.setenv('EULER_TEAM_CURSOR_AUTH', str(credentials / 'auth.json'))
    monkeypatch.setenv('EULER_TEAM_CLAUDE_AUTH', str(credentials / '.credentials.json'))
    # Prevent real macOS Keychain access by the local Claude executable double.
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'test-only-oauth')
    monkeypatch.delenv('EULER_DISABLE_CURSOR', raising=False)


@pytest.mark.parametrize('provider', ['gemini', 'cursor', 'claude-code'])
def test_native_conversation_roundtrip_and_restored_branch(tmp_path, native_binaries, provider):
    root = tmp_path / 'project'; root.mkdir()
    team.initialize(root, [{'id': 'lead', 'role': 'lead', 'provider': provider,
                           'model': 'exact-test-model', 'effort': 'high' if provider == 'claude-code' else 'model-defined'}],
                    objective='A synthetic systems test', turn_seconds=30)
    prepared = team.prepare(root)
    assert prepared['complete_native_recovery']
    assert prepared['policy']['quota_monitor'] == 'provider_managed'
    team.queue_question(root, 'lead', 'First public question', key='one'); team.drain(root)
    original = team.load(root)
    assert original['messages'][-1]['state'] == 'ANSWERED', original['messages'][-1].get('error')
    assert original['messages'][-1]['reply'] == 'Native public answer.'
    source = Path(original['members'][0]['archive'])
    if source.is_dir():source = source / 'store.db'
    captured_bytes = source.read_bytes()
    checkpoint = Milestones(root).capture('Reviewed step', key='checkpoint', projection=lambda: public_projection(root))
    # Parent continues after capture; the child must receive the exact older store.
    source.write_bytes(captured_bytes + b'\nORIGINAL-FUTURE')
    child = Path(Milestones(root).restore(checkpoint['commit'], key='branch')['project_dir'])
    restored = team.prepare(child)
    restored_member = team.load(child)['members'][0]
    restored_file = Path(restored_member['archive'])
    if restored_file.is_dir():restored_file = restored_file / 'store.db'
    assert restored_file.read_bytes() == captured_bytes
    assert restored['instance_id'] != original['instance_id']
    assert restored_member['recovery'] == ('FORK_READY' if provider == 'claude-code' else 'ISOLATED_RESUME')
    team.queue_question(child, 'lead', 'Continue in the branch', key='two'); team.drain(child)
    outcome = team.load(child)
    assert outcome['messages'][-1]['state'] == 'ANSWERED', outcome['messages'][-1].get('error')
    assert outcome['messages'][-1]['reply'] == 'Native public answer.'
    assert source.read_bytes() == captured_bytes + b'\nORIGINAL-FUTURE'
    assert outcome['members'][0]['session_id'] != original['members'][0]['session_id'] if provider == 'claude-code' else outcome['members'][0]['session_id'] == original['members'][0]['session_id']
    public = json.dumps(team.public_state(child))
    assert 'PRIVATE' not in public and 'OLD REPLAY' not in public and 'test_credentials' not in public
    assert not (child / 'fact_graph').exists()


def test_five_providers_and_aliases_are_registered_without_inference(tmp_path):
    members = [dict(id=name, provider=name, role='lead' if name=='claude' else 'assistant',
                    model='test', effort='model-defined' if name in {'gemini','cursor'} else 'high')
               for name in ['codex','claude','gemini','grok','cursor']]
    state=team.initialize(tmp_path, members, objective='Test')
    assert {m['provider'] for m in state['members']}==team.PROVIDERS
    assert state['policy']['quota_monitor']=='provider_managed'
    for name in ['cursor','gemini']:
        with pytest.raises(team.TeamError,match='effort'):
            team.validate_member({'provider':name,'model':'exact','effort':'ultra'})


def test_seatbelt_profile_rejects_overlap_and_quotes_paths(tmp_path):
    old=tmp_path/'old';old.mkdir();new=tmp_path/'new " branch';new.mkdir()
    profile=macos_profile([new],[old])
    assert '(deny file-write*)' in profile and '(deny file-read*' in profile
    assert '\\"' in profile
    with pytest.raises(IsolationError):macos_profile([tmp_path],[old])
    with pytest.raises(IsolationError):macos_profile(['/'],[])


def test_real_os_isolation_entrypoint_blocks_parent_and_symlink(tmp_path):
    if platform.system() not in {'Linux','Darwin'}:pytest.skip('supported execution hosts only')
    old=tmp_path/'old';old.mkdir();(old/'proof').write_text('saved proof')
    new=tmp_path/'new';new.mkdir();(new/'link').symlink_to(old/'proof')
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'writable':[str(new)],'hidden':[str(old)]}))
    code='''import sys
from pathlib import Path
old,new=map(Path,sys.argv[1:]);(new/'output').write_text('new proof')
for path in [old/'proof',new/'link']:
 for action in [lambda:path.read_text(),lambda:path.write_text('bad')]:
  try:action();raise AssertionError('source access allowed')
  except PermissionError:pass
'''
    subprocess.run([sys.executable,'-m','euler.workspace.isolation','--policy',str(policy),'--',
                    sys.executable,'-c',code,str(old),str(new)],check=True)
    assert (old/'proof').read_text()=='saved proof'
    assert (new/'output').read_text()=='new proof'


def test_cursor_auto_uses_advertised_native_id_without_guessing():
    from euler.workspace.team_acp import apply_model
    calls=[]
    class Rpc:
        def request(self,method,params):calls.append((method,params));return {}
    member={'provider':'cursor','model':'auto'}
    result={'models':{'currentModelId':'default[]'},'configOptions':[
        {'id':'model','currentValue':'default[]','options':[{'value':'default[]','name':'Auto'}]}]}
    options=apply_model(Rpc(),'saved',member,result)
    assert options['native_model_id']=='default[]' and not calls
    # A different requested ID cannot be silently replaced with Auto.
    apply_model(Rpc(),'saved',{'provider':'cursor','model':'exact[fast=false]'},result)
    assert calls==[('session/set_model',{'sessionId':'saved','modelId':'exact[fast=false]'})]


def test_cursor_long_paths_get_private_short_runtime_alias(tmp_path):
    from euler.workspace.team_provider import runtime_launch
    root=tmp_path/('x'*150);root.mkdir();home=root/'.euler-provider/cursor';home.mkdir(parents=True)
    with runtime_launch(root,{'provider':'cursor'},{},home,{},['cursor-agent','acp']) as (argv,env):
        alias=Path(env['CURSOR_DATA_DIR']);temporary=Path(env['TMPDIR'])
        assert len(str(alias))<60 and alias.resolve()==home/'cursor-data'
        policy=json.loads((home/'isolation.json').read_text())
        assert str(temporary) in policy['writable'] and '/tmp' not in policy['writable']
        assert argv[-2:]==['cursor-agent','acp']
    assert not temporary.exists() and (home/'cursor-data').is_dir()


def test_doctor_module_reports_missing_clis_without_exception():
    env=dict(os.environ,PATH='/nonexistent')
    for key in list(env):
        if key.startswith('EULER_TEAM_') and key.endswith('_BIN'):env.pop(key)
    result=subprocess.run([sys.executable,'-m','euler.workspace.team','doctor'],env=env,
                          capture_output=True,text=True,check=True)
    data=json.loads(result.stdout)
    assert not data['inference_started']
    assert set(data['providers'])==team.PROVIDERS
    assert not any(p['available'] for p in data['providers'].values())
