"""Native research -> real MCP gateway -> HTTP checker -> graph -> final audit.

The HTTP checker is a deterministic protocol fixture, not a model or theorem
prover. No test injects Fact Graph files or accepted evidence directly.
"""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

import pytest

from euler.control.contracts import MathematicalScope, ProblemContract, EvidenceKind
from euler.control.obligations import Obligation
from euler.control.policy import BudgetPolicy, DimensionLimit, RESOURCE_DIMENSIONS
from euler.control.project import initialize_project, open_project
from euler.control.ids import sha256_hex
from euler.workspace import team, team_research as research
from euler.workspace.team_mcp import euler_team_propose_steps


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'fresh'; root.mkdir()
    (root / 'project.json').write_text(json.dumps({'name': 'fresh'}))
    scope = MathematicalScope.build(assumptions=[], quantifiers=[], objects=[],
        parameter_domain='Integers', definitions_hash=sha256_hex('ordinary integer addition'))
    contract = ProblemContract.build(problem_id='fresh', version=1,
        exact_statement='For every integer n, n + 0 = n.', scope=scope,
        target_kind='PROOF', required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        permitted_tools=[], external_help_policy='Protocol fixture, no mathematical discovery',
        completion_checks=['Independent final audit'], budget_policy_ref='unlimited')
    target = Obligation.build(contract_id=contract.contract_id, exact_statement=contract.exact_statement,
        scope=scope, desired_output='checked proof', created_by='human', version=1)
    policy = BudgetPolicy(**{k: DimensionLimit.unlimited() for k in RESOURCE_DIMENSIONS})
    with initialize_project(root, contract, target, policy, 'human', team.now()) as opened:
        opened.artifacts.put_text('ordinary integer addition')
    team.initialize(root, [
        {'id': 'lead', 'role': 'lead', 'provider': 'codex', 'model': 'fixture', 'effort': 'high'},
        {'id': 'helper', 'role': 'assistant', 'provider': 'grok', 'model': 'fixture', 'effort': 'high'},
        {'id': 'reviewer', 'role': 'verifier', 'provider': 'grok', 'model': 'fixture', 'effort': 'high'}],
        objective=contract.exact_statement)
    monkeypatch.setenv('EULER_TEAM_PROJECT', str(root))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[3]))
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    return root, target


def activate(monkeypatch, root, member):
    monkeypatch.setenv('EULER_TEAM_MEMBER', member)
    with team.edit(root) as state:
        state['state'] = 'READY'
        for m in state['members']:
            m.update(status='RUNNING' if m['id'] == member else 'READY', recovery='NEW_SESSION')
        job = {'id': str(uuid.uuid4()), 'state': 'RUNNING', 'key': str(uuid.uuid4()),
               'request': {'recipient': member, 'sender': 'human', 'question': 'Investigate the exact goal.',
                           'object_id': None, 'parent': None}, 'reply': None, 'error': None}
        state['messages'].append(job)
    return job


def settle(root, job):
    with team.edit(root) as state:
        for m in state['members']:m['status'] = 'READY'
        next(m for m in state['messages'] if m['id'] == job['id'])['state'] = 'ANSWERED'


