# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Native-team research tools over the existing control and truth boundaries.

Discussion never becomes evidence. Assistant submissions travel through the
actual worker MCP gateway; the root still needs the independent final audit.
"""
from __future__ import annotations

from datetime import timedelta
from contextlib import asynccontextmanager
import asyncio
import fcntl
import inspect
import json
import os
from pathlib import Path
import sys
from typing import Literal

from euler.control.contracts import ValidityStatus
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, Route
from euler.control.project import open_project, is_euler_project
from euler.control.research_service import ResearchService, frontier_payload
from . import team


def identity(*, lead=False, assistant=False, active=False):
    root = Path(os.environ['EULER_TEAM_PROJECT']).resolve()
    state = team.load(root)
    member = next((m for m in (state or {}).get('members', [])
                   if m['id'] == os.environ.get('EULER_TEAM_MEMBER')), None)
    if member is None:
        raise ValueError('unregistered team identity')
    if lead and member['id'] != state['lead']:
        raise ValueError('only the lead may change the research agenda')
    if assistant and member['role'] != 'assistant':
        raise ValueError('only an assistant may submit proofs through the worker gateway')
    job = next((m for m in state['messages'] if m['state'] == 'RUNNING'
                and m['request']['recipient'] == member['id']), None)
    if active and (job is None or state['state'] != 'READY'):
        raise ValueError('an active, unpaused team turn is required')
    return root, member, state, job


def _obligation(project, identifier=None):
    identifier = identifier or project.manifest.target_obligation_id
    found = project.research_view().get_obligation(identifier)
    if found is None or found.validity is not ValidityStatus.VALID:
        raise ValueError('unknown or invalid obligation; read the current research state')
    return found


def research_state(root):
    with open_project(root) as project:
        service = ResearchService(project, actor='native-team-controller')
        service.sync_fact_dependencies()
        view = project.research_view()
        frontier = frontier_payload(project)
        return {**frontier, 'contract': view.get_contract(project.manifest.contract_id).to_dict(),
                'budget_policy': project.policy.to_dict(),
                'all_obligations': [o.to_dict() for o in view.obligations()],
                'all_routes': [r.to_dict() for r in view.routes()],
                'all_evidence': [e.to_dict() for e in view.evidence()],
                'acceptance': latest_acceptance(project),
                'verification_available': bool(os.environ.get('EULER_VERIFY_URL'))}


def latest_acceptance(project):
    """Acceptance must still match the live root; revoked evidence reopens it."""
    view = project.research_view()
    root = view.get_obligation(project.manifest.target_obligation_id)
    if root.status.value not in {'DISCHARGED', 'REFUTED'}:
        return None
    records = [dict(e.payload) for _, e in project.event_store.iter_project(project.project_id)
               if e.event_type == 'euler.acceptance_recorded']
    if not records:
        return None
    result = records[-1]
    acceptance = result.get('acceptance', {})
    if acceptance.get('obligation_id') != root.obligation_id or acceptance.get('contract_id') != root.contract_id:
        return None
    if acceptance.get('verdict') == 'PASS':
        identity = result.get('identity', {})
        if identity.get('evidence_id') not in root.evidence_refs:
            return None
        from euler.control.adapters.project import snapshot_project
        facts = {f.fact_id: f for f in snapshot_project(project.project_dir).active_facts}
        fact = facts.get(identity.get('fact_id'))
        if fact is None or fact.content_sha256 != identity.get('fact_digest'):
            return None
    return result


def euler_team_research_state(offset: int = 0, limit: int = 20) -> dict:
    """Read exact research records in bounded pages, with counts and next offsets.

    Contract, root ID and final acceptance are always included. Use returned IDs
    to focus subsequent tasks; do not mistake a partial page for the entire graph.
    """
    if not 1 <= limit <= 50 or offset < 0:
        raise ValueError('limit must be 1–50 and offset nonnegative')
    root, _, _, _ = identity()
    result = research_state(root)
    pages = {}
    for key, value in list(result.items()):
        if isinstance(value, list):
            result[key] = value[offset:offset + limit]
            pages[key] = {'total': len(value), 'offset': offset,
                          'next_offset': offset + limit if offset + limit < len(value) else None}
    result['pages'] = pages
    return result


def propose_steps(root, author, statements, mechanism, parent_id=None):
    if not isinstance(statements, list) or not 1 <= len(statements) <= 12 or any(
            not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in statements):
        raise ValueError('provide 1–12 precise statements, each up to 4000 characters')
    if not isinstance(mechanism, str) or not mechanism.strip() or len(mechanism) > 8000:
        raise ValueError('a bounded explanation of the proposed route is required')
    with open_project(root) as project:
        parent = _obligation(project, parent_id)
        if parent.exact_statement in statements:
            raise ValueError('a goal cannot be its own prerequisite')
        obligations = [Obligation.build(contract_id=parent.contract_id, exact_statement=x,
            scope=parent.scope, desired_output='A checked proof step', created_by=author, version=1)
            for x in statements]
        route = Route.build(target_obligation_id=parent.obligation_id, mechanism=mechanism,
            required_obligation_ids=[o.obligation_id for o in obligations],
            progress_claim=parent.exact_statement)
        ResearchService(project, actor=author).apply([
            *[{'event_type': 'obligation.created', 'payload': {'obligation': o.to_dict()}}
              for o in obligations],
            {'event_type': 'route.created', 'payload': {'route': route.to_dict()}}])
        return {'target': parent.obligation_id, 'obligations': [o.obligation_id for o in obligations],
                'route': route.route_id, 'status': 'OPEN; proposals only, not Fact admission'}


def euler_team_update_research(operations: list[dict]) -> dict:
    """Lead: apply typed route/relation/failure updates; evidence cannot be fabricated.

    Read euler_team_research_state for exact IDs and scopes. Operations contain
    event_type, payload, optional idempotency_key. Allowed events are
    obligation.created/status_changed/reopened, route.created/status_changed,
    relation.created and failure.recorded. Checked closure requires real evidence.
    """
    root, member, _, _ = identity(lead=True, active=True)
    with open_project(root) as project:
        # Root acceptance and contract changes have separate validation paths.
        for operation in operations:
            payload = operation.get('payload', {})
            if (operation.get('event_type') == 'obligation.status_changed'
                    and payload.get('obligation_id') == project.manifest.target_obligation_id
                    and payload.get('status') in {'DISCHARGED', 'REFUTED'}):
                raise ValueError('submit the exact root proof and run the independent final audit')
        return ResearchService(project, actor=member['id']).apply(operations)


def euler_team_set_route_status(route_id: str, status: Literal['PROPOSED', 'ACTIVE', 'BLOCKED', 'PARKED'],
                                 reason: str) -> dict:
    """Lead: activate, block or park a route with a recorded reason.

    Use ACTIVE to resume a BLOCKED route. OPEN is an obligation status, not a
    route status. COMPLETED is reserved for proof evidence and final acceptance.
    """
    if not reason.strip() or len(reason) > 8000:
        raise ValueError('a bounded explanation for the route change is required')
    return euler_team_update_research([{'event_type': 'route.status_changed',
        'payload': {'route_id': route_id, 'status': status, 'reason': reason}}])


def euler_team_record_obstacle(obligation_id: str, explanation: str,
                               repair_conditions: list[str], route_id: str | None = None) -> dict:
    """Record a scoped diagnostic blocker. A failed approach does not refute a theorem."""
    from euler.control.failures import FailureRecord, FailureKind, ConfidenceClass
    root, member, _, _ = identity(active=True)
    if not explanation.strip() or len(explanation) > 8000 or not repair_conditions:
        raise ValueError('describe the obstacle and what would permit further progress')
    with open_project(root) as project:
        target = _obligation(project, obligation_id)
        if route_id:
            route = project.research_view().get_route(route_id)
            if route is None or route.target_obligation_id != target.obligation_id:
                raise ValueError('route does not belong to this obligation')
        record = FailureRecord.build(kind=FailureKind.MISSING_PREMISE,
            exact_failed_claim=target.exact_statement, scope=target.scope,
            confidence=ConfidenceClass.MEDIUM, context_version=target.version,
            provenance='native-team:' + member['id'], created_at=team.now(),
            target_obligation_id=target.obligation_id, contract_id=target.contract_id,
            route_id=route_id, rejected_step=explanation, repair_conditions=repair_conditions)
        with project.event_store.transaction(immediate=True):
            operations = [] if project.research_view().get_failure(record.failure_id) else [
                {'event_type': 'failure.recorded', 'payload': {'failure': record.to_dict()}}]
            if route_id and project.research_view().get_route(route_id).status.value in {'PROPOSED', 'ACTIVE'}:
                operations.append({'event_type': 'route.status_changed',
                    'payload': {'route_id': route_id, 'status': 'BLOCKED', 'evidence_refs': []}})
            result = ResearchService(project, actor=member['id']).apply(operations) if operations else {
                'events': [], 'frontier': frontier_payload(project)}
        return {'failure_id': record.failure_id, 'evidence_level': 'diagnostic', **result}


@asynccontextmanager
async def submission_lock(root):
    path = Path(root) / team.TEAM / 'proof-submission.lock'
    with path.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def verified_premises(project, predecessors):
    """Validate the closure and pass direct, scope-bound interfaces to the checker.

    Full native proofs stay in their own Facts. Recursively embedding them in
    every descendant would amplify both stored proof size and verification tokens.
    """
    from euler.control.adapters.project import snapshot_project
    from euler.control.acceptance import collect_fact_admissions
    snapshot = snapshot_project(project.project_dir)
    facts = {fact.fact_id: fact for fact in snapshot.active_facts}
    admitted = {(a.fact_id, a.exact_claim) for a in collect_fact_admissions(snapshot) if a.accepted}
    view = project.research_view()
    links = {}
    for _, event in project.event_store.iter_project(project.project_id):
        if event.event_type == 'euler.fact_linked':
            links.setdefault(event.payload['fact_id'], []).append(dict(event.payload))
    def scope_bindings(identifier):
        bindings = []
        for link in links.get(identifier, []):
            obligation = view.get_obligation(link['obligation_id'])
            evidence = view.get_evidence(link['evidence_id'])
            if (obligation and evidence and obligation.status.value == 'DISCHARGED'
                    and obligation.validity.value == evidence.validity.value == 'VALID'):
                bindings.append({'scope': obligation.scope.to_dict(), 'evidence_id': evidence.artifact_id,
                                 'obligation_id': obligation.obligation_id})
        if identifier in links and not bindings:
            raise ValueError('predecessor no longer has valid discharged scope evidence')
        return bindings
    visited, active = set(), set()
    def visit(identifier):
        if identifier in active:
            raise ValueError('cycle in submitted Fact dependencies')
        if identifier in visited:
            return
        fact = facts.get(identifier)
        if fact is None or (identifier, fact.statement) not in admitted:
            raise ValueError('predecessor must be an active verifier-admitted Fact in this project')
        scope_bindings(identifier)
        active.add(identifier)
        for parent in fact.predecessors:
            visit(parent)
        active.remove(identifier); visited.add(identifier)
    interfaces = []
    for identifier in dict.fromkeys(predecessors):
        visit(identifier)
        fact = facts[identifier]
        bindings = scope_bindings(identifier)
        interface = {'fact_id': identifier, 'exact_statement': fact.statement,
                     'source_hash': fact.content_sha256, 'scope_bindings': bindings}
        if not bindings:
            # Legacy Facts may predate research bindings. Quote the actual source
            # so its assumptions remain inspectable; never invent a missing scope.
            interface['scope_status'] = 'unregistered; inspect the original proof'
            interface['original_record'] = fact.raw_markdown
        interfaces.append(interface)
    text = canonical_json(interfaces)
    if len(text) > 300000:
        raise ValueError('prerequisite context exceeds the verification limit; use a smaller checked interface')
    return text


def original_definitions(project, target):
    """Resolve definition text against the contract's content hash, never a paraphrase."""
    expected = target.scope.definitions_hash
    if project.artifacts.exists(expected):
        return project.artifacts.get_text(expected)
    if expected == sha256_hex(''):
        return ''
    source = project.project_dir / 'PROBLEM.md'
    if source.is_file() and not source.is_symlink() and source.stat().st_size <= 300000:
        text = source.read_text(encoding='utf-8')
        marker = '## Definitions\n\n'
        if marker in text:
            value = text.split(marker, 1)[1]
            for candidate in (value, value.removesuffix('\n')):
                if sha256_hex(candidate) == expected:
                    project.artifacts.put_text(candidate)
                    return candidate
    raise ValueError('Original definition text is missing or changed; restore the artifact matching definitions_hash before submitting a proof.')