@pytest.fixture
def verifier(monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            wrong = 'WRONG' in body['proof'] or (body.get('force_fresh') and 'reject-final-fixture' in body['proof'])
            payload = {'verdict': 'wrong' if wrong else 'correct',
                'verification_report': {'summary': 'Deterministic protocol fixture',
                    'critical_errors': [{'location': 'fixture', 'issue': 'Rejected test proof'}] if wrong else [], 'gaps': []},
                'repair_hints': 'Repair the rejected step.' if wrong else ''}
            encoded = json.dumps(payload).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        def log_message(self, *_):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('EULER_VERIFY_URL', f'http://127.0.0.1:{server.server_port}/verify')
    monkeypatch.setenv('EULER_VERIFY_TIMEOUT', '10')
    yield calls
    server.shutdown(); server.server_close(); thread.join()


def test_live_proposals_gateway_admission_and_independent_final_audit(project, verifier, monkeypatch):
    root, target = project
    with open_project(root) as p:assert len(p.research_view().obligations()) == 1
    job = activate(monkeypatch, root, 'lead')
    research.euler_team_continue_research()
    plan = euler_team_propose_steps(['Zero is an integer.', 'Zero is an additive identity.'], 'Use the additive identity.')
    assert len(research.research_state(root)['all_obligations']) == 3
    settle(root, job)
    accepted = []
    for oid in plan['obligations']:
        job = activate(monkeypatch, root, 'helper')
        result = asyncio.run(research.euler_team_submit_proof(oid, 'The assertion follows from the definition of zero.'))
        assert result['accepted'], result
        accepted.append(result['fact_id']);settle(root, job)
    job = activate(monkeypatch, root, 'helper')
    proof = 'By the additive identity axiom, adding zero preserves every integer.'
    result = asyncio.run(research.euler_team_submit_proof(target.obligation_id, proof, predecessors=accepted))
    assert result['accepted'] and result['root_final_audit_required']
    assert all(fid in verifier[-1]['proof'] for fid in accepted)
    assert 'ordinary integer addition' in verifier[-1]['proof']
    assert 'The assertion follows from the definition of zero.' not in verifier[-1]['proof']
    assert 'scope_bindings' in verifier[-1]['proof']
    count = len(verifier)
    repeated = asyncio.run(research.euler_team_submit_proof(target.obligation_id, proof, predecessors=accepted))
    assert repeated['fact_id'] == result['fact_id'] and len(verifier) == count
    assert research.research_state(root)['acceptance'] is None
    settle(root, job)
    activate(monkeypatch, root, 'lead')
    final = research.euler_team_final_audit()
    assert final['status'] == 'SOLVED', final
    assert len(verifier) == count + 1 and verifier[-1]['force_fresh'] is True
    state = research.research_state(root)
    assert state['all_routes'][0]['status'] == 'COMPLETED'
    assert not state['obligations']
    assert state['acceptance']['acceptance']['project_outcome'] == 'SOLVED'
    assert research.euler_team_continue_research(enabled=False)['status'] == 'COMPLETED'
    assert research.euler_team_final_audit()['replayed']
    assert len(verifier) == count + 1


def test_rejected_proof_records_scoped_failure_and_never_closes_claim(project, verifier, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'helper')
    result = asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'WRONG fixture argument'))
    assert result['accepted'] is False
    repeated = asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'WRONG fixture argument'))
    assert repeated['failure_id'] == result['failure_id']
    state = research.research_state(root)
    assert state['all_obligations'][0]['status'] == 'OPEN'
    assert state['blockers'][0]['kind'] == 'INVALID_PROOF'
    assert not state['all_evidence']
    assert not list((root / 'fact_graph/facts').glob('*.md'))


def test_main_and_reviewer_cannot_submit_or_fabricate_evidence(project, verifier, monkeypatch):
    root, target = project
    for member in ['lead', 'reviewer']:
        job = activate(monkeypatch, root, member)
        with pytest.raises(ValueError, match='only an assistant'):
            asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'A claimed proof'))
        settle(root, job)
    activate(monkeypatch, root, 'lead')
    with pytest.raises(ValueError, match='checked evidence'):
        research.euler_team_update_research([{'event_type': 'evidence.registered', 'payload': {'checker_result': 'PASS'}}])
    with pytest.raises(ValueError, match='independent final audit'):
        research.euler_team_update_research([{'event_type': 'obligation.status_changed', 'payload': {
            'obligation_id': target.obligation_id, 'status': 'DISCHARGED', 'evidence_refs': []}}])
    assert not verifier


def test_missing_verifier_and_pause_do_not_make_facts(project, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'helper');monkeypatch.delenv('EULER_VERIFY_URL', raising=False)
    with pytest.raises(ValueError, match='verifier unavailable'):
        asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'A proof'))
    team.command(root, 'pause')
    with pytest.raises(ValueError, match='unpaused'):
        asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'A proof'))
    assert research.research_state(root)['all_obligations'][0]['status'] == 'OPEN'


def test_nested_routes_and_blockers_enter_context_and_live_graph(project, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'lead')
    plan = euler_team_propose_steps(['A prerequisite'], 'First route')
    child = euler_team_propose_steps(['A smaller prerequisite'], 'Refine the step', plan['obligations'][0])
    assert child['target'] == plan['obligations'][0]
    result = research.euler_team_record_obstacle(child['target'], 'A precise missing premise',
                                                ['Find an argument for the missing premise'], child['route'])
    assert result['evidence_level'] == 'diagnostic'
    repeated = research.euler_team_record_obstacle(child['target'], 'A precise missing premise',
        ['Find an argument for the missing premise'], child['route'])
    assert repeated['failure_id'] == result['failure_id'] and not repeated['events']
    state = research.research_state(root)
    assert state['blockers'][0]['target_obligation_id'] == child['target']
    job = {'id': str(uuid.uuid4()), 'request': {'object_id': child['target'], 'question': 'Repair this step'}}
    context = research.compile_context(root, job)
    assert 'A precise missing premise' in context and 'A prerequisite' in context
    from euler.observability.monitor import snapshot
    graph = snapshot(root)['research_graph']
    assert len(graph['nodes']) >= 5 and graph['edges']