async def euler_team_submit_proof(obligation_id: str, proof: str,
                                 predecessors: list[str] | None = None,
                                 glossary_introduces: dict[str, str] | None = None) -> dict:
    """Assistant only: submit a self-contained proof to the independent truth gateway.

    The original statement and scope are supplied by Euler. Returns the actual
    verifier verdict and, on acceptance, links its Fact to this exact obligation.
    Main and verifier roles cannot submit. This does not complete the root audit.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root, member, state, job = identity(assistant=True, active=True)
    if not isinstance(proof, str) or not proof.strip() or len(proof) > 200000:
        raise ValueError('provide a self-contained proof of at most 200000 characters')
    if not os.environ.get('EULER_VERIFY_URL'):
        raise ValueError('independent verifier unavailable; configure EULER_VERIFY_URL or a supported local verifier')
    with open_project(root) as project:
        target = _obligation(project, obligation_id)
        contract = project.research_view().get_contract(target.contract_id)
        scope_hash = sha256_hex(canonical_json(target.scope.to_dict()))
        bound_proof = ('Original contract and explicit scope for this proof:\n'
                       + canonical_json({'statement': target.exact_statement, 'scope': target.scope.to_dict(),
                                         'contract_id': target.contract_id, 'definitions': original_definitions(project, target)})
                       + '\n\nAdmitted prerequisite interfaces (use only within the recorded scopes):\n' + verified_premises(project, predecessors or [])
                       + '\n\nProof:\n' + proof)
        identity_hash = sha256_hex(canonical_json([target.obligation_id, scope_hash, bound_proof, predecessors or [], glossary_introduces or {}]))
    # Pin the child to this project and worker role; never inherit another campaign's round.
    env = {k: v for k, v in os.environ.items() if k not in {
        'EULER_AGENTS_ROOT', 'EULER_WORKER_ID', 'EULER_RUN_ID', 'EULER_BRANCH_ID', 'EULER_SUBGOAL_ID'}}
    env.update(EULER_PROJECT_DIR=str(root), EULER_PROJECT_ID=root.name,
               EULER_ROLE='worker', EULER_AUTHOR=member['id'], EULER_PROBLEM_ID=contract.problem_id)
    parameters = {'statement': target.exact_statement, 'proof': bound_proof,
                  'predecessors': predecessors or [], 'glossary_introduces': glossary_introduces or {},
                  'attempt_id': 'native-' + identity_hash}
    # A project lock serializes submission/replay, not a lock on the whole team mailbox.
    async with submission_lock(root):
        async with stdio_client(StdioServerParameters(command=sys.executable,
                args=['-m', 'euler.gateway'], env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                seconds = int(os.environ.get('EULER_VERIFY_TIMEOUT', '3600')) + 30
                annotation = inspect.signature(session.call_tool).parameters['read_timeout_seconds'].annotation
                timeout = timedelta(seconds=seconds) if 'timedelta' in str(annotation) else float(seconds)
                response = await session.call_tool('fact_submit', parameters, read_timeout_seconds=timeout)
                if getattr(response, 'isError', False) or getattr(response, 'is_error', False):
                    raise ValueError('truth gateway failed; no proof was admitted')
                blocks = [x.text for x in response.content if getattr(x, 'type', '') == 'text']
                result = json.loads(''.join(blocks))
        with open_project(root) as project:
            live = _obligation(project, obligation_id)
            if live.contract_id != target.contract_id or live.scope != target.scope:
                raise ValueError('proof target changed during verification; recheck scope')
            if result.get('accepted'):
                service = ResearchService(project, actor='native-team-gateway:' + member['id'])
                linked = service.admit_fact(result['fact_id'], obligation_id,
                    confirmed_scope_hash=scope_hash,
                    alignment_note='Gateway checked the unchanged canonical statement with its explicit scope and contract, compiled by Euler.')
                service.reuse_verified_evidence(obligation_id)
                result['research'] = linked
                result['root_final_audit_required'] = obligation_id == project.manifest.target_obligation_id
            else:
                from euler.control.failures import FailureRecord, FailureKind, ConfidenceClass
                reason = str(result.get('repair_hints') or result.get('error') or result.get('detail') or 'Verification did not accept this proof.')
                kind = FailureKind.INVALID_PROOF if result.get('verdict') == 'wrong' else FailureKind.INFRASTRUCTURE_FAILURE
                failure = FailureRecord.build(kind=kind, exact_failed_claim=target.exact_statement,
                    scope=target.scope, confidence=ConfidenceClass.HIGH, context_version=target.version,
                    provenance='native-team-gateway:' + member['id'], created_at=team.now(),
                    contract_id=target.contract_id, target_obligation_id=obligation_id,
                    rejected_step=reason, repair_conditions=[reason])
                with project.event_store.transaction(immediate=True):
                    if project.research_view().get_failure(failure.failure_id) is None:
                        ResearchService(project, actor=member['id']).apply([
                            {'event_type': 'failure.recorded', 'payload': {'failure': failure.to_dict()}}])
                result['failure_id'] = failure.failure_id
        return result


def euler_team_final_audit(reservation: dict | None = None, retry_reason: str = '') -> dict:
    """Lead: independently audit the original root after admitted proof/evidence closure.

    A failed audit reopens the root. An infrastructure failure stays unknown.
    For limited budgets pass an explicit ResourceVector reservation. A retry after
    infrastructure failure needs a nonempty retry_reason. No cached verifier PASS.
    """
    from euler.control.final_audit import run_final_audit
    from euler.control.policy import ResourceVector
    from .milestones import _lock
    root, member, _, _ = identity(lead=True, active=True)
    with _lock(root / team.TEAM / 'final-audit'), open_project(root) as project:
        identity(lead=True, active=True)
        result = run_final_audit(project,
            requested=ResourceVector.from_dict(reservation) if reservation else None,
            retry_infra=bool(retry_reason), retry_reason=retry_reason or None)
        if result.status.value == 'SOLVED':
            view = project.research_view()
            goal = _obligation(project)
            service = ResearchService(project, actor=member['id'])
            for route in view.routes():
                if (route.target_obligation_id == goal.obligation_id
                        and route.status.value in {'PROPOSED', 'ACTIVE'}
                        and route.progress_claim == goal.exact_statement
                        and all(view.get_obligation(o).status.value == 'DISCHARGED' for o in route.required_obligation_ids)):
                    service.apply([{'event_type': 'route.status_changed', 'payload': {
                        'route_id': route.route_id, 'status': 'COMPLETED', 'evidence_refs': list(goal.evidence_refs)}}])
        output = result.to_dict()
    if result.status.value == 'SOLVED':
        with team.edit(root) as state:
            if state.get('research_campaign'):
                state['research_campaign'].update(enabled=False, status='COMPLETED')
    return output


def euler_team_continue_research(obligation_id: str | None = None, enabled: bool = True) -> dict:
    """Lead: enable continued research on an exact goal, or end this research campaign.

    Use for a request to solve/investigate, not a request merely to explain. When
    the mailbox empties the controller schedules another frontier-focused turn.
    Pause/stop/quota limits remain effective; repeated unchanged turns pause work.
    """
    root, _, _, _ = identity(lead=True, active=True)
    with open_project(root) as project:
        goal = _obligation(project, obligation_id)
        digest = frontier_payload(project)['frontier_hash']
    with team.edit(root) as state:
        old = state.get('research_campaign') or {}
        if not enabled and old.get('status') == 'COMPLETED' and old.get('goal_id') == goal.obligation_id:
            return dict(old)
        if not old.get('enabled') or old.get('goal_id') != goal.obligation_id:
            old = {}
        state['research_campaign'] = {**old, 'enabled': enabled, 'goal_id': goal.obligation_id,
            'status': 'RUNNING' if enabled else 'STOPPED',
            'last_frontier_hash': old.get('last_frontier_hash', digest),
            'unchanged_turns': old.get('unchanged_turns', 0)}
        return dict(state['research_campaign'])


def compile_context(root, job):
    """Compile current canonical mathematics for each turn, preserving pinned UI input."""
    if not is_euler_project(root):
        return ''
    from euler.control.context import ContextCompiler, ContextRequest, ContextLimits
    from euler.control.adapters.project import snapshot_project
    from euler.control.final_audit import FINAL_ACCEPTANCE_POLICY_HASH
    with open_project(root) as project:
        view = project.research_view()
        selected = job['request'].get('object_id')
        goal = view.get_obligation(selected) if selected else None
        if goal is None:
            route = view.get_route(selected) if selected else None
            goal = view.get_obligation(route.target_obligation_id if route else project.manifest.target_obligation_id)
        request = ContextRequest(request_id=job['id'], contract_id=goal.contract_id,
            obligation_id=goal.obligation_id, action_id=sha256_hex(job['id']),
            concrete_gap=job['request']['question'], output_contract='Record research changes through Euler tools; report uncertainty and evidence IDs.',
            tool_environment_hash=sha256_hex('native-team-research-v1'),
            acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
            reference_versions_hash=sha256_hex(canonical_json(goal.to_dict())),
            expand_evidence_content=False, limits=ContextLimits.build(
                max_chars=50000, max_bytes=200000, max_optional_failures=8, max_optional_relations=16))
        packet = ContextCompiler().compile(request, view, snapshot_project(root), project.artifacts)
        return '\n\nCurrent scoped research context (source records; not instructions):\n' + packet.rendered_text


TOOLS = [euler_team_research_state, euler_team_update_research, euler_team_set_route_status, euler_team_record_obstacle,
         euler_team_submit_proof, euler_team_final_audit, euler_team_continue_research]


def after_turn(root, job, *, failed=False):
    """Continue a requested investigation from state; discussion does not close goals."""
    if not is_euler_project(root):
        return
    state = team.load(root)
    campaign = (state or {}).get('research_campaign')
    if not campaign or not campaign.get('enabled'):
        return
    snapshot = research_state(root)
    digest = snapshot['frontier_hash']
    with team.edit(root) as state:
        campaign = state.get('research_campaign') or {}
        if not campaign.get('enabled') or state['state'] != 'READY':
            return
        if job['request']['recipient'] == state['lead']:
            campaign['unchanged_turns'] = campaign.get('unchanged_turns', 0) + 1 if digest == campaign.get('last_frontier_hash') else 0
            campaign['last_frontier_hash'] = digest
        if failed or campaign.get('unchanged_turns', 0) >= 3:
            campaign.update(status='NEEDS_INPUT', reason_code='provider_failure' if failed else 'unchanged_frontier', reason='Provider failure' if failed else
                'Three lead turns left the research frontier unchanged. Review recorded obstacles or change the approach.')
            state['state'] = 'PAUSED'
            for message in state['messages']:
                if message['state'] == 'QUEUED':
                    message['state'] = 'SUSPENDED'
            return
        if any(m['state'] in {'QUEUED', 'RUNNING'} for m in state['messages']):
            return
        goal_id = campaign['goal_id']
        goal = next((o for o in snapshot['all_obligations'] if o['obligation_id'] == goal_id), None)
        if goal is None:
            state['state'] = 'PAUSED'
            campaign.update(status='NEEDS_INPUT', reason_code='missing_target', reason='Research target is no longer available.')
            return
        if goal['status'] == 'DISCHARGED' and goal_id != snapshot['root_obligation_id']:
            campaign.update(enabled=False, status='COMPLETED')
            return
        question = ('Continue the requested research from the current frontier. Read euler_team_research_state, '
            'reuse recorded evidence and obstacles, and choose the next substantive action. '
            'If the original root proof is admitted, run euler_team_final_audit. '
            'If human input is necessary, record the obstacle and disable continued research. '
            'Do not repeat an unchanged proof or treat discussion as accepted evidence.')
        follow_key = 'frontier-continuation:' + job['id']
    team.queue_question(root, state['lead'], question, key=follow_key,
                        object_id=goal_id, sender='research-controller', parent=job['id'])