def test_continuation_pauses_after_unchanged_lead_turns(project, monkeypatch):
    root, target = project
    job = activate(monkeypatch, root, 'lead')
    research.euler_team_continue_research()
    for i in range(3):
        if i:job = activate(monkeypatch, root, 'lead')
        settle(root, job)
        # No queued work from previous iteration is being claimed as progress.
        with team.edit(root) as state:
            for message in state['messages']:
                if message['state'] == 'QUEUED':message['state'] = 'ANSWERED'
        research.after_turn(root, job)
    state = team.load(root)
    assert state['state'] == 'PAUSED'
    assert state['research_campaign']['status'] == 'NEEDS_INPUT'
    assert not any(m['state'] == 'QUEUED' for m in state['messages'])
    assert research.research_state(root)['all_obligations'][0]['status'] == 'OPEN'


def test_plain_conversation_does_not_enable_autonomous_work(project, monkeypatch):
    root, _ = project
    job = activate(monkeypatch, root, 'lead');settle(root, job)
    research.after_turn(root, job)
    assert len(team.load(root)['messages']) == 1


@pytest.mark.parametrize('auth_mode', ['explicit', 'refreshed'])
def test_owned_verifier_lifecycle_uses_cold_service_and_keeps_private_storage_outside_project(project, monkeypatch, tmp_path, auth_mode):
    import sys
    import urllib.request
    from euler.workspace.team_verifier import managed_verifier
    root, target = project
    binary = tmp_path / 'grok-fixture'
    binary.write_text('#!' + sys.executable + '\n' + '''import json,sys,re,os
from pathlib import Path
assert json.loads((Path(os.environ['GROK_HOME'])/'auth.json').read_text())['fixture']=='not-a-credential'
prompt=sys.argv[-1]
match=re.search(r'Write the verification JSON to this exact path: (.+)\\.$',prompt)
if not match:raise SystemExit('unexpected verifier invocation')
output=Path(match.group(1));output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps({'verdict':'correct','verification_report':{'summary':'Protocol fixture','critical_errors':[],'gaps':[]},'repair_hints':''}))
''')
    binary.chmod(0o755)
    auth = tmp_path / 'fixture-auth.json';auth.write_text('{"fixture":"not-a-credential"}')
    monkeypatch.setenv('EULER_TEAM_GROK_BIN', str(binary))
    if auth_mode == 'explicit':
        monkeypatch.setenv('EULER_GROK_AUTH_FILE', str(auth))
    else:
        monkeypatch.delenv('EULER_GROK_AUTH_FILE', raising=False)
        monkeypatch.delenv('GROK_HOME', raising=False)
        host = tmp_path / 'host'; (host / '.grok').mkdir(parents=True)
        (host / '.grok/auth.json').write_text('{"fixture":"expired-login"}')
        monkeypatch.setenv('HOME', str(host))
        private = root / '.euler-provider/reviewer/auth.json'; private.parent.mkdir(parents=True)
        private.write_text(auth.read_text())
    monkeypatch.delenv('EULER_VERIFY_URL', raising=False)
    with managed_verifier(root, team.load(root)):
        endpoint = os.environ['EULER_VERIFY_URL']
        assert endpoint.startswith('http://127.0.0.1:')
        job = activate(monkeypatch, root, 'helper')
        result = asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'The additive identity axiom proves the claim.'))
        assert result['accepted'], result
        settle(root, job);activate(monkeypatch, root, 'lead')
        assert research.euler_team_final_audit()['status'] == 'SOLVED'
    assert 'EULER_VERIFY_URL' not in os.environ
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(OSError):opener.open(endpoint.replace('/verify', '/health'), timeout=1)
    assert not list(root.rglob('fixture-auth.json'))


def test_concurrent_duplicate_submission_calls_verifier_once(project, verifier, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'helper')
    async def submit():
        return await asyncio.gather(*[research.euler_team_submit_proof(target.obligation_id,
            'The additive identity axiom proves the claim.') for _ in range(2)])
    replies = asyncio.run(submit())
    assert all(r['accepted'] for r in replies)
    assert replies[0]['fact_id'] == replies[1]['fact_id']
    assert len(verifier) == 1


def test_restore_keeps_campaign_paused_and_captures_native_history(project, monkeypatch):
    from euler.workspace.milestones import Milestones, public_projection
    root, target = project
    job = activate(monkeypatch, root, 'lead');research.euler_team_continue_research();settle(root, job)
    with team.edit(root) as state:
        for member in state['members']:
            archive = root / member['workdir'] / 'session.jsonl'
            archive.write_text('opaque native session fixture\n')
            member.update(archive=str(archive), session_id='fixture-' + member['id'], recovery='NEW_SESSION')
    saved = Milestones(root).capture('Before a different route', key='phase', projection=lambda: public_projection(root))
    restored = Path(Milestones(root).restore(saved['commit'], key='branch')['project_dir'])
    assert team.load(restored)['research_campaign']['enabled'] is False
    assert team.load(root)['research_campaign']['enabled'] is True
    assert team.load(restored)['state'] == 'RESTORED_PAUSED'
    with open_project(restored) as p:assert p.manifest.target_obligation_id == target.obligation_id


def test_final_audit_rejection_reopens_root_with_no_success(project, verifier, monkeypatch):
    root, target = project
    job = activate(monkeypatch, root, 'helper')
    result = asyncio.run(research.euler_team_submit_proof(target.obligation_id,
        'The additive identity axiom proves the claim. reject-final-fixture'))
    assert result['accepted']
    settle(root, job);activate(monkeypatch, root, 'lead')
    result = research.euler_team_final_audit()
    assert result['status'] == 'AUDIT_FAIL'
    state = research.research_state(root)
    assert state['all_obligations'][0]['status'] == 'OPEN'
    assert state['acceptance'] is None
    assert verifier[-1]['force_fresh'] is True


def test_mcp_validation_errors_are_actionable_and_route_schema_is_typed(project, monkeypatch):
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root, _ = project
    activate(monkeypatch, root, 'lead')
    plan = euler_team_propose_steps(['A prerequisite'], 'Fixture route')
    async def check():
        async with stdio_client(StdioServerParameters(command=sys.executable,
                args=['-m', 'euler.workspace.team_mcp'], env=dict(os.environ))) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool = next(x for x in listed.tools if x.name == 'euler_team_set_route_status')
                schema = getattr(tool, 'input_schema', None) or getattr(tool, 'inputSchema')
                assert schema['properties']['status']['enum'] == ['PROPOSED', 'ACTIVE', 'BLOCKED', 'PARKED']
                result = await session.call_tool('euler_team_update_research', {'operations': [{
                    'event_type': 'route.status_changed', 'payload': {'route_id': plan['route'], 'status': 'OPEN'}}]})
                text = ''.join(x.text for x in result.content if getattr(x, 'type', '') == 'text')
                assert 'OPEN' in text and ('RouteStatus' in text or 'status' in text), text
    asyncio.run(check())


def test_definition_text_must_match_the_contract_before_verification(project, verifier, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'helper')
    with open_project(root) as opened:
        opened.artifacts.path_for(target.scope.definitions_hash).unlink()
    (root / 'PROBLEM.md').write_text('## Definitions\n\nA different meaning of addition\n')
    with pytest.raises(ValueError, match='definition text'):
        asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'A proof'))
    assert not verifier


def test_unknown_predecessor_cannot_be_passed_to_verifier(project, verifier, monkeypatch):
    root, target = project
    activate(monkeypatch, root, 'helper')
    with pytest.raises(ValueError, match='active verifier-admitted Fact'):
        asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'A proof', predecessors=['a' * 16]))
    assert not verifier


def test_runner_captures_intermediate_graph_before_final_audit(project, verifier, monkeypatch):
    from euler.workspace import team_provider
    from euler.workspace.milestones import Milestones, _git
    root, target = project
    with team.edit(root) as state:
        state['state'] = 'READY'
        for member in state['members']:
            archive = root / member['workdir'] / 'session.jsonl'
            archive.write_text('opaque session fixture\n')
            member.update(status='READY', recovery='NEW_SESSION', archive=str(archive),
                          session_id='fixture-' + member['id'])
    stage = []
    def turn(project, member, state, question, cancelled):
        monkeypatch.setenv('EULER_TEAM_MEMBER', member['id'])
        assert not cancelled()
        if member['id'] == 'lead' and not stage:
            research.euler_team_continue_research()
            plan = euler_team_propose_steps(['Zero is an additive identity.'], 'Use the additive identity')
            stage.append(plan)
            from euler.workspace.team_mcp import euler_team_ask
            euler_team_ask('helper', 'Prove the prerequisite and target.', target.obligation_id)
        elif member['id'] == 'helper':
            child = asyncio.run(research.euler_team_submit_proof(stage[0]['obligations'][0], 'By the definition of zero.'))
            assert child['accepted']
            assert asyncio.run(research.euler_team_submit_proof(target.obligation_id,
                'Apply the additive identity axiom.', predecessors=[child['fact_id']]))['accepted']
        else:
            assert research.euler_team_final_audit()['status'] == 'SOLVED'
        return {'reply': 'Protocol fixture completed.', 'native_session_id': member['session_id']}
    monkeypatch.setattr(team_provider, 'run_turn', turn)
    team.queue_question(root, 'lead', 'Prove the exact statement.', key='start')
    team.drain(root)
    history = Milestones(root)
    checkpoints = history.list()
    snapshots = [json.loads(_git(history.repo, 'show', x['commit'] + ':view/snapshot.json')) for x in checkpoints]
    assert any(x['workspace']['control'].get('outcome') == 'SOLVED' for x in snapshots)
    assert any(x['workspace']['control']['target_status'] == 'DISCHARGED'
               and not x['workspace']['control'].get('outcome') for x in snapshots)
    assert any(len(x['research_graph']['nodes']) >= 3 and not x['facts_graph']['nodes'] for x in snapshots)
    assert team.load(root)['research_campaign']['status'] == 'COMPLETED'
    assert team.load(root)['turns_used'] == 3


def test_repeated_transport_retries_stop_without_a_mathematical_verdict(monkeypatch, tmp_path):
    from euler.workspace import team_provider
    cancel = threading.Event()
    class Rpc:
        def __init__(self, callback):self.callback = callback
        def request(self, method, params=None, timeout=45):
            if method == 'session/load':
                return {'configOptions': [{'id': 'model', 'currentValue': 'fixture'},
                                          {'id': 'reasoning_effort', 'currentValue': 'high'}]}
            if method == 'session/prompt':
                for _ in range(5):
                    self.callback({'method': 'session/update', 'params': {'update': {
                        'sessionUpdate': 'retry_state', 'type': 'retrying'}}})
                cancel.wait(3)
                return {'stopReason': 'cancelled'}
            raise AssertionError(method)
        def notify(self, method, params):
            assert method == 'session/cancel';cancel.set()
    @contextmanager
    def connection(root, member, state, callback=None):yield Rpc(callback), tmp_path, {}
    monkeypatch.setattr(team_provider, 'connection', connection)
    member = {'id': 'helper', 'role': 'assistant', 'provider': 'grok', 'session_id': 'fixture', 'model': 'fixture',
              'effort': 'high', 'workdir': '.', 'goal': None}
    with pytest.raises(team.TeamError, match='five consecutive transport retries'):
        team_provider.run_turn(tmp_path, member, {'policy': {'turn_seconds': 10}, 'objective': 'Protocol test'}, 'Review', lambda: False)
    assert cancel.is_set()


def test_interrupted_final_audit_owner_is_recovered_without_inventing_acceptance(project, verifier, monkeypatch):
    import subprocess
    import sys
    from euler.control.final_audit import run_final_audit
    root, target = project
    activate(monkeypatch, root, 'helper')
    assert asyncio.run(research.euler_team_submit_proof(target.obligation_id, 'By the additive identity axiom.'))['accepted']
    script = '''import sys,time
from euler.control.project import open_project
from euler.control.final_audit import run_final_audit
class Interrupted:
    def verify(self, statement, proof):
        print('VERIFY_STARTED',flush=True)
        time.sleep(60)
with open_project(sys.argv[1]) as project:
    run_final_audit(project,transport=Interrupted())
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(root)], stdout=subprocess.PIPE,
                            text=True, stdin=subprocess.DEVNULL)
    try:
        assert proc.stdout.readline().strip() == 'VERIFY_STARTED'
        with open_project(root) as opened:
            busy = run_final_audit(opened)
            assert busy.status.value == 'DEFERRED'
            assert opened.runtime_snapshot().verifier_slots_in_use == 1
        proc.kill();proc.wait(timeout=5)
        with open_project(root) as opened:
            interrupted = run_final_audit(opened)
            assert interrupted.status.value == 'INFRA'
            assert opened.runtime_snapshot().verifier_slots_in_use == 0
            assert research.latest_acceptance(opened) is None
            resumed = run_final_audit(opened, retry_infra=True, retry_reason='Owner exited in interruption test')
            assert resumed.status.value == 'SOLVED'
        assert len(verifier) == 2 and verifier[-1]['force_fresh'] is True
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        proc.stdout.close()
